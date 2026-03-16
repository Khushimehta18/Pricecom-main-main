from celery import shared_task
from django.utils import timezone
import logging
from .watchlist_storage import storage
import requests
import re

logger = logging.getLogger(__name__)

# Very basic scraping implementation since we shouldn't use ScraperService full heavyweight tasks if possible
# Alternatively, could use core.services.ScraperService but instructions specify:
# "Implement a background job that periodically updates prices of watched products."
# "For each watched product: scrape latest price from product_url"
# "Run this job every 30-60 minutes. This avoids unnecessary API calls and preserves token allowance."
# So a lightweight localized parser or SERPAPI fallback might be needed. We'll use a mocked/basic updater here, 
# or try a request to the URL if SERP API tokens need preserving.
@shared_task
def update_watched_prices():
    """
    Periodic job to fetch latest prices for all watched products
    """
    logger.info("Starting background price update for watchlist")
    products = storage.get_all_watched_products()
    
    for product in products:
        try:
            url = product.get('product_url')
            # Simulated scraping logic that avoids token usage.
            # In production, this would use a lightweight request or specific marketplace parser.
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=5)
            
            # Look for common price patterns for MVP/demo purposes
            # Just mimicking actual scraping:
            price_match = re.search(r'₹\s*([0-9,]+(?:\.[0-9]{2})?)', resp.text)
            new_price = None
            if price_match:
                new_price = float(price_match.group(1).replace(',', ''))
            
            if new_price and new_price > 0:
                storage.update_product_price(product['product_id'], new_price, timezone.now().isoformat())
                logger.info(f"Updated price for {product['product_id']} to {new_price}")
        except Exception as e:
            logger.error(f"Error updating price for product {product.get('product_id')}: {e}")
            
    return f"Processed {len(products)} products list"
