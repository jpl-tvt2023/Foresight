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
    RECENT_RATE_MIN_DAYS, RECENT_RATE_WINDOW,
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


def _load_zero_stock(conn) -> dict[int, set[str]]:
    """Dates each item had zero counted stock nationally (censored demand).

    Counted = ageing/reconstructed/panel snapshots ('allocated' rows are the
    balancing run's estimates, not counts). Items with no snapshot history at
    all get no entry — their series are never masked.
    """
    out: dict[int, set[str]] = {}
    for r in conn.execute("""
        SELECT item_id, as_of_date, SUM(units_on_hand) AS u
        FROM stock_snapshots WHERE source != 'allocated'
        GROUP BY item_id, as_of_date"""):
        if r["u"] <= 0:
            out.setdefault(r["item_id"], set()).add(r["as_of_date"])
    return out


def _mask_censored(s: pd.Series, zero_dates: set[str]) -> pd.Series:
    """Zero-sales days that were zero-stock days aren't demand signal — mark
    them missing so models don't learn that demand died while shelves were bare."""
    if not zero_dates:
        return s
    censored = (s.values == 0) & np.array(
        [d.date().isoformat() in zero_dates for d in s.index])
    if not censored.any():
        return s
    s = s.copy()
    s[censored] = np.nan
    return s


def _fit_forecast(series: pd.Series, horizon: int) -> tuple[np.ndarray, float, str]:
    """Return (point forecasts, residual sigma, method).

    NaNs in `series` are censored (zero-stock) days: interpolated for ETS,
    skipped in the naive path's means.
    """
    # Too little history for weekly structure: flat trailing in-stock rate.
    if len(series) < RECENT_RATE_MIN_DAYS:
        tail = series[-RECENT_RATE_WINDOW:]
        rate = float(np.nanmean(tail.values)) if np.isfinite(tail.values).any() else 0.0
        rate = max(rate, 0.0)
        sigma = float(np.nanstd(tail.values)) if np.isfinite(tail.values).sum() > 1 else max(rate, 1.0)
        return np.full(horizon, rate), sigma, "recent_rate"

    y = series.interpolate(limit_direction="both").values
    if len(y) >= 21 and np.nansum(y) > 0:
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
    # seasonal-naive with drift: average by weekday over last 28 days + linear
    # drift (means skip censored NaN days)
    tail = series[-28:] if len(series) >= 28 else series
    by_dow = tail.groupby(tail.index.dayofweek).mean()
    overall = float(tail.mean()) if len(tail) else 0.0
    if not np.isfinite(overall):
        overall = 0.0
    drift = 0.0
    if len(series) >= 28:
        d = (series[-14:].mean() - series[-28:-14].mean()) / 14.0
        drift = float(d) if np.isfinite(d) else 0.0
    last_day = series.index[-1]
    fc = np.array([
        max(0.0, by_dow.get((last_day + timedelta(days=h + 1)).dayofweek, overall) + drift * (h + 1))
        for h in range(horizon)])
    fc = np.nan_to_num(fc, nan=overall)
    sigma = float(tail.std()) if tail.notna().sum() > 1 else max(overall, 1.0)
    return fc, sigma, "naive_drift"


def run_forecast(conn, horizon: int = FORECAST_HORIZON_DAYS) -> dict:
    pid = db.get_platform_id(conn)
    df = _load_sales(conn)
    if df.empty:
        return {"forecast_cells": 0}

    trained_through = df["sale_date"].max()
    tt_iso = trained_through.date().isoformat()
    future_dates = [(trained_through + timedelta(days=h + 1)).date().isoformat()
                    for h in range(horizon)]

    conn.execute("DELETE FROM demand_forecasts WHERE trained_through=?", (tt_iso,))

    stats = {"direct": 0, "pooled": 0, "cells": 0}
    share_cutoff = trained_through - timedelta(days=POOL_SHARE_WINDOW - 1)
    zero_stock = _load_zero_stock(conn)

    for item_id, item_df in df.groupby("item_id"):
        # National series starts at the item's own first sale — padding a
        # late launch back to the global start trains on fake zero days.
        item_zero = zero_stock.get(item_id, set())
        nat = _mask_censored(
            _daily_series(item_df, item_df["sale_date"].min(), trained_through),
            item_zero)
        nat_fc, nat_sigma, nat_method = _fit_forecast(nat, horizon)

        recent = item_df[item_df["sale_date"] >= share_cutoff]
        recent_by_loc = recent.groupby("location_id")["units"].sum()
        recent_total = float(recent_by_loc.sum())

        for loc_id, cell_df in item_df.groupby("location_id"):
            first_sale = cell_df["sale_date"].min()
            cell = _mask_censored(
                _daily_series(cell_df, first_sale, trained_through), item_zero)
            n_days = len(cell)
            total_units = float(np.nansum(cell.values))

            if n_days >= DENSE_MIN_DAYS and total_units >= DENSE_MIN_UNITS:
                fc, sigma, m = _fit_forecast(cell, horizon)
                method = "ets_direct" if m == "ets" else m
            else:
                share = (float(recent_by_loc.get(loc_id, 0.0)) / recent_total
                         if recent_total > 0 else 0.0)
                fc = nat_fc * share
                sigma = nat_sigma * max(share, 0.02)
                # New items (< RECENT_RATE_MIN_DAYS of history) surface as
                # 'recent_rate' so the UI can say "recent rate — new item"
                # instead of implying a modeled pooled forecast.
                method = "recent_rate" if nat_method == "recent_rate" else "pooled_share"

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
    """Hold out the last `holdout_days`, retrain, report accuracy at SKU-national level.

    Honesty gauge for the dashboard (spec §7.5). Headline `mape_pct` is
    volume-weighted (sum of absolute errors / total units sold), so a 2-unit
    item can't swamp the metric and dead items still pay for overforecasts.
    Each item trains from its own first sale with censored (zero-stock) days
    masked — same treatment as production.
    """
    df = _load_sales(conn)
    if df.empty:
        return None
    end = df["sale_date"].max()
    cut = end - timedelta(days=holdout_days)
    train, test = df[df["sale_date"] <= cut], df[df["sale_date"] > cut]
    if train["sale_date"].nunique() < 28 or test.empty:
        return None
    zero_stock = _load_zero_stock(conn)
    pairs = []                                    # (actual, forecast) per item
    for item_id, g in train.groupby("item_id"):
        s = _mask_censored(_daily_series(g, g["sale_date"].min(), cut),
                           zero_stock.get(item_id, set()))
        fc, _, _ = _fit_forecast(s, holdout_days)
        actual = _daily_series(test[test["item_id"] == item_id], cut + timedelta(days=1), end)
        pairs.append((float(actual.sum()), float(fc.sum())))
    total_vol = sum(a for a, _ in pairs)
    if not pairs or total_vol <= 0:
        return None
    weighted = sum(abs(a - f) for a, f in pairs) / total_vol
    apes = [abs(a - f) / a for a, f in pairs if a > 0]
    apes_50u = [abs(a - f) / a for a, f in pairs if a >= 50]
    return {"holdout_days": holdout_days, "n_items": len(pairs),
            "mape_pct": round(100 * weighted, 1),
            "median_ape_pct": round(100 * float(np.median(apes)), 1) if apes else None,
            "mean_ape_50u_pct": round(100 * float(np.mean(apes_50u)), 1) if apes_50u else None}
