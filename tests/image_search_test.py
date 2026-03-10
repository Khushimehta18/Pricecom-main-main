import requests
import time
import base64
import os

BASE = 'http://127.0.0.1:8000'
DASH = BASE + '/dashboard/'
UPLOAD = BASE + '/dashboard/api/image-search/'
RESULT = BASE + '/dashboard/api/result/'

# tiny 1x1 PNG
png_b64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2P4'
    '//8/AwAI/AL+5kN2AAAAAElFTkSuQmCC'
)

def write_tmp():
    path = os.path.join('tests', 'tmp_test_img.png')
    with open(path, 'wb') as f:
        f.write(base64.b64decode(png_b64))
    return path

def test_image_upload_and_poll():
    s = requests.Session()
    r = s.get(DASH, timeout=10)
    r.raise_for_status()
    csrftoken = None
    for c in s.cookies:
        if c.name == 'csrftoken':
            csrftoken = c.value
    if not csrftoken:
        print('No csrftoken; abort')
        return

    img_path = write_tmp()
    with open(img_path, 'rb') as f:
        files = {'image': ('tmp_test_img.png', f, 'image/png')}
        headers = {'X-CSRFToken': csrftoken}
        resp = s.post(UPLOAD, files=files, headers=headers, timeout=30)
    print('upload status', resp.status_code, resp.text[:500])
    if resp.status_code != 200:
        return
    data = resp.json()
    task_id = data.get('task_id')
    if not task_id:
        print('no task id')
        return

    # poll
    for i in range(30):
        r = s.get(RESULT + task_id + '/', timeout=10)
        if r.status_code == 200:
            obj = r.json()
            print('poll', i, obj.get('status'))
            if obj.get('status') and obj.get('status') != 'PENDING':
                print('final', obj)
                break
        else:
            print('poll http', r.status_code)
        time.sleep(1)

    try:
        os.remove(img_path)
    except Exception:
        pass


if __name__ == '__main__':
    test_image_upload_and_poll()
