import json
import redis
import logging
from typing import List, Dict, Optional
from django.conf import settings
from decimal import Decimal

logger = logging.getLogger(__name__)

class WatchlistStorage:
    """
    Redis-backed persistent storage for Watchlist.
    """
    def __init__(self):
        # Allow connecting via REDIS_URL or fallback to local
        broker_url = getattr(settings, 'CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
        self.client = redis.Redis.from_url(broker_url, decode_responses=True)
        
    def _user_key(self, user_id) -> str:
        return f"watchlist:user:{user_id}"
        
    def _product_key(self, product_id) -> str:
        return f"watchlist:product:{product_id}"

    def get_user_watchlist(self, user_id) -> List[Dict]:
        """Fetch all watched products for a user"""
        try:
            product_ids = self.client.smembers(self._user_key(user_id))
            if not product_ids:
                return []
                
            products = []
            for pid in product_ids:
                product_data = self.get_product(pid)
                if product_data:
                    products.append(product_data)
            return sorted(products, key=lambda x: x.get('last_checked', ''), reverse=True)
        except redis.RedisError as e:
            logger.error(f"Redis fallback fetching watchlist for user {user_id}: {e}")
            return []

    def get_product(self, product_id) -> Optional[Dict]:
        """Fetch a specific watched product's data"""
        try:
            data = self.client.get(self._product_key(product_id))
            if data:
                return json.loads(data)
            return None
        except redis.RedisError as e:
            logger.error(f"Redis error fetching product {product_id}: {e}")
            return None

    def add_product(self, user_id, product_data: dict) -> bool:
        """Add product to global storage and link to user"""
        try:
            product_id = str(product_data['product_id'])
            # Cast decimals to string/float for JSON serialization
            if 'current_price' in product_data and isinstance(product_data['current_price'], Decimal):
                product_data['current_price'] = float(product_data['current_price'])
            if 'lowest_price_seen' in product_data and isinstance(product_data['lowest_price_seen'], Decimal):
                product_data['lowest_price_seen'] = float(product_data['lowest_price_seen'])
                
            # Initialize lowest_price_seen if not present or lower
            existing = self.get_product(product_id)
            if existing:
                # Merge existing lowest price if it's lower
                current_lowest = existing.get('lowest_price_seen', product_data.get('current_price'))
                if product_data.get('current_price') < current_lowest:
                    product_data['lowest_price_seen'] = product_data.get('current_price')
                else:
                    product_data['lowest_price_seen'] = current_lowest
            
            # Store product details globally
            self.client.set(self._product_key(product_id), json.dumps(product_data))
            
            # Link to user
            self.client.sadd(self._user_key(user_id), product_id)
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error adding product for user {user_id}: {e}")
            return False

    def remove_product(self, user_id, product_id) -> bool:
        """Remove product link from user"""
        try:
            product_id = str(product_id)
            self.client.srem(self._user_key(user_id), product_id)
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error removing product {product_id} for user {user_id}: {e}")
            return False

    def get_all_watched_products(self) -> List[Dict]:
        """Used by the background scheduler to fetch all unique watched products"""
        try:
            # Find all keys matching watchlist:product:*
            keys = self.client.keys("watchlist:product:*")
            products = []
            for k in keys:
                data = self.client.get(k)
                if data:
                    products.append(json.loads(data))
            return products
        except redis.RedisError as e:
            logger.error(f"Redis error fetching all watched products: {e}")
            return []

    def update_product_price(self, product_id, new_price, last_checked) -> None:
        """Called by background job to update product pricing"""
        try:
            product_id = str(product_id)
            product = self.get_product(product_id)
            if product:
                new_price = float(new_price)
                if new_price < float(product.get('lowest_price_seen', new_price)):
                    product['lowest_price_seen'] = new_price
                
                # Keep history of previous price if we want to show 'Price Dropped'
                product['previous_price'] = product.get('current_price')
                product['current_price'] = new_price
                product['last_checked'] = last_checked
                
                self.client.set(self._product_key(product_id), json.dumps(product))
        except redis.RedisError as e:
            logger.error(f"Redis error updating product price {product_id}: {e}")

storage = WatchlistStorage()
