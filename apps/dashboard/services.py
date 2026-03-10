from typing import Any, Dict, List, Optional
from apps.scraper.models import Product, StorePrice
from .utils_fuzzy_search import best_matches
from django.db import transaction
import logging

logger = logging.getLogger(__name__)


def fuzzy_lookup_and_save(query: str) -> Dict[str, Any]:
    """Try to find matching Product(s) for query and return a canonical product id and candidates.

    If a product is found, return {'product': Product, 'candidates': [...]}
    """
    # Build candidate list from product names and skus
    products = list(Product.objects.filter(is_active=True).only('id', 'name', 'sku'))
    choices = [p.name for p in products]
    matches = best_matches(query, choices, limit=5)
    candidates = []
    for name, score in matches:
        p = next((x for x in products if x.name == name), None)
        if p:
            candidates.append({'id': p.id, 'name': p.name, 'score': float(score)})

    primary = None
    if candidates:
        primary_id = candidates[0]['id']
        primary = Product.objects.filter(id=primary_id).first()

    return {'product': primary, 'candidates': candidates}


def save_storeprice(product_id: int, store_name: str, price: float, url: Optional[str] = None) -> StorePrice:
    """Create or update a StorePrice record for given product and store."""
    with transaction.atomic():
        sp, created = StorePrice.objects.select_for_update().get_or_create(
            product_id=product_id, store_name=store_name,
            defaults={'current_price': price, 'product_url': url or ''}
        )
        if not created:
            sp.current_price = price
            if url:
                sp.product_url = url
            sp.save()
    logger.info('Saved StorePrice product=%s store=%s price=%s', product_id, store_name, price)
    return sp
from typing import List, Dict, Any

class MatrixConstructor:
    """
    Data Flattening Engine.
    Takes nested or heterogeneous inputs and strictly builds the 
    Actionable Intelligence Grid structure to prevent frontend errors.
    """

    @staticmethod
    def null_safety_handler(store_name: str) -> Dict[str, Any]:
        """
        The Availability Guard: 
        Ensures a structurally sound table row even if the Scraper failed or item is OOS.
        """
        return {
            'store': store_name,
            'price': 'N/A',
            'status': 'Notify Me',
            'is_available': False,
            'is_best_deal': False,
            'recommendation_text': ''
        }
        
    @staticmethod
    def build_intelligence_matrix(grouped_products: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """
        Matrix Flattening context constructor.
        Takes Semantic Matches and maps them to ['Amazon', 'Flipkart'] columns.
        """
        unified_matrix = []
        
        target_stores = ['Amazon', 'Flipkart']
        
        for group in grouped_products:
            # Determine the Primary Row Key
            primary_name = group[0].get('title', 'Unknown Value')
            
            store_parallel_data = []
            
            # Map known stores
            for store in target_stores:
                # Find product for this store
                store_item = next((item for item in group if store.lower() in str(item.get('store', '')).lower()), None)
                
                if store_item:
                    # Sanitize object for dashboard view
                    store_item['is_available'] = True
                    store_item['status'] = 'In Stock'
                    store_parallel_data.append(store_item)
                else:
                    # Implement safe null placeholders
                    store_parallel_data.append(MatrixConstructor.null_safety_handler(store))
                    
            unified_matrix.append({
                'common_product_name': primary_name,
                'store_data_list': store_parallel_data
            })
            
        return unified_matrix
