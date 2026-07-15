# Foresight — notes for Claude

Inventory balancing + demand forecasting platform for Blinkit (Royal Mart seller).
Python 3.14 venv at `.venv` (spec says 3.12; 3.14 is what's installed and works).
Run things with `.venv\Scripts\python -m foresight <cmd>` — see README for commands.

## Ground truths that are easy to get wrong

- **Two location grains.** Demand/sales/forecasts are **Customer City** grain
  (city ≈ dark-store city in q-commerce; `locations.city` NOT NULL). Counted
  stock (Daily Ageing, GRN) is **supply state** grain (`locations.city` IS NULL).
  The balancing run splits each state pool across its cities by 28-day demand
  share → `stock_snapshots(source='allocated')`; state stock no city claims
  surfaces as state-wide dead-stock alerts (city NULL in recommendations).
  Never sum 'allocated' rows together with counted rows — same units twice.
- **Blinkit renamed the zip's files across export versions** (Mar 2026:
  `Forward Orders.xlsx` / `Return Cancelled.xlsx` / `Ageing Inventory.xlsx` /
  `Upfront Orders.xlsx`; Apr+May: `Forward & Return Cancelled Orders.xlsx` /
  `Storage Charges.xlsx`). Sheet names are stable — discovery is **by sheet name**
  (`_sheet_map` in `ingest/blinkit.py`), never by filename. A zip without a
  'Forward Orders' sheet is rejected before any write.
- **Daily Ageing is a daily stock count**, one row per state × item × ageing slab,
  date columns wide. Sum slabs per date = on-hand. BUT a sheet's date columns
  **before its own cycle period are residual junk** (April's Mar-15 column sums to
  ~144 units vs the March zip's 13,748) — `stock.build_snapshots_from_ageing`
  only trusts columns within [period_start − 1 day, period_end], in-period owner
  outranking a boundary claim.
- **Upfront Storage Charges sheet = GRN receipts** (date, warehouse, state, qty) —
  used as ledger receipts and to build the warehouse→state map for recalls.
- Blinkit xlsx headers are NOT on row 1 — find them by anchor column name
  (`_sheet_rows` in `ingest/blinkit.py`). Dates come as strings like "1 May 2026".
- State names may carry a "(Newly Added)" suffix — normalize with `_norm_state`.
- Monthly zips may overlap dates (May zip includes late-April orders/ageing).
  Sales upsert **adds** across cycles; re-ingesting the same cycle is skipped via
  `payout_cycles`. `python -m foresight purge <label>` removes a cycle's
  cycle-tagged rows — safe for partial ingests; a fully ingested cycle needs a
  fresh `run-all` rebuild (sales/ledger rows aren't cycle-tagged).
- `unit_margin_estimate` = avg Item Level Payout ÷ units from Forward Orders
  (payout proxy, not true margin — COGS unknown).

## Pipeline order matters

ingest → `stock.build_snapshots_from_ageing` → `stock.reconstruct_forward`
→ `forecast.run_forecast` → `balance.run_balancing`. The API upload endpoint
(`api.py /api/upload`) runs the same chain; keep them in sync.

## Dashboard

`foresight/static/index.html` — single file, no CDN, hand-rolled SVG charts,
light+dark via CSS custom properties. Heatmap ramps were validated with the
dataviz skill's palette validator (per-arm ordinal); if you change chart colors,
re-run the validator. Tabs deep-link via `#hash`.

## Deployment

Vercel (read-only dashboard) + Turso (hosted libSQL) — see README "Deploy".
`api/index.py` is the Vercel entrypoint; `api/requirements.txt` is the slim
function dep set (root requirements.txt is .vercelignore'd — pandas/statsmodels
must never ship to Vercel). `db.connect()` returns a `_TursoConnection` adapter
when `FORESIGHT_TURSO_URL` is set (rows wrapped in-house, no driver row_factory
dependence). Ingest/forecast always run locally; push the sqlite file with
`turso db create foresight --from-file foresight.db`. `FORESIGHT_READONLY=1`
disables `/api/upload` (501). The `libsql` pip package has no cp314 Windows
wheel — it installs on Vercel (linux/cp312) but not in the local venv; local
code paths never import it.

## Testing without polluting real data

Copy `foresight.db` into the scratchpad and point `FORESIGHT_DB` env var at the
copy (see `config.py`). The synthetic daily-sales test in the July 2026 build did
exactly this.
