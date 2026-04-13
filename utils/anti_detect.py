import asyncio
import random
import logging

from config.settings import (
    MIN_PAGE_DELAY, MAX_PAGE_DELAY,
    MIN_ACTION_DELAY, MAX_ACTION_DELAY,
    VIEWPORTS, USER_AGENTS,
)

logger = logging.getLogger(__name__)


def get_browser_context_args():
    viewport = random.choice(VIEWPORTS)
    user_agent = random.choice(USER_AGENTS)
    logger.debug("Using viewport %s, UA: %s", viewport, user_agent[:50])
    return {
        "viewport": viewport,
        "user_agent": user_agent,
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "permissions": [],
        "java_script_enabled": True,
    }


async def random_page_delay():
    delay = random.uniform(MIN_PAGE_DELAY, MAX_PAGE_DELAY)
    logger.debug("Page delay: %.1fs", delay)
    await asyncio.sleep(delay)


async def random_action_delay():
    delay = random.uniform(MIN_ACTION_DELAY, MAX_ACTION_DELAY)
    await asyncio.sleep(delay)


async def human_scroll(page):
    for _ in range(random.randint(2, 4)):
        scroll_amount = random.randint(200, 500)
        await page.mouse.wheel(0, scroll_amount)
        await asyncio.sleep(random.uniform(0.3, 0.8))
