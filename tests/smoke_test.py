import requests

BASE = 'http://127.0.0.1:8000'

def test_dashboard_up():
    r = requests.get(BASE + '/dashboard/')
    assert r.status_code == 200

if __name__ == '__main__':
    print('dashboard', requests.get(BASE + '/dashboard/').status_code)
