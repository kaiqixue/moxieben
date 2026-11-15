# Keep one application process: grading coordination is process-local.
bind = '0.0.0.0:8000'
workers = 1
worker_class = 'gthread'
threads = 4
preload_app = False
reload = False
timeout = 300
graceful_timeout = 300
keepalive = 5
accesslog = '-'
errorlog = '-'
# No query strings, request bodies, cookies, emails, tokens or referrers.
access_log_format = '%(s)s %(m)s %(U)s %(L)s'
capture_output = True
