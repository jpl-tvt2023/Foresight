"""Ingestion adapters. Each platform adapter maps source files to canonical records.

Adapter contract: parse(source) -> writes canonical rows (items, locations, sales_daily,
storage_ageing, stock_ledger, replenishments, charges, payout_*) for its platform_id.
"""
