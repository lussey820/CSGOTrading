"""Daily advisor entry - run analysis at 20:00 and push advice to WeChat.

Usage:
  python daily.py --now       # Run analysis immediately and push
  python daily.py --daemon     # Run as scheduler daemon (20:00 daily)
"""

import sys
import os
import argparse
import time
import yaml
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from run import run_single_experiment
from notify import generate_report, send_wechat
from util.cs2_db_helper import cs2_db_initialize, get_cs2_db
from util.logger import logger

load_dotenv()

PROJECT_ROOT = Path(__file__).parent
LIVE_CONFIG = PROJECT_ROOT / "config" / "live.yaml"
WATCHLIST = PROJECT_ROOT / "config" / "watchlist.yaml"


def load_watchlist_tickers():
    if not WATCHLIST.exists():
        return []
    with open(WATCHLIST, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tickers", [])


def is_already_run_today(exp_name):
    """Check if today's analysis already exists in DB."""
    cs2_db_initialize(use_local_db=True)
    db = get_cs2_db()
    config_id = db.get_config_id_by_name(exp_name)
    if not config_id:
        return False
    latest = db.get_latest_trading_date(config_id)
    if latest and latest.date() == datetime.now().date():
        return True
    return False


def run_today():
    """Run analysis for today and push advice."""
    today = datetime.now().strftime("%Y-%m-%d")
    tickers = load_watchlist_tickers()
    if not tickers:
        logger.warning("Watchlist is empty, nothing to analyze")
        print('Watchlist is empty. Add items first: python watchlist.py add "Item Name"')
        return False

    # Load live config and inject watchlist tickers
    with open(LIVE_CONFIG, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["tickers"] = tickers
    exp_name = cfg["exp_name"]

    # Skip analysis if already done today
    if is_already_run_today(exp_name):
        logger.info(f"Today's analysis already done, pushing report only")
    else:
        # Write merged config to temp file for run_single_experiment
        temp_path = PROJECT_ROOT / "config" / "_live_today.yaml"
        with open(temp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        try:
            logger.info(f"Running daily analysis for {today} with {len(tickers)} items")
            run_single_experiment("config/_live_today.yaml", today, use_local_db=True)
        finally:
            temp_path.unlink(missing_ok=True)

    # Generate and push report
    report = generate_report(exp_name, today)
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60 + "\n")
    send_wechat(f"CS2交易建议 {today}", report)
    logger.info("Daily advisor finished")
    return True


def run_daemon():
    """Run as scheduler daemon - trigger at 20:00 every day using stdlib."""
    print("调度已启动，每天 20:00 自动运行分析并推送建议。Ctrl+C 退出。")
    last_run_date = None
    while True:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        # Trigger when hour >= 20 and not yet run today
        if now.hour >= 20 and last_run_date != today_str:
            try:
                run_today()
            except Exception as e:
                logger.error(f"Daemon run error: {e}")
            last_run_date = today_str
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Daily CS2 trading advisor")
    parser.add_argument("--now", action="store_true", help="Run analysis immediately and push")
    parser.add_argument("--daemon", action="store_true", help="Run as scheduler daemon (20:00 daily)")
    args = parser.parse_args()

    if args.now:
        run_today()
    elif args.daemon:
        run_daemon()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
