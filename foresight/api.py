"""FastAPI app — dashboard + JSON API (spec §8)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from foresight import db

app = FastAPI(title="Foresight Stock Intelligence")
STATIC = Path(__file__).parent / "static"


def _conn():
    conn = db.connect()
    db.init_db(conn)
    return conn


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/summary")
def summary():
    conn = _conn()
    try:
        # counted stock only — 'allocated' rows are the same units re-cut by city
        as_of = conn.execute(
            "SELECT MAX(as_of_date) m FROM stock_snapshots WHERE source != 'allocated'"
        ).fetchone()["m"]
        on_hand = conn.execute("""
            WITH pool AS (SELECT * FROM stock_snapshots WHERE source != 'allocated')
            SELECT SUM(units_on_hand) u, COUNT(*) cells FROM pool s
            JOIN (SELECT item_id, location_id, MAX(as_of_date) m
                  FROM pool GROUP BY 1,2) t
              ON t.item_id=s.item_id AND t.location_id=s.location_id AND t.m=s.as_of_date
        """).fetchone()
        run = conn.execute("SELECT MAX(run_date) m FROM recommendations").fetchone()["m"]
        alerts = {r["risk_type"]: {"n": r["n"], "value": r["v"]} for r in conn.execute(
            "SELECT risk_type, COUNT(*) n, SUM(priority_score) v FROM recommendations "
            "WHERE run_date=? GROUP BY risk_type", (run,))}
        bt = conn.execute("SELECT value FROM meta WHERE key='backtest'").fetchone()
        cycles = [dict(r) for r in conn.execute(
            "SELECT cycle_label, period_start, period_end FROM payout_cycles ORDER BY cycle_label")]
        fc = conn.execute(
            "SELECT MAX(trained_through) tt, COUNT(DISTINCT item_id||'-'||location_id) cells "
            "FROM demand_forecasts").fetchone()
        mode = conn.execute(
            "SELECT source FROM stock_snapshots WHERE as_of_date=? AND source != 'allocated' LIMIT 1",
            (as_of,)).fetchone()
        return {
            "as_of": as_of, "stock_units": on_hand["u"], "stock_cells": on_hand["cells"],
            "stock_mode": "A (live panel)" if mode and mode["source"] == "panel" else "B (reconstructed)",
            "run_date": run,
            "stockout": alerts.get("stockout", {"n": 0, "value": 0}),
            "overstock": alerts.get("overstock", {"n": 0, "value": 0}),
            "backtest": json.loads(bt["value"]) if bt else None,
            "cycles": cycles, "trained_through": fc["tt"], "forecast_cells": fc["cells"],
            "low_confidence": len(cycles) < 4,
        }
    finally:
        conn.close()


@app.get("/api/feed")
def feed(risk_type: str | None = None, limit: int = 100):
    conn = _conn()
    try:
        run = conn.execute("SELECT MAX(run_date) m FROM recommendations").fetchone()["m"]
        q = """
            SELECT r.*, i.name AS item_name, i.platform_item_id, l.state, l.city
            FROM recommendations r
            JOIN items i ON i.id=r.item_id JOIN locations l ON l.id=r.location_id
            WHERE r.run_date=?"""
        params: list = [run]
        if risk_type:
            q += " AND r.risk_type=?"
            params.append(risk_type)
        q += " ORDER BY r.priority_score DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(q, params)]
    finally:
        conn.close()


@app.get("/api/grid")
def grid():
    """Days-of-cover per SKU x city (allocated stock vs mean next-14-day forecast).

    City rows come from the balancing run's 'allocated' snapshots. A state-grain
    row is included only when that item+state has no allocated city rows at all
    (dead stock — counted stock that no city demand claims), so state pools are
    never double-counted next to their own cities.
    """
    conn = _conn()
    try:
        tt = conn.execute("SELECT MAX(trained_through) m FROM demand_forecasts").fetchone()["m"]
        rows = conn.execute("""
            WITH latest AS (
                SELECT s.item_id, s.location_id, s.units_on_hand, s.source FROM stock_snapshots s
                JOIN (SELECT item_id, location_id, MAX(as_of_date) m
                      FROM stock_snapshots GROUP BY 1,2) t
                  ON t.item_id=s.item_id AND t.location_id=s.location_id AND t.m=s.as_of_date
            ), fdd AS (
                SELECT item_id, location_id, AVG(point) AS d, MIN(method) AS method
                FROM demand_forecasts
                WHERE trained_through=? AND target_date <= date(?, '+14 days')
                GROUP BY item_id, location_id
            )
            SELECT i.name AS item_name, i.platform_item_id, l.state, l.city,
                   latest.units_on_hand,
                   ROUND(COALESCE(fdd.d, 0), 2) AS daily_demand,
                   CASE WHEN COALESCE(fdd.d,0) > 0
                        THEN ROUND(latest.units_on_hand / fdd.d, 1) END AS days_of_cover,
                   COALESCE(fdd.method, 'none') AS method,
                   latest.item_id, latest.location_id
            FROM latest
            JOIN items i ON i.id=latest.item_id
            JOIN locations l ON l.id=latest.location_id
            LEFT JOIN fdd ON fdd.item_id=latest.item_id AND fdd.location_id=latest.location_id
            WHERE l.city IS NOT NULL
               OR NOT EXISTS (
                    SELECT 1 FROM latest l2 JOIN locations lo ON lo.id=l2.location_id
                    WHERE l2.item_id=latest.item_id AND lo.state=l.state
                      AND lo.city IS NOT NULL)
        """, (tt, tt)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/storage")
def storage():
    conn = _conn()
    try:
        latest_cycle = conn.execute(
            "SELECT id, cycle_label FROM payout_cycles ORDER BY cycle_label DESC LIMIT 1").fetchone()
        by_item = [dict(r) for r in conn.execute("""
            SELECT i.platform_item_id, i.name AS item_name, ROUND(SUM(c.amount)) AS amount
            FROM charges c JOIN items i ON i.id=c.item_id
            WHERE c.charge_type='storage' AND c.cycle_id=?
            GROUP BY i.id ORDER BY amount DESC""", (latest_cycle["id"],))]
        slabs = [dict(r) for r in conn.execute("""
            SELECT ageing_slab, SUM(units) units, ROUND(SUM(charge_amount),0) daily_cost
            FROM storage_ageing
            WHERE ageing_date=(SELECT MAX(ageing_date) FROM storage_ageing)
            GROUP BY ageing_slab ORDER BY MIN(per_day_charge)""")]
        run = conn.execute("SELECT MAX(run_date) m FROM recommendations").fetchone()["m"]
        recalls = [dict(r) for r in conn.execute("""
            SELECT i.platform_item_id, i.name AS item_name, l.state, l.city, r.units_on_hand, r.days_of_cover,
                   r.recommended_units, r.projected_storage_cost, r.priority_score AS net_saving
            FROM recommendations r
            JOIN items i ON i.id=r.item_id JOIN locations l ON l.id=r.location_id
            WHERE r.run_date=? AND r.risk_type='overstock'
            ORDER BY r.priority_score DESC""", (run,))]
        return {"cycle": latest_cycle["cycle_label"], "by_item": by_item,
                "slabs": slabs, "recall_candidates": recalls}
    finally:
        conn.close()


@app.get("/api/cells")
def cells():
    """Item/state pairs that have data, for the demand explorer selectors."""
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute("""
            SELECT DISTINCT i.id AS item_id, i.platform_item_id, i.name AS item_name,
                   l.id AS location_id, l.state, l.city, SUM(s.units) AS total_units
            FROM sales_daily s
            JOIN items i ON i.id=s.item_id JOIN locations l ON l.id=s.location_id
            GROUP BY i.id, l.id ORDER BY i.name, total_units DESC""")]
    finally:
        conn.close()


@app.get("/api/demand")
def demand(item_id: int, location_id: int | None = None):
    conn = _conn()
    try:
        loc_filter = "AND location_id=?" if location_id else ""
        params: tuple = (item_id, location_id) if location_id else (item_id,)
        hist = [dict(r) for r in conn.execute(f"""
            SELECT sale_date AS d, SUM(units) AS units FROM sales_daily
            WHERE item_id=? {loc_filter} GROUP BY sale_date ORDER BY sale_date""", params)]
        tt = conn.execute("SELECT MAX(trained_through) m FROM demand_forecasts").fetchone()["m"]
        fc = [dict(r) for r in conn.execute(f"""
            SELECT target_date AS d, ROUND(SUM(point),2) AS point,
                   ROUND(SUM(lo_80),2) AS lo, ROUND(SUM(hi_80),2) AS hi,
                   MIN(method) AS method
            FROM demand_forecasts
            WHERE trained_through=? AND item_id=? {loc_filter.replace('location_id', 'location_id')}
            GROUP BY target_date ORDER BY target_date""",
            (tt, item_id, location_id) if location_id else (tt, item_id))]
        # stock is counted per supply state; for a city cell, chart its state's pool
        stock_loc, stock_scope = location_id, "all India"
        if location_id:
            loc = conn.execute("SELECT state, city FROM locations WHERE id=?",
                               (location_id,)).fetchone()
            if loc and loc["city"]:
                st = conn.execute(
                    "SELECT id FROM locations WHERE state=? AND city IS NULL "
                    "AND dark_store_code IS NULL", (loc["state"],)).fetchone()
                if st:
                    stock_loc = st["id"]
                stock_scope = f"{loc['state']} state pool"
            elif loc:
                stock_scope = f"{loc['state']} state pool"
        sfilter = "AND location_id=?" if stock_loc else ""
        sparams = (item_id, stock_loc) if stock_loc else (item_id,)
        stock = [dict(r) for r in conn.execute(f"""
            SELECT as_of_date AS d, SUM(units_on_hand) AS units FROM stock_snapshots
            WHERE item_id=? {sfilter} AND source != 'allocated'
            GROUP BY as_of_date ORDER BY as_of_date""", sparams)]
        return {"history": hist, "forecast": fc, "stock": stock, "stock_scope": stock_scope}
    finally:
        conn.close()


@app.get("/api/payout")
def payout():
    conn = _conn()
    try:
        cycles = [dict(r) for r in conn.execute(
            "SELECT id, cycle_label FROM payout_cycles ORDER BY cycle_label")]
        out = []
        for c in cycles:
            particulars = [dict(r) for r in conn.execute(
                "SELECT particular, delivered_amount, cancelled_returned_amount, total_amount "
                "FROM payout_summary WHERE cycle_id=?", (c["id"],))]
            charges = [dict(r) for r in conn.execute(
                "SELECT charge_type, ROUND(SUM(amount)) amount, COUNT(*) n "
                "FROM charges WHERE cycle_id=? GROUP BY charge_type ORDER BY amount DESC",
                (c["id"],))]
            out.append({"cycle": c["cycle_label"], "particulars": particulars, "charges": charges})
        return out
    finally:
        conn.close()


@app.post("/api/upload")
async def upload(file: UploadFile):
    import os
    if os.environ.get("FORESIGHT_READONLY") == "1":
        return JSONResponse(
            {"error": "This deployment is read-only. Ingest locally with "
                      "`python -m foresight run-all <zip>` and push the DB to Turso "
                      "(`turso db create foresight --from-file foresight.db`)."},
            status_code=501)
    from foresight.ingest import blinkit
    from foresight import stock, forecast, balance

    suffix = Path(file.filename or "upload").suffix.lower() or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    conn = _conn()
    try:
        if suffix == ".zip":
            result = blinkit.ingest_monthly_zip(conn, tmp_path,
                                                cycle_label=_label_from_name(file.filename))
        else:
            result = blinkit.ingest_daily_sales(conn, tmp_path)
        if "skipped" not in result:
            stock.build_snapshots_from_ageing(conn)
            result |= stock.reconstruct_forward(conn)
            result |= forecast.run_forecast(conn)
            bt = forecast.backtest_mape(conn)
            if bt:
                conn.execute("INSERT OR REPLACE INTO meta(key, value, updated_at) "
                             "VALUES ('backtest', ?, datetime('now'))", (json.dumps(bt),))
                conn.commit()
            result |= balance.run_balancing(conn)
        return JSONResponse(result)
    except Exception as e:  # surfaced in the upload UI
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()
        tmp_path.unlink(missing_ok=True)


def _label_from_name(name: str | None) -> str | None:
    import re
    if not name:
        return None
    m = re.search(r"(\d{4}-\d{2})", name)
    return m.group(1) if m else None
