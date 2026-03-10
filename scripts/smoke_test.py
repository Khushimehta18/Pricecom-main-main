import urllib.request
import sys

urls = [
    'http://127.0.0.1:8000/dashboard/',
    'http://127.0.0.1:8000/dashboard/api/products/',
    'http://127.0.0.1:8000/dashboard/api/system-health/',
    'http://127.0.0.1:8000/dashboard/api/watchlist/',
    'http://127.0.0.1:8000/dashboard/api/search/',
]

for u in urls:
    try:
        if u.endswith('/search/'):
            data = urllib.parse.urlencode({'q': 'IPHONE-14'}).encode()
            req = urllib.request.Request(u, data=data)
            resp = urllib.request.urlopen(req, timeout=10)
        else:
            resp = urllib.request.urlopen(u, timeout=10)
        body = resp.read()
        print(u, 'STATUS', resp.getcode(), 'LEN', len(body))
    except Exception as e:
        print('ERR', u, repr(e))
        sys.exit(2)

print('SMOKE_TEST_COMPLETE')
