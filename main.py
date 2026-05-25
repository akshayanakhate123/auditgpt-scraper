from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import json
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health_check():
    return {"status": "ok", "service": "auditgpt-scraper"}


async def scrape_page(url: str, stealth: Stealth, timeout: int = 60000):
    """Scrape a single page and return clean text + metadata."""
    async with stealth.use_async(async_playwright()) as p:
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

        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout)
            await page.wait_for_timeout(3000)

            html = await page.content()
            soup = BeautifulSoup(html, "lxml")

            # Extract meta tags before removing elements
            meta_desc = ""
            meta_tag = soup.find("meta", attrs={"name": "description"})
            if meta_tag:
                meta_desc = meta_tag.get("content", "")

            # Extract all headings
            headings = []
            for tag in ["h1", "h2", "h3"]:
                for h in soup.find_all(tag):
                    headings.append({"tag": tag, "text": h.get_text(strip=True)})

            # Extract links for discovering product/cart pages
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if text and len(text) < 100:
                    links.append({"href": href, "text": text})

            # Check for specific elements
            has_email_signup = bool(
                soup.find("input", {"type": "email"})
                or soup.find(string=re.compile(r"newsletter|subscribe|sign.?up", re.I))
            )
            has_loyalty = bool(
                soup.find(string=re.compile(r"loyalty|rewards|points|referral", re.I))
            )
            has_reviews = bool(
                soup.find(string=re.compile(r"reviews?|ratings?|stars?|\d+\s*out\s*of\s*5", re.I))
            )
            has_cart = bool(
                soup.find(string=re.compile(r"add.to.cart|buy.now|shop.now", re.I))
            )
            cta_count = len(soup.find_all(
                ["a", "button"],
                string=re.compile(r"buy|shop|add|get|start|try|book|order", re.I)
            ))

            # Remove noise
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)
            trimmed = text[:5000]
            title = soup.title.string if soup.title else ""

            return {
                "status": "success",
                "url": url,
                "title": title,
                "meta_description": meta_desc,
                "headings": headings[:20],
                "content": trimmed,
                "content_length": len(trimmed),
                "signals": {
                    "has_email_signup": has_email_signup,
                    "has_loyalty_program": has_loyalty,
                    "has_reviews": has_reviews,
                    "has_cart_cta": has_cart,
                    "cta_count": cta_count,
                },
                "internal_links": links[:30],
            }

        except Exception as e:
            return {
                "status": "blocked",
                "url": url,
                "reason": str(e),
            }

        finally:
            await browser.close()


@app.get("/scrape")
async def scrape_single(
    url: str = Query(..., description="URL to scrape"),
    target: str = Query("website", description="website | meta | instagram"),
):
    """Scrape a single URL."""
    stealth = Stealth()
    result = await scrape_page(url, stealth)
    result["target"] = target
    return result


@app.post("/scrape-audit")
async def scrape_audit(payload: dict):
    """
    Scrape all URLs needed for a full audit.
    Expects JSON body:
    {
        "website_url": "https://brand.com",
        "meta_ad_url": "https://facebook.com/ads/library/...",  (optional)
        "instagram_handle": "yourbrand",  (optional)
        "competitor_urls": ["https://comp1.com", ...]  (optional)
    }
    """
    stealth = Stealth()
    results = {
        "website": None,
        "product_page": None,
        "meta": None,
        "instagram": None,
        "competitors": [],
    }

   # 1. Scrape brand homepage (required)
    website_url = payload.get("website_url", "")
    if website_url:
        results["website"] = await scrape_page(website_url, stealth)

    # 1b. Scrape product page if provided
    product_url = payload.get("product_url", "")
    if product_url:
        results["product_page"] = await scrape_page(product_url, stealth)
    else:
        results["product_page"] = None

    # 2. Attempt Meta Ad Library (optional, likely blocked)
    meta_url = payload.get("meta_ad_url", "")
    if meta_url:
        results["meta"] = await scrape_page(meta_url, stealth, timeout=30000)

    # 3. Attempt Instagram (optional, likely blocked)
    ig_handle = payload.get("instagram_handle", "")
    if ig_handle:
        ig_handle = ig_handle.replace("@", "")
        ig_url = f"https://www.instagram.com/{ig_handle}/"
        results["instagram"] = await scrape_page(ig_url, stealth, timeout=30000)

    # 4. Scrape competitor websites (optional)
    competitor_urls = payload.get("competitor_urls", [])
    for comp_url in competitor_urls[:3]:
        if comp_url:
            comp_result = await scrape_page(comp_url, stealth)
            results["competitors"].append(comp_result)

    return results