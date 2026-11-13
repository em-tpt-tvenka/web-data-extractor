"""
Automated Scheduler for Samsung Laptop Scraping Pipeline.
Runs the scraping pipeline on an automated recurring schedule (e.g. daily or every N hours).

Usage:
    # Run once immediately, then repeat every 24 hours:
    python scheduler.py --interval-hours 24

    # Run every day at 02:00 AM:
    python scheduler.py --daily 02:00

    # Run with Playwright engine every 12 hours:
    python scheduler.py --interval-hours 12 --playwright
"""

import sys
import time
import argparse
import logging
from datetime import datetime

from main import run_pipeline

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("scheduler")


def run_job(use_playwright: bool = False, limit: int = None):
    """Executes a single scheduled scraping run with robust error handling."""
    start_time = datetime.now()
    logger.info("=" * 70)
    logger.info(f"🚀 [SCHEDULED RUN STARTED] at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    try:
        run_pipeline(
            limit=limit,
            past_year_only=True,
            use_playwright=use_playwright,
            save_to_mongo=True
        )
        duration = (datetime.now() - start_time).total_seconds()
        logger.info(f"✓ [SCHEDULED RUN SUCCESS] Completed in {duration:.1f} seconds.")
    except Exception as e:
        logger.error(f"❌ [SCHEDULED RUN ERROR] Pipeline execution encountered an issue: {e}", exc_info=True)


def start_interval_schedule(interval_hours: float, use_playwright: bool = False, limit: int = None):
    """Runs immediately, then sleeps for the specified hours in a loop."""
    logger.info(f"⏰ Scheduler active: recurring every {interval_hours} hour(s). Press Ctrl+C to stop.")
    while True:
        run_job(use_playwright=use_playwright, limit=limit)
        next_run_epoch = time.time() + (interval_hours * 3600)
        next_run_str = datetime.fromtimestamp(next_run_epoch).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"⏳ Sleeping... Next automated run at: {next_run_str}")
        try:
            time.sleep(interval_hours * 3600)
        except KeyboardInterrupt:
            logger.info("🛑 Scheduler stopped by user.")
            break


def start_daily_schedule(daily_time_str: str, use_playwright: bool = False, limit: int = None):
    """Runs every day at the given time (HH:MM format, 24-hour clock)."""
    try:
        target_hour, target_minute = map(int, daily_time_str.split(":"))
    except ValueError:
        logger.error("Invalid time format. Please use HH:MM (e.g. 02:00 or 14:30)")
        sys.exit(1)

    logger.info(f"⏰ Scheduler active: runs daily at {daily_time_str}. Press Ctrl+C to stop.")

    while True:
        now = datetime.now()
        target_today = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        if target_today <= now:
            # Tomorrow
            next_target = target_today.replace(day=target_today.day + 1)
        else:
            next_target = target_today

        sleep_seconds = (next_target - now).total_seconds()
        logger.info(f"⏳ Waiting for next daily run at: {next_target.strftime('%Y-%m-%d %H:%M:%S')} (in {sleep_seconds/3600:.1f} hrs)")

        try:
            time.sleep(sleep_seconds)
            run_job(use_playwright=use_playwright, limit=limit)
        except KeyboardInterrupt:
            logger.info("🛑 Scheduler stopped by user.")
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated Samsung Laptop Scraper Scheduler")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--interval-hours", type=float, help="Repeat scraping every N hours (e.g. --interval-hours 24)")
    group.add_argument("--daily", type=str, help="Run once daily at specific 24h time HH:MM (e.g. --daily 02:00)")
    parser.add_argument("--playwright", action="store_true", help="Use Playwright Chromium engine instead of HTTP")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit on products per run")

    args = parser.parse_args()

    if args.interval_hours:
        start_interval_schedule(args.interval_hours, use_playwright=args.playwright, limit=args.limit)
    elif args.daily:
        start_daily_schedule(args.daily, use_playwright=args.playwright, limit=args.limit)

