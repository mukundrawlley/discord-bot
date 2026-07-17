from playwright.async_api import async_playwright, Browser
import logging

logger = logging.getLogger("Journey.BrowserManager")

class BrowserManager:
    _playwright = None
    _browser = None

    @classmethod
    async def initialize(cls) -> Browser:
        """Launches a headless Chromium instance if it doesn't already exist."""
        if cls._browser is None:
            logger.info("Launching headless Chromium browser instance...")
            cls._playwright = await async_playwright().start()
            cls._browser = await cls._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            logger.info("Headless Chromium browser launched successfully.")
        return cls._browser

    @classmethod
    async def get_browser(cls) -> Browser:
        """Returns the active persistent Browser instance, initializing it if necessary."""
        if cls._browser is None:
            await cls.initialize()
        return cls._browser

    @classmethod
    async def close(cls) -> None:
        """Closes the active browser instance and stops Playwright."""
        if cls._browser is not None:
            logger.info("Closing Chromium browser instance...")
            try:
                await cls._browser.close()
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
            cls._browser = None
            
        if cls._playwright is not None:
            try:
                await cls._playwright.stop()
            except Exception as e:
                logger.error(f"Error stopping playwright: {e}")
            cls._playwright = None
            logger.info("Playwright stopped.")
