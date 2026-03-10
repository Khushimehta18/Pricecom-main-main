import redis,sys
try:
    r=redis.Redis(host='127.0.0.1',port=6379,db=0, socket_connect_timeout=2)
    r.ping()
    print('OK')
except Exception as e:
    print('ERR', type(e).__name__, str(e))
    sys.exit(2)
