import http.cookiejar
import urllib.request

BASE = 'http://127.0.0.1:8000'
DASH = BASE + '/dashboard/'

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

r = opener.open(DASH, timeout=10)
print('CODE', r.getcode())
print('RESPONSE HEADERS:')
for h in r.getheaders():
    print(' ', h)

print('\nCOOKIES IN JAR:')
for c in cj:
    print(' ', c.name, c.value)
