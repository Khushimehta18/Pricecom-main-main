import logging
import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

try:  # Optional but recommended SerpAPI client
    from serpapi import GoogleSearch  # type: ignore
except Exception:  # pragma: no cover
    GoogleSearch = None

from apps.scraper.models import PriceHistory, Product, StorePrice

logger = logging.getLogger(__name__)
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"


class ScraperService:
    """SerpAPI-backed scraper for Google Shopping results."""

    def __init__(self) -> None:
        self.api_key = getattr(settings, "SERPAPI_API_KEY", "") or os.getenv("SERPAPI_API_KEY", "")
        self.last_error = ""
        self._events: List[str] = []

    # ------------------------------------------------------------------
    def scrape(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        self.last_error = ""
        self._events.clear()
        if not query:
            self.last_error = "Empty query"
            return []
        if not self.api_key:
            self.last_error = "Missing SERPAPI_API_KEY"
            return []

        self._log_event(f"QUERY: {query}")
        results = self._fetch_serpapi(query, limit)
        self._log_event(f"SERPAPI RESULT COUNT: {len(results)}")
        if not results and not self.last_error:
            self.last_error = "SerpAPI returned no shopping results"
        return results

    def test_scrape(self, query: str = "sony headphones", minimum: int = 3) -> List[Dict[str, Any]]:
        attempts = [query, f"{query} price", f"buy {query}"]
        last: List[Dict[str, Any]] = []
        for attempt in attempts:
            last = self.scrape(attempt, limit=10)
            if len(last) >= minimum:
                return last
        raise RuntimeError(
            f"SerpAPI failed to return {minimum}+ results for '{query}'. Last error: {self.last_error or 'unknown'}"
        )

    def persist_results(self, clean_query: str, raw_query: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not results:
            return {"product": None, "rows": []}

        product_name = clean_query or raw_query or results[0]["name"]
        product, _ = Product.objects.get_or_create(name=product_name)

        with transaction.atomic():
            for item in results:
                store = item.get("store") or "Unknown"
                defaults = {
                    "current_price": item.get("price"),
                    "product_url": item.get("url") or "",
                    "metadata": {
                        "provider": item.get("provider"),
                        "store": store,
                    },
                    "is_available": True,
                }
                store_price, _ = StorePrice.objects.update_or_create(
                    product=product,
                    store_name=store,
                    defaults=defaults,
                )
                PriceHistory.objects.create(
                    store_price=store_price,
                    price=item.get("price"),
                    currency=item.get("currency", "INR"),
                    recorded_at=timezone.now(),
                )
                store_price.last_updated = timezone.now()
                store_price.save(update_fields=["last_updated"])

        if hasattr(product, "update_lowest_price"):
            try:
                product.update_lowest_price()
            except Exception:
                logger.debug("Skipped update_lowest_price for product_id=%s", product.id)

        rows = self._build_rows(product)
        print("PRODUCT COUNT:", Product.objects.count())
        print("STOREPRICE COUNT:", StorePrice.objects.count())
        return {"product": product, "rows": rows}

    def consume_events(self) -> List[str]:
        events = list(self._events)
        self._events.clear()
        return events

    # ------------------------------------------------------------------
    def _fetch_serpapi(self, query: str, limit: int) -> List[Dict[str, Any]]:
        params = {
            "engine": "google_shopping",
            "q": query,
            "api_key": self.api_key,
            "gl": "in",
            "hl": "en",
            "num": max(limit, 5),
        }
        try:
            if GoogleSearch:
                search = GoogleSearch(params)
                data = search.get_dict()
            else:
                resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            self.last_error = f"SerpAPI error: {exc}"
            logger.exception("SerpAPI request failed")
            return []

        normalized: List[Dict[str, Any]] = []
        for item in data.get("shopping_results", []):
            if len(normalized) >= limit:
                break
            title = item.get("title")
            price = self._parse_price(item.get("price"))
            store = item.get("source") or item.get("merchant") or item.get("store")
            link = item.get("product_link") or item.get("link")
            if not (title and price and store and link):
                continue
            normalized.append(
                {
                    "store": store,
                    "name": title,
                    "price": price,
                    "currency": item.get("currency") or self._infer_currency(item.get("price")),
                    "url": link,
                    "provider": "SerpAPI",
                }
            )
        if not normalized:
            self.last_error = "SerpAPI returned zero normalized items"
        return normalized

    @staticmethod
    def _parse_price(raw: Optional[str]) -> Optional[Decimal]:
        if not raw:
            return None
        cleaned = re.sub(r"[^0-9.,]", "", raw)
        cleaned = cleaned.replace(",", "")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except Exception:
            return None

    @staticmethod
    def _infer_currency(raw: Optional[str]) -> str:
        if not raw:
            return "INR"
        raw = raw.upper()
        if "₹" in raw or "INR" in raw:
            return "INR"
        if "$" in raw or "USD" in raw:
            return "USD"
        return "INR"

    def _build_rows(self, product: Product) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        prices = list(product.prices.all())
        if not prices:
            return rows
        price_map = {p.store_name: p.current_price for p in prices}
        amz = price_map.get("Amazon")
        flip = price_map.get("Flipkart")
        numeric = [v for v in price_map.values() if v is not None]
        min_val = min(numeric) if numeric else None
        delta_label = "STABLE_00"
        if amz is not None and flip is not None and amz != flip:
            diff = Decimal(amz) - Decimal(flip)
            prefix = "DROP" if diff > 0 else "RISE"
            delta_label = f"{prefix}_{abs(diff):.0f}"
        rows.append(
            {
                "id": product.id,
                "name": product.name.upper()[:24],
                "amz": self._fmt(amz),
                "flip": self._fmt(flip),
                "min": self._fmt(min_val),
                "delta": delta_label,
                "status": getattr(product, "trend_indicator", None) or "LIVE",
            }
        )
        return rows

    @staticmethod
    def _fmt(value: Optional[Decimal]) -> str:
        if value is None:
            return "N/A"
        try:
            return f"₹{int(Decimal(value)):,}"
        except Exception:
            return "N/A"

    def _log_event(self, message: str) -> None:
        self._events.append(message)
        print(message)
