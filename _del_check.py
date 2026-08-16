# -*- coding: utf-8 -*-
"""查看 AI 端 FAMAS 卖出记录与 portfolio 状态(删除前)"""
import sqlite3
import json

conn = sqlite3.connect("assets/cs2.db")
conn.row_factory = sqlite3.Row

print("=== cs2_ai_trade 全部记录 ===")
for r in conn.execute("SELECT * FROM cs2_ai_trade ORDER BY created_at"):
    print("id:", r["id"])
    for k in r.keys():
        print("   ", k, "=", r[k])

print()
print("=== AI 端最新 portfolio ===")
row = conn.execute(
    """
    SELECT p.id, c.exp_name, p.cashflow, p.total_assets, p.positions, p.trading_date, p.updated_at
    FROM cs2_portfolio p JOIN cs2_config c ON c.id = p.config_id
    WHERE c.exp_name = 'ai-account'
    ORDER BY p.updated_at DESC LIMIT 1
    """
).fetchone()
if row:
    print("id:", row["id"])
    print("cashflow:", row["cashflow"])
    print("total_assets:", row["total_assets"])
    print("trading_date:", row["trading_date"])
    print("updated_at:", row["updated_at"])
    print("positions:", row["positions"])

print()
print("=== FAMAS 相关决策(cs2_decision) ===")
for r in conn.execute(
    "SELECT id, item_name, action, quantity, price, portfolio_id, updated_at "
    "FROM cs2_decision WHERE item_name LIKE '%FAMAS%' ORDER BY updated_at DESC LIMIT 10"
):
    print("  id={} | {} | {}x{} | pf={} | {}".format(
        r["id"], r["action"], r["quantity"], r["price"], r["portfolio_id"][:8], r["updated_at"]))
conn.close()
