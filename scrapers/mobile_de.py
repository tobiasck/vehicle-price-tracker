import asyncio
import logging
import os
import random
import re

import nodriver

from config.settings import (
    MIN_PAGE_DELAY, MAX_PAGE_DELAY,
    BLOCK_RETRY_WAIT_MIN, BLOCK_RETRY_WAIT_MAX, MAX_RETRIES,
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
            # Run *headful* under Xvfb on the VM. nodriver in headless mode
            # leaks too many automation signals for mobile.de's bot manager.
            # Do NOT override --user-agent: let Chromium report its own native
            # UA so it stays consistent with the actual browser version,
            # platform and all other fingerprint signals.
            viewport_w = random.choice([1280, 1366, 1440, 1536, 1600, 1920])
            viewport_h = random.choice([720, 800, 864, 900, 1080])

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

            # Step 1: Visit homepage and establish session
            logger.info("Step 1: Visiting mobile.de homepage")
            page = await browser.get("https://www.mobile.de")
            await asyncio.sleep(random.uniform(4, 7))

            if await self._is_blocked(page):
                if self.debug:
                    await self._save_debug(page, "blocked_homepage")
                raise RuntimeError("Blocked by mobile.de on homepage — IP may be banned")

            # Step 2: Dismiss cookie consent
            await self._dismiss_consent(page)
            await asyncio.sleep(random.uniform(2, 4))

            # Step 3: Browse organically — click on "Suche" or similar nav element
            logger.info("Step 2: Navigating organically to search")
            await self._human_scroll(page)
            await asyncio.sleep(random.uniform(2, 3))

            # Try clicking the search link from homepage
            search_clicked = False
            for text in ["Suche", "Fahrzeuge suchen", "Detailsuche", "Erweiterte Suche"]:
                try:
                    link = await page.find(text, best_match=True, timeout=3)
                    if link:
                        await asyncio.sleep(random.uniform(1, 2))
                        await link.click()
                        await asyncio.sleep(random.uniform(3, 5))
                        search_clicked = True
                        logger.info("Clicked navigation link: '%s'", text)
                        break
                except Exception:
                    continue

            # Step 4: Navigate to actual search URL (after organic warmup)
            logger.info("Step 3: Loading search results")
            await asyncio.sleep(random.uniform(2, 4))
            page = await browser.get(self.search_url)
            await asyncio.sleep(random.uniform(4, 7))

            # Check if blocked
            if await self._is_blocked(page):
                if self.debug:
                    await self._save_debug(page, "blocked_after_warmup")

                # Wait longer and retry
                wait = random.uniform(60, 120)
                logger.warning("Blocked after warmup, waiting %.0fs before retry", wait)
                await asyncio.sleep(wait)

                page = await browser.get(self.search_url)
                await asyncio.sleep(random.uniform(4, 7))

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

                # Stop if no valid listings parsed — avoids infinite loops on
                # pages where the selector matches ads/empty elements only.
                if not listings:
                    logger.info("No listings on page %d — stopping pagination", page_num)
                    break

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
        """Extract all listing cards via a single JS evaluation — avoids
        unreliable per-element nodriver attribute access."""
        listings = []

        # Note: nodriver evaluate() runs the expression as-is via Runtime.evaluate,
        # so we need an IIFE — a bare arrow function is never called.
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
            return cards.map(c => ({
                href: c.href || c.getAttribute('href') || '',
                text: c.textContent || ''
            }));
        })()
        """
        try:
            raw_cards = await page.evaluate(js)
        except Exception as e:
            logger.warning("JS evaluation failed: %s", e)
            return listings

        if not raw_cards:
            return listings

        logger.debug("JS extracted %d raw cards", len(raw_cards))

        seen_ids = set()
        for card in raw_cards:
            try:
                listing = self._parse_card_data(card.get("href", ""), card.get("text", ""))
                if listing and listing.get("platform_id"):
                    if listing["platform_id"] not in seen_ids:
                        seen_ids.add(listing["platform_id"])
                        listings.append(listing)
            except Exception as e:
                logger.warning("Failed to parse card: %s", e)

        return listings

    def _parse_card_data(self, href: str, text: str) -> dict | None:
        """Parse a single listing card from its href and text content."""
        if not href:
            return None

        # Build absolute URL
        listing_url = href if href.startswith("http") else f"https://suchen.mobile.de{href}"

        # ID is a query param: ?id=445527390
        match = re.search(r"[?&]id=(\d+)", href)
        if not match:
            return None
        platform_id = match.group(1)

        if not text.strip():
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

            # Build next-page URL using pageNumber= query param
            next_page = current_page_num + 1
            if "pageNumber=" in current_url:
                next_url = re.sub(r"pageNumber=\d+", f"pageNumber={next_page}", current_url)
            else:
                separator = "&" if "?" in current_url else "?"
                next_url = f"{current_url}{separator}pageNumber={next_page}"

            page = await browser.get(next_url)
            await asyncio.sleep(random.uniform(3, 5))

            # Check if we got listing results on this page
            for selector in [
                "a[data-testid^='srx-result-listing-']",
                "a[href*='/fahrzeuge/details.html']",
            ]:
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
