import asyncio
import logging
import re

from scrapers.base_scraper import BaseScraper
from utils.anti_detect import random_action_delay, random_page_delay

logger = logging.getLogger(__name__)


class MobileDeScraper(BaseScraper):

    async def _navigate_with_warmup(self, page, target_url):
        """Navigate to mobile.de via homepage first to establish a normal session."""
        logger.info("Warming up session via mobile.de homepage")

        # Visit homepage first
        await page.goto("https://www.mobile.de", wait_until="domcontentloaded", timeout=30000)
        await random_page_delay()

        # Dismiss consent on homepage
        await self.dismiss_consent(page)
        await asyncio.sleep(2)

        # Now navigate to the search
        logger.info("Navigating to search URL")
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

    async def navigate_to_search(self, page):
        """Override: visit homepage first to warm up session, then search."""
        await self._navigate_with_warmup(page, self.search_url)

    async def dismiss_consent(self, page):
        try:
            # mobile.de uses a CMP consent popup
            for selector in [
                "button:has-text('Einverstanden')",
                "button:has-text('Alle akzeptieren')",
                "button:has-text('Accept All')",
                "#mde-consent-accept-btn",
                "[data-testid='gdpr-consent-accept-all']",
            ]:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await random_action_delay()
                    await btn.first.click()
                    logger.info("Dismissed cookie consent via: %s", selector)
                    await random_action_delay()
                    return
        except Exception as e:
            logger.debug("No consent banner or failed to dismiss: %s", e)

    async def parse_listing_cards(self, page) -> list[dict]:
        listings = []

        # mobile.de listing cards — try multiple selector strategies
        selectors = [
            "a.link--muted.no--text--decoration",
            "[data-testid='result-listing']",
            "div.cBox-body--resultitem",
            "article[data-listing-id]",
        ]

        elements = None
        for selector in selectors:
            candidate = page.locator(selector)
            count = await candidate.count()
            if count > 0:
                elements = candidate
                logger.debug("Using selector '%s' — found %d items", selector, count)
                break

        if elements is None:
            # Last resort: find all links that look like listing URLs
            elements = page.locator("a[href*='/fahrzeuge/details/']")
            count = await elements.count()
            logger.debug("Fallback link selector found %d items", count)

        count = await elements.count()
        for i in range(count):
            try:
                element = elements.nth(i)
                listing = await self._parse_single_card(element, page)
                if listing and listing.get("platform_id"):
                    # Deduplicate by platform_id within a page
                    if not any(l["platform_id"] == listing["platform_id"] for l in listings):
                        listings.append(listing)
            except Exception as e:
                logger.warning("Failed to parse listing card %d: %s", i, e)
                continue

        return listings

    async def _parse_single_card(self, element, page) -> dict | None:
        data = {}

        # Extract listing URL and platform ID
        tag = await element.evaluate("el => el.tagName.toLowerCase()")
        if tag == "a":
            href = await element.get_attribute("href")
        else:
            link = element.locator("a[href*='/fahrzeuge/details/']")
            if await link.count() > 0:
                href = await link.first.get_attribute("href")
            else:
                href = None

        if href:
            data["listing_url"] = self._build_full_url(href)
            # Extract ID from URL like /fahrzeuge/details/123456789
            match = re.search(r"/details/(\d+)", href)
            if match:
                data["platform_id"] = match.group(1)
            else:
                match = re.search(r"/(\d{6,})", href)
                if match:
                    data["platform_id"] = match.group(1)

        # Try data-listing-id attribute
        if "platform_id" not in data:
            lid = await element.get_attribute("data-listing-id")
            if lid:
                data["platform_id"] = lid

        if "platform_id" not in data:
            return None

        if "listing_url" not in data:
            data["listing_url"] = ""

        # Get text content for extraction
        text = await element.text_content() or ""

        data["title"] = self._extract_title(text)
        data["price_cents"] = self._extract_price(text)
        data["mileage_km"] = self._extract_mileage(text)
        data["year"] = self._extract_year(text)
        data["location"] = self._extract_location(text)
        data["seller_type"] = self._extract_seller_type(text)

        return data

    def _build_full_url(self, href):
        if href.startswith("http"):
            return href
        return f"https://suchen.mobile.de{href}"

    def _extract_title(self, text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if lines:
            return lines[0][:200]
        return None

    def _extract_price(self, text):
        patterns = [
            r"(\d{1,3}(?:\.\d{3})*)\s*€",
            r"€\s*(\d{1,3}(?:\.\d{3})*)",
            r"EUR\s*(\d{1,3}(?:\.\d{3})*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                price_str = match.group(1).replace(".", "")
                try:
                    return int(price_str) * 100
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

    async def get_next_page(self, page) -> bool:
        try:
            # mobile.de uses pageNumber parameter
            current_url = page.url
            match = re.search(r"pageNumber=(\d+)", current_url)
            current_page = int(match.group(1)) if match else 1

            # Check for "next" button
            next_selectors = [
                "a[data-testid='pagination-next']",
                "span.btn.btn--muted.btn--s:has-text('»')",
                "a:has-text('Nächste')",
                "a:has-text('»')",
            ]
            for selector in next_selectors:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await random_action_delay()
                    await btn.first.click()
                    await page.wait_for_load_state("domcontentloaded")
                    return True

            # Fallback: manually set pageNumber
            next_page = current_page + 1
            if "pageNumber=" in current_url:
                next_url = re.sub(r"pageNumber=\d+", f"pageNumber={next_page}", current_url)
            else:
                separator = "&" if "?" in current_url else "?"
                next_url = f"{current_url}{separator}pageNumber={next_page}"

            await page.goto(next_url, wait_until="domcontentloaded")

            # Verify we got results
            for selector in ["a.link--muted", "[data-testid='result-listing']", "a[href*='/fahrzeuge/details/']"]:
                if await page.locator(selector).count() > 0:
                    return True

            return False

        except Exception as e:
            logger.warning("Failed to navigate to next page: %s", e)
            return False
