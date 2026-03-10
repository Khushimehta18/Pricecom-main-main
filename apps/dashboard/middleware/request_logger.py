import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)
LOG_PATH = os.path.join(os.getcwd(), 'tmp_request_log.txt')

class RequestLoggerMiddleware:
    """Simple middleware to log incoming requests for debugging routing issues."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            msg = f"[REQ] {request.method} {request.path} HOST={request.get_host()}"
            hv = request.META.get('HTTP_HOST')
            ua = request.META.get('HTTP_USER_AGENT')
            log_line = f"{datetime.utcnow().isoformat()}Z {msg} HOST_HDR={hv} UA={ua}\n"
            logger.debug(log_line)
            print(log_line, end='')
            try:
                with open(LOG_PATH, 'a', encoding='utf-8') as fh:
                    fh.write(log_line)
            except Exception:
                pass
        except Exception:
            pass
        return self.get_response(request)
