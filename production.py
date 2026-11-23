"""Fail-closed server configuration. No database or network access on import."""
import ipaddress
import logging
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

from werkzeug.middleware.proxy_fix import ProxyFix


def secret_file(env, name, minimum=1):
    path = env.get(name, '')
    try:
        value = Path(path).read_text(encoding='utf-8').strip() if path else ''
    except (OSError, UnicodeError):
        raise ValueError(f'{name} cannot be read') from None
    if not minimum <= len(value) <= 8192:
        raise ValueError(f'{name} is missing or invalid')
    return value


def hostname(value):
    return bool(re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+'
                             r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', value))


def load_config(env):
    if env.get('APP_ENV') not in ('staging', 'production') or env.get('FLASK_DEBUG', '0') != '0':
        raise ValueError('APP_ENV must be staging/production; Flask debug is forbidden')
    host = env.get('APP_HOST', '').lower()
    if not hostname(host):
        raise ValueError('APP_HOST must be an exact domain, without scheme or port')
    data = env.get('DATA_DIR', '')
    if not data or not Path(data).is_absolute():
        raise ValueError('DATA_DIR must be an absolute persistent directory')
    registration = env.get('REGISTRATION_OPEN', '0')
    if registration not in ('0', '1'):
        raise ValueError('REGISTRATION_OPEN must be 0 or 1')
    result_hosts = [h.strip().lower() for h in env.get('PADDLE_RESULT_HOSTS', '').split(',') if h.strip()]
    if not result_hosts or not all(hostname(h) for h in result_hosts):
        raise ValueError('PADDLE_RESULT_HOSTS must list exact trusted result domains')
    from paddle_cloud import JOB_URL, MODEL
    ocr = {'provider': 'paddle_cloud', 'base_url': '', 'model': '', 'api_key': '',
           'paddle_url': JOB_URL, 'paddle_model': MODEL,
           'paddle_token': secret_file(env, 'PADDLE_OCR_TOKEN_FILE'),
           'result_hosts': result_hosts, 'outbound_hosts': [urlsplit(JOB_URL).hostname]}
    visual = None
    if env.get('VLM_REVIEW_BASE_URL'):
        url = urlsplit(env['VLM_REVIEW_BASE_URL'])
        if not hostname(url.hostname or '') or not env.get('VLM_REVIEW_MODEL', '').strip():
            raise ValueError('Visual model configuration is incomplete')
        # Validate structure now; public DNS is checked immediately before the request.
        validate_outbound_url(env['VLM_REVIEW_BASE_URL'], [url.hostname], resolve=False)
        visual = {'base_url': env['VLM_REVIEW_BASE_URL'], 'model': env['VLM_REVIEW_MODEL'],
                  'api_key': secret_file(env, 'VLM_REVIEW_API_KEY_FILE'), 'outbound_hosts': [url.hostname]}
    if env.get('VLM_REVIEW_AUTO', '0') not in ('0', '1'):
        raise ValueError('VLM_REVIEW_AUTO must be 0 or 1')
    if env.get('VLM_REVIEW_AUTO') == '1' and not visual:
        raise ValueError('Automatic visual review requires a configured visual service')
    return {'PRODUCTION_MODE': True, 'DEBUG': False, 'TESTING': False,
            'TEACHER_TENANT_MODE': True, 'TEACHER_AUTH_REQUIRED': True,
            'TEACHER_REGISTRATION_OPEN': registration == '1',
            'DATA_DIR': data, 'SECRET_KEY': secret_file(env, 'SECRET_KEY_FILE', 64),
            'SESSION_COOKIE_SECURE': True, 'PUBLIC_ORIGIN': 'https://' + host,
            'TRUSTED_HOSTS': [host, 'localhost', '127.0.0.1'],
            'PREFERRED_URL_SCHEME': 'https', 'OCR_CONFIG': ocr, 'VISUAL_REVIEW_CONFIG': visual,
            'AI_REVIEW_AUTO': env.get('VLM_REVIEW_AUTO') == '1'}


def validate_outbound_url(value, allowed_hosts, *, resolve=True):
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in allowed_hosts or url.port not in (None, 443)
                or url.username is not None or url.password is not None or url.fragment):
            raise ValueError()
        if resolve:
            addresses = socket.getaddrinfo(url.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError()
    except (ValueError, OSError):
        raise ValueError('Outbound service address is not permitted') from None


def create_production_app(env=None):
    import os
    from app import create_app
    config = load_config(os.environ if env is None else env)
    app = create_app(config)
    # Only Caddy may reach this socket. Never trust forwarded Host or path headers.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    return app
