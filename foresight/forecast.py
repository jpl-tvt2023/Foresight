"""Demand forecasting — rolling 90-day, per SKU x state (spec §7.1).

Dense cells get a direct ETS (Holt-Winters, weekly additive seasonality, damped
additive trend). Sparse cells are forecast at SKU-national level and allocated
down by each state's recent share of that SKU's volume, flagged 'pooled' so the
UI can distinguish measured from inferred. Cold-start fallback is seasonal-naive
with drift.

80% bands come from in-sample residual sigma, widened linearly to ~2x by day 90
(matches the empirical band growth seen in the Apr–May validation run).
"""
from __future__ import annotations

import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd

from foresight import db
from foresight.config import (
    DENSE_MIN_DAYS, DENSE_MIN_UNITS, FORECAST_HORIZON_DAYS, POOL_SHARE_WINDOW,
)

Z80 = 1.2816


def _load_sales(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT item_id, location_id, sale_date, units FROM sales_daily", conn)
    if df.empty:
        return df
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    return df


def _daily_series(df: pd.DataFrame, start, end) -> pd.Series:
    """Continuous daily series (zero-filled) over [start, end]."""
    idx = pd.date_range(start, end, freq="D")
    s = df.groupby("sale_date")["units"].sum().reindex(idx, fill_value=0)
    return s.astype(float)


def _fit_forecast(series: pd.Series, horizon: int) -> tuple[np.ndarray, float, str]:
    """Return (point forecasts, residual sigma, method)."""
    y = series.values
    if len(y) >= 21 and y.sum() > 0:
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = ExponentialSmoothing(
                    y, trend="add", damped_trend=True,
                    seasonal="add", seasonal_periods=7,
                    initialization_method="estimated").fit(optimized=True)
            fc = np.asarray(model.forecast(horizon))
            resid = y - model.fittedvalues
            sigma = float(np.nanstd(resid))
            if np.all(np.isfinite(fc)):
                return np.clip(fc, 0, None), sigma, "ets"
        except Exception:
            pass
    # seasonal-naive with drift: average by weekday over last 28 days + linear drift
    tail = series[-28:] if len(series) >= 28 else series
    by_dow = tail.groupby(tail.index.dayofweek).mean()
    overall = float(tail.mean()) if len(tail) else 0.0
    drift = 0.0
    if len(series) >= 28:
        drift = (series[-14:].mean() - series[-28:-14].mean()) / 14.0
    last_day = series.index[-1]
    fc = np.array([
        max(0.0, by_dow.get((last_day + timedelta(days=h + 1)).dayofweek, overall) + drift * (h + 1))
        for h in range(horizon)])
    sigma = float(tail.std()) if len(tail) > 1 else max(overall, 1.0)
    return fc, sigma, "naive_drift"


def run_forecast(conn, horizon: int = FORECAST_HORIZON_DAYS) -> dict:
    pid = db.get_platform_id(conn)
    df = _load_sales(conn)
    if df.empty:
        return {"forecast_cells": 0}

    trained_through = df["sale_date"].max()
    start = df["sale_date"].min()
    tt_iso = trained_through.date().isoformat()
    future_dates = [(trained_through + timedelta(days=h + 1)).date().isoformat()
                    for h in range(horizon)]

    conn.execute("DELETE FROM demand_forecasts WHERE trained_through=?", (tt_iso,))

    stats = {"direct": 0, "pooled": 0, "cells": 0}
    share_cutoff = trained_through - timedelta(days=POOL_SHARE_WINDOW - 1)

    for item_id, item_df in df.groupby("item_id"):
        nat = _daily_series(item_df, start, trained_through)
        nat_fc, nat_sigma, nat_method = _fit_forecast(nat, horizon)

        recent = item_df[item_df["sale_date"] >= share_cutoff]
        recent_by_loc = recent.groupby("location_id")["units"].sum()
        recent_total = float(recent_by_loc.sum())

        for loc_id, cell_df in item_df.groupby("location_id"):
            first_sale = cell_df["sale_date"].min()
            cell = _daily_series(cell_df, first_sale, trained_through)
            n_days = len(cell)
            total_units = float(cell.sum())

            if n_days >= DENSE_MIN_DAYS and total_units >= DENSE_MIN_UNITS:
                fc, sigma, m = _fit_forecast(cell, horizon)
                method = "ets_direct" if m == "ets" else "naive_drift"
            else:
                share = (float(recent_by_loc.get(loc_id, 0.0)) / recent_total
                         if recent_total > 0 else 0.0)
                fc = nat_fc * share
                sigma = nat_sigma * max(share, 0.02)
                method = "pooled_share"

            rows = []
            for h in range(horizon):
                widen = 1.0 + h / horizon          # sigma doubles by day 90
                half = Z80 * sigma * widen
                point = float(fc[h])
                rows.append((pid, item_id, loc_id, future_dates[h],
                             round(point, 3), round(max(0.0, point - half), 3),
                             round(point + half, 3), method, tt_iso))
            conn.executemany(
                "INSERT OR REPLACE INTO demand_forecasts"
                "(platform_id, item_id, location_id, target_date, point, lo_80, hi_80, method, trained_through) "
                "VALUES (?,?,?,?,?,?,?,?,?)", rows)
            stats["cells"] += 1
            stats["direct" if method != "pooled_share" else "pooled"] += 1

    conn.commit()
    stats["trained_through"] = tt_iso
    return stats


def backtest_mape(conn, holdout_days: int = 14) -> dict | None:
    """Hold out the last `holdout_days`, retrain, report MAPE at SKU-national level.

    Honesty gauge for the dashboard (spec §7.5); needs enough history to matter.
    """
    df = _load_sales(conn)
    if df.empty:
        return None
    end = df["sale_date"].max()
    cut = end - timedelta(days=holdout_days)
    train, test = df[df["sale_date"] <= cut], df[df["sale_date"] > cut]
    if train["sale_date"].nunique() < 28 or test.empty:
        return None
    results = []
    for item_id, g in train.groupby("item_id"):
        s = _daily_series(g, train["sale_date"].min(), cut)
        fc, _, _ = _fit_forecast(s, holdout_days)
        actual = _daily_series(test[test["item_id"] == item_id], cut + timedelta(days=1), end)
        a, f = float(actual.sum()), float(fc.sum())
        if a > 0:
            results.append(abs(a - f) / a)
    if not results:
        return None
    return {"holdout_days": holdout_days, "n_items": len(results),
            "mape_pct": round(100 * float(np.mean(results)), 1)}
