"""Tunable defaults. Override via environment variables where noted."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load a git-ignored .env (KEY=VALUE lines) so Turso credentials live per-project.
# Real environment variables always win (setdefault).
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

DB_PATH = Path(os.environ.get("FORESIGHT_DB", PROJECT_ROOT / "foresight.db"))

# --- Balancing engine defaults (per spec §10.3: tunable, confirmed later) ---
SERVICE_LEVEL_Z = 1.645          # 95% service level
COVER_TARGET_DAYS = 7            # min cover before "push stock" fires
MAX_COVER_DAYS = 45              # cover ceiling before overstock fires
FORECAST_HORIZON_DAYS = 90       # rolling planning horizon
RECENT_DEMAND_WINDOW = 14        # days averaged for forecast_daily_demand

# --- Forecast pooling thresholds (dense vs sparse SKU×city cells) ---
DENSE_MIN_DAYS = 21              # minimum days of history for a direct model
DENSE_MIN_UNITS = 90             # minimum total units for a direct model
RECENT_RATE_MIN_DAYS = 28        # below this history, skip ETS: use recent in-stock rate
RECENT_RATE_WINDOW = 14          # trailing days averaged for the recent-rate fallback
POOL_SHARE_WINDOW = 28           # days used to compute allocation shares
LOW_CONFIDENCE_CYCLES = 4        # cycles below which forecasts are labeled low-confidence

# --- Storage ageing slabs: fallback per-day ₹/unit when not derivable from data ---
DEFAULT_SLAB_RATES = {
    "0 to 30 days": 1.00,
    "31 to 60 days": 1.25,
    "61 to 90 days": 1.50,
    "91 to 120 days": 2.00,
    "120+ days": 2.50,
}
