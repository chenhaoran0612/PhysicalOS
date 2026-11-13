"""Local multi-workspace identity. Production hosting must supply TLS and secret management.

Accounts are e-mail + password (PBKDF2-SHA256, 600k rounds). A login creates a session token
stored only as a SHA-256 hash; the browser keeps it in an HttpOnly cookie. "Remember me" keeps
the session for SESSION_REMEMBER_S, otherwise SESSION_SHORT_S.

E-mail ownership is proven with a 6-digit code (registration and password reset). Codes are stored
hashed, expire after CODE_TTL_S, allow CODE_MAX_ATTEMPTS guesses, and can be re-sent once every
CODE_RESEND_S (at most CODE_HOURLY_MAX per address and purpose per hour).
"""
import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SESSION_SHORT_S = 12 * 3600
SESSION_REMEMBER_S = 30 * 24 * 3600
PASSWORD_HINT = '密码至少 10 个字符，并同时包含字母和数字'
COMMON_PASSWORDS = frozenset('''password1 password12 password123 password1234 1234567890 0123456789 qwerty1234 qwerty12345
qwertyuiop1 1q2w3e4r5t 1qaz2wsx3edc abc1234567 abcd123456 a123456789 aa12345678 woaini1314 iloveyou12 admin12345
admin123456 letmein123 welcome123 passw0rd123 p@ssw0rd123 test123456 abc123456789 zxcvbnm123 asdfghjkl1'''.split())
# Built-in account for trying the app before an e-mail service is configured. Only usable while no SMTP server is set.
TEST_EMAIL = 'test@neuxmind.ai'
TEST_PASSWORD = 'Test123456'
TEST_NAME = '测试账号'
CODE_TTL_S = 10 * 60
CODE_RESEND_S = 60
CODE_HOURLY_MAX = 5
CODE_MAX_ATTEMPTS = 5
PURPOSES = ('register', 'reset')


class Identity:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / 'identity.sqlite3', check_same_thread=False)
        (root / 'identity.sqlite3').chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, salt TEXT, password TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, user_id TEXT, expires REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS email_codes (email TEXT, purpose TEXT, hash TEXT, expires REAL, attempts INTEGER, PRIMARY KEY (email, purpose))')
        self.db.execute('CREATE TABLE IF NOT EXISTS email_code_sends (email TEXT, purpose TEXT, at REAL)')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(users)')}
        for column in ('created_at', 'last_login'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE users ADD COLUMN {column} REAL')
        self.db.commit()
        self.lock = threading.RLock()
        self.attempts = {}
        self.test_account_enabled = False

    def enable_test_account(self, enabled):
        """Creates the built-in test account the first time; while disabled it cannot sign in."""
        self.test_account_enabled = bool(enabled)
        if not enabled:
            return
        with self.lock:
            if self.db.execute('SELECT 1 FROM users WHERE email=?', (TEST_EMAIL,)).fetchone():
                return
            salt = secrets.token_hex(16)
            self.db.execute('INSERT INTO users (id,email,name,salt,password,created_at) VALUES (?,?,?,?,?,?)',
                            (secrets.token_hex(16), TEST_EMAIL, TEST_NAME, salt, self.password_hash(TEST_PASSWORD, salt), time.time()))
            self.db.commit()

    def password_hash(self, password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()

    @staticmethod
    def _check_password(password, email=''):
        """Policy for a NEW password (register, reset, change). Signing in only checks _check_login_password."""
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise ValueError(PASSWORD_HINT)
        if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
            raise ValueError(PASSWORD_HINT)
        if password.lower() in COMMON_PASSWORDS:
            raise ValueError('这个密码太常见，请换一个')
        local = email.split('@')[0].lower() if isinstance(email, str) else ''
        if email and password.lower() in (email.lower(), local):
            raise ValueError('密码不能与邮箱相同')

    @staticmethod
    def _check_login_password(password):
        if not isinstance(password, str) or not password or len(password) > 256:
            raise ValueError('请输入密码')

    @staticmethod
    def _check_name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError('请输入 1–100 个字符的名称')
        return name.strip()

    @staticmethod
    def _check_email(email):
        email = email.strip().lower() if isinstance(email, str) else ''
        local, _, domain = email.rpartition('@')
        if (not local or not domain or len(email) > 200 or any(c.isspace() for c in email) or '@' in local
                or '.' not in domain or any(not part for part in domain.split('.'))):
            raise ValueError('请输入有效的邮箱')
        return email

    @staticmethod
    def _code_hash(email, purpose, code):
        return hashlib.sha256(f'{purpose}:{email}:{code}'.encode()).hexdigest()

    def _issue_code(self, email, purpose):
        """Creates a fresh code (replacing any earlier one) and returns it. Caller holds the lock."""
        now = time.time()
        self.db.execute('DELETE FROM email_code_sends WHERE at<?', (now - 3600,))
        sends = [r[0] for r in self.db.execute('SELECT at FROM email_code_sends WHERE email=? AND purpose=? ORDER BY at', (email, purpose))]
        if sends and now - sends[-1] < CODE_RESEND_S:
            raise ValueError(f'请 {int(CODE_RESEND_S - (now - sends[-1])) + 1} 秒后再重新发送')
        if len(sends) >= CODE_HOURLY_MAX:
            raise ValueError('验证码发送次数过多，请一小时后再试')
        code = f'{secrets.randbelow(10 ** 6):06d}'
        self.db.execute('INSERT OR REPLACE INTO email_codes VALUES (?,?,?,?,0)', (email, purpose, self._code_hash(email, purpose, code), now + CODE_TTL_S))
        self.db.execute('INSERT INTO email_code_sends VALUES (?,?,?)', (email, purpose, now))
        self.db.commit()
        return code

    def discard_code(self, email, purpose):
        """Used when the e-mail could not be sent, so the address is not locked out by the resend timer."""
        with self.lock:
            email = self._check_email(email)
            self.db.execute('DELETE FROM email_codes WHERE email=? AND purpose=?', (email, purpose))
            self.db.execute('DELETE FROM email_code_sends WHERE rowid=(SELECT MAX(rowid) FROM email_code_sends WHERE email=? AND purpose=?)', (email, purpose))
            self.db.commit()

    def _consume_code(self, email, purpose, code):
        """Checks a code; on success it is used up. Caller holds the lock."""
        code = code.strip() if isinstance(code, str) else ''
        if not code:
            raise ValueError('请输入邮箱验证码')
        row = self.db.execute('SELECT hash,expires,attempts FROM email_codes WHERE email=? AND purpose=?', (email, purpose)).fetchone()
        if not row or row[1] < time.time():
            raise ValueError('验证码已过期，请重新获取')
        if row[2] >= CODE_MAX_ATTEMPTS:
            raise ValueError('验证码错误次数过多，请重新获取')
        if not secrets.compare_digest(self._code_hash(email, purpose, code), row[0]):
            self.db.execute('UPDATE email_codes SET attempts=attempts+1 WHERE email=? AND purpose=?', (email, purpose))
            self.db.commit()
            left = CODE_MAX_ATTEMPTS - row[2] - 1
            raise ValueError('验证码不正确' if left > 0 else '验证码错误次数过多，请重新获取')
        self.db.execute('DELETE FROM email_codes WHERE email=? AND purpose=?', (email, purpose))

    def start_registration(self, payload):
        """Validates the sign-up form and returns (email, code) to be mailed. No account is created yet."""
        email = self._check_email(payload.get('email', ''))
        self._check_name(payload.get('name', ''))
        self._check_password(payload.get('password', ''), email)
        with self.lock:
            if self.db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
                raise ValueError('此邮箱已注册，请直接登录')
            return email, self._issue_code(email, 'register')

    def request_reset(self, payload):
        """Returns (email, code) for a registered address, (email, None) otherwise; callers must answer the same either way."""
        email = self._check_email(payload.get('email', ''))
        with self.lock:
            self._throttle('reset:' + email)
            if not self.db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
                return email, None
            return email, self._issue_code(email, 'reset')

    def reset_password(self, payload):
        """Sets a new password with an e-mailed code, signs out every session and signs in. Returns (token, user, lifetime)."""
        email = self._check_email(payload.get('email', ''))
        password = payload.get('password', '')
        self._check_password(password, email)
        remember = payload.get('remember', True) is not False
        with self.lock:
            self._throttle('reset-try:' + email)
            self._consume_code(email, 'reset', payload.get('code', ''))
            row = self.db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
            if not row:
                self.db.commit()
                raise ValueError('验证码已过期，请重新获取')
            salt = secrets.token_hex(16)
            self.db.execute('UPDATE users SET salt=?, password=? WHERE id=?', (salt, self.password_hash(password, salt), row[0]))
            self.db.execute('DELETE FROM sessions WHERE user_id=?', (row[0],))
            token, lifetime = self._session(row[0], remember)
            self.attempts.pop(email, None)
            return token, self._user_by_id(row[0]), lifetime

    def _throttle(self, key):
        attempts = [t for t in self.attempts.get(key, []) if t > time.time() - 60]
        if len(attempts) >= 8:
            raise ValueError('尝试过于频繁，请一分钟后再试')
        self.attempts[key] = attempts + [time.time()]

    def _session(self, user_id, remember):
        lifetime = SESSION_REMEMBER_S if remember else SESSION_SHORT_S
        token = secrets.token_urlsafe(36)
        now = time.time()
        self.db.execute('DELETE FROM sessions WHERE expires<?', (now,))
        self.db.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user_id, now + lifetime))
        self.db.execute('UPDATE users SET last_login=? WHERE id=?', (now, user_id))
        self.db.commit()
        return token, lifetime

    def authenticate(self, payload, register=False):
        """Returns (token, user). Kept for existing callers; see sign_in()."""
        token, user, _ = self.sign_in(payload, register)
        return token, user

    def sign_in(self, payload, register=False, require_code=False):
        """Returns (token, user, lifetime_seconds). With require_code a registration needs the e-mailed code."""
        email = self._check_email(payload.get('email', ''))
        password = payload.get('password', '')
        if register:
            self._check_password(password, email)
        else:
            self._check_login_password(password)
            if email == TEST_EMAIL and not self.test_account_enabled:
                raise ValueError('测试账号已停用，请使用你自己的账号登录')
        remember = payload.get('remember', True) is not False
        with self.lock:
            self._throttle(email)
            row = self.db.execute('SELECT id,email,name,salt,password FROM users WHERE email=?', (email,)).fetchone()
            if register:
                name = self._check_name(payload.get('name', ''))
                if row:
                    raise ValueError('此邮箱已注册，请直接登录')
                if require_code:
                    self._consume_code(email, 'register', payload.get('code', ''))
                key, salt = secrets.token_hex(16), secrets.token_hex(16)
                self.db.execute('INSERT INTO users (id,email,name,salt,password,created_at) VALUES (?,?,?,?,?,?)',
                                (key, email, name, salt, self.password_hash(password, salt), time.time()))
                user_id = key
            elif not row or not secrets.compare_digest(self.password_hash(password, row[3]), row[4]):
                raise ValueError('邮箱或密码不正确')
            else:
                user_id = row[0]
            token, lifetime = self._session(user_id, remember)
            self.attempts.pop(email, None)
            return token, self._user_by_id(user_id), lifetime

    def _user_by_id(self, user_id):
        row = self.db.execute('SELECT id,email,name,created_at,last_login FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            return None
        return {'id': row[0], 'email': row[1], 'name': row[2], 'created_at': row[3], 'last_login': row[4]}

    def user(self, token):
        if not token:
            return None
        with self.lock:
            row = self.db.execute('SELECT u.id FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.hash=? AND s.expires>?',
                                  (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return self._user_by_id(row[0]) if row else None

    def update_profile(self, user_id, payload):
        name = self._check_name(payload.get('name', ''))
        with self.lock:
            self.db.execute('UPDATE users SET name=? WHERE id=?', (name, user_id))
            self.db.commit()
            return self._user_by_id(user_id)

    def change_password(self, user_id, token, payload):
        """Verifies the current password, sets the new one and signs out every other session."""
        current, new = payload.get('current_password', ''), payload.get('new_password', '')
        with self.lock:
            row = self.db.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
        self._check_password(new, row[0] if row else '')
        with self.lock:
            self._throttle('pw:' + user_id)
            row = self.db.execute('SELECT salt,password FROM users WHERE id=?', (user_id,)).fetchone()
            if not row or not isinstance(current, str) or not secrets.compare_digest(self.password_hash(current, row[0]), row[1]):
                raise ValueError('当前密码不正确')
            if current == new:
                raise ValueError('新密码不能与当前密码相同')
            salt = secrets.token_hex(16)
            self.db.execute('UPDATE users SET salt=?, password=? WHERE id=?', (salt, self.password_hash(new, salt), user_id))
            keep = hashlib.sha256(token.encode()).hexdigest()
            signed_out = self.db.execute('DELETE FROM sessions WHERE user_id=? AND hash<>?', (user_id, keep)).rowcount
            self.db.commit()
            self.attempts.pop('pw:' + user_id, None)
            return {'signed_out_sessions': signed_out}

    def sessions(self, user_id):
        with self.lock:
            return self.db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=? AND expires>?', (user_id, time.time())).fetchone()[0]

    def logout(self, token):
        with self.lock:
            self.db.execute('DELETE FROM sessions WHERE hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
            self.db.commit()

    def close(self):
        self.db.close()
