import os, json, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.conf import settings
print('Django settings loaded. CELERY_BROKER_URL=', settings.CELERY_BROKER_URL)

# Attempt to import redis
try:
    import redis
except Exception as e:
    print('REDIS_IMPORT_ERROR', str(e))
    sys.exit(2)

# determine redis URL from settings.CELERY_BROKER_URL
from urllib.parse import urlparse
url = settings.CELERY_BROKER_URL
p = urlparse(url)
host = p.hostname or '127.0.0.1'
port = p.port or 6379
db = 0
if p.path:
    try:
        db = int(p.path.replace('/', ''))
    except Exception:
        db = 0

print('Connecting to Redis', host, port, 'db', db)
try:
    r = redis.Redis(host=host, port=port, db=db, socket_timeout=5)
    keys = list(r.scan_iter(match='pricecom:task:*', count=100))
    print('FOUND_KEYS_COUNT', len(keys))
    if keys:
        key = keys[0].decode() if isinstance(keys[0], bytes) else keys[0]
        print('SAMPLE_KEY', key)
        raw = r.get(key)
        if raw:
            try:
                payload = json.loads(raw)
                print('SAMPLE_PAYLOAD_KEYS', list(payload.keys()))
                # check results
                results = payload.get('results', [])
                print('SAMPLE_RESULTS_COUNT', len(results))
                if len(results):
                    print('SAMPLE_PRODUCT_0', results[0])
            except Exception as e:
                print('SAMPLE_PAYLOAD_PARSE_ERROR', str(e))
    else:
        print('NO_CACHE_KEYS')
except Exception as e:
    print('REDIS_CONNECT_ERROR', str(e))

# If a key exists, call the endpoint via Django test client for the first key's task_id
from django.test import Client
c = Client()
if 'key' in locals() and key:
    # extract task_id after last colon
    task_id = key.split(':')[-1]
    print('TESTING_ENDPOINT_FOR_TASK', task_id)
    r = c.get(f'/dashboard/api/result/{task_id}/', HTTP_HX_REQUEST='true')
    print('ENDPOINT_STATUS', r.status_code)
    content = r.content.decode('utf-8')
    print('ENDPOINT_CONTENT_SNIPPET', content[:300].replace('\n',' '))
else:
    print('SKIP_ENDPOINT_NO_KEY')

# Now run mock insertion test
mock_task = 'MOCK-TEST-1'
mock_key = f'pricecom:task:{mock_task}'
mock_payload = {
    'status': 'SUCCESS',
    'results': [
        {'name': 'FAKE PROD A', 'price': 12.34, 'store': 'MockStore', 'url': '#'},
        {'name': 'FAKE PROD B', 'price': 45.67, 'store': 'MockStore', 'url': '#'}
    ],
    'chart': {}
}
try:
    r.set(mock_key, json.dumps(mock_payload), ex=600)
    print('MOCK_KEY_SET', mock_key)
    r2 = c.get(f'/dashboard/api/result/{mock_task}/', HTTP_HX_REQUEST='true')
    print('MOCK_ENDPOINT_STATUS', r2.status_code)
    content2 = r2.content.decode('utf-8')
    print('MOCK_CONTENT_SNIPPET', content2[:500].replace('\n',' '))
    # simple checks
    ok_name = 'FAKE PROD A' in content2
    ok_price = '12.34' in content2 or '12' in content2
    print('MOCK_CHECK_NAME', ok_name)
    print('MOCK_CHECK_PRICE', ok_price)
except Exception as e:
    print('MOCK_TEST_ERROR', str(e))

print('DONE')
