"""Stock handling — Mode B reconstruction.

The monthly Daily Ageing sheet is a true daily count of units sitting in Blinkit
storage per state x item (sum across ageing slabs), so past days come from actual
counts (source='ageing'). From each cell's last counted day we roll the ledger
forward — on_hand(t) = on_hand(t-1) - sales(t) + returns(t) + receipts(t) - recalls(t)
— and store source='reconstructed'. When a newer ageing sheet lands, its actual
counts overwrite the reconstructed span (monthly self-correction, spec §6).

Mode A (live panel report) later just inserts source='panel' snapshots; everything
downstream reads the latest snapshot regardless of source.
"""
from __future__ import annotations

from datetime import date, timedelta

from foresight import db


def build_snapshots_from_ageing(conn) -> int:
    """Collapse storage_ageing slabs to daily on-hand snapshots. Latest cycle wins on overlap."""
    pid = db.get_platform_id(conn)
    # A zip's ageing sheet carries date columns well before its own cycle, but
    # those pre-period columns hold residual junk (verified: April's Mar-15
    # column sums to ~144 units vs the March zip's 13,748). Only trust a
    # cycle's columns within [period_start - 1 day, period_end]; where two
    # cycles both own a boundary date, the newer cycle wins.
    conn.execute("DELETE FROM stock_snapshots WHERE source IN ('ageing', 'reconstructed', 'allocated')")
    rows = conn.execute("""
        WITH ranked AS (
            SELECT sa.item_id, sa.location_id, sa.ageing_date,
                   SUM(sa.units) AS units,
                   RANK() OVER (PARTITION BY sa.item_id, sa.location_id, sa.ageing_date
                                ORDER BY (CASE WHEN sa.ageing_date BETWEEN pc.period_start
                                                                       AND pc.period_end
                                               THEN 1 ELSE 0 END) DESC,
                                         pc.cycle_label DESC) AS rnk
            FROM storage_ageing sa
            JOIN payout_cycles pc ON pc.id = sa.cycle_id
            WHERE pc.period_start IS NULL OR pc.period_end IS NULL
               OR (sa.ageing_date >= date(pc.period_start, '-1 day')
                   AND sa.ageing_date <= pc.period_end)
            GROUP BY sa.cycle_id, sa.item_id, sa.location_id, sa.ageing_date
        )
        SELECT item_id, location_id, ageing_date, units FROM ranked WHERE rnk = 1
    """).fetchall()
    n = 0
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO stock_snapshots"
            "(platform_id, item_id, location_id, as_of_date, units_on_hand, source) "
            "VALUES (?,?,?,?,?, 'ageing')",
            (pid, r["item_id"], r["location_id"], r["ageing_date"], r["units"]))
        n += 1
    conn.commit()
    return n


def reconstruct_forward(conn, through: str | None = None) -> dict:
    """Roll each cell forward from its last counted snapshot using ledger events.

    `through` defaults to the last date any data exists (sales or ageing) — projecting
    beyond the data would just freeze balances and pretend freshness we don't have.
    """
    pid = db.get_platform_id(conn)
    if through is None:
        row = conn.execute("""
            SELECT MAX(d) AS m FROM (
                SELECT MAX(sale_date) AS d FROM sales_daily
                UNION ALL SELECT MAX(as_of_date) FROM stock_snapshots
            )""").fetchone()
        through = row["m"]
    if through is None:
        return {"reconstructed": 0}

    # Counted stock (ageing/panel) is state-grain; sales/returns in the ledger
    # are city-grain. Roll each state cell forward with its state's total
    # ledger movement (join locations to aggregate cities up to their state).
    cells = conn.execute("""
        SELECT s.item_id, s.location_id, l.state, MAX(s.as_of_date) AS last_date
        FROM stock_snapshots s JOIN locations l ON l.id = s.location_id
        WHERE s.source IN ('ageing', 'panel')
        GROUP BY s.item_id, s.location_id
    """).fetchall()

    deltas: dict[tuple[int, str], dict[str, int]] = {}
    for r in conn.execute("""
        SELECT sl.item_id, l.state, sl.event_date, SUM(sl.units_delta) AS delta
        FROM stock_ledger sl JOIN locations l ON l.id = sl.location_id
        GROUP BY sl.item_id, l.state, sl.event_date
    """):
        deltas.setdefault((r["item_id"], r["state"]), {})[r["event_date"]] = r["delta"]

    n = 0
    for c in cells:
        key = (c["item_id"], c["location_id"])
        last = c["last_date"]
        if last >= through:
            continue
        on_hand = conn.execute(
            "SELECT units_on_hand FROM stock_snapshots "
            "WHERE item_id=? AND location_id=? AND as_of_date=?",
            (*key, last)).fetchone()["units_on_hand"]
        d = date.fromisoformat(last)
        end = date.fromisoformat(through)
        cell_deltas = deltas.get((c["item_id"], c["state"]), {})
        while d < end:
            d += timedelta(days=1)
            on_hand = max(0, on_hand + cell_deltas.get(d.isoformat(), 0))
            conn.execute(
                "INSERT OR REPLACE INTO stock_snapshots"
                "(platform_id, item_id, location_id, as_of_date, units_on_hand, source) "
                "VALUES (?,?,?,?,?, 'reconstructed')",
                (pid, *key, d.isoformat(), on_hand))
            n += 1
    conn.commit()
    return {"reconstructed": n, "through": through}


def current_stock(conn, include_allocated: bool = False) -> list[dict]:
    """Latest on-hand per cell, with its as-of date, source, and location.

    By default excludes source='allocated' rows (state stock apportioned to
    cities by the balancing run) so counted stock is never double-counted.
    """
    src = "" if include_allocated else "WHERE source != 'allocated'"
    rows = conn.execute(f"""
        WITH pool AS (SELECT * FROM stock_snapshots {src})
        SELECT s.item_id, s.location_id, s.units_on_hand, s.as_of_date, s.source,
               l.state, l.city
        FROM pool s
        JOIN locations l ON l.id = s.location_id
        JOIN (SELECT item_id, location_id, MAX(as_of_date) AS m
              FROM pool GROUP BY item_id, location_id) latest
          ON latest.item_id = s.item_id AND latest.location_id = s.location_id
         AND latest.m = s.as_of_date
    """).fetchall()
    return [dict(r) for r in rows]
