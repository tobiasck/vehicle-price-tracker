import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://scraper:scraper@localhost:5432/vehicle_scraper")

# Scraper platform -> class mapping
PLATFORM_SCRAPERS = {
    "mobile_de": "scrapers.mobile_de.MobileDeScraper",
    "autoscout24": "scrapers.autoscout24.AutoScout24Scraper",
    "kleinanzeigen": "scrapers.kleinanzeigen.KleinanzeigenScraper",
}

# Anti-detection settings
MIN_PAGE_DELAY = 2.0
MAX_PAGE_DELAY = 5.0
MIN_ACTION_DELAY = 0.5
MAX_ACTION_DELAY = 1.5

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
]

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
]

# Retry settings
BLOCK_RETRY_WAIT_MIN = 30
BLOCK_RETRY_WAIT_MAX = 60
MAX_RETRIES = 1
