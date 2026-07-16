# Foresight — Stock Intelligence

Inventory balancing & demand forecasting for Blinkit dark stores (Royal Mart).
Keeps every SKU × state cell in a healthy band: enough stock to avoid stockouts,
not so much that units age into storage charges. Payout analytics ride along as a
secondary layer. See the full technical spec (v1.0, 14 Jul 2026) for background.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# one-shot: ingest → reconstruct stock → forecast → balance
.venv\Scripts\python -m foresight run-all "C:\path\payout_sheet_2026-04.zip" "C:\path\payout_sheet_2026-05.zip"

# dashboard at http://127.0.0.1:8000
.venv\Scripts\python -m foresight serve
```

Daily loop: upload the seller-panel daily sales export (csv/xlsx) on the
dashboard's Upload tab (or `python -m foresight run-all daily.csv`) — the stock
ledger rolls forward, the 90-day forecast retrains, and the action feed re-ranks.

## CLI

| Command | Does |
|---|---|
| `python -m foresight ingest <file>...` | Monthly payout zip(s) or daily sales export |
| `python -m foresight reconstruct` | Ageing → snapshots, ledger roll-forward (Mode B) |
| `python -m foresight forecast` | 90-day ETS + pooled forecast per SKU × state |
| `python -m foresight balance` | Balancing engine → ranked recommendations |
| `python -m foresight run-all <file>...` | All of the above in order |
| `python -m foresight purge <cycle>` | Remove a (partially) ingested cycle, e.g. `2026-03` |
| `python -m foresight serve [--port N]` | FastAPI dashboard |

## How it works

- **Demand** comes from the Forward Orders sheet (and, when available, the daily
  sales export), aggregated per SKU × supply state × day into `sales_daily`.
- **Stock (Mode B)** — the monthly *Daily Ageing* sheet is a true daily count of
  units in Blinkit storage (sum across ageing slabs), so past days are actual
  counts (`source='ageing'`); after the last counted day the ledger
  (sales − returns − recalls + GRN receipts from the *Upfront Storage Charges*
  sheet) rolls the balance forward (`source='reconstructed'`). The next monthly
  zip snaps balances back to true counts. Mode A (live panel report) is a
  drop-in: insert `source='panel'` snapshots, nothing else changes.
- **Forecast** — dense cells (≥21 days, ≥90 units) get a direct Holt-Winters ETS
  with weekly seasonality; sparse cells are forecast at SKU-national level and
  allocated by 28-day share, flagged `pooled` in every view. 80% bands widen to
  ~2× by day 90. A 14-day holdout backtest MAPE is shown on the dashboard.
- **Balancing** — walks the 90-day forecast against projected stock per cell:
  below `reorder_point` (7-day cover + 95% safety stock) → stockout alert with
  the projected stockout date; above 45 days cover → overstock alert with
  projected storage ₹ vs recall ₹ (recommends recall only when it nets positive).
  Alerts rank by ₹ at stake. Tunables live in `foresight/config.py`.

## Layout

```
foresight/
  config.py          tunable defaults (service level, cover band, thresholds)
  db.py              schema + sqlite3 connection (libSQL/Turso-compatible SQL)
  ingest/blinkit.py  monthly zip adapter (13 files) + daily sales adapter
  stock.py           Mode B snapshots + ledger reconstruction
  forecast.py        ETS + pooling + backtest
  balance.py         balancing math + recommendations
  api.py             FastAPI JSON API + upload pipeline
  static/index.html  dashboard (self-contained, no CDN)
foresight.db         SQLite database (created on first ingest)
```

Multi-platform: every fact table carries `platform_id`; a Flipkart/Amazon adapter
just maps its files into the same canonical writes (`foresight/ingest/`).

Blinkit has shipped at least two zip layouts (March 2026 vs April/May 2026 file
names); the adapter locates data by **sheet name**, so both ingest identically.

## Verified against real data (Mar + Apr + May 2026 zips)

- 82k forward-order rows → 93 continuous days of demand across ~30 SKUs × ~30 states.
- May storage in Payout Breakup: ₹16,32,661 + ₹2,93,879 GST — reconciles with
  ingested detail (₹14.39L daily ageing + ₹1.93L upfront inwarding).
- Forecast June ≈ 28.1k / July ≈ 29.4k / Aug ≈ 27.8k units (spec's validation run
  landed 26.7k/27.8k/26.0k on slightly less data).
- Run of 2026-05-31: 52 stockout alerts (₹9.0L margin at risk), 137 overstock
  alerts (₹5.9L net storage saving available).

## Known limits (Phase 1)

- `unit_margin_estimate` is avg item-level payout per unit (COGS unknown), so
  stockout ₹ is revenue-flavoured, not true margin.
- Storage projection uses the cell's blended per-day rate stepping toward the next
  slab — an estimate, labeled as such.
- Under 4 payout cycles of history the dashboard flags all forecasts low-confidence;
  festive-season regressors (Oct–Nov surge) need more history.
- Daily sales adapter matches columns by alias; lock the exact schema once a real
  panel export sample is available (spec §10.2).

## Deploy — Turso (DB) + Vercel (dashboard)

The dashboard deploys to Vercel as a read-only FastAPI function reading a
hosted Turso (libSQL) database. Ingestion and forecasting stay local (they need
pandas/statsmodels and minutes of CPU — wrong shape for serverless); after each
monthly ingest you push the refreshed sqlite file to Turso.

One-time setup:

```powershell
# Turso CLI (PowerShell installer; or `scoop install turso` / WSL)
irm get.tur.so/install.ps1 | iex
turso auth signup                       # or: turso auth login

# create the hosted DB straight from the local file
turso db create foresight --from-file foresight.db
turso db show foresight --url           # -> libsql://foresight-<org>.turso.io
turso db tokens create foresight        # -> auth token

# Vercel CLI
npm i -g vercel
vercel login
vercel link                             # from the repo root
vercel env add FORESIGHT_TURSO_URL      # paste the libsql:// URL
vercel env add FORESIGHT_TURSO_TOKEN    # paste the token
vercel env add FORESIGHT_READONLY      # value: 1
vercel deploy --prod
```

Local `.env` (git-ignored, repo root) — same values as the Vercel env, used by
the post-ingest auto-sync:

```
FORESIGHT_TURSO_URL=libsql://foresight-<org>.turso.io
FORESIGHT_TURSO_TOKEN=<token>
```

## Daily / monthly data flow

Blinkit has no public seller API, so exports are downloaded from the seller
panel by hand; everything after that is automatic:

1. Download the daily sales export (or the monthly payout zip).
2. Double-click `Foresight.bat` — starts the local dashboard and opens the
   browser (or `.venv\Scripts\python -m foresight serve --open`).
3. Upload tab → pick the file → **Ingest**. The pipeline runs locally
   (ingest → stock reconstruction → 90-day forecast → balancing → prune),
   then syncs the results to Turso. The banner ends with
   "rows synced to the remote dashboard" — at that point the Vercel dashboard
   is fresh. Takes ~5–10 min; the forecast retrain dominates.
   Re-uploading the same file is safe (idempotent).

If the sync fails (offline, bad token), the banner says so and the local
dashboard stays current; retry with:

```powershell
.venv\Scripts\python -m foresight sync-turso        # incremental, convergent
.venv\Scripts\python -m foresight sync-turso --full # repair/rebuild the remote
```

The CLI path does the same as the upload tab:
`python -m foresight run-all <file>` ingests, prunes, and auto-syncs when
`.env` has the Turso URL.

Layout notes: `api/index.py` is the Vercel entrypoint (exposes the FastAPI
`app`); the **root `requirements.txt` is the serverless dep set** (fastapi,
python-multipart, libsql) because Vercel's builder only installs from the
root file — the full local stack lives in `requirements-local.txt`
(.vercelignore'd). `foresight/db.py` serves from Turso when deployed
(`VERCEL` env or `FORESIGHT_READONLY=1`) and from the sqlite file locally;
`foresight/sync.py` holds the incremental sync. `/api/upload` returns 501 on
the deployment — uploads are local-only by design.
