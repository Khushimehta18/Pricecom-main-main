import http.cookiejar
import urllib.request
import urllib.parse
import sys

BASE = 'http://127.0.0.1:8000'
DASH = BASE + '/dashboard/'
SEARCH = BASE + '/dashboard/api/search/'

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

try:
    r = opener.open(DASH, timeout=10)
    # ensure cookies received
    token = None
    for c in cj:
        if c.name == 'csrftoken' or c.name == 'csrfmiddlewaretoken':
            token = c.value
            break
    if not token:
        # also check Set-Cookie header fallback
        hdrs = r.headers.get_all('Set-Cookie') if hasattr(r.headers, 'get_all') else r.headers.get_all('Set-Cookie') if 'get_all' in dir(r.headers) else None
        if hdrs:
            for h in hdrs:
                if 'csrftoken=' in h:
                    token = h.split('csrftoken=')[1].split(';')[0]
                    break

    if not token:
        print('ERR: no csrftoken cookie found')
        sys.exit(2)

    data = urllib.parse.urlencode({'q': 'IPHONE-14'}).encode()
    req = urllib.request.Request(SEARCH, data=data, headers={
        'X-CSRFToken': token,
        'HX-Request': 'true',
        'User-Agent': 'HTMX-Test-Client'
    })

    resp = opener.open(req, timeout=10)
    body = resp.read()
    print('SEARCH_STATUS', resp.getcode(), 'LEN', len(body))
    sys.exit(0)

except Exception as e:
    print('ERR', repr(e))
    sys.exit(3)
