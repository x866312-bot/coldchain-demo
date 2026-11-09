"""Cloud entrypoint; the original local application remains unchanged."""
import os
from server import app

app.config['SESSION_COOKIE_SECURE'] = os.environ.get('COLDCHAIN_HTTPS', '1') == '1'


@app.get('/healthz')
def health():
    # Do not allocate a session or wait for the optimizer's global lock.
    return {'status': 'ok', 'service': 'coldchain'}


@app.get('/robots.txt')
def robots():
    return 'User-agent: *\nDisallow: /\n', 200, {'Content-Type': 'text/plain'}
