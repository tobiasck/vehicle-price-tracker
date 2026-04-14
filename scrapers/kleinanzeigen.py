import logging
import re

from scrapers.base_scraper import BaseScraper
from utils.anti_detect import random_action_delay

logger = logging.getLogger(__name__)


class KleinanzeigenScraper(BaseScraper):
    """Scraper for kleinanzeigen.de (formerly eBay Kleinanzeigen).

    This module is implemented as a reserve — not active by default.
    To activate, insert a search_config row with platform='kleinanzeigen'
    and a valid search URL.

    Example search URLs:
      Cars:  https://www.kleinanzeigen.de/s-autos/bmw-z3/k0c216
      Motos: https://www.kleinanzeigen.de/s-motorraeder/honda-cb-750/k0c305
    """

    async def dismiss_consent(self, page):
        try:
            btn = page.locator("#gdpr-banner-accept, button:has-text('Alle akzeptieren')")
            if await btn.count() > 0:
                await random_action_delay()
                await btn.first.click()
                logger.info("Dismissed cookie consent")
                await random_action_delay()
        except Exception as e:
            logger.debug("No consent banner or failed to dismiss: %s", e)

    async def parse_listing_cards(self, page) -> list[dict]:
        listings = []

        # Kleinanzeigen uses article.aditem elements for listings
        selectors = [
            "article.aditem",
            "li.ad-listitem article",
            "[data-adid]",
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
            elements = page.locator("a[href*='/s-anzeige/']")
            logger.debug("Fallback: found %d link items", await elements.count())

        count = await elements.count()
        for i in range(count):
            try:
                element = elements.nth(i)
                listing = await self._parse_single_card(element)
                if listing and listing.get("platform_id"):
                    listings.append(listing)
            except Exception as e:
                logger.warning("Failed to parse listing card %d: %s", i, e)
                continue

        return listings

    async def _parse_single_card(self, element) -> dict | None:
        data = {}

        # Platform ID from data-adid attribute
        ad_id = await element.get_attribute("data-adid")
        if ad_id:
            data["platform_id"] = ad_id

        # Extract from link href
        link = element.locator("a[href*='/s-anzeige/']")
        if await link.count() > 0:
            href = await link.first.get_attribute("href")
            if href:
                data["listing_url"] = self._build_full_url(href)
                if "platform_id" not in data:
                    match = re.search(r"/(\d+)$", href)
                    if match:
                        data["platform_id"] = match.group(1)

        if "platform_id" not in data:
            return None

        if "listing_url" not in data:
            data["listing_url"] = ""

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
        return f"https://www.kleinanzeigen.de{href}"

    def _extract_title(self, text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return lines[0][:200] if lines else None

    def _extract_price(self, text):
        # Kleinanzeigen patterns: "12.500 €", "VB 12.500 €", "12.500 € VB"
        patterns = [
            r"(\d{1,3}(?:\.\d{3})+)\s*€",
            r"€\s*(\d{1,3}(?:\.\d{3})+)",
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
        # Broad year range to support both vehicles
        match = re.search(r"\b(19[6-9]\d|200\d)\b", text)
        if match:
            return int(match.group(1))
        return None

    def _extract_location(self, text):
        match = re.search(r"(\d{5})\s+([A-ZÄÖÜa-zäöüß\s-]+)", text)
        if match:
            return f"{match.group(1)} {match.group(2).strip()}"
        return None

    def _extract_seller_type(self, text):
        text_lower = text.lower()
        if "gewerblich" in text_lower or "händler" in text_lower:
            return "dealer"
        if "privat" in text_lower:
            return "private"
        return None

    async def get_next_page(self, page) -> bool:
        try:
            next_btn = page.locator("a.pagination-next, a[data-testid='pagination-next']")
            if await next_btn.count() > 0:
                await random_action_delay()
                await next_btn.first.click()
                await page.wait_for_load_state("domcontentloaded")
                return True

            # Fallback: URL-based pagination (seite:2)
            current_url = page.url
            match = re.search(r"/seite:(\d+)/", current_url)
            current_page = int(match.group(1)) if match else 1
            next_page = current_page + 1

            if match:
                next_url = re.sub(r"/seite:\d+/", f"/seite:{next_page}/", current_url)
            else:
                next_url = re.sub(r"(/k0c\d+)", f"/seite:{next_page}\\1", current_url)

            if next_url == current_url:
                return False

            await page.goto(next_url, wait_until="domcontentloaded")

            # Check if results exist
            for selector in ["article.aditem", "[data-adid]"]:
                if await page.locator(selector).count() > 0:
                    return True

            return False

        except Exception as e:
            logger.warning("Failed to navigate to next page: %s", e)
            return False
