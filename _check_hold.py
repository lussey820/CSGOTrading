# -*- coding: utf-8 -*-
"""查 AI 端最新 portfolio(可能被自动运行覆盖)"""
import sqlite3
import json

conn = sqlite3.connect("assets/cs2.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT p.id, p.cashflow, p.total_assets, p.positions, p.trading_date, p.updated_at
    FROM cs2_portfolio p JOIN cs2_config c ON c.id = p.config_id
    WHERE c.exp_name = 'ai-account'
    ORDER BY p.updated_at DESC LIMIT 5
    """
).fetchall()
for r in rows:
    print("pf={} | cash={} | assets={} | updated={}".format(
        r["id"][:8], r["cashflow"], r["total_assets"], r["updated_at"]))
    print("  positions:", r["positions"])
conn.close()
