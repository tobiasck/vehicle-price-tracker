import logging
import re

from scrapers.base_scraper import BaseScraper
from utils.anti_detect import random_action_delay

logger = logging.getLogger(__name__)


class AutoScout24Scraper(BaseScraper):

    async def dismiss_consent(self, page):
        try:
            # AutoScout24 uses a cookie consent banner
            consent_btn = page.locator("button:has-text('Einverstanden'), button:has-text('Alle akzeptieren'), button:has-text('Accept All')")
            if await consent_btn.count() > 0:
                await random_action_delay()
                await consent_btn.first.click()
                logger.info("Dismissed cookie consent")
                await random_action_delay()
        except Exception as e:
            logger.debug("No consent banner or failed to dismiss: %s", e)

    async def parse_listing_cards(self, page) -> list[dict]:
        listings = []

        # AutoScout24 renders listings as article elements with data-guid
        articles = page.locator("article[data-guid]")
        count = await articles.count()

        if count == 0:
            # Fallback: try broader selectors
            articles = page.locator("a[href*='/angebote/'], a[href*='/offers/']")
            count = await articles.count()
            logger.debug("Fallback selector found %d items", count)

        for i in range(count):
            try:
                article = articles.nth(i)
                listing = await self._parse_single_card(article, page)
                if listing and listing.get("platform_id"):
                    listings.append(listing)
            except Exception as e:
                logger.warning("Failed to parse listing card %d: %s", i, e)
                continue

        return listings

    async def _parse_single_card(self, article, page) -> dict | None:
        data = {}

        # Platform ID from data-guid attribute
        guid = await article.get_attribute("data-guid")
        if guid:
            data["platform_id"] = guid
        else:
            # Try to extract from link href
            link = article.locator("a[href*='/angebote/'], a[href*='/offers/']")
            if await link.count() > 0:
                href = await link.first.get_attribute("href")
                if href:
                    match = re.search(r"/(?:angebote|offers)/([a-f0-9-]+)", href)
                    if match:
                        data["platform_id"] = match.group(1)
                    data["listing_url"] = self._build_full_url(href)

        if "platform_id" not in data:
            return None

        # Listing URL
        if "listing_url" not in data:
            link = article.locator("a[href*='/angebote/'], a[href*='/offers/']")
            if await link.count() > 0:
                href = await link.first.get_attribute("href")
                data["listing_url"] = self._build_full_url(href) if href else ""
            else:
                data["listing_url"] = ""

        # Get all text content for regex extraction
        text = await article.text_content() or ""

        # Title
        data["title"] = self._extract_title(text)

        # Price — look for patterns like "12.500 €" or "€ 12.500" or "EUR 12,500"
        data["price_cents"] = self._extract_price(text)

        # Mileage — patterns like "45.000 km"
        data["mileage_km"] = self._extract_mileage(text)

        # Year — 4-digit year in range 1969-1978
        data["year"] = self._extract_year(text)

        # Location
        data["location"] = self._extract_location(text)

        # Seller type
        data["seller_type"] = self._extract_seller_type(text)

        return data

    def _build_full_url(self, href):
        if href.startswith("http"):
            return href
        return f"https://www.autoscout24.de{href}"

    def _extract_title(self, text):
        # First meaningful line is usually the title
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if lines:
            # Return first non-empty line, capped at 200 chars
            return lines[0][:200]
        return None

    def _extract_price(self, text):
        # Patterns: "12.500 €", "€ 12.500", "EUR 12.500", "12,500 €"
        patterns = [
            r"(\d{1,3}(?:\.\d{3})*)\s*€",
            r"€\s*(\d{1,3}(?:\.\d{3})*)",
            r"EUR\s*(\d{1,3}(?:\.\d{3})*)",
            r"(\d{1,3}(?:,\d{3})*)\s*€",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                price_str = match.group(1).replace(".", "").replace(",", "")
                try:
                    return int(price_str) * 100  # Convert to cents
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
        # Look for registration date patterns: "01/1975", "1975", "EZ 03/1972"
        patterns = [
            r"(?:EZ|Erstzulassung)[:\s]*(\d{2})/(\d{4})",
            r"(\d{2})/(\d{4})",
            r"\b(19[6-7]\d)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                year = int(match.group(match.lastindex))
                if 1969 <= year <= 1978:
                    return year
        return None

    def _extract_location(self, text):
        # Location patterns: "DE-12345 Berlin" or "D-80331 München"
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
            next_btn = page.locator("a[aria-label='Nächste Seite'], a[aria-label='Next page'], button[aria-label='Next']")
            if await next_btn.count() > 0:
                is_disabled = await next_btn.first.get_attribute("aria-disabled")
                if is_disabled == "true":
                    return False
                await random_action_delay()
                await next_btn.first.click()
                await page.wait_for_load_state("domcontentloaded")
                return True

            # Fallback: look for page number links
            current_url = page.url
            match = re.search(r"[?&]page=(\d+)", current_url)
            current_page = int(match.group(1)) if match else 1
            next_page_url = re.sub(
                r"([?&])page=\d+",
                f"\\1page={current_page + 1}",
                current_url,
            )
            if next_page_url == current_url:
                separator = "&" if "?" in current_url else "?"
                next_page_url = f"{current_url}{separator}page={current_page + 1}"

            await page.goto(next_page_url, wait_until="domcontentloaded")

            # Check if we got results on this page
            articles = page.locator("article[data-guid]")
            if await articles.count() == 0:
                return False

            return True

        except Exception as e:
            logger.warning("Failed to navigate to next page: %s", e)
            return False
