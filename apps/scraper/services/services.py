import logging
import os
import random
import time
import re
from typing import Dict, Optional, Any, List
from decimal import Decimal
from datetime import datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from django.db import transaction
from django.utils import timezone
from django.conf import settings

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

from apps.scraper.models import Product, StorePrice, PriceHistory

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
]


class ScraperService:
    """Generic scraper that can search Amazon/Flipkart and persist prices."""

    def __init__(self) -> None:
        # last_error is used by callers to understand why scraping returned no results
        self.last_error: Optional[str] = None

    def search_all(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        # Use SerpAPI-backed scrape by default for reliability
        return self._serp_scrape(query, limit)

    def _serp_scrape(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Fetch results from SerpAPI Google Shopping and normalize them."""
        self.last_error = None
        SERPAPI_API_KEY = getattr(settings, 'SERPAPI_API_KEY', '') or os.getenv('SERPAPI_API_KEY', '')
        if not SERPAPI_API_KEY:
            self.last_error = 'Missing SERPAPI_API_KEY'
            logger.error(self.last_error)
            return []

        def _call_serp(q: str) -> Optional[dict]:
            params = {
                'engine': 'google_shopping',
                'q': q,
                'api_key': SERPAPI_API_KEY,
                'hl': 'en',
                'gl': 'in',
                'num': 20,
            }
            try:
                resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                logger.info('SERPAPI RAW COUNT %s', len(data.get('shopping_results', [])))
                return data
            except Exception as exc:
                self.last_error = f'SerpAPI error: {exc}'
                logger.exception('SerpAPI request failed')
                return None

        # Try original query, then fallbacks if no shopping_results
        attempts = [query, f"{query} price", f"{query} buy online"]
        data = None
        for attempt in attempts:
            data = _call_serp(attempt)
            if data and data.get('shopping_results'):
                # prefer first non-empty result set
                break

        if not data or not data.get('shopping_results'):
            self.last_error = 'SerpAPI returned no shopping results after retries'
            return []

        normalized: List[Dict[str, Any]] = []
        for item in data.get('shopping_results', []):
            if len(normalized) >= limit:
                break
            title = item.get('title') or item.get('name')
            raw_price = item.get('price') or item.get('extracted_price') or item.get('converted_price')
            if not raw_price:
                # skip items without price
                continue
            price = None
            try:
                cleaned = re.sub(r'[^0-9.]', '', str(raw_price))
                if cleaned:
                    from decimal import Decimal
                    price = Decimal(cleaned)
            except Exception:
                price = None
            if price is None:
                continue

            store = item.get('source') or item.get('merchant') or item.get('store')
            link = item.get('product_link') or item.get('link') or item.get('url')
            if not (title and store and link):
                continue
            normalized.append({'store': store, 'name': title, 'price': price, 'url': link, 'provider': 'SerpAPI'})

        logger.info('SERPAPI RESULT COUNT %d for query %s', len(normalized), query)
        if not normalized:
            self.last_error = 'SerpAPI returned zero normalized items after filtering'
        return normalized

    # --- HTTP helpers ---
    def _fetch(self, url: str) -> Optional[str]:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.warning("fetch non-200 %s %s", resp.status_code, url)
                return None
            if "captcha" in resp.text.lower() or "robot" in resp.text.lower():
                return None
            return resp.text
        except Exception as exc:
            logger.warning("fetch error %s", exc)
            return None

    def _fetch_selenium(self, url: str) -> Optional[str]:
        try:
            from apps.scraper.stealth_browser import StealthBrowser
        except Exception:
            return None
        try:
            with StealthBrowser() as browser:
                browser.get(url)
                time.sleep(random.uniform(1.2, 2.2))
                return browser.page_source
        except Exception as exc:
            logger.warning("selenium fetch failed %s", exc)
            return None

    # --- Parsing helpers ---
    def _parse_price(self, text: str) -> Optional[Decimal]:
        if not text:
            return None
        cleaned = re.sub(r"[^0-9.]", "", text)
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except Exception:
            return None

    def _best(self, *values):
        for v in values:
            if v:
                return v
        return None

    def search_amazon(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        url = f"https://www.amazon.in/s?k={quote_plus(query)}"
        html = self._fetch(url) or self._fetch_selenium(url)
        if not html:
            self.last_error = "Scraping blocked or no results"
            return []
        soup = BeautifulSoup(html, "html.parser")
        time.sleep(random.uniform(0.4, 1.1))
        cards = soup.select("div.s-result-item[data-component-type='s-search-result']")
        results: List[Dict[str, Any]] = []
        for card in cards[:limit]:
            title_el = card.select_one("h2 a span") or card.select_one("span.a-size-medium.a-color-base.a-text-normal")
            price_el = card.select_one("span.a-price span.a-offscreen") or card.select_one("span.a-price-whole")
            rating_el = card.select_one("span.a-icon-alt")
            link_el = card.select_one("h2 a")
            name = title_el.text.strip() if title_el else None
            price = self._parse_price(price_el.text) if price_el else None
            rating = rating_el.text.strip() if rating_el else None
            href = link_el.get("href") if link_el else None
            if not name or not href:
                continue
            full_url = href if href.startswith("http") else f"https://www.amazon.in{href}"
            results.append({
                "store": "Amazon",
                "name": name,
                "price": price,
                "rating": rating,
                "url": full_url,
            })
        return results

    def search_flipkart(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        url = f"https://www.flipkart.com/search?q={quote_plus(query)}"
        html = self._fetch(url) or self._fetch_selenium(url)
        if not html:
            self.last_error = "Scraping blocked or no results"
            return []
        soup = BeautifulSoup(html, "html.parser")
        time.sleep(random.uniform(0.4, 1.1))
        cards = soup.select("a._1fQZEK") or soup.select("a.s1Q9rs")
        results: List[Dict[str, Any]] = []
        for card in cards[:limit]:
            title_el = card.select_one("div._4rR01T") or card.select_one("div.KzDlHZ") or card
            price_el = card.select_one("div._30jeq3._1_WHN1") or card.select_one("div._30jeq3")
            rating_el = card.select_one("div._3LWZlK")
            name = title_el.text.strip() if title_el else None
            price = self._parse_price(price_el.text) if price_el else None
            rating = rating_el.text.strip() if rating_el else None
            href = card.get("href") if card else None
            if not name or not href:
                continue
            full_url = href if href.startswith("http") else f"https://www.flipkart.com{href}"
            results.append({
                "store": "Flipkart",
                "name": name,
                "price": price,
                "rating": rating,
                "url": full_url,
            })
        return results

    # --- Persistence and fuzzy matching ---
    def _fuzzy_match_product(self, clean_query: str) -> Optional[Product]:
        try:
            from django.contrib.postgres.search import TrigramSimilarity
            qs = (
                Product.objects.annotate(similarity=TrigramSimilarity('name', clean_query))
                .filter(similarity__gte=0.3)
                .order_by('-similarity')
            )
            candidate = qs.first()
            if candidate and candidate.similarity >= 0.7:
                return candidate
        except Exception:
            pass

        # fallback manual
        best = None
        best_score = 0.0
        for prod in Product.objects.order_by('-updated_at')[:20]:
            score = SequenceMatcher(None, prod.name.lower(), clean_query.lower()).ratio()
            if score > best_score:
                best_score = score
                best = prod
        if best_score >= 0.7:
            return best
        return None

    def persist_results(self, clean_query: str, raw_query: str, results: List[Dict[str, Any]]):
        if not results:
            return {'product': None, 'rows': []}

        product = self._fuzzy_match_product(clean_query)
        if not product:
            product = Product.objects.create(name=clean_query or raw_query or "Unknown Product")

        rows = []
        with transaction.atomic():
            for res in results:
                store = res.get('store') or 'Amazon'
                price_val = res.get('price') or Decimal('0')
                url = res.get('url') or ''
                defaults = {
                    'current_price': price_val,
                    'product_url': url,
                    'image_url': res.get('image_url') or '',
                    'is_available': True,
                    'metadata': {'rating': res.get('rating')},
                }
                sp, created = StorePrice.objects.update_or_create(
                    product=product,
                    store_name=store,
                    defaults=defaults,
                )

                # price history with simple delta
                last_price = None
                if not created:
                    last_price = sp.current_price
                PriceHistory.objects.create(
                    store_price=sp,
                    price=price_val,
                    currency='INR',
                    change_percentage=None,
                )
                sp.last_updated = timezone.now()
                sp.save()

            product.update_lowest_price()

        # build rows for table
        price_map = {p.store_name: p.current_price for p in product.prices.all()}
        amz = price_map.get('Amazon')
        flip = price_map.get('Flipkart')
        min_val = min([v for v in [amz, flip] if v is not None], default=None)
        delta_label = 'STABLE_00'
        if amz is not None and flip is not None:
            diff = Decimal(amz) - Decimal(flip)
            if diff != 0:
                prefix = 'DROP' if diff > 0 else 'RISE'
                magnitude = abs(diff)
                magnitude_label = f"{magnitude:.0f}"
                delta_label = f"{prefix}_{magnitude_label}"

        fmt = lambda v: f"₹{int(Decimal(v)):,}" if v is not None else 'N/A'
        row = {
            'id': product.id,
            'name': product.name.upper()[:24],
            'amz': fmt(amz),
            'flip': fmt(flip),
            'min': fmt(min_val),
            'delta': delta_label,
            'status': product.trend_indicator or 'LIVE',
            'url': results[0].get('url') if results else '',
        }
        rows.append(row)
        return {'product': product, 'rows': rows}

    # Compatibility wrapper: allow callers to use `scrape()` like other implementations
    def scrape(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        return self.search_all(query, limit)