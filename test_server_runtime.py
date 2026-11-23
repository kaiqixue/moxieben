"""Real Gunicorn lifecycle on Linux; private port, synthetic data, no OCR calls."""
import json
import os
import secrets
import importlib.util
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


@unittest.skipUnless(sys.platform == 'linux' and importlib.util.find_spec('gunicorn'), 'Requires Linux Gunicorn runtime')
class ServerRuntimeTests(unittest.TestCase):
    def test_single_worker_start_stop_restart_preserves_private_data(self):
        with tempfile.TemporaryDirectory(prefix='moxieben-runtime-') as folder:
            root = Path(folder)
            (root / 'session').write_text(secrets.token_hex(32))
            (root / 'token').write_text('synthetic-not-a-real-token')
            env = dict(os.environ, APP_ENV='staging', APP_HOST='school.example.test',
                       DATA_DIR=str(root / 'data'), SECRET_KEY_FILE=str(root / 'session'),
                       PADDLE_OCR_TOKEN_FILE=str(root / 'token'), PADDLE_RESULT_HOSTS='results.example.test')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            def start():
                log = (root / 'server.log').open('a')
                process = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--config',
                    'gunicorn.conf.py', '--bind', f'127.0.0.1:{port}', 'wsgi:app'],
                    # CI must exercise the packaged /app, not the mounted source checkout.
                    cwd=Path(os.environ.get('MOXIEBEN_RUNTIME_ROOT', Path(__file__).parent)),
                    env=env, stdout=log, stderr=log)
                log.close()
                try:
                    for _ in range(150):
                        try:
                            with urllib.request.urlopen(base + '/healthz', timeout=1) as response:
                                if response.status == 200:
                                    self.assertEqual(json.load(response), {'status': 'ok'})
                                    return process
                        except OSError:
                            if process.poll() is not None:
                                self.fail('Gunicorn exited before becoming healthy')
                            time.sleep(.1)
                    self.fail('Gunicorn startup timed out')
                except BaseException:
                    process.terminate()
                    process.wait(timeout=20)
                    raise
            def stop(process):
                process.terminate()
                self.assertEqual(process.wait(timeout=20), 0)
            process = start()
            try:
                request = urllib.request.Request(base + '/teacher/login', headers={
                    'Host': 'school.example.test', 'X-Forwarded-Proto': 'https'})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertIn('login-form', response.read().decode())
                asset = urllib.request.Request(base + '/static/theme.css', headers={
                    'Host': 'school.example.test', 'X-Forwarded-Proto': 'https'})
                with urllib.request.urlopen(asset, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertGreater(len(response.read()), 100)
                marker = root / 'data/uploads/synthetic.marker'
                marker.write_text('preserved')
            finally:
                stop(process)
            process = start()
            try:
                self.assertEqual(marker.read_text(), 'preserved')
                self.assertTrue((root / 'data/dictation.sqlite3').is_file())
            finally:
                stop(process)
