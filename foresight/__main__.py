"""CLI: python -m foresight <command>

Commands:
  ingest <zip-or-file> [...]   Ingest monthly payout zip(s) or a daily sales export
  reconstruct                  Build stock snapshots (ageing) + roll ledger forward
  forecast                     Run the 90-day demand forecast
  balance                      Run the balancing engine → recommendations
  run-all <zip> [...]          ingest → reconstruct → forecast → balance
  purge <cycle-label>          Remove a (partially) ingested cycle, e.g. 2026-03
  serve [--port 8000]          Start the dashboard API
"""
import json
import sys
from pathlib import Path

from foresight import db


def _pipeline(conn, do_ingest=(), do_stock=True, do_forecast=True, do_balance=True):
    from foresight.ingest import blinkit
    from foresight import stock, forecast, balance

    for src in do_ingest:
        p = Path(src)
        if p.suffix.lower() == ".zip":
            print(json.dumps(blinkit.ingest_monthly_zip(conn, p), default=str))
        else:
            print(json.dumps(blinkit.ingest_daily_sales(conn, p), default=str))
    if do_stock:
        print(json.dumps({"ageing_snapshots": stock.build_snapshots_from_ageing(conn)}
                         | stock.reconstruct_forward(conn)))
    if do_forecast:
        print(json.dumps(forecast.run_forecast(conn)))
        bt = forecast.backtest_mape(conn)
        if bt:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value, updated_at) "
                "VALUES ('backtest', ?, datetime('now'))", (json.dumps(bt),))
            conn.commit()
            print(json.dumps({"backtest": bt}))
    if do_balance:
        print(json.dumps(balance.run_balancing(conn)))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *args = argv
    conn = db.connect()
    db.init_db(conn)
    try:
        if cmd == "ingest":
            _pipeline(conn, do_ingest=args, do_stock=False, do_forecast=False, do_balance=False)
        elif cmd == "reconstruct":
            _pipeline(conn, do_forecast=False, do_balance=False)
        elif cmd == "forecast":
            _pipeline(conn, do_stock=False, do_balance=False)
        elif cmd == "balance":
            _pipeline(conn, do_stock=False, do_forecast=False)
        elif cmd == "run-all":
            _pipeline(conn, do_ingest=args)
        elif cmd == "purge":
            if not args:
                print("usage: purge <cycle-label>")
                return 1
            print(json.dumps(db.purge_cycle(conn, args[0])))
        elif cmd == "serve":
            port = int(args[args.index("--port") + 1]) if "--port" in args else 8000
            import uvicorn
            from foresight.api import app
            uvicorn.run(app, host="127.0.0.1", port=port)
        else:
            print(__doc__)
            return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
