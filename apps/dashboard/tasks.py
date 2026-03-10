from __future__ import annotations
import os
import logging
from celery import shared_task
from typing import Dict, Any, Optional

from core.services.query_cleaner import normalize_query
from core.services.ScraperService import ScraperService

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def search_and_scrape(self, query: str) -> Dict[str, Any]:
    """Universal search task: clean text, fuzzy match/create product, scrape, persist, return rows+chart."""
    try:
        cleaned = normalize_query(query)
        clean_q = cleaned.get('clean') or query
        logger.info('CELERY TASK STARTED')
        logger.info('QUERY: %s', clean_q)
        service = ScraperService()
        results = service.scrape(clean_q)
        for event in service.consume_events():
            print(event)
        logger.info('RESULT COUNT: %s', len(results))
        logger.info('RESULT SAMPLE: %s', results[:2])
        if not results:
            error_msg = service.last_error or 'SerpAPI error or no results.'
            return {
                'status': 'FAILURE',
                'error': error_msg,
                'raw_query': cleaned.get('raw'),
                'clean_query': clean_q,
                'results': [],
                'chart': {},
            }

        persisted = service.persist_results(clean_q, cleaned.get('raw'), results)
        try:
            from apps.scraper.models import Product, StorePrice
            print('PRODUCT COUNT:', Product.objects.count())
            print('STOREPRICE COUNT:', StorePrice.objects.count())
        except Exception:
            pass
        product = persisted.get('product')
        rows = persisted.get('rows', [])

        chart = {}
        if product:
            from apps.dashboard.views import _chart_payload
            chart = _chart_payload(product_id=product.id)

        result_payload = {
            'status': 'SUCCESS',
            'raw_query': cleaned.get('raw'),
            'clean_query': clean_q,
            'product_id': product.id if product else None,
            'results': rows,
            'chart': chart,
        }

        # If a task_id was provided in request kwargs, persist payload to Redis for polling
        try:
            task_id = getattr(self.request, 'id', None) or getattr(self.request, 'kwargs', {}).get('task_id')
            if task_id:
                try:
                    import redis, json as _json
                    r = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_timeout=5)
                    r.set(f"pricecom:task:{task_id}", _json.dumps(result_payload, default=str), ex=600)
                    logger.info('Cached results for task_id=%s', task_id)
                except Exception:
                    logger.exception('Failed to cache task results')
        except Exception:
            pass

        return result_payload
    except Exception as exc:
        logger.exception('search_and_scrape failure')
        return {
            'status': 'FAILURE',
            'error': str(exc),
            'raw_query': query,
            'clean_query': query,
            'results': [],
            'chart': {},
        }


@shared_task(bind=True)
def image_search_task(self, temp_path: str, ocr_text: Optional[str] = None) -> Dict[str, Any]:
    """Proxy image search to universal search using OCR text as query."""
    query = ocr_text or os.path.basename(temp_path)
    return search_and_scrape.apply(args=[query]).get()
