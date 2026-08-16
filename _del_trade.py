# -*- coding: utf-8 -*-
"""再次恢复 AI 端 portfolio 为持有 FAMAS 2股 @445,并确认交易记录为空"""
import sqlite3
import json

conn = sqlite3.connect("assets/cs2.db")
conn.row_factory = sqlite3.Row

# 确认交易记录
n = conn.execute("SELECT COUNT(*) AS c FROM cs2_ai_trade").fetchone()["c"]
print("cs2_ai_trade 记录数:", n)

# 恢复最新 portfolio 为持有 FAMAS 2股
positions = {
    "FAMAS | ZX Spectron (Factory New)": {
        "value": 890.0,
        "shares": 2,
        "avg_cost": 445.0,
    }
}
conn.execute(
    "UPDATE cs2_portfolio SET cashflow = ?, total_assets = ?, positions = ? "
    "WHERE id = ?",
    (0.0, 890.0, json.dumps(positions, ensure_ascii=False),
     "181c5f8b-65a9-40cb-9938-8cecd57593dc"),
)
conn.commit()

row = conn.execute(
    "SELECT cashflow, total_assets, positions, updated_at FROM cs2_portfolio WHERE id = ?",
    ("181c5f8b-65a9-40cb-9938-8cecd57593dc",),
).fetchone()
print("portfolio: cash={} assets={} updated={}".format(row["cashflow"], row["total_assets"], row["updated_at"]))
print("positions:", row["positions"])
conn.close()
