import asyncio
import logging
import os
import random
import re

import nodriver

from config.settings import (
    MIN_PAGE_DELAY, MAX_PAGE_DELAY,
    BLOCK_RETRY_WAIT_MIN, BLOCK_RETRY_WAIT_MAX, MAX_RETRIES,
    USER_AGENTS,
)
from db.models import (
    create_scrape_run, finish_scrape_run,
    upsert_listing, insert_snapshot, update_run_statistics,
)

logger = logging.getLogger(__name__)

DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug")


class MobileDeScraper:
    """mobile.de scraper using nodriver for better anti-detection."""

    def __init__(self, search_config, conn):
        self.config = search_config
        self.conn = conn
        self.platform = search_config["platform"]
        self.search_url = search_config["search_url"]
        self.config_id = search_config["id"]
        self.vehicle_name = search_config["vehicle_name"]
        self.debug = False

    async def run(self, dry_run=False, debug=False):
        logger.info("Starting scrape: %s on %s (nodriver)", self.vehicle_name, self.platform)
        self.debug = debug

        run_id = None
        if not dry_run:
            run_id = create_scrape_run(self.conn, self.config_id)

        total_listings = 0
        browser = None
        try:
            browser = await nodriver.start(
                headless=True,
                lang="de-DE",
                browser_args=[
                    f"--user-agent={random.choice(USER_AGENTS)}",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            # Warm up: visit homepage first
            logger.info("Warming up session via mobile.de homepage")
            page = await browser.get("https://www.mobile.de")
            await asyncio.sleep(random.uniform(3, 5))

            # Dismiss cookie consent
            await self._dismiss_consent(page)
            await asyncio.sleep(random.uniform(2, 4))

            # Navigate to search
            logger.info("Navigating to search URL")
            page = await browser.get(self.search_url)
            await asyncio.sleep(random.uniform(3, 5))

            # Check if blocked
            if await self._is_blocked(page):
                if self.debug:
                    await self._save_debug(page, "blocked_after_warmup")

                # Retry once with longer wait
                wait = random.uniform(BLOCK_RETRY_WAIT_MIN, BLOCK_RETRY_WAIT_MAX)
                logger.warning("Blocked after warmup, waiting %.0fs", wait)
                await asyncio.sleep(wait)

                page = await browser.get(self.search_url)
                await asyncio.sleep(random.uniform(3, 5))

                if await self._is_blocked(page):
                    if self.debug:
                        await self._save_debug(page, "blocked_final")
                    raise RuntimeError("Blocked by mobile.de after retry")

            # Dismiss consent again if it appeared on search page
            await self._dismiss_consent(page)

            page_num = 1
            while True:
                logger.info("Parsing page %d", page_num)
                await self._human_scroll(page)

                if self.debug:
                    await self._save_debug(page, f"page_{page_num}")

                listings = await self._parse_listing_cards(page)
                logger.info("Found %d listings on page %d", len(listings), page_num)

                for listing_data in listings:
                    if dry_run:
                        logger.info("[DRY RUN] %s", listing_data)
                    else:
                        self._save_listing(run_id, listing_data)

                total_listings += len(listings)

                has_next = await self._get_next_page(page, browser, page_num)
                if not has_next:
                    break

                page = browser.main_tab
                await asyncio.sleep(random.uniform(MIN_PAGE_DELAY, MAX_PAGE_DELAY))
                page_num += 1

            if not dry_run:
                finish_scrape_run(self.conn, run_id, "success", total_listings)
                update_run_statistics(self.conn, run_id)

            logger.info("Scrape complete: %s — %d listings total", self.vehicle_name, total_listings)

        except Exception as e:
            logger.error("Scrape failed for %s: %s", self.vehicle_name, e, exc_info=True)
            if not dry_run and run_id:
                finish_scrape_run(self.conn, run_id, "failed", total_listings, str(e))
            raise

        finally:
            if browser:
                browser.stop()

        return total_listings

    async def _dismiss_consent(self, page):
        try:
            # Try to find and click cookie consent buttons
            for text in ["Einverstanden", "Alle akzeptieren", "Accept All"]:
                btn = await page.find(text, best_match=True, timeout=3)
                if btn:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await btn.click()
                    logger.info("Dismissed cookie consent: '%s'", text)
                    await asyncio.sleep(1)
                    return
        except Exception as e:
            logger.debug("No consent banner or failed to dismiss: %s", e)

    async def _is_blocked(self, page):
        content = await page.get_content()
        content_lower = content.lower()

        if "zugriff verweigert" in content_lower or "access denied" in content_lower:
            return True
        if "cf-challenge" in content_lower or "cf-turnstile" in content_lower:
            return True

        # Check page title
        try:
            title_el = await page.query_selector("title")
            if title_el:
                title = await title_el.get_js_attribute("textContent") or ""
                if any(w in title.lower() for w in ["zugriff verweigert", "access denied", "captcha", "just a moment"]):
                    return True
        except Exception:
            pass

        return False

    async def _human_scroll(self, page):
        for _ in range(random.randint(2, 4)):
            scroll_amount = random.randint(200, 500)
            await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            await asyncio.sleep(random.uniform(0.3, 0.8))

    async def _save_debug(self, page, label):
        os.makedirs(DEBUG_DIR, exist_ok=True)
        prefix = f"{self.platform}_{label}"
        try:
            await page.save_screenshot(os.path.join(DEBUG_DIR, f"{prefix}.png"))
            content = await page.get_content()
            with open(os.path.join(DEBUG_DIR, f"{prefix}.html"), "w") as f:
                f.write(content)
            logger.info("Debug saved: %s", prefix)
        except Exception as e:
            logger.warning("Failed to save debug for %s: %s", prefix, e)

    async def _parse_listing_cards(self, page) -> list[dict]:
        listings = []

        # Try multiple selectors for listing cards
        elements = []
        for selector in [
            "a.link--muted.no--text--decoration",
            "[data-testid='result-listing']",
            "div.cBox-body--resultitem",
            "article[data-listing-id]",
            "a[href*='/fahrzeuge/details/']",
        ]:
            try:
                found = await page.query_selector_all(selector)
                if found:
                    elements = found
                    logger.debug("Using selector '%s' — found %d items", selector, len(found))
                    break
            except Exception:
                continue

        for i, element in enumerate(elements):
            try:
                listing = await self._parse_single_card(element)
                if listing and listing.get("platform_id"):
                    if not any(l["platform_id"] == listing["platform_id"] for l in listings):
                        listings.append(listing)
            except Exception as e:
                logger.warning("Failed to parse listing card %d: %s", i, e)
                continue

        return listings

    async def _parse_single_card(self, element) -> dict | None:
        data = {}

        # Extract href
        href = await element.get_js_attribute("href")
        if not href:
            # Try to find a link inside
            link = await element.query_selector("a[href*='/fahrzeuge/details/']")
            if link:
                href = await link.get_js_attribute("href")

        if href:
            data["listing_url"] = href if href.startswith("http") else f"https://suchen.mobile.de{href}"
            match = re.search(r"/details/(\d+)", href)
            if match:
                data["platform_id"] = match.group(1)
            else:
                match = re.search(r"/(\d{6,})", href)
                if match:
                    data["platform_id"] = match.group(1)

        # Try data-listing-id
        if "platform_id" not in data:
            lid = await element.get_js_attribute("data-listing-id")
            if lid:
                data["platform_id"] = str(lid)

        if "platform_id" not in data:
            return None

        if "listing_url" not in data:
            data["listing_url"] = ""

        # Get text content
        text = await element.get_js_attribute("textContent") or ""

        data["title"] = self._extract_title(text)
        data["price_cents"] = self._extract_price(text)
        data["mileage_km"] = self._extract_mileage(text)
        data["year"] = self._extract_year(text)
        data["location"] = self._extract_location(text)
        data["seller_type"] = self._extract_seller_type(text)

        return data

    def _save_listing(self, run_id, data):
        listing_id = upsert_listing(
            self.conn, self.config_id, data["platform_id"], data["listing_url"],
        )
        insert_snapshot(self.conn, listing_id, run_id, data)

    def _extract_title(self, text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return lines[0][:200] if lines else None

    def _extract_price(self, text):
        patterns = [
            r"(\d{1,3}(?:\.\d{3})+)\s*€",
            r"€\s*(\d{1,3}(?:\.\d{3})+)",
            r"EUR\s*(\d{1,3}(?:\.\d{3})+)",
            r"(\d{3,6})\s*€",
            r"€\s*(\d{3,6})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                price_str = match.group(1).replace(".", "")
                try:
                    price = int(price_str)
                    if price >= 100:
                        return price * 100
                except ValueError:
                    continue
        return None

    def _extract_mileage(self, text):
        match = re.search(r"(\d{1,3}(?:\.\d{3})*)\s*km", text)
        if match:
            km_str = match.group(1).replace(".", "")
            try:
                return int(km_str)
            except ValueError:
                pass
        return None

    def _extract_year(self, text):
        patterns = [
            r"EZ\s*(\d{2})/(\d{4})",
            r"(\d{2})/(\d{4})",
            r"\b(199[6-8])\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                year = int(match.group(match.lastindex))
                if 1996 <= year <= 1998:
                    return year
        return None

    def _extract_location(self, text):
        match = re.search(r"(?:DE?-?\s*)(\d{5})\s+([A-ZÄÖÜa-zäöüß\s-]+)", text)
        if match:
            return f"{match.group(1)} {match.group(2).strip()}"
        return None

    def _extract_seller_type(self, text):
        text_lower = text.lower()
        if "händler" in text_lower or "dealer" in text_lower or "gewerblich" in text_lower:
            return "dealer"
        if "privat" in text_lower or "private" in text_lower:
            return "private"
        return None

    async def _get_next_page(self, page, browser, current_page_num) -> bool:
        try:
            current_url = page.url
            match = re.search(r"pageNumber=(\d+)", current_url)
            current_page = int(match.group(1)) if match else 1

            next_page = current_page + 1
            if "pageNumber=" in current_url:
                next_url = re.sub(r"pageNumber=\d+", f"pageNumber={next_page}", current_url)
            else:
                separator = "&" if "?" in current_url else "?"
                next_url = f"{current_url}{separator}pageNumber={next_page}"

            page = await browser.get(next_url)
            await asyncio.sleep(random.uniform(2, 4))

            # Check if we got results
            for selector in ["a.link--muted", "[data-testid='result-listing']", "a[href*='/fahrzeuge/details/']"]:
                try:
                    found = await page.query_selector_all(selector)
                    if found:
                        return True
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.warning("Failed to navigate to next page: %s", e)
            return False
