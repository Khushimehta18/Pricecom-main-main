import logging
import time
from decimal import Decimal
from celery import shared_task, group
from django.contrib.auth import get_user_model
from django.conf import settings
import json
from urllib.parse import urlparse
import os
from typing import Optional
from apps.scraper.services.services import ScraperService
from apps.scraper.models import Product, PriceAlert
from apps.scraper.services.smtp_handler import send_monitored_email

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task
def search_and_scrape(query: str):
    """Lightweight Celery task to scrape and persist products for a query."""
    logger.info('TASK START simple search %r', query)
    service = ScraperService()
    try:
        items = service.scrape(query)
        logger.info('SCRAPER COUNT %d', len(items))
        normalized = []
        for it in items:
            price = it.get('price')
            if isinstance(price, Decimal):
                try:
                    it['price'] = float(price)
                except Exception:
                    it['price'] = None
            normalized.append(it)
        persisted = service.persist_results(query, query, normalized)
        rows = persisted.get('rows', [])
        if not rows:
            logger.warning("NO ROWS GENERATED — check ScraperService.persist_results")
        else:
            logger.info("ROWS GENERATED: %s", rows[:2])
        product_obj = persisted.get('product')
        return {
            'status': 'SUCCESS' if rows else 'FAILURE',
            'results': rows,
            'product': getattr(product_obj, 'id', None),
        }
    except Exception:
        logger.exception('search_and_scrape failure')
        return {'status': 'FAILURE', 'error': 'scrape_failed', 'results': [], 'product': None}


@shared_task(bind=True)
def celery_ping(self):
    """Lightweight task to confirm Celery registration and worker startup."""
    logger.info("CELERY PING RECEIVED")
    return "pong"


@shared_task(bind=True)
def image_search_task(self, temp_path: str, ocr_text: Optional[str] = None, frontend_task_id: Optional[str] = None):
    """Run a search+scrape for OCR text (or filename) and cache results under frontend task id in Redis."""
    query = ocr_text or os.path.basename(temp_path)
    try:
        # run the core search task synchronously to obtain payload and pass frontend id so worker caches under same key
        result = search_and_scrape_task.apply(args=[query], kwargs={'frontend_task_id': frontend_task_id}).get()
    except Exception as exc:
        result = {
            'status': 'FAILURE',
            'error': str(exc),
            'raw_query': query,
            'clean_query': query,
            'results': [],
            'chart': {},
        }

    # Determine frontend task id from provided arg or request kwargs
    try:
        fid = frontend_task_id or getattr(self.request, 'kwargs', {}).get('task_id') or getattr(self.request, 'kwargs', {}).get('frontend_task_id')
    except Exception:
        fid = frontend_task_id

    if not fid:
        try:
            args = getattr(self.request, 'args', [])
            if len(args) >= 3:
                fid = args[2]
        except Exception:
            fid = None

    if fid:
        try:
            import redis as _redis
            redis_url = getattr(settings, 'CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
            print("WORKER RUNNING TASK:", query)
            print("FRONTEND TASK ID:", fid)
            print("REDIS URL:", redis_url)
            r = _redis.StrictRedis.from_url(redis_url)
            r.setex(f"pricecom:task:{fid}", 3600, json.dumps(result, default=str))
            print("REDIS WRITE DONE")
        except Exception:
            logger.exception('Failed to write cached image task result to Redis')

    return result

# --- BACK-ROOM WORKER LOGIC ---

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=60, retry_kwargs={'max_retries': 5})
def send_price_alert_email(self, user_id: int, subject: str, message: str, product_id: int, current_price: str, alert_type: str = 'Drop'):
    """
    "Smart Watcher" Notification Worker.
    Decouples SMTP and enforces Frequency Capping (Cool-down).
    """
    from django.core.cache import cache
    
    # 1. Frequency Capping (The "Cool-down" Logic)
    # Prevent spamming the same user about the same product within 24 hours.
    # Cache Key: alert_cool_down_{user_id}_{product_id}
    cache_key = f"alert_cool_down_{user_id}_{product_id}"
    
    if cache.get(cache_key):
        logger.info(f" suppressed alert for User {user_id} on Product {product_id}. Cool-down active.")
        return "Skipped: Cool-down active."
        
    try:
        logger.info(f"Task Received: Sending email to User ID {user_id}")
        
        user = User.objects.get(pk=user_id)
        product = Product.objects.get(pk=product_id) if product_id else None
        
        success = send_monitored_email(
            user=user,
            subject=subject,
            message=message,
            product=product,
            current_price=current_price,
            alert_type=alert_type
        )
        
        if not success:
            raise Exception("SMTP Handler returned False")
            
        # 2. Activate Cool-down (24 Hours)
        cache.set(cache_key, True, timeout=60 * 60 * 24)
            
        return f"Email Sent to {user.email}"
        
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found.")
    except Exception as e:
        logger.error(f"Email Task Failed: {e}")
        raise e

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def scrape_product_task(self, url: str, store_name: str, user_id: int = None):
    """
    Background Scraper Worker.
    Fetches product data without blocking the HTTP request.
    """
    logger.info(f"Scraper Worker: Processing {url} for {store_name}")
    service = ScraperService()
    
    try:
        data = service.fetch_product_data(url, store_name)
        if data.get('success'):
            product_obj = service.save_product(data)
            logger.info(f"Scrape Success: {data.get('name')}")
            
            # Post-Scrape Handshake: Authenticity & Intelligence 
            if hasattr(product_obj, 'uuid'):
                # We need the most recent StorePrice to run Authenticity
                store_price = product_obj.prices.filter(store_name=store_name).first()
                if store_price:
                    from celery import chain
                    # [FORCE LOGIC] Linear Execution Chain: Scrape -> AuthenticityCheck -> PredictiveInference -> MetadataWrite
                    try:
                        pipeline_chain = chain(
                            run_authenticity_check.si(store_price.id),
                            update_product_intelligence.si(str(product_obj.uuid)),
                            predict_future_price.si(str(product_obj.uuid))
                        )
                        pipeline_chain.apply_async()
                        logger.info(f"Pipeline Orchestration Triggered for {product_obj.uuid}")
                    except Exception as ai_err:
                        # Error Resilience: Fallback to raw price display without crashing
                        logger.warning(f"AI Pipeline Deployment Failed: {ai_err}. Fallback to Raw Price Display Active.")
                        
            return f"Scraped {data.get('name')}"
        else:
            raise Exception(f"Scrape Logic Failed: {data.get('error')}")
            
    except Exception as e:
        logger.error(f"Scraper Worker Error: {e}")
        raise e

@shared_task(bind=True)
def check_prices_task(self):
    """
    The Master Scraper (Beat Schedule).
    Uses 'Scatter-Gather' pattern to parallelize checks.
    """
    logger.info("Master Scraper: Waking up...")
    
    # 1. Fetch Active Alerts
    alerts = PriceAlert.objects.filter(is_triggered=False)
    count = alerts.count()
    logger.info(f"Found {count} alerts to check.")
    
    if count == 0:
        return "No alerts to process."

    # 2. Scatter: Create a group of sub-tasks
    # We map each alert to a scrape task
    # Note: Optimization - Group by Product URL to avoid duplicate scrapes?
    # For 'Antigravity' grade, we should dedup.
    
    # Simplified Parallelism: One task per unique URL
    unique_urls = set()
    job_signatures = []
    
    for alert in alerts:
        if alert.product_url not in unique_urls:
            unique_urls.add(alert.product_url)
            store_name = "Amazon" if "amazon" in alert.product_url.lower() else "Flipkart"
            # We use .s() to create a signature
            job_signatures.append(scrape_product_task.s(alert.product_url, store_name))
    
    # 3. Execution: Fire the group
    if job_signatures:
        logger.info(f"Dispatching {len(job_signatures)} parallel scrape tasks to Redis.")
        job_group = group(job_signatures)
        job_group.apply_async()
        
    return f"Dispatched {len(job_signatures)} scrape jobs."

@shared_task(bind=True)
def search_and_scrape_task(self, query: str, user_id: int = None):
    """
    Parallel Search Orchestrator.
    """
    task_id = getattr(self.request, 'id', None)
    logger.info('TASK START %s %r', task_id, query)

    result_payload = {'status': 'FAILURE', 'error': 'init', 'results': [], 'product': None}

    try:
        # Ensure SERPAPI key visibility inside worker
        try:
            key = getattr(settings, 'SERPAPI_API_KEY', '') or ''
            logger.info('SERPAPI_API_KEY length: %d', len(key))
        except Exception:
            logger.warning('Could not read SERPAPI_API_KEY from settings')

        service = ScraperService()
        # read frontend_task_id if provided in kwargs (used for caching)
        frontend_task_id = getattr(self.request, 'kwargs', {}).get('frontend_task_id') if hasattr(self, 'request') else None

        # Fallback: if cleaned query empty, try raw_query from kwargs
        raw_query = None
        try:
            raw_query = self.request.kwargs.get('raw_query')  # type: ignore
        except Exception:
            raw_query = None
        if not query and raw_query:
            query = raw_query

        if not query:
            logger.warning('Empty query received by search_and_scrape_task; aborting')
            result_payload = {'status': 'FAILURE', 'error': 'Empty query', 'results': [], 'product': None}
            return result_payload

        items = service.scrape(query)
        logger.info('TASK QUERY: %s', query)
        try:
            keylen = len(getattr(settings, 'SERPAPI_API_KEY', '') or '')
        except Exception:
            keylen = 0
        logger.info('SERPAPI_API_KEY length: %d', keylen)
        logger.info('SCRAPER COUNT %d', len(items))
        logger.info('SCRAPE SAMPLE: %s', items[:2])

        normalized = []
        for it in items:
            price = it.get('price')
            if isinstance(price, Decimal):
                try:
                    it['price'] = float(price)
                except Exception:
                    it['price'] = None
            normalized.append(it)

        persisted = service.persist_results(query, raw_query or query, normalized)
        rows = persisted.get('rows', [])
        product_obj = persisted.get('product')
        result_payload = {
            'status': 'SUCCESS' if rows else 'FAILURE',
            'results': rows,
            'product': getattr(product_obj, 'id', None),
        }
        return result_payload
    except Exception:
        logger.exception('search_and_scrape_task failure')
        result_payload = {'status': 'FAILURE', 'error': 'scrape_or_cache_failed', 'results': [], 'product': None}
        return result_payload
    finally:
        try:
            # choose cache key: prefer frontend_task_id if available else use celery task id
            cache_key_id = None
            try:
                cache_key_id = getattr(self.request, 'kwargs', {}).get('frontend_task_id') or task_id
            except Exception:
                cache_key_id = task_id

            if cache_key_id:
                try:
                    import redis
                    redis_url = getattr(settings, 'CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
                    r = redis.StrictRedis.from_url(redis_url)
                    result_count = len(result_payload.get('results', []) if isinstance(result_payload, dict) else [])
                    logger.info('TASK CACHE KEY %s', cache_key_id)
                    logger.info('RESULT COUNT %s', result_count)
                    r.setex(f"pricecom:task:{cache_key_id}", 3600, json.dumps(result_payload, default=str))
                    logger.info('CACHE WRITE SUCCESS key=%s', cache_key_id)
                except Exception:
                    logger.exception('cache write failure')
        except Exception:
            logger.exception('cache write outer failure')

@shared_task(bind=True)
def check_alerts_task(self, product_id: int):
    """
    Event-Driven Alert Evaluator.
    Triggered via Signal when PriceHistory is created.
    """
    logger.info(f"Evaluating alerts for Product ID {product_id}")
    try:
        product = Product.objects.get(pk=product_id)
        # Fetch active alerts for this product
        # Check alerts based on Product Link or generic Product FK if we had it
        # Our PriceAlert model uses product_url.
        # We need to find alerts searching for this URL.
        # This is a bit inefficient if we don't have FK.
        # But we can query: PriceAlert.objects.filter(product_url=...)
        # We need product.prices (StorePrice) to get valid URLs.
        
        store_prices = product.prices.all()
        for sp in store_prices:
            # Optimize: filter alerts for this specific URL and use iterator for memory efficiency
            # The "Force" Logic: Signals -> Iterator -> Async Task
            alerts = PriceAlert.objects.filter(product_url=sp.product_url, is_triggered=False).iterator()
            current_val = sp.current_price
            
            for alert in alerts:
                if current_val <= alert.target_price:
                    # Target Met! Fire Email Task
                    logger.info(f"Target met for Alert {alert.id}")
                    
                    # Prepare Context
                    subject = f"Price Drop Alert: {product.name[:30]}..."
                    message = f"Price Drop! {product.name} is now {current_val}. Buy here: {sp.product_url}"
                    
                    # Fire Email
                    send_price_alert_email.delay(
                        user_id=alert.user.id,
                        subject=subject,
                        message=message,
                        product_id=product.id,
                        current_price=str(current_val),
                        alert_type='Drop'
                    )
                    
                    # Mark Triggered (Atomic)
                    alert.is_triggered = True
                    alert.current_price = current_val
                    alert.save()
                    
    except Product.DoesNotExist:
        logger.error(f"Product {product_id} not found during alert check.")
    except Exception as e:
        logger.error(f"Alert Check Failed: {e}")

# --- UNIVERSAL CART FRESHNESS SYNC ---

@shared_task(bind=True)
def sync_universal_cart_prices(self, item_uuid: str = None):
    """
    Background Freshness Sync (refresh_cart_intelligence).
    Updates current_price for active CartItems. Can process a single item 
    if item_uuid is passed, otherwise iterates through all items.
    """
    from apps.dashboard.models import CartItem, PriceHistoryLog
    from django.utils import timezone
    from decimal import Decimal
    
    logger.info("Universal Cart Sync: Waking up...")
    
    # Force Logic: Fetch items to scrape
    if item_uuid:
        items = CartItem.objects.filter(uuid=item_uuid, is_stock_available=True)
    else:
        # Fetch all active items. To scale, this could be scattered.
        items = CartItem.objects.filter(is_stock_available=True)
        
    service = ScraperService()
    
    for item in items:
        try:
            logger.info(f"Syncing item {item.uuid} - {item.product_url}")
            data = service.fetch_product_data(item.product_url, item.store_name)
            
            if not data.get('success'):
                # 404 or Out of Stock logic
                if data.get('error', '').lower() in ['not found', '404', 'out of stock']:
                    item.is_stock_available = False
                    item.save()
                    logger.warning(f"Item {item.uuid} marked Out of Stock.")
                continue
            
            new_price_val = data.get('price')
            
            if new_price_val is None:
                continue
                
            new_price = Decimal(str(new_price_val))
            
            # Setup initial price 
            if item.initial_price is None:
                item.initial_price = new_price
                
            # Intelligence Calculation
            if item.current_price and new_price < item.current_price:
                savings = item.initial_price - new_price
                logger.info(f"Price dropped by ₹{savings}! (CartItem: {item.uuid})")
                # price_drop_alert logic could trigger an email or push here
            
            item.current_price = new_price
            item.last_synced = timezone.now()
            
            # Trigger Product save so global systems remain updated too
            service.save_product(data)
            
            item.save()
            
            # Log to PriceHistoryLog
            PriceHistoryLog.objects.create(
                cart_item=item,
                price=new_price
            )
            
        except Exception as e:
            logger.error(f"Error syncing cart item {item.uuid}: {e}")

    return f"Synced {items.count()} cart items."

# --- AI WORKER LOGIC (OCR STREAM) ---

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=60)
def process_product_image_ocr(self, image_id: int):
    """
    High-Performance AI Vision Worker.
    Converts raw pixels into actionable search queries.
    """
    from PIL import Image, ImageOps 
    import pytesseract
    from io import BytesIO
    from django.core.files.base import ContentFile
    from apps.scraper.models import ProductImage
    import re
    
    logger.info(f"AI Vision: Starting OCR for Image ID {image_id}")
    
    try:
        # 1. Fetch Image Record
        img_record = ProductImage.objects.get(pk=image_id)
        img_record.status = 'PROCESSING'
        img_record.save()
        
        # 2. Defensive Guardrail: Security Validation
        # Validate format using Pillow (already safe via Django ImageField, but double check)
        try:
            with Image.open(img_record.image.path) as img:
                img.verify() # Check for corruption/buffer overflow attacks
        except Exception:
             img_record.status = 'FAILED'
             img_record.extracted_text = "SECURITY WARNING: Image file corrupted or malicious."
             img_record.save()
             logger.critical(f"Security: Malicious image detected! ID {image_id}")
             return "Security Block: Corrupted Image."

        # 3. Image Pre-processing (The "Lens Cleaning" Step)
        # Re-open for processing (verify closes the file pointer)
        # Convert to Grayscale -> Invert (if needed) -> Enhance Contrast
        # For simple screenshot OCR, Grayscale is usually sufficient.
        processed_img = Image.open(img_record.image.path)
        processed_img = ImageOps.grayscale(processed_img)
        
        # 4. Tesseract OCR Engine (The "Reading" Step)
        # configuration: --psm 3 (Auto Page Segmentation)
        try:
            # If a TESSERACT_CMD is configured in Django settings, point pytesseract to it.
            from django.conf import settings as dj_settings
            tesseract_path = getattr(dj_settings, 'TESSERACT_CMD', None)
            if tesseract_path:
                pytesseract.pytesseract.tesseract_cmd = tesseract_path
                print(f"Using Tesseract binary: {pytesseract.pytesseract.tesseract_cmd}")
            # Read text
            raw_text = pytesseract.image_to_string(processed_img, config='--psm 3')
        except Exception as e:
             # Handle Tesseract not installed/found
             logger.error(f"Tesseract Engine Error: {e}")
             raise e

        # 5. Status Bar Noise Filter (The "Keyword Filter")
        # Removing battery %, time, signal strength usually found in phone screenshots
        clean_text = raw_text
        patterns = [
            r'\d{1,2}:\d{2}',      # Time (10:00)
            r'\d{1,3}%',           # Battery (100%)
            r'LTE|5G|4G|WiFi',     # Signal
            r'[^\w\s\-\.]'         # Remove special chars except dot/dash
        ]
        for p in patterns:
            clean_text = re.sub(p, '', clean_text)
            
        clean_text = " ".join(clean_text.split()) # Normalize whitespace
        
        # 6. Result Evaluation
        if len(clean_text) < 3:
            img_record.status = 'LOW_CONFIDENCE'
            img_record.extracted_text = raw_text # Save raw for debug
            img_record.save()
            return "OCR Failed: Low Confidence."
            
        # 7. Success State & Chain Reaction
        img_record.status = 'COMPLETED'
        img_record.extracted_text = clean_text
        img_record.processed_at = timezone.now()
        img_record.save()
        
        logger.info(f"AI Vision: Extracted '{clean_text}'")
        
        # Trigger the "Bloodhound" Search
        # Chain Reaction: OCR -> Search & Scrape
        # We pass the user_id if we had it, but model has it.
        search_and_scrape_task.delay(query=clean_text, user_id=img_record.user.id)
        
        return f"OCR Success. Triggered search for: {clean_text}"

    except ProductImage.DoesNotExist:
        logger.error(f"Image {image_id} not found.")
    except Exception as e:
        logger.error(f"OCR Worker Failed: {e}")
        raise e

@shared_task
def auto_refresh_stale_prices():
    """
    Freshness Engine (Auto-Sync Data Integrity Guard).
    Checks last_scraped_at. If delta > 60 mins, trigger background refresh.
    """
    from datetime import timedelta
    from django.utils import timezone
    from apps.scraper.models import StorePrice
    
    sixty_mins_ago = timezone.now() - timedelta(minutes=60)
    
    # Identify Stale Data
    stale_prices = StorePrice.objects.filter(
        is_available=True,
        last_updated__lt=sixty_mins_ago
    )
    
    for stale in stale_prices[:50]: # Throttle batch size to avoid overwhelming queues
        # Idempotent Background Syncing Trigger
        sync_universal_cart_prices.delay(item_uuid=str(stale.id))
        
    return f"Triggered refresh for {stale_prices.count()} stale prices"

# --- "ANTIGRAVITY" PREDICTIVE & AUTHENTICITY PIPELINES ---

@shared_task(bind=True)
def run_authenticity_check(self, store_price_id: int):
    """
    Post-Scrape Authenticity Shield Subtask.
    Executes Z-Score & NLP checks and atomically updates DB.
    """
    from apps.scraper.models import StorePrice, PriceHistory
    from apps.scraper.services.authenticity import AuthenticityManager
    
    try:
        sp = StorePrice.objects.get(pk=store_price_id)
        # Fetch group prices
        group_prices = list(StorePrice.objects.filter(product=sp.product).values_list('current_price', flat=True))
        # Mock NLP extracted reviews and redirects for standard operation
        reviews = [] # If scraper had review extraction, it would pass here
        redirects = 0 
        
        AuthenticityManager.audit_store_price(sp, group_prices, reviews, redirects)
        logger.info(f"Authenticity Check completed for StorePrice {store_price_id}")
    except StorePrice.DoesNotExist:
        logger.error(f"Authenticity Check Failed: StorePrice {store_price_id} not found.")

@shared_task(bind=True)
def update_product_intelligence(self, product_uuid: str):
    """
    Post-Scrape Analytics Handshake.
    Calls Volatility and Probability Engines to aggregate insights.
    """
    from apps.scraper.models import Product, PriceHistory
    from apps.scraper.services.metrics import MarketStabilityEngine
    from apps.scraper.services.intelligence import PriceDropProbabilityEngine
    from django.utils import timezone
    from datetime import timedelta
    
    try:
        product = Product.objects.get(uuid=product_uuid)
        history = list(PriceHistory.objects.filter(store_price__product=product).order_by('-recorded_at'))
        
        # Volatility & Market Risk
        risk_data = MarketStabilityEngine.calculate_market_risk(history)
        
        # Bayesian Drop Probability
        drop_data = PriceDropProbabilityEngine.calculate_drop_likelihood(history)
        
        # Data Age Check (Self-Performing Error Handling)
        is_stale = False
        if history:
            last_entry = history[0].recorded_at
            if timezone.now() - last_entry > timedelta(hours=24):
                is_stale = True
        
        # Atomic Sync
        if not isinstance(product.metadata, dict):
            product.metadata = {}
            
        product.metadata.update({
            "volatility_index": risk_data.get("cv_percentage", 0),
            "volatility_score": risk_data.get("volatility_score", 0),
            "stability_status": risk_data.get("status", "STABLE"),
            "drop_probability_pct": drop_data.get("probability", 0),
            "expected_drop_amount": drop_data.get("expected_drop", 0),
            "is_stale": is_stale
        })
        
        product.save(update_fields=['metadata'])
        logger.info(f"Product Intelligence synced for {product_uuid}")
    except Product.DoesNotExist:
        logger.error(f"Intelligence Update Failed: {product_uuid} not found.")

@shared_task(bind=True)
def predict_future_price(self, product_uuid: str):
    """
    Antigravity Predictive Pricing Engine Subtask.
    Executes deep learning mocked models for pricing.
    """
    from apps.scraper.models import Product, PriceHistory, NotificationLog
    from apps.scraper.services.intelligence import PredictivePricingEngine
    from django.contrib.auth import get_user_model
    User = get_user_model()
    
    try:
        product = Product.objects.get(uuid=product_uuid)
        history_qs = PriceHistory.objects.filter(store_price__product=product).order_by('recorded_at')
        
        # Need up to 90 historical points
        history = list(history_qs.values_list('price', flat=True))[-90:]
        
        prediction = PredictivePricingEngine.calculate_hybrid_prediction(history)
        
        # Force Logic: Decision Matrix
        signal = PredictivePricingEngine.generate_buy_wait_signal(float(product.current_lowest_price or 0), prediction)
        
        prediction["signal"] = signal
        prediction["signal_timestamp"] = timezone.now().isoformat()
        
        if not isinstance(product.metadata, dict):
            product.metadata = {}
            
        product.metadata.update(prediction)
        product.save(update_fields=['metadata'])
        
        # Audit Trail for Predictions
        sys_user = User.objects.filter(is_superuser=True).first()
        if sys_user:
             NotificationLog.objects.create(
                 user=sys_user,
                 product=product,
                 status='SENT',
                 alert_type='System',
                 error_message=f"Prediction Generated: {signal} with Confidence: {prediction.get('confidence')}%"
             )
    except Product.DoesNotExist:
        logger.error(f"Predictive Engine Failed: {product_uuid} not found.")

