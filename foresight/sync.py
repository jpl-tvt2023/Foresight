"""Incremental local→Turso sync + local pruning.

The local sqlite file is canonical: the full pipeline runs against it, then
sync_turso() refreshes the hosted Turso copy the Vercel dashboard reads.
Destroy/recreate of the Turso DB is never used — it would churn the DB URL and
auth token. Instead each table gets the cheapest refresh that is correct:

- small tables:            full DELETE + reinsert (hundreds to low-thousands of rows)
- demand_forecasts:        partition swap — remote holds only the latest
                           trained_through partition (~200k rows, the daily bulk)
- dated event tables:      per-date signature diff (COUNT/SUMs grouped by date,
                           id-independent because snapshot ids churn every run);
                           only differing dates are deleted + reinserted

The diff is convergent: an interrupted sync is repaired by simply re-running.
Remote reads here use positional indexing only, so any dbapi-ish target works
(including a plain sqlite3 connection in tests).
"""
from __future__ import annotations

from foresight import db

SMALL_TABLES = [
    "platforms", "items", "locations", "payout_cycles", "payout_summary",
    "charges", "recommendations", "replenishments", "meta",
]

# table -> (date column, value column used in the content signature)
DATED_TABLES = {
    "sales_daily": ("sale_date", "units"),
    "stock_ledger": ("event_date", "units_delta"),
    "storage_ageing": ("ageing_date", "units"),
    "stock_snapshots": ("as_of_date", "units_on_hand"),
}

BATCH_ROWS = 80  # rows per multi-row INSERT; keeps bind params well under 999


def _columns(local, table: str) -> list[str]:
    return [r["name"] for r in local.execute(f"PRAGMA table_info({table})")]


def _insert_rows(remote, table: str, cols: list[str], rows: list[tuple]) -> int:
    if not rows:
        return 0
    col_sql = ", ".join(cols)
    one = "(" + ", ".join("?" for _ in cols) + ")"
    for i in range(0, len(rows), BATCH_ROWS):
        chunk = rows[i:i + BATCH_ROWS]
        sql = f"INSERT INTO {table} ({col_sql}) VALUES " + ", ".join(one for _ in chunk)
        params: list = []
        for r in chunk:
            params.extend(r)
        remote.execute(sql, params)
    remote.commit()
    return len(rows)


def _ensure_schema(remote) -> None:
    # full-line comments can contain ';' (e.g. "light in P1; fed from GRN") —
    # drop them before splitting into statements
    sql = "\n".join(l for l in db.SCHEMA.splitlines() if not l.strip().startswith("--"))
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if not stmt or stmt.upper().startswith("PRAGMA"):
            continue
        remote.execute(stmt)
    remote.commit()


def _sig_by_date(conn, table: str, date_col: str, val_col: str) -> dict:
    """Content signature per date, independent of row ids (ids churn on rebuild)."""
    rows = conn.execute(f"""
        SELECT {date_col}, COUNT(*), COALESCE(SUM({val_col}), 0),
               COALESCE(SUM({val_col} * item_id), 0),
               COALESCE(SUM({val_col} * location_id), 0)
        FROM {table} GROUP BY {date_col}
    """)
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def sync_turso(local, remote, full: bool = False) -> dict:
    """Refresh the Turso copy from the local canonical DB. Returns rows written per table."""
    _ensure_schema(remote)
    stats: dict[str, int] = {}

    # children before parents on delete (harmless when remote FKs are off,
    # correct when they're on); parents before children on insert
    for table in reversed(SMALL_TABLES):
        remote.execute(f"DELETE FROM {table}")
    remote.commit()
    for table in SMALL_TABLES:
        cols = _columns(local, table)
        rows = [tuple(r) for r in local.execute(f"SELECT {', '.join(cols)} FROM {table}")]
        stats[table] = _insert_rows(remote, table, cols, rows)

    # forecasts: remote mirrors only the newest partition
    cols = _columns(local, "demand_forecasts")
    tt = local.execute("SELECT MAX(trained_through) m FROM demand_forecasts").fetchone()[0]
    remote.execute("DELETE FROM demand_forecasts")
    remote.commit()
    rows = [tuple(r) for r in local.execute(
        f"SELECT {', '.join(cols)} FROM demand_forecasts WHERE trained_through=?", (tt,))]
    stats["demand_forecasts"] = _insert_rows(remote, "demand_forecasts", cols, rows)

    for table, (date_col, val_col) in DATED_TABLES.items():
        cols = _columns(local, table)
        local_sig = _sig_by_date(local, table, date_col, val_col)
        remote_sig = {} if full else _sig_by_date(remote, table, date_col, val_col)
        if full:
            remote.execute(f"DELETE FROM {table}")
            remote.commit()
        stale = [d for d in remote_sig if d not in local_sig]
        changed = [d for d, sig in local_sig.items() if remote_sig.get(d) != sig]
        written = 0
        for d in stale + changed:
            remote.execute(f"DELETE FROM {table} WHERE {date_col}=?", (d,))
        if stale or changed:
            remote.commit()
        for d in changed:
            rows = [tuple(r) for r in local.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE {date_col}=?", (d,))]
            written += _insert_rows(remote, table, cols, rows)
        stats[table] = written

    return stats


def prune(conn) -> dict:
    """Keep the local DB bounded: only the newest forecast partition and the
    last 30 balancing runs. Without this every daily run adds ~200k forecast rows."""
    fc = conn.execute(
        "DELETE FROM demand_forecasts WHERE trained_through != "
        "(SELECT MAX(trained_through) FROM demand_forecasts)").rowcount
    rec = conn.execute("""
        DELETE FROM recommendations WHERE run_date NOT IN (
            SELECT DISTINCT run_date FROM recommendations
            ORDER BY run_date DESC LIMIT 30)""").rowcount
    conn.commit()
    return {"pruned_forecast_rows": fc, "pruned_recommendation_rows": rec}
