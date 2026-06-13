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
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(5000)

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


async def scrape_meta_ads(url: str, stealth: Stealth, timeout: int = 60000):
    """
    Scrape Meta Ad Library and return structured ad metrics + sample copy.

    Uses the same selector chain as mokobara_scraper.py, adapted for async
    Playwright. Runs headless (no login), so Meta may return limited data.
    """
    META_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    SCROLL_COUNT = 5
    SCROLL_DELAY_MS = 2000

    async with stealth.use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=META_USER_AGENT,
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(3000)

            # Scroll to load more ads (mirrors SCROLL_COUNT in reference)
            for _ in range(SCROLL_COUNT):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(SCROLL_DELAY_MS)

            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(1000)

            # Selector chain from reference (same order)
            card_selectors = [
                "div[class*='_7jvw']",
                "div[role='article']",
                "div[class*='xrvj5dj']",
            ]

            cards_loc = None
            selector_used = None

            for sel in card_selectors:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    cards_loc = loc
                    selector_used = sel
                    break

            if cards_loc is None:
                fallback = page.locator("div._99s5")
                if await fallback.count() > 0:
                    cards_loc = fallback
                    selector_used = "div._99s5"
                else:
                    ult = page.locator("div:has(img[src*='fbcdn'])")
                    if await ult.count() > 0:
                        cards_loc = ult
                        selector_used = "div:has(img[src*='fbcdn'])"

            if cards_loc is None or await cards_loc.count() == 0:
                page_text = await page.inner_text("body")
                return {
                    "status": "blocked",
                    "url": url,
                    "reason": "No ad cards found with any known selector",
                    "page_preview": page_text[:500],
                    "selector_used": None,
                }

            total_cards = await cards_loc.count()
            ads = []

            for i in range(total_cards):
                card = cards_loc.nth(i)
                ad_data = {
                    "index": i,
                    "body_copy": "",
                    "cta_text": "",
                    "has_image": False,
                    "has_video": False,
                }

                try:
                    # Body copy — longest text block in card
                    try:
                        text_els = card.locator(
                            "div[style*='webkit'], div[class*='x1lliihq'], span[class*='x1lliihq']"
                        )
                        best_text = ""
                        for t in range(min(await text_els.count(), 10)):
                            try:
                                txt = (await text_els.nth(t).inner_text(timeout=1000)).strip()
                                if len(txt) > len(best_text):
                                    best_text = txt
                            except Exception:
                                pass
                        if not best_text:
                            all_text = await card.inner_text(timeout=3000)
                            paragraphs = [
                                p.strip() for p in all_text.split("\n") if len(p.strip()) > 30
                            ]
                            best_text = max(paragraphs, key=len) if paragraphs else ""
                        ad_data["body_copy"] = best_text
                    except Exception:
                        pass

                    # CTA text
                    try:
                        cta_el = card.locator("a[class*='cta'], button, div[role='button']")
                        for c in range(min(await cta_el.count(), 5)):
                            try:
                                cta_text = (await cta_el.nth(c).inner_text(timeout=1000)).strip()
                                if cta_text and len(cta_text) < 40:
                                    ad_data["cta_text"] = cta_text
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Detect image presence (fbcdn-hosted)
                    try:
                        imgs = card.locator("img[src*='fbcdn']")
                        ad_data["has_image"] = await imgs.count() > 0
                    except Exception:
                        pass

                    # Detect video presence
                    try:
                        videos = card.locator("video[src], video source")
                        ad_data["has_video"] = await videos.count() > 0
                    except Exception:
                        pass

                    if ad_data["has_image"] or ad_data["has_video"] or ad_data["body_copy"]:
                        ads.append(ad_data)

                except Exception:
                    pass

            if not ads:
                page_text = await page.inner_text("body")
                return {
                    "status": "blocked",
                    "url": url,
                    "reason": (
                        f"Found {total_cards} card elements but extracted 0 ads with content"
                    ),
                    "page_preview": page_text[:500],
                    "selector_used": selector_used,
                }

            # --- Metrics ---
            total_ads = len(ads)
            video_ads = sum(1 for a in ads if a["has_video"])
            image_ads = sum(1 for a in ads if a["has_image"] and not a["has_video"])
            mixed_ads = sum(1 for a in ads if a["has_image"] and a["has_video"])
            ads_with_cta = sum(1 for a in ads if a["cta_text"])
            unique_ctas = {a["cta_text"] for a in ads if a["cta_text"]}
            unique_hooks = {a["body_copy"][:80] for a in ads if a["body_copy"]}
            video_ratio_pct = round((video_ads / total_ads) * 100, 1) if total_ads else 0.0

            metrics = {
                "total_ads": total_ads,
                "image_ads": image_ads,
                "video_ads": video_ads,
                "mixed_ads": mixed_ads,
                "video_ratio_pct": video_ratio_pct,
                "unique_cta_count": len(unique_ctas),
                "unique_hook_count": len(unique_hooks),
                "ads_with_cta": ads_with_cta,
            }

            # --- Text summary for Groq ---
            lines = [
                "Meta Ad Library Analysis",
                f"URL: {url}",
                "",
                "=== METRICS ===",
                f"Total ads found: {total_ads}",
                f"Image-only ads: {image_ads}",
                f"Video ads: {video_ads}",
                f"Mixed (image+video) ads: {mixed_ads}",
                f"Video ratio: {video_ratio_pct}%",
                f"Ads with CTA: {ads_with_cta}",
                f"Unique CTAs ({len(unique_ctas)}): {', '.join(sorted(unique_ctas)[:10])}",
                f"Unique opening hooks: {len(unique_hooks)}",
                "",
                "=== SAMPLE AD COPY (first 5 ads) ===",
            ]
            for ad in ads[:5]:
                body_preview = ad["body_copy"][:200] if ad["body_copy"] else "(no copy)"
                cta = ad["cta_text"] or "(no CTA)"
                media_parts = []
                if ad["has_image"]:
                    media_parts.append("image")
                if ad["has_video"]:
                    media_parts.append("video")
                media_str = "+".join(media_parts) if media_parts else "no media"
                lines.append(f"--- Ad {ad['index'] + 1} [{media_str}] | CTA: {cta} ---")
                lines.append(body_preview)
                lines.append("")

            return {
                "status": "success",
                "url": url,
                "content": "\n".join(lines),
                "metrics": metrics,
                "selector_used": selector_used,
            }

        except Exception as e:
            return {
                "status": "blocked",
                "url": url,
                "reason": str(e),
                "selector_used": None,
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
    if target == "meta":
        result = await scrape_meta_ads(url, stealth)
    else:
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

    # 2. Attempt Meta Ad Library (optional)
    meta_url = payload.get("meta_ad_url", "")
    if meta_url:
        results["meta"] = await scrape_meta_ads(meta_url, stealth)

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