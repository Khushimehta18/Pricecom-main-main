import requests, base64, os
BASE='http://127.0.0.1:8000'
DASH=BASE+'/dashboard/'
UPLOAD=BASE+'/dashboard/api/image-search/'

png_b64=('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2P4'
    '//8/AwAI/AL+5kN2AAAAAElFTkSuQmCC')
path='tests/tmp_req_img.png'
with open(path,'wb') as f: f.write(base64.b64decode(png_b64))

s=requests.Session()
r=s.get(DASH, timeout=10)
print('GET', r.status_code)
csrftoken=None
for c in s.cookies:
    if c.name=='csrftoken': csrftoken=c.value
print('csrftoken', csrftoken)
files={'image':('tmp.png', open(path,'rb'),'image/png')}
headers={'X-CSRFToken': csrftoken} if csrftoken else {}
resp=s.post(UPLOAD, files=files, headers=headers, timeout=30)
print('STATUS', resp.status_code)
print('URL', resp.url)
print('HISTORY', [(h.status_code,h.headers.get('Location')) for h in resp.history])
print(resp.text[:2000])
try: os.remove(path)
except: pass
