import logging
from typing import Dict

from django.utils import timezone
from .watchlist_storage import storage

logger = logging.getLogger(__name__)

class WatchlistService:
    @staticmethod
    def add_to_watchlist(user_id, data: dict) -> dict:
        """
        Validates and adds product data to watchlist storage
        """
        required_fields = ['product_id', 'product_name', 'product_image', 'product_url', 'store', 'current_price']
        for field in required_fields:
            if field not in data:
                return {"success": False, "error": f"Missing required field: {field}"}
                
        # Prep data
        product_data = {
            'product_id': str(data['product_id']),
            'product_name': data['product_name'],
            'product_image': data['product_image'],
            'product_url': data['product_url'],
            'store': data['store'],
            'current_price': float(data['current_price']),
            'lowest_price_seen': float(data['current_price']), # will be merged by storage if exists
            'last_checked': timezone.now().isoformat(),
        }

        metadata: Dict = {}
        alert_price = data.get('alert_price')
        if alert_price not in (None, ''):
            try:
                metadata['alert_price'] = float(alert_price)
                metadata['last_alerted_price'] = None
            except (TypeError, ValueError):
                return {
                    "success": False,
                    "error": "alert_price must be a valid number",
                }

        if metadata.get('alert_price') is not None:
            metadata['notify_email'] = bool(data.get('notify_email', False))
            metadata['alert_enabled'] = True

        success = storage.add_product(user_id, product_data, metadata if metadata else None)
        if success:
            if metadata:
                product_data.update(metadata)
            return {"success": True, "product": product_data}
        return {"success": False, "error": "Storage error"}

    @staticmethod
    def get_watchlist(user_id) -> list:
        return storage.get_user_watchlist(user_id)
        
    @staticmethod
    def remove_from_watchlist(user_id, product_id) -> bool:
        return storage.remove_product(user_id, product_id)
