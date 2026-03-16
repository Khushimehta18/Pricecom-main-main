from django.utils import timezone
from .watchlist_storage import storage
import logging

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
        
        success = storage.add_product(user_id, product_data)
        if success:
            return {"success": True, "product": product_data}
        return {"success": False, "error": "Storage error"}

    @staticmethod
    def get_watchlist(user_id) -> list:
        return storage.get_user_watchlist(user_id)
        
    @staticmethod
    def remove_from_watchlist(user_id, product_id) -> bool:
        return storage.remove_product(user_id, product_id)
