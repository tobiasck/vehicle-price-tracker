import asyncio
import json
import logging
import os
import random
import re

import nodriver

from config.settings import (
    MIN_PAGE_DELAY, MAX_PAGE_DELAY,
)
from db.models import (
    create_scrape_run, finish_scrape_run,
    upsert_listing, insert_snapshot, update_run_statistics,
)

logger = logging.getLogger(__name__)

DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug")


class MobileDeScraper:
    """mobile.de scraper using nodriver (headful under Xvfb) for anti-detection."""

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
            viewport_w = random.choice([1280, 1366, 1440, 1536, 1920])
            viewport_h = random.choice([720, 800, 900, 1080])

            browser = await nodriver.start(
                headless=False,
                lang="de-DE",
                browser_args=[
                    f"--window-size={viewport_w},{viewport_h}",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-dev-shm-usage",
                    "--start-maximized",
                ],
            )

            # Step 1: Organic warmup on homepage
            logger.info("Step 1: Visiting mobile.de homepage")
            page = await browser.get("https://www.mobile.de")
            await asyncio.sleep(random.uniform(4, 7))

            if await self._is_blocked(page):
                if self.debug:
                    await self._save_debug(page, "blocked_homepage")
                raise RuntimeError("Blocked by mobile.de on homepage")

            await self._dismiss_consent(page)
            await asyncio.sleep(random.uniform(2, 3))

            # Click Suche nav link for organic behaviour
            for text in ["Suche", "Fahrzeuge suchen", "Detailsuche"]:
                try:
                    link = await page.find(text, best_match=True, timeout=3)
                    if link:
                        await link.click()
                        await asyncio.sleep(random.uniform(2, 4))
                        logger.info("Clicked nav link: '%s'", text)
                        break
                except Exception:
                    continue

            # Step 2: Load actual search URL
            logger.info("Step 2: Loading search results: %s", self.search_url)
            page = await browser.get(self.search_url)
            await asyncio.sleep(random.uniform(3, 5))

            if await self._is_blocked(page):
                if self.debug:
                    await self._save_debug(page, "blocked_search")
                raise RuntimeError("Blocked by mobile.de on search page")

            await self._dismiss_consent(page)

            # Step 3: Collect all listings via infinite scroll
            logger.info("Step 3: Scrolling to collect all listings")
            all_listings = await self._collect_all_via_scroll(page)
            logger.info("Collected %d unique listings total", len(all_listings))

            if self.debug:
                await self._save_debug(page, "page_final")

            for listing_data in all_listings:
                if dry_run:
                    logger.info("[DRY RUN] %s", listing_data)
                else:
                    self._save_listing(run_id, listing_data)

            total_listings = len(all_listings)

            if not dry_run:
                finish_scrape_run(self.conn, run_id, "success", total_listings)
                update_run_statistics(self.conn, run_id)

            logger.info("Scrape complete: %s — %d listings", self.vehicle_name, total_listings)

        except Exception as e:
            logger.error("Scrape failed for %s: %s", self.vehicle_name, e, exc_info=True)
            if not dry_run and run_id:
                finish_scrape_run(self.conn, run_id, "failed", total_listings, str(e))
            raise

        finally:
            if browser:
                browser.stop()

        return total_listings

    async def _collect_all_via_scroll(self, page) -> list[dict]:
        """Scroll down incrementally until no new listings appear (infinite scroll).

        mobile.de loads ~20 cards per batch. We scroll near the bottom to
        trigger each batch load, wait for new cards to appear, then repeat.
        """
        seen_ids = set()
        all_listings = []
        no_new_count = 0
        scroll_round = 0

        # Collect initial cards before any scrolling
        current = await self._extract_cards(page)
        for listing in current:
            pid = listing.get("platform_id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_listings.append(listing)
        logger.info("Initial cards: %d", len(all_listings))

        while no_new_count < 3:
            scroll_round += 1

            # Scroll down by one viewport height to trigger lazy loading
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.85)")
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Wait up to 8s for new cards to appear in DOM
            prev_count = len(seen_ids)
            for _ in range(8):
                current = await self._extract_cards(page)
                new_found = sum(1 for l in current if l.get("platform_id") not in seen_ids)
                if new_found > 0:
                    break
                await asyncio.sleep(1.0)

            # Count and register new listings
            new_count = 0
            for listing in current:
                pid = listing.get("platform_id")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_listings.append(listing)
                    new_count += 1

            logger.info("Scroll round %d: %d new listings (%d total)",
                        scroll_round, new_count, len(all_listings))

            if new_count == 0:
                no_new_count += 1
            else:
                no_new_count = 0

            if scroll_round >= 100:
                logger.warning("Reached scroll limit of 100 rounds")
                break

        return all_listings

    async def _scroll_to_bottom(self, page):
        """Scroll to the absolute bottom of the page."""
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            logger.debug("Scroll error: %s", e)

    async def _extract_cards(self, page) -> list[dict]:
        """Extract all listing cards currently in the DOM via JS."""
        js = """
        (() => {
            const selectors = [
                "a[data-testid^='srx-result-listing-']",
                "a[href*='/fahrzeuge/details.html']"
            ];
            let cards = [];
            for (const sel of selectors) {
                cards = Array.from(document.querySelectorAll(sel));
                if (cards.length > 0) break;
            }
            return JSON.stringify(cards.map(c => ({
                href: c.href || c.getAttribute('href') || '',
                text: (c.textContent || '').trim().slice(0, 2000)
            })));
        })()
        """
        try:
            json_str = await page.evaluate(js)
            if not json_str:
                return []
            raw_cards = json.loads(json_str)
        except Exception as e:
            logger.warning("JS card extraction failed: %s", e)
            return []

        listings = []
        for card in raw_cards:
            try:
                listing = self._parse_card_data(card.get("href", ""), card.get("text", ""))
                if listing:
                    listings.append(listing)
            except Exception as e:
                logger.debug("Card parse error: %s", e)

        return listings

    def _parse_card_data(self, href: str, text: str) -> dict | None:
        if not href or not text.strip():
            return None

        # Build absolute URL
        listing_url = href if href.startswith("http") else f"https://suchen.mobile.de{href}"

        # ID from query param ?id=XXXXXXXXX
        match = re.search(r"[?&]id=(\d+)", href)
        if not match:
            return None
        platform_id = match.group(1)

        # Skip "ähnliche Fahrzeuge" — shown when no exact match exists
        if "Andere Suchkriterien" in text:
            return None

        return {
            "platform_id": platform_id,
            "listing_url": listing_url,
            "title": self._extract_title(text),
            "price_cents": self._extract_price(text),
            "mileage_km": self._extract_mileage(text),
            "year": self._extract_year(text),
            "location": self._extract_location(text),
            "seller_type": self._extract_seller_type(text),
        }

    def _save_listing(self, run_id, data):
        listing_id = upsert_listing(
            self.conn, self.config_id, data["platform_id"], data["listing_url"],
        )
        insert_snapshot(self.conn, listing_id, run_id, data)

    def _extract_title(self, text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return lines[0][:200] if lines else None

    def _extract_price(self, text):
        for pattern in [
            r"(\d{1,3}(?:\.\d{3})+)\s*€",
            r"€\s*(\d{1,3}(?:\.\d{3})+)",
            r"(\d{3,6})\s*€",
        ]:
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
            try:
                return int(match.group(1).replace(".", ""))
            except ValueError:
                pass
        return None

    def _extract_year(self, text):
        for pattern in [r"EZ\s*\d{2}/(\d{4})", r"\b(\d{2})/(\d{4})\b", r"\b(19\d{2})\b"]:
            match = re.search(pattern, text)
            if match:
                year = int(match.group(match.lastindex))
                if 1960 <= year <= 2010:
                    return year
        return None

    def _extract_location(self, text):
        match = re.search(r"(\d{5})\s+([A-ZÄÖÜa-zäöüß][a-zäöüßA-ZÄÖÜ\s\-]{2,30})", text)
        if match:
            return f"{match.group(1)} {match.group(2).strip()}"
        return None

    def _extract_seller_type(self, text):
        t = text.lower()
        if any(w in t for w in ["händler", "autohaus", "gmbh", "ag ", "dealer", "gewerblich"]):
            return "dealer"
        if "privat" in t:
            return "private"
        return None

    async def _dismiss_consent(self, page):
        for text in ["Einverstanden", "Alle akzeptieren", "Accept All", "Akzeptieren"]:
            try:
                btn = await page.find(text, best_match=True, timeout=3)
                if btn:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await btn.click()
                    logger.info("Dismissed consent: '%s'", text)
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

    async def _is_blocked(self, page):
        try:
            content = await page.get_content()
            c = content.lower()
            if "zugriff verweigert" in c or "access denied" in c:
                return True
            if "cf-challenge" in c or "cf-turnstile" in c:
                return True
        except Exception:
            pass
        return False

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
            logger.warning("Failed to save debug %s: %s", prefix, e)
