from decimal import Decimal
import logging
import re

from celery import shared_task
from django.utils import timezone
import requests

from apps.scraper.tasks import send_price_alert_email
from .watchlist_storage import storage

logger = logging.getLogger(__name__)

def _evaluate_alerts_for_product(product: dict, current_price: float) -> None:
    if current_price is None:
        return
    product_id = str(product.get('product_id'))
    if not product_id:
        return

    try:
        price_decimal = Decimal(str(current_price))
    except Exception:
        return

    users = storage.get_users_for_product(product_id)
    if not users:
        return

    product_name = product.get('product_name', 'Item')
    product_url = product.get('product_url') or 'Link unavailable'

    for user_id in users:
        metadata = storage.get_user_product_metadata(user_id, product_id)
        alert_price = metadata.get('alert_price')
        if alert_price is None:
            continue
        try:
            target_price = Decimal(str(alert_price))
        except Exception:
            continue

        last_alerted = metadata.get('last_alerted_price')
        last_alerted_decimal = None
        if last_alerted not in (None, ''):
            try:
                last_alerted_decimal = Decimal(str(last_alerted))
            except Exception:
                last_alerted_decimal = None

        if price_decimal > target_price:
            if last_alerted_decimal is not None:
                storage.update_user_product_metadata(user_id, product_id, {'last_alerted_price': None})
            continue

        if last_alerted_decimal is not None and price_decimal >= last_alerted_decimal:
            continue

        if metadata.get('notify_email'):
            subject = f"Watchlist Alert: {product_name} fell below ₹{target_price:.2f}"
            message = (
                f"{product_name} is now ₹{price_decimal:.2f}. "
                f"Target was ₹{target_price:.2f}. Purchase: {product_url}"
            )
            try:
                send_price_alert_email.delay(
                    user_id=user_id,
                    subject=subject,
                    message=message,
                    product_id=product_id,
                    current_price=str(price_decimal),
                    alert_type='Watchlist',
                )
                logger.info('Queued watchlist alert email for user %s, product %s', user_id, product_id)
            except Exception:
                logger.exception('Failed to queue watchlist email for user %s', user_id)

        storage.update_user_product_metadata(user_id, product_id, {'last_alerted_price': float(price_decimal)})

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
                _evaluate_alerts_for_product(product, new_price)
                logger.info(f"Updated price for {product['product_id']} to {new_price}")
        except Exception as e:
            logger.error(f"Error updating price for product {product.get('product_id')}: {e}")
            
    return f"Processed {len(products)} products list"
