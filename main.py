from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from bs4 import BeautifulSoup
import json

app = FastAPI()

# Allow your Vercel frontend to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health_check():
    return {"status": "ok", "service": "auditgpt-scraper"}


@app.get("/scrape")
async def scrape(
    url: str = Query(..., description="URL to scrape"),
    target: str = Query("website", description="website | meta | instagram"),
):
    """
    Scrapes the given URL using headless Chrome.
    target param tells us what type of page to expect,
    so we can extract the right content.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        # Apply stealth to hide bot fingerprints
        await stealth_async(page)

        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)

            # Wait a moment for JS-rendered content
            await page.wait_for_timeout(3000)

            # Get the full page HTML
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")

            # Remove noise: scripts, styles, nav, footer
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()

            # Extract clean text
            text = soup.get_text(separator="\n", strip=True)

            # Limit to ~5000 chars to stay within Groq context limits
            trimmed = text[:5000]

            # Try to extract page title
            title = soup.title.string if soup.title else ""

            return {
                "status": "success",
                "target": target,
                "url": url,
                "title": title,
                "content": trimmed,
                "content_length": len(trimmed),
            }

        except Exception as e:
            return {
                "status": "blocked",
                "target": target,
                "url": url,
                "reason": str(e),
            }

        finally:
            await browser.close()