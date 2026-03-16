from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.scraper.models import (
    NotificationLog,
    PriceAlert,
    Product,
    StorePrice,
    Watchlist,
)


User = get_user_model()


class WatchlistAlertPipelineTest(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="tester@example.com", password="pass1234")
        self.product = Product.objects.create(
            name="Test Widget",
            base_price=Decimal('120.00'),
            current_lowest_price=Decimal('120.00'),
        )
        self.store_price = StorePrice.objects.create(
            product=self.product,
            store_name="Amazon",
            current_price=Decimal('120.00'),
            product_url="https://example.com/product",
            is_available=True,
        )
        self.watchlist = Watchlist.objects.create(
            user=self.user,
            product=self.product,
            added_price=Decimal('120.00'),
            last_recorded_price=Decimal('120.00'),
            target_price=Decimal('80.00'),
            was_out_of_stock=False,
        )

    def test_price_drop_and_target_alerts_triggered(self):
        # Simulate the scraper detection of a new lower price
        self.store_price.current_price = Decimal('75.00')
        self.store_price.save()

        self.watchlist.refresh_from_db()
        self.product.refresh_from_db()

        alerts = PriceAlert.objects.filter(user=self.user)
        logs = NotificationLog.objects.filter(user=self.user, product=self.product)

        self.assertEqual(alerts.count(), 1)
        self.assertEqual(logs.filter(alert_type='System').count(), 1)
        self.assertEqual(logs.filter(alert_type='Drop').count(), 1)

        alert = alerts.first()
        self.assertEqual(alert.target_price, Decimal('80.00'))
        self.assertEqual(alert.current_price, Decimal('75.00'))
        self.assertTrue(alert.is_triggered)

        self.assertEqual(self.watchlist.last_notified_price, Decimal('75.00'))
        self.assertEqual(self.watchlist.last_recorded_price, Decimal('75.00'))
        self.assertFalse(self.watchlist.was_out_of_stock)