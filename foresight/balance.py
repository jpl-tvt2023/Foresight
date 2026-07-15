"""Balancing engine — per SKU x city daily run (spec §7.2/§7.3).

Demand and forecasts are city-grain (Customer City == dark-store city in
q-commerce). Counted stock (Daily Ageing) exists only per state, so each state's
on-hand is allocated to its cities by their share of the last 28 days' demand and
written back as stock_snapshots(source='allocated') for the grid. Alerts carry
the same measured/pooled flag as the forecast; stock allocation is an estimate by
construction and labeled as such in the UI.

State stock with zero recent demand anywhere in the state can't be allocated —
it surfaces as a state-level overstock alert (the recall lever's clearest case).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from foresight import db, stock as stock_mod
from foresight.config import (
    COVER_TARGET_DAYS, DEFAULT_SLAB_RATES, MAX_COVER_DAYS,
    POOL_SHARE_WINDOW, RECENT_DEMAND_WINDOW, SERVICE_LEVEL_Z,
)


def _slab_rate(conn, item_id: int, location_id: int | None) -> tuple[float, float]:
    """(current blended per-unit-day rate, next-slab rate) from the latest ageing
    data at the item's *state* location (ageing is state-grain)."""
    rows = conn.execute("""
        SELECT ageing_slab, units, per_day_charge FROM storage_ageing
        WHERE item_id=? AND location_id=?
          AND ageing_date = (SELECT MAX(ageing_date) FROM storage_ageing
                             WHERE item_id=? AND location_id=?)
    """, (item_id, location_id, item_id, location_id)).fetchall() if location_id else []
    if not rows or sum(r["units"] for r in rows) == 0:
        return DEFAULT_SLAB_RATES["0 to 30 days"], DEFAULT_SLAB_RATES["31 to 60 days"]
    units = sum(r["units"] for r in rows)
    blended = sum(r["units"] * r["per_day_charge"] for r in rows) / units
    top = max(r["per_day_charge"] for r in rows)
    rates = sorted({r["per_day_charge"] for r in rows} | set(DEFAULT_SLAB_RATES.values()))
    nxt = next((x for x in rates if x > top), top)
    return blended, nxt


def _recall_unit_cost(conn) -> float:
    row = conn.execute(
        "SELECT SUM(amount)/NULLIF(SUM(units),0) AS c FROM charges WHERE charge_type='recall'"
    ).fetchone()
    return float(row["c"]) if row and row["c"] else 5.9


def _insert_reco(conn, pid, item_id, loc_id, run_date, risk, **f):
    conn.execute(
        "INSERT INTO recommendations(platform_id, item_id, location_id, run_date, risk_type, "
        "days_of_cover, forecast_daily_demand, units_on_hand, stockout_date, "
        "projected_storage_cost, recommended_action, recommended_units, priority_score, forecast_source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, item_id, loc_id, run_date, risk,
         f.get("days_of_cover"), f.get("fdd"), f.get("units_on_hand"), f.get("stockout_date"),
         f.get("projected_storage_cost"), f["action"], f.get("recommended_units"),
         f["priority"], f.get("source", "direct")))


def run_balancing(conn) -> dict:
    pid = db.get_platform_id(conn)

    tt = conn.execute("SELECT MAX(trained_through) AS m FROM demand_forecasts").fetchone()["m"]
    if tt is None:
        return {"error": "no forecasts — run forecast first"}

    fc = pd.read_sql_query(
        "SELECT item_id, location_id, target_date, point, method FROM demand_forecasts "
        "WHERE trained_through=? ORDER BY target_date", conn, params=(tt,))
    loc_info = {r["id"]: (r["state"], r["city"])
                for r in conn.execute("SELECT id, state, city FROM locations")}
    state_loc = {r["state"]: r["id"] for r in conn.execute(
        "SELECT id, state FROM locations WHERE city IS NULL AND dark_store_code IS NULL")}
    items = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM items")}

    # counted stock is state-grain (ageing/reconstructed; panel later)
    state_stock: dict[tuple[int, str], int] = {}
    for r in stock_mod.current_stock(conn):
        state_stock[(r["item_id"], r["state"])] = \
            state_stock.get((r["item_id"], r["state"]), 0) + r["units_on_hand"]

    # in-transit replenishments, aggregated to state
    in_transit: dict[tuple[int, str], int] = {}
    for r in conn.execute("""
        SELECT rp.item_id, l.state, SUM(rp.units) AS u
        FROM replenishments rp JOIN locations l ON l.id=rp.location_id
        WHERE rp.status='in_transit' GROUP BY rp.item_id, l.state"""):
        in_transit[(r["item_id"], r["state"])] = r["u"]

    # 28-day demand: city shares of their state, and per-cell daily std
    recent = pd.read_sql_query(
        "SELECT s.item_id, s.location_id, s.sale_date, s.units, l.state "
        "FROM sales_daily s JOIN locations l ON l.id=s.location_id "
        "WHERE s.sale_date >= date(?, ?)",
        conn, params=(tt, f"-{POOL_SHARE_WINDOW - 1} days"))
    if recent.empty:
        return {"error": "no recent sales"}
    cell_units = recent.groupby(["item_id", "location_id"])["units"].sum()
    state_units = recent.groupby(["item_id", "state"])["units"].sum()
    hist_std = recent.groupby(["item_id", "location_id"])["units"].std().fillna(0.0).to_dict()

    recall_cost = _recall_unit_cost(conn)
    run_date = tt
    conn.execute("DELETE FROM recommendations WHERE run_date=?", (run_date,))
    conn.execute("DELETE FROM stock_snapshots WHERE source='allocated'")

    n_stockout = n_overstock = 0
    demanded_states: set[tuple[int, str]] = set()

    for (item_id, loc_id), cell_fc in fc.groupby(["item_id", "location_id"]):
        state, city = loc_info.get(loc_id, (None, None))
        if state is None:
            continue
        st_total = float(state_units.get((item_id, state), 0.0))
        if st_total > 0:
            demanded_states.add((item_id, state))
        share = (float(cell_units.get((item_id, loc_id), 0.0)) / st_total
                 if st_total > 0 else 0.0)
        on_hand = int(round(state_stock.get((item_id, state), 0) * share))
        transit = int(round(in_transit.get((item_id, state), 0) * share))
        available = on_hand + transit

        conn.execute(
            "INSERT OR REPLACE INTO stock_snapshots"
            "(platform_id, item_id, location_id, as_of_date, units_on_hand, source) "
            "VALUES (?,?,?,?,?, 'allocated')",
            (pid, item_id, loc_id, run_date, on_hand))

        points = cell_fc["point"].to_numpy()
        dates = cell_fc["target_date"].tolist()
        method = cell_fc["method"].iloc[0]
        source = "pooled" if method == "pooled_share" else "direct"

        fdd = float(points[:RECENT_DEMAND_WINDOW].mean())
        sd = hist_std.get((item_id, loc_id), 0.0)
        safety = SERVICE_LEVEL_Z * sd * math.sqrt(COVER_TARGET_DAYS)
        reorder_point = fdd * COVER_TARGET_DAYS + safety
        ceiling = fdd * MAX_COVER_DAYS
        cover = available / fdd if fdd > 0 else float("inf")
        margin = items.get(item_id, {}).get("unit_margin_estimate") or 0.0

        proj = available - np.cumsum(points)
        zero_idx = next((i for i in range(len(proj)) if proj[i] <= 0), None)
        reorder_cross = next((dates[i] for i in range(len(proj)) if proj[i] < reorder_point), None)

        if fdd > 0 and available < reorder_point:
            days_short_demand = float(points[zero_idx:].sum()) if zero_idx is not None else 0.0
            priority = days_short_demand * margin if days_short_demand else fdd * margin * COVER_TARGET_DAYS
            zero_date = dates[zero_idx] if zero_idx is not None else None
            action = (f"Push stock now — projected stockout ~{zero_date}"
                      if zero_date else "Below reorder point — push stock / nudge Blinkit replenishment")
            push_units = max(int(math.ceil(fdd * MAX_COVER_DAYS * 0.66 - available)), 0)
            _insert_reco(conn, pid, item_id, loc_id, run_date, "stockout",
                         days_of_cover=round(cover, 1), fdd=round(fdd, 2),
                         units_on_hand=on_hand, stockout_date=zero_date or reorder_cross,
                         action=action, recommended_units=push_units,
                         priority=round(priority, 2), source=source)
            n_stockout += 1

        elif available > 0 and cover > MAX_COVER_DAYS:
            excess = int(available - math.ceil(ceiling)) if fdd > 0 else available
            if excess <= 0:
                continue
            rate_now, rate_next = _slab_rate(conn, item_id, state_loc.get(state))
            sell_through = np.cumsum(points)
            excess_path = np.clip(available - sell_through - ceiling, 0, excess)
            eff_rate = (rate_now + rate_next) / 2
            storage_cost = float((excess_path * eff_rate).sum())
            net_saving = storage_cost - excess * recall_cost
            if net_saving <= 0:
                continue
            cover_txt = "∞" if math.isinf(cover) else f"{cover:.0f}"
            action = (f"Recall ~{excess} units — {cover_txt} days of cover at "
                      f"{fdd:.1f}/day; ₹{storage_cost:,.0f} projected storage over "
                      f"{len(points)}d vs ₹{excess * recall_cost:,.0f} recall cost")
            _insert_reco(conn, pid, item_id, loc_id, run_date, "overstock",
                         days_of_cover=None if math.isinf(cover) else round(cover, 1),
                         fdd=round(fdd, 2), units_on_hand=on_hand,
                         projected_storage_cost=round(storage_cost, 2),
                         action=action, recommended_units=excess,
                         priority=round(net_saving, 2), source=source)
            n_overstock += 1

    # State stock that no city demand claims (zero sales statewide in 28d):
    # pure dead stock — recall candidate at state grain.
    n_dead = 0
    for (item_id, state), units in state_stock.items():
        if units <= 0 or (item_id, state) in demanded_states:
            continue
        loc_id = state_loc.get(state)
        if loc_id is None:
            continue
        rate_now, rate_next = _slab_rate(conn, item_id, loc_id)
        horizon = 90
        storage_cost = units * (rate_now + rate_next) / 2 * horizon
        net_saving = storage_cost - units * recall_cost
        if net_saving <= 0:
            continue
        action = (f"Recall all {units} units — no sales in {state} for "
                  f"{POOL_SHARE_WINDOW}+ days; ₹{storage_cost:,.0f} projected storage "
                  f"over {horizon}d vs ₹{units * recall_cost:,.0f} recall cost")
        _insert_reco(conn, pid, item_id, loc_id, run_date, "overstock",
                     days_of_cover=None, fdd=0.0, units_on_hand=units,
                     projected_storage_cost=round(storage_cost, 2),
                     action=action, recommended_units=units,
                     priority=round(net_saving, 2), source="direct")
        n_overstock += 1
        n_dead += 1

    conn.commit()
    return {"run_date": run_date, "stockout_alerts": n_stockout,
            "overstock_alerts": n_overstock, "dead_stock_states": n_dead}
