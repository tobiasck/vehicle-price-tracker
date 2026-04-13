#!/usr/bin/env python3
import argparse
import asyncio
import logging
import sys

from config.logging_config import setup_logging
from db.connection import get_connection
from db.models import get_active_search_configs
from scrapers.mobile_de import MobileDeScraper
from scrapers.autoscout24 import AutoScout24Scraper
from scrapers.kleinanzeigen import KleinanzeigenScraper

logger = logging.getLogger(__name__)

SCRAPER_MAP = {
    "mobile_de": MobileDeScraper,
    "autoscout24": AutoScout24Scraper,
    "kleinanzeigen": KleinanzeigenScraper,
}


async def run_scraper(search_config, conn, dry_run=False):
    platform = search_config["platform"]
    scraper_cls = SCRAPER_MAP.get(platform)

    if not scraper_cls:
        logger.error("No scraper for platform '%s'", platform)
        return False

    scraper = scraper_cls(search_config, conn)
    try:
        count = await scraper.run(dry_run=dry_run)
        logger.info(
            "Finished %s/%s: %d listings",
            search_config["vehicle_name"], platform, count,
        )
        return True
    except Exception as e:
        logger.error(
            "Failed %s/%s: %s",
            search_config["vehicle_name"], platform, e,
        )
        return False


async def main():
    parser = argparse.ArgumentParser(description="Vehicle Market Price Scraper")
    parser.add_argument("--dry-run", action="store_true", help="Parse and log but don't write to DB")
    parser.add_argument("--target", type=str, help="Only run scraper for this platform (mobile_de, autoscout24, kleinanzeigen)")
    args = parser.parse_args()

    setup_logging()
    logger.info("Starting vehicle scraper (dry_run=%s, target=%s)", args.dry_run, args.target)

    conn = get_connection()
    try:
        configs = get_active_search_configs(conn)
        if args.target:
            configs = [c for c in configs if c["platform"] == args.target]

        if not configs:
            logger.warning("No active search configs found")
            return 0

        logger.info("Found %d active search config(s)", len(configs))

        successes = 0
        for config in configs:
            logger.info("--- Running: %s on %s ---", config["vehicle_name"], config["platform"])
            ok = await run_scraper(config, conn, dry_run=args.dry_run)
            if ok:
                successes += 1

        logger.info("Done: %d/%d scrapers succeeded", successes, len(configs))
        return 0 if successes > 0 else 1

    finally:
        conn.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
