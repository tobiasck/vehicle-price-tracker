import asyncio
import logging
import os
import random
from abc import ABC, abstractmethod

from patchright.async_api import async_playwright

from config.settings import BLOCK_RETRY_WAIT_MIN, BLOCK_RETRY_WAIT_MAX, MAX_RETRIES
from db.models import (
    create_scrape_run, finish_scrape_run,
    upsert_listing, insert_snapshot, update_run_statistics,
)
from utils.anti_detect import (
    get_browser_context_args, random_page_delay, human_scroll,
)

logger = logging.getLogger(__name__)

DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug")


class BaseScraper(ABC):
    def __init__(self, search_config, conn):
        self.config = search_config
        self.conn = conn
        self.platform = search_config["platform"]
        self.search_url = search_config["search_url"]
        self.config_id = search_config["id"]
        self.vehicle_name = search_config["vehicle_name"]

    async def run(self, dry_run=False, debug=False):
        logger.info("Starting scrape: %s on %s", self.vehicle_name, self.platform)
        self.debug = debug

        run_id = None
        if not dry_run:
            run_id = create_scrape_run(self.conn, self.config_id)

        total_listings = 0
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(**get_browser_context_args())
            page = await context.new_page()

            try:
                await self._navigate_with_retry(page, self.search_url)
                await self.dismiss_consent(page)
                await random_page_delay()

                page_num = 1
                while True:
                    logger.info("Parsing page %d", page_num)
                    await human_scroll(page)

                    if self.debug:
                        await self._save_debug(page, f"page_{page_num}")

                    listings = await self.parse_listing_cards(page)
                    logger.info("Found %d listings on page %d", len(listings), page_num)

                    for listing_data in listings:
                        if dry_run:
                            logger.info("[DRY RUN] %s", listing_data)
                        else:
                            self._save_listing(run_id, listing_data)

                    total_listings += len(listings)

                    has_next = await self.get_next_page(page)
                    if not has_next:
                        break

                    await random_page_delay()
                    page_num += 1

                if not dry_run:
                    finish_scrape_run(self.conn, run_id, "success", total_listings)
                    update_run_statistics(self.conn, run_id)

                logger.info(
                    "Scrape complete: %s — %d listings total",
                    self.vehicle_name, total_listings,
                )

            except Exception as e:
                logger.error("Scrape failed for %s: %s", self.vehicle_name, e, exc_info=True)
                if not dry_run and run_id:
                    finish_scrape_run(self.conn, run_id, "failed", total_listings, str(e))
                raise

            finally:
                await context.close()
                await browser.close()

        return total_listings

    async def _navigate_with_retry(self, page, url):
        for attempt in range(MAX_RETRIES + 1):
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            if await self._is_blocked(page):
                await self._save_debug(page, f"blocked_attempt_{attempt + 1}")
                if attempt < MAX_RETRIES:
                    wait = random.uniform(BLOCK_RETRY_WAIT_MIN, BLOCK_RETRY_WAIT_MAX)
                    logger.warning("Blocked on attempt %d, waiting %.0fs", attempt + 1, wait)
                    await asyncio.sleep(wait)
                else:
                    raise RuntimeError(f"Blocked by anti-bot protection after {MAX_RETRIES + 1} attempts")
            else:
                return

    async def _save_debug(self, page, label):
        os.makedirs(DEBUG_DIR, exist_ok=True)
        prefix = f"{self.platform}_{label}"
        try:
            await page.screenshot(path=os.path.join(DEBUG_DIR, f"{prefix}.png"), full_page=True)
            html = await page.content()
            with open(os.path.join(DEBUG_DIR, f"{prefix}.html"), "w") as f:
                f.write(html)
            logger.info("Debug saved: %s", prefix)
        except Exception as e:
            logger.warning("Failed to save debug for %s: %s", prefix, e)

    async def _is_blocked(self, page):
        content = await page.content()
        content_lower = content.lower()
        # Check for Cloudflare challenge specifically
        if "cf-challenge" in content_lower or "cf-turnstile" in content_lower:
            return True
        # Check for explicit block pages (but not normal page content)
        title = await page.title()
        title_lower = title.lower()
        if any(w in title_lower for w in ["access denied", "blocked", "captcha", "just a moment"]):
            return True
        # Check for very short pages that are likely block pages
        if len(content) < 1000 and ("403" in content or "rate limit" in content_lower):
            return True
        return False

    def _save_listing(self, run_id, data):
        listing_id = upsert_listing(
            self.conn,
            self.config_id,
            data["platform_id"],
            data["listing_url"],
        )
        insert_snapshot(self.conn, listing_id, run_id, data)

    @abstractmethod
    async def parse_listing_cards(self, page) -> list[dict]:
        pass

    @abstractmethod
    async def get_next_page(self, page) -> bool:
        pass

    @abstractmethod
    async def dismiss_consent(self, page):
        pass
