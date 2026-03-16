from decimal import Decimal

from django.db import transaction

from apps.scraper.models import PriceAlert, Watchlist
from apps.scraper.services.smtp_handler import send_monitored_email

DROP_THRESHOLD = Decimal('10.0')
TARGET_PRIORITY = 'HIGH'


def _create_price_alert(entry, product_url, current_price):
    return PriceAlert.objects.create(
        user=entry.user,
        product_url=product_url,
        target_price=entry.target_price,
        current_price=current_price,
        alert_priority=TARGET_PRIORITY,
        is_triggered=True,
    )


def _log_notification(entry, product, price, alert_type, message):
    subject = f"PriceCom Alert: {product.name} ({alert_type})"
    return send_monitored_email(
        entry.user,
        subject=subject,
        message=message,
        product=product,
        current_price=price,
        alert_type=alert_type,
    )


def evaluate_watchlist_targets(product):
    best_store = product.prices.filter(current_price__isnull=False).order_by('current_price').first()
    if not best_store:
        return

    best_price = best_store.current_price
    if best_price is None:
        return

    is_available = product.prices.filter(is_available=True).exists()
    watchlist_items = Watchlist.objects.filter(product=product).select_related('user')
    if not watchlist_items:
        return

    with transaction.atomic():
        for entry in watchlist_items:
            updated_fields = set()

            previous_price = entry.last_recorded_price
            drop_pct = Decimal('0.00')

            if previous_price and previous_price > best_price:
                drop_pct = ((previous_price - best_price) / previous_price) * Decimal('100.0')
                if drop_pct >= DROP_THRESHOLD:
                    drop_message = (
                        f"Price dropped {round(drop_pct, 2)}% from {previous_price} to {best_price} "
                        f"on {best_store.store_name}."
                    )
                    _log_notification(entry, product, best_price, 'Drop', drop_message)

            # Always refresh last_recorded_price so the next delta comparison uses the latest value
            if entry.last_recorded_price != best_price:
                entry.last_recorded_price = best_price
                updated_fields.add('last_recorded_price')

            if entry.target_price and best_price <= entry.target_price and (
                entry.last_notified_price is None or best_price < entry.last_notified_price
            ):
                _create_price_alert(entry, best_store.product_url, best_price)
                target_message = (
                    f"Target price of {entry.target_price} reached for {product.name}. "
                    f"Current lowest is {best_price} at {best_store.store_name}."
                )
                _log_notification(entry, product, best_price, 'System', target_message)
                entry.last_notified_price = best_price
                updated_fields.add('last_notified_price')
                entry.last_recorded_price = best_price
                updated_fields.add('last_recorded_price')

            if is_available and entry.was_out_of_stock:
                restock_message = (
                    f"{product.name} returned to stock at {best_store.store_name} for {best_price}."
                )
                _log_notification(entry, product, best_price, 'Restock', restock_message)
                entry.was_out_of_stock = False
                updated_fields.add('was_out_of_stock')

            if not is_available and not entry.was_out_of_stock:
                entry.was_out_of_stock = True
                updated_fields.add('was_out_of_stock')

            if updated_fields:
                entry.save(update_fields=list(updated_fields))
*** End Fragment