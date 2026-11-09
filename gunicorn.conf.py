import os

bind = '0.0.0.0:' + os.environ.get('PORT', '10000')
# Session state lives in memory: multiple workers would give inconsistent state.
workers = 1
worker_class = 'gthread'
threads = 4
timeout = 180
accesslog = '-'
errorlog = '-'
capture_output = True
