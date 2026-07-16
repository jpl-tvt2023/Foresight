"""Database layer.

Local: stdlib sqlite3. Turso: set FORESIGHT_TURSO_URL (+ FORESIGHT_TURSO_TOKEN)
and connect() returns a thin adapter over the `libsql` driver with the same
row-by-name access the codebase uses (rows are wrapped here, so no reliance on
the driver's row_factory support). The dashboard on Vercel runs read-only
against Turso; ingestion/forecast run locally on sqlite and the file is pushed
with `turso db create foresight --from-file foresight.db`.
"""
import os
import sqlite3
from foresight.config import DB_PATH

TURSO_URL = os.environ.get("FORESIGHT_TURSO_URL")
TURSO_TOKEN = os.environ.get("FORESIGHT_TURSO_TOKEN")


class _Row:
    """Minimal sqlite3.Row stand-in: r["col"], r[0], dict(r)."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols: dict, vals: tuple):
        self._cols, self._vals = cols, vals

    def __getitem__(self, key):
        return self._vals[key if isinstance(key, int) else self._cols[key]]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    def _cols(self) -> dict:
        return {d[0]: i for i, d in enumerate(self._cur.description or [])}

    def fetchone(self):
        row = self._cur.fetchone()
        return None if row is None else _Row(self._cols(), tuple(row))

    def fetchall(self):
        cols = self._cols()
        return [_Row(cols, tuple(r)) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid


class _TursoConnection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        return _Cursor(self._raw.execute(sql, tuple(params)))

    def executemany(self, sql, seq):
        return _Cursor(self._raw.executemany(sql, [tuple(p) for p in seq]))

    def execute_batch(self, stmts):
        for sql, params in stmts:
            self._raw.execute(sql, tuple(params))
        self._raw.commit()

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


class _ClientResult:
    """Cursor-ish view over a libsql_client ResultSet."""

    def __init__(self, rs):
        self._rs = rs
        self._cols = {name: i for i, name in enumerate(rs.columns or ())}
        self._pos = 0

    def fetchone(self):
        if self._pos >= len(self._rs.rows):
            return None
        row = self._rs.rows[self._pos]
        self._pos += 1
        return _Row(self._cols, tuple(row))

    def fetchall(self):
        rows = [_Row(self._cols, tuple(r)) for r in self._rs.rows[self._pos:]]
        self._pos = len(self._rs.rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._rs.rows_affected

    @property
    def lastrowid(self):
        return self._rs.last_insert_rowid


class _ClientConnection:
    """Adapter over libsql_client's sync client (pure Python — no wheel needed).
    Statements auto-commit remotely; commit() is a no-op."""

    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=()):
        return _ClientResult(self._client.execute(sql, list(params)))

    def executemany(self, sql, seq):
        import libsql_client
        self._client.batch([libsql_client.Statement(sql, list(p)) for p in seq])
        return None

    def execute_batch(self, stmts):
        """Many statements in ONE transactional HTTP request — the fast path
        for bulk sync (per-statement round trips dominate otherwise)."""
        import libsql_client
        self._client.batch([libsql_client.Statement(sql, list(p)) for sql, p in stmts])

    def commit(self):
        pass

    def close(self):
        self._client.close()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS platforms (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    state TEXT NOT NULL,
    city TEXT,
    dark_store_code TEXT,
    UNIQUE(platform_id, state, city, dark_store_code)
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    platform_item_id TEXT NOT NULL,
    name TEXT,
    variant TEXT,
    l0_category TEXT,
    l1_category TEXT,
    l2_category TEXT,
    hsn_code TEXT,
    unit_margin_estimate REAL,
    UNIQUE(platform_id, platform_item_id)
);

-- DEMAND (primary)
CREATE TABLE IF NOT EXISTS sales_daily (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    sale_date TEXT NOT NULL,             -- ISO yyyy-mm-dd
    units INTEGER NOT NULL DEFAULT 0,
    gross_value REAL NOT NULL DEFAULT 0,
    returns_units INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform_id, item_id, location_id, sale_date)
);
CREATE INDEX IF NOT EXISTS idx_sales_item_loc ON sales_daily(item_id, location_id, sale_date);

-- STOCK
CREATE TABLE IF NOT EXISTS stock_snapshots (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    as_of_date TEXT NOT NULL,
    units_on_hand INTEGER NOT NULL,
    source TEXT NOT NULL,                -- 'panel' | 'ageing' | 'reconstructed'
    UNIQUE(platform_id, item_id, location_id, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_snap_item_loc ON stock_snapshots(item_id, location_id, as_of_date);

CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,            -- 'sale','receipt','return','recall','adjustment'
    units_delta INTEGER NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_item_loc ON stock_ledger(item_id, location_id, event_date);

CREATE TABLE IF NOT EXISTS storage_ageing (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES payout_cycles(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    ageing_date TEXT NOT NULL,
    units INTEGER NOT NULL,
    per_day_charge REAL,
    ageing_slab TEXT,
    charge_amount REAL,
    UNIQUE(cycle_id, item_id, location_id, ageing_date, ageing_slab)
);
CREATE INDEX IF NOT EXISTS idx_ageing_item_loc ON storage_ageing(item_id, location_id, ageing_date);

-- REPLENISHMENT (light in P1; fed from GRN 'Upfront Storage Charges' sheet)
CREATE TABLE IF NOT EXISTS replenishments (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    dispatched_date TEXT,
    expected_live_date TEXT,
    units INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'live', -- 'in_transit','live'
    sto_number TEXT,
    UNIQUE(platform_id, item_id, location_id, expected_live_date, sto_number)
);

-- FORECAST + DECISIONS
CREATE TABLE IF NOT EXISTS demand_forecasts (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    target_date TEXT NOT NULL,
    point REAL NOT NULL,
    lo_80 REAL NOT NULL,
    hi_80 REAL NOT NULL,
    method TEXT NOT NULL,                -- 'ets_direct','pooled_share','naive_drift'
    trained_through TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(platform_id, item_id, location_id, target_date, trained_through)
);
CREATE INDEX IF NOT EXISTS idx_fc_item_loc ON demand_forecasts(item_id, location_id, target_date);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    run_date TEXT NOT NULL,
    risk_type TEXT NOT NULL,             -- 'stockout' | 'overstock'
    days_of_cover REAL,
    forecast_daily_demand REAL,
    units_on_hand INTEGER,
    stockout_date TEXT,
    projected_storage_cost REAL,
    recommended_action TEXT NOT NULL,
    recommended_units INTEGER,
    priority_score REAL NOT NULL,
    forecast_source TEXT,                -- 'direct' | 'pooled'
    status TEXT NOT NULL DEFAULT 'open',
    UNIQUE(platform_id, item_id, location_id, run_date, risk_type)
);

-- PAYOUT ANALYTICS (secondary)
CREATE TABLE IF NOT EXISTS payout_cycles (
    id INTEGER PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES platforms(id),
    cycle_label TEXT NOT NULL,           -- e.g. '2026-05'
    period_start TEXT,
    period_end TEXT,
    source_file TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(platform_id, cycle_label)
);

CREATE TABLE IF NOT EXISTS payout_summary (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES payout_cycles(id),
    particular TEXT NOT NULL,
    delivered_amount REAL,
    cancelled_returned_amount REAL,
    total_amount REAL,
    UNIQUE(cycle_id, particular)
);

CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES payout_cycles(id),
    charge_type TEXT NOT NULL,           -- 'storage','recall','return','courier','cn_dn','lost_damaged','upfront_storage',...
    item_id INTEGER REFERENCES items(id),
    location_id INTEGER REFERENCES locations(id),
    charge_date TEXT,
    units INTEGER,
    amount REAL NOT NULL,
    gst_amount REAL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_charges_cycle ON charges(cycle_id, charge_type);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path=None):
    # Serve from Turso in deployed/read-only contexts (Vercel sets VERCEL=1).
    # Locally the pipeline always runs on the sqlite file even when .env
    # carries Turso credentials for the post-ingest sync.
    deployed = os.environ.get("VERCEL") or os.environ.get("FORESIGHT_READONLY") == "1"
    if TURSO_URL and db_path is None and deployed:
        return turso_connect(TURSO_URL, TURSO_TOKEN)
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def turso_connect(url: str | None = None, token: str | None = None):
    """Explicit remote handle (used by the dashboard on Vercel and by sync-turso).

    Prefers the native `libsql` driver (has wheels on Vercel's linux runtime);
    falls back to the pure-Python `libsql-client` where no wheel exists
    (e.g. Windows / Python 3.14 dev machines).
    """
    url, token = url or TURSO_URL, token or TURSO_TOKEN
    if not url:
        raise RuntimeError("FORESIGHT_TURSO_URL is not set")
    try:
        import libsql
        try:
            raw = libsql.connect(url, auth_token=token)
        except Exception:
            # driver builds without remote-only mode: embedded replica in /tmp
            raw = libsql.connect("/tmp/foresight-replica.db",
                                 sync_url=url, auth_token=token)
            raw.sync()
        return _TursoConnection(raw)
    except ImportError:
        import libsql_client
        # newer Turso DBs reject the legacy websocket protocol libsql:// implies
        # in this client — Hrana over HTTP works everywhere
        http_url = url.replace("libsql://", "https://")
        return _ClientConnection(libsql_client.create_client_sync(http_url, auth_token=token))


def init_db(conn) -> None:
    if isinstance(conn, _TursoConnection):
        return  # schema arrives with the pushed file; don't re-run DDL per request
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO platforms(code, name) VALUES ('blinkit', 'Blinkit')"
    )
    conn.commit()


def purge_cycle(conn: sqlite3.Connection, cycle_label: str, platform_code: str = "blinkit") -> dict:
    """Delete a payout cycle's cycle-tagged rows (charges, payout_summary,
    storage_ageing) so the zip can be re-ingested.

    NOTE: sales_daily and stock_ledger rows are NOT cycle-tagged — purging a
    fully ingested cycle leaves its demand/ledger contributions in place, and
    re-ingesting would double-count them. That case needs a fresh DB rebuild
    (`python -m foresight run-all <all zips>`). Purging a *partially* ingested
    cycle (e.g. one whose Forward Orders were never found) is safe.
    """
    pid = get_platform_id(conn, platform_code)
    row = conn.execute(
        "SELECT id FROM payout_cycles WHERE platform_id=? AND cycle_label=?",
        (pid, cycle_label)).fetchone()
    if row is None:
        return {"cycle": cycle_label, "purged": False, "reason": "no such cycle"}
    cid = row["id"]
    counts = {}
    for table in ("charges", "payout_summary", "storage_ageing"):
        counts[table] = conn.execute(
            f"DELETE FROM {table} WHERE cycle_id=?", (cid,)).rowcount
    conn.execute("DELETE FROM payout_cycles WHERE id=?", (cid,))
    conn.commit()
    return {"cycle": cycle_label, "purged": True, **counts}


def get_platform_id(conn: sqlite3.Connection, code: str = "blinkit") -> int:
    row = conn.execute("SELECT id FROM platforms WHERE code=?", (code,)).fetchone()
    if row is None:
        raise ValueError(f"unknown platform {code!r}")
    return row["id"]


def upsert_location(conn, platform_id: int, state: str, city: str | None = None,
                    dark_store_code: str | None = None) -> int:
    row = conn.execute(
        "SELECT id FROM locations WHERE platform_id=? AND state=? "
        "AND city IS ? AND dark_store_code IS ?",
        (platform_id, state, city, dark_store_code),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO locations(platform_id, state, city, dark_store_code) VALUES (?,?,?,?)",
        (platform_id, state, city, dark_store_code),
    )
    return cur.lastrowid


def upsert_item(conn, platform_id: int, platform_item_id: str, **fields) -> int:
    row = conn.execute(
        "SELECT id FROM items WHERE platform_id=? AND platform_item_id=?",
        (platform_id, str(platform_item_id)),
    ).fetchone()
    if row:
        item_id = row["id"]
        updates = {k: v for k, v in fields.items() if v is not None}
        if updates:
            sets = ", ".join(f"{k}=COALESCE({k}, ?)" for k in updates)
            conn.execute(f"UPDATE items SET {sets} WHERE id=?",
                         (*updates.values(), item_id))
        return item_id
    cols = ["platform_id", "platform_item_id", *fields.keys()]
    q = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO items({', '.join(cols)}) VALUES ({q})",
        (platform_id, str(platform_item_id), *fields.values()),
    )
    return cur.lastrowid
