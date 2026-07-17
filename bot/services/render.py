import json
import logging
import os
from pathlib import Path
from bot.services.browser import BrowserManager

logger = logging.getLogger("Journey.RendererService")

TEMPLATES_DIR = Path(__file__).parent.parent / "renderer" / "templates"

class RendererService:
    @staticmethod
    async def render_template(
        template_name: str, 
        data: dict, 
        width: int = 1200, 
        height: int = 900
    ) -> bytes:
        """Loads a local HTML template, injects data, and returns screenshot PNG bytes."""
        template_path = TEMPLATES_DIR / template_name / "index.html"
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found at: {template_path}")
            
        file_url = template_path.as_uri()
        browser = await BrowserManager.get_browser()
        
        # Open a new isolated browser page
        page = await browser.new_page()
        try:
            await page.set_viewport_size({"width": width, "height": height})
            
            # Go to the local HTML template file
            await page.goto(file_url)
            
            # Inject JSON data into window.renderData
            js_data = json.dumps(data)
            await page.evaluate(f"window.renderData = {js_data};")
            
            # Trigger template initialization script
            await page.evaluate("if (window.initializeUI) window.initializeUI();")
            
            # Wait for all web fonts to load completely
            await page.evaluate("document.fonts.ready")
            
            # Wait for all image elements (avatars, backgrounds) to load completely
            await page.evaluate("""
                Promise.all(
                    Array.from(document.images)
                        .filter(img => !img.complete)
                        .map(img => new Promise(resolve => {
                            img.onload = img.onerror = resolve;
                        }))
                )
            """)
            
            # Small buffer wait for layout/render stabilizers
            await page.wait_for_timeout(100)
            
            # Capture the viewport screenshot as PNG
            png_bytes = await page.screenshot(type="png", omit_background=True)
            return png_bytes
            
        finally:
            await page.close()
