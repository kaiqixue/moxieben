"""Local teacher login and the isolated multi-account preview."""
import hashlib
import re
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import click
from flask import abort, g, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash


_INTERNAL_REQUEST = object()
SESSION_SECONDS = 12 * 60 * 60


def is_internal_request():
    return request.environ.get('moxieben.internal_request') is _INTERNAL_REQUEST


def internal_post(app, path, payload, *, owner_id=None, environ=None):
    """Use a server-only WSGI value, never an HTTP header, for queued work."""
    with app.test_client() as client:
        return client.post(path, json=payload, headers={'X-CSRF-Token': app.config['CSRF_TOKEN']},
                           environ_overrides={**(environ or {}), 'moxieben.internal_request': _INTERNAL_REQUEST,
                                              'moxieben.internal_owner_id': owner_id})


def auth_required(app):
    return (not app.testing or app.config.get('TEACHER_AUTH_REQUIRED', False)
            or app.config.get('TEACHER_ACCOUNT_PREVIEW', False)
            or app.config.get('TEACHER_TENANT_MODE', False))


def csrf_token(app):
    if not auth_required(app) or request.environ.get('moxieben.internal_request') is _INTERNAL_REQUEST:
        return app.config['CSRF_TOKEN']
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']


def session_hash():
    value = session.get('teacher_session')
    return token_digest(value) if isinstance(value, str) else ''


def normalized_email(value):
    email = value.strip().lower()
    local, _, domain = email.partition('@')
    if (len(email) <= 254 and re.fullmatch(r'[-a-z0-9.!#$%&\'*+/=?^_`{|}~]{1,64}', local)
            and not local.startswith('.') and not local.endswith('.') and '..' not in local
            and re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+'
                             r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', domain)):
        return email
    return ''


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def register(app, db):
    preview = app.config.get('TEACHER_ACCOUNT_PREVIEW', False)
    accounts = preview or app.config.get('TEACHER_TENANT_MODE', False)
    if not app.secret_key:
        app.secret_key = secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_NAME='moxieben_accounts' if accounts else 'moxieben_teacher',
                      SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax')
    with db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('teacher','platform_admin')),
                disabled_at INTEGER);
            CREATE TABLE IF NOT EXISTS user_sessions (
                digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS user_sessions_user ON user_sessions(user_id);
        ''' if accounts else '''
            CREATE TABLE IF NOT EXISTS teacher_account (
                id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS teacher_sessions (
                digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
        ''')

    failed_attempts = deque()
    preview_attempts = deque()
    attempt_lock = threading.Lock()
    dummy_hash = generate_password_hash(secrets.token_urlsafe(32)) if accounts else None

    def actor():
        digest = session_hash()
        if not digest:
            return None
        with db() as conn:
            query = ('''SELECT u.id,u.name,u.role FROM user_sessions s JOIN users u ON u.id=s.user_id
                        WHERE s.digest=? AND s.expires_at>? AND u.disabled_at IS NULL''' if accounts else
                     'SELECT 1 FROM teacher_sessions WHERE digest=? AND expires_at>?')
            return conn.execute(query, (digest, int(time.time()))).fetchone()

    def start_user_session(conn, user_id):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        conn.execute('DELETE FROM user_sessions WHERE expires_at<=? OR digest=?', (now, session_hash()))
        conn.execute('INSERT INTO user_sessions(digest,user_id,expires_at) VALUES (?,?,?)',
                     (token_digest(token), user_id, now + SESSION_SECONDS))
        return token

    def signed_in(token, target='/'):
        session.clear()
        session['teacher_session'] = token
        return redirect(target, code=303)

    def allow_preview_attempt(action, limit, email=''):
        stamp = time.monotonic()
        address = request.remote_addr
        with attempt_lock:
            while preview_attempts and stamp - preview_attempts[0][0] >= 300:
                preview_attempts.popleft()
            recent = sum(kind == action and (ip == address or bool(email and key == email))
                         for _, kind, ip, key in preview_attempts)
            if recent >= limit:
                return False
            preview_attempts.append((stamp, action, address, email))
            return True

    def next_page(value):
        if (preview or not value or len(value) > 2048 or not value.startswith('/') or value.startswith('//')
                or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return '/'
        try:
            path = urlsplit(value)
            endpoint, _ = app.url_map.bind('localhost').match(path.path, method='GET')
        except (ValueError, HTTPException):
            return '/'
        return value if endpoint == 'index' and not (path.netloc or path.scheme) else '/'

    @app.before_request
    def teacher_gate():
        if not auth_required(app) or request.endpoint in ('static', 'teacher_login', 'teacher_register', 'healthz'):
            return
        if request.environ.get('moxieben.internal_request') is _INTERNAL_REQUEST and not preview:
            if accounts:
                owner_id = request.environ.get('moxieben.internal_owner_id')
                if type(owner_id) is not int or owner_id < 1:
                    abort(403, description='后台任务缺少教师身份。')
                with db() as conn:
                    owner = conn.execute('SELECT 1 FROM users WHERE id=? AND role=? AND disabled_at IS NULL',
                                         (owner_id, 'teacher')).fetchone()
                if owner is None:
                    abort(403, description='后台任务的教师账号已失效。')
                g.teacher_id = owner_id
            return
        user = actor()
        if user is None:
            if request.path.startswith('/api/'):
                abort(401, description='请先登录老师端。已有草稿请先保留。')
            target = request.full_path.rstrip('?') if request.endpoint == 'index' else '/'
            return redirect(url_for('teacher_login', next=next_page(target)))
        if accounts and user['role'] == 'teacher':
            g.teacher_id = user['id']
            g.teacher_name = user['name']
        if preview and request.endpoint != 'teacher_logout':
            if request.endpoint != 'account_preview':
                abort(403, description='账号预览仅开放注册和登录。')
            g.preview_user = user

    @app.before_request
    def check_csrf():
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return
        form_route = request.endpoint in ('teacher_login', 'teacher_logout', 'teacher_register')
        supplied = request.form.get('csrf_token', '') if form_route else request.headers.get('X-CSRF-Token', '')
        if not secrets.compare_digest(supplied.encode(), csrf_token(app).encode()):
            abort(403, description='页面验证已失效，请刷新页面后重试。')

    @app.route('/teacher/login', methods=('GET', 'POST'))
    def teacher_login():
        value = request.form.get('next') if request.method == 'POST' else request.args.get('next')
        target = next_page(value)
        if request.method != 'POST':
            if actor():
                return redirect(target)
            if accounts:
                return preview_page()
        if accounts:
            email = normalized_email(request.form.get('email', ''))
            password = request.form.get('password', '')
            if not allow_preview_attempt('login', 10, email):
                return preview_page('尝试次数过多，请稍后再试。', 429, email=email)
            with db() as conn:
                user = conn.execute('SELECT id,password_hash,disabled_at FROM users WHERE email=?', (email,)).fetchone()
            if (not 8 <= len(password) <= 128
                    or not check_password_hash(user['password_hash'] if user else dummy_hash, password)
                    or user is None or user['disabled_at'] is not None):
                return preview_page('邮箱或密码不正确。', 401, email=email)
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT password_hash,disabled_at FROM users WHERE id=?', (user['id'],)).fetchone()
                if current is None or current['disabled_at'] is not None or current['password_hash'] != user['password_hash']:
                    return preview_page('邮箱或密码不正确。', 401, email=email)
                token = start_user_session(conn, user['id'])
            return signed_in(token)
        with db() as conn:
            account = conn.execute('SELECT username,password_hash FROM teacher_account WHERE id=1').fetchone()

        def page(error='', status=200, username=''):
            return render_template('teacher_login.html', csrf_token=csrf_token(app), target=target,
                                   ready=account is not None, error=error, username=username), status

        if request.method != 'POST':
            return page()
        if account is None:
            return page('请先在本机创建老师账号。', 503)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if len(username) > 64 or len(password) > 128:
            return page('账号或密码不正确。', 401)
        stamp = time.monotonic()
        with attempt_lock:
            while failed_attempts and stamp - failed_attempts[0] >= 300:
                failed_attempts.popleft()
            if len(failed_attempts) >= 5:
                return page('尝试次数过多，请等几分钟再登录。', 429, username)
            failed_attempts.append(stamp)
        password_ok = check_password_hash(account['password_hash'], password)
        if not (password_ok and secrets.compare_digest(username.encode(), account['username'].encode())):
            return page('账号或密码不正确。', 401, username)
        old_digest = session_hash()
        token = secrets.token_urlsafe(32)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT password_hash FROM teacher_account WHERE id=1').fetchone()
            if current is None or current['password_hash'] != account['password_hash']:
                return page('账号已更新，请重新登录。', 401, username)
            conn.execute('DELETE FROM teacher_sessions WHERE expires_at<=? OR digest=?', (int(time.time()), old_digest))
            conn.execute('INSERT INTO teacher_sessions VALUES (?,?)',
                         (token_digest(token), int(time.time()) + SESSION_SECONDS))
        with attempt_lock:
            failed_attempts.clear()
        return signed_in(token, target)

    def preview_page(error='', status=200, *, is_register=False, name='', email=''):
        return render_template('teacher_login.html', csrf_token=csrf_token(app), ready=True, preview=True,
                               registration_open=app.config.get('TEACHER_REGISTRATION_OPEN', True),
                               is_register=is_register, error=error, name=name, email=email, target='/'), status

    @app.route('/teacher/register', methods=('GET', 'POST'))
    def teacher_register():
        if not accounts:
            abort(404)
        if not app.config.get('TEACHER_REGISTRATION_OPEN', True):
            abort(403, description='当前暂未开放注册，请联系平台负责人。')
        if request.method != 'POST':
            return redirect('/') if actor() else preview_page(is_register=True)
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        key = normalized_email(email)
        password = request.form.get('password', '')

        def invalid(message, status=400):
            return preview_page(message, status, is_register=True, name=name[:80], email=email[:254])

        if not allow_preview_attempt('register', 8):
            return invalid('注册尝试次数过多，请稍后再试。', 429)
        if not 1 <= len(name) <= 80 or not key:
            return invalid('请填写姓名和有效邮箱。')
        if not 8 <= len(password) <= 128 or password != request.form.get('confirm_password'):
            return invalid('请填写 8–128 个字符的密码，并确认两次输入一致。')
        hashed = generate_password_hash(password)
        with db() as conn:
            cursor = conn.execute("INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,'teacher') "
                                  'ON CONFLICT(email) DO NOTHING', (name, key, hashed))
            if cursor.rowcount == 0:
                return invalid('该邮箱已注册，请直接登录。', 409)
            token = start_user_session(conn, cursor.lastrowid)
        return signed_in(token)

    @app.post('/teacher/logout')
    def teacher_logout():
        with db() as conn:
            query = 'DELETE FROM user_sessions WHERE digest=?' if accounts else 'DELETE FROM teacher_sessions WHERE digest=?'
            conn.execute(query, (session_hash(),))
        session.clear()
        return redirect(url_for('teacher_login'), code=303)

    if preview:
        @app.get('/')
        def account_preview():
            return render_template('teacher_login.html', csrf_token=csrf_token(app), user=g.preview_user, preview=True)

        register_account_cli(app, db)
        return

    if accounts:
        register_account_cli(app, db)
        return

    @app.cli.command('teacher-account')
    @click.option('--reset', is_flag=True, help='重设老师账号并退出所有旧会话。')
    def teacher_account(reset):
        """Interactively create or reset the only teacher account."""
        with db() as conn:
            present = bool(conn.execute('SELECT 1 FROM teacher_account WHERE id=1').fetchone())
        if present != reset:
            raise click.ClickException('老师账号已存在，请使用 --reset。' if present else '请先创建老师账号。')
        username = click.prompt('老师账号').strip()
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,64}', username):
            raise click.ClickException('账号须为 3–64 位字母、数字或 _ . @ -。')
        password = click.prompt('密码', hide_input=True, confirmation_prompt=True)
        if not 8 <= len(password) <= 128:
            raise click.ClickException('密码长度须为 8–128 个字符。')
        password_hash = generate_password_hash(password)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            exists = bool(conn.execute('SELECT 1 FROM teacher_account WHERE id=1').fetchone())
            if exists != reset:
                raise click.ClickException('老师账号状态已变化，请重新运行命令。')
            conn.execute('INSERT INTO teacher_account VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET '
                         'username=excluded.username,password_hash=excluded.password_hash', (username, password_hash))
            conn.execute('DELETE FROM teacher_sessions')
        with attempt_lock:
            failed_attempts.clear()
        click.echo('老师账号已保存。')


def register_account_cli(app, db):
    @app.cli.command('platform-admin')
    def platform_admin():
        """Create a preview platform administrator interactively."""
        name = click.prompt('管理员姓名').strip()
        email = normalized_email(click.prompt('邮箱'))
        if not 1 <= len(name) <= 80 or not email:
            raise click.ClickException('请填写姓名和有效邮箱。')
        password = click.prompt('密码', hide_input=True, confirmation_prompt=True)
        if not 12 <= len(password) <= 128:
            raise click.ClickException('管理员密码长度须为 12–128 个字符。')
        hashed = generate_password_hash(password)
        with db() as conn:
            cursor = conn.execute("INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,'platform_admin') "
                                  'ON CONFLICT(email) DO NOTHING', (name, email, hashed))
            if cursor.rowcount == 0:
                raise click.ClickException('该邮箱已存在。')
        click.echo('平台管理员账号已创建。')

    @app.cli.command('account-status')
    @click.argument('email')
    @click.option('--disable/--enable', default=None)
    def account_status(email, disable):
        """Disable or enable an account and revoke its sessions."""
        if disable is None:
            raise click.UsageError('请指定 --disable 或 --enable。')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            user = conn.execute('SELECT id FROM users WHERE email=?', (normalized_email(email),)).fetchone()
            if user is None:
                raise click.ClickException('未找到该账号。')
            conn.execute('UPDATE users SET disabled_at=? WHERE id=?', (int(time.time()) if disable else None, user['id']))
            conn.execute('DELETE FROM user_sessions WHERE user_id=?', (user['id'],))
        click.echo('账号已禁用。' if disable else '账号已启用，请重新登录。')
