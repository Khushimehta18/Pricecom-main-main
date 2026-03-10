import os, sys, time
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
try:
    django.setup()
except Exception as e:
    print('DJANGO_SETUP_ERROR', type(e).__name__, e)
    sys.exit(2)

try:
    from apps.scraper.tasks import celery_ping, search_and_scrape_task
except Exception as e:
    print('IMPORT_TASKS_ERROR', type(e).__name__, e)
    sys.exit(3)

print('Dispatching celery_ping...')
try:
    r = celery_ping.delay()
    print('PING_TASK_ID', getattr(r, 'id', None))
    try:
        res = r.get(timeout=15)
        print('PING_RESULT', res)
    except Exception as e:
        print('PING_GET_ERROR', type(e).__name__, e)
except Exception as e:
    print('PING_DISPATCH_ERROR', type(e).__name__, e)

print('Dispatching search_and_scrape_task for "iphone 14"...')
try:
    s = search_and_scrape_task.delay('iphone 14')
    print('SEARCH_TASK_ID', getattr(s, 'id', None))
    try:
        data = s.get(timeout=60)
        print('SEARCH_STATUS', data.get('status'))
        print('SEARCH_PRODUCT_ID', data.get('product_id'))
        print('SEARCH_RESULTS_COUNT', len(data.get('results', [])))
    except Exception as e:
        print('SEARCH_GET_ERROR', type(e).__name__, e)
except Exception as e:
    print('SEARCH_DISPATCH_ERROR', type(e).__name__, e)

print('Done')
