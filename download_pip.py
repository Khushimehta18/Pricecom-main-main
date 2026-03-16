
import urllib.request
import ssl
import logging

logger = logging.getLogger(__name__)

try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    # Legacy Python that doesn't verify HTTPS certificates by default
    pass
else:
    # Handle target environment that doesn't support HTTPS verification
    ssl._create_default_https_context = _create_unverified_https_context

url = 'https://bootstrap.pypa.io/get-pip.py'
output = 'get-pip.py'

logger.info("Downloading %s...", url)
try:
    with urllib.request.urlopen(url) as response, open(output, 'wb') as out_file:
        data = response.read()
        out_file.write(data)
    logger.info("Download complete.")
except Exception:
    logger.exception("Error downloading %s", url)
