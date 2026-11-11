"""Local multi-workspace identity. Production hosting must supply TLS and secret management."""
import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path


class Identity:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / 'identity.sqlite3', check_same_thread=False)
        (root / 'identity.sqlite3').chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, salt TEXT, password TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, user_id TEXT, expires REAL)')
        self.db.commit()
        self.lock = threading.RLock()
        self.attempts = {}

    def password_hash(self, password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()

    def authenticate(self, payload, register=False):
        email = payload.get('email', '').strip().lower()
        password = payload.get('password', '')
        if not isinstance(password, str) or not 10 <= len(password) <= 256 or '@' not in email or len(email) > 200:
            raise ValueError('请输入有效邮箱和 10–256 位密码')
        with self.lock:
            attempts = [t for t in self.attempts.get(email, []) if t > time.time() - 60]
            if len(attempts) >= 8:
                raise ValueError('尝试过于频繁，请一分钟后再试')
            self.attempts[email] = attempts + [time.time()]
            row = self.db.execute('SELECT id,email,name,salt,password FROM users WHERE email=?', (email,)).fetchone()
            if register:
                name = payload.get('name', '').strip()
                if not name or len(name) > 100:
                    raise ValueError('请输入 1–100 字符的工作空间名称')
                if row:
                    raise ValueError('此邮箱已注册，请登录')
                key, salt = secrets.token_hex(16), secrets.token_hex(16)
                self.db.execute('INSERT INTO users VALUES (?,?,?,?,?)', (key, email, name, salt, self.password_hash(password, salt)))
                row = (key, email, name)
            elif not row or not secrets.compare_digest(self.password_hash(password, row[3]), row[4]):
                raise ValueError('邮箱或密码不正确')
            token = secrets.token_urlsafe(36)
            self.db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            self.db.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), row[0], time.time() + 43200))
            self.db.commit()
            self.attempts.pop(email, None)
            return token, {'id': row[0], 'email': row[1], 'name': row[2]}

    def user(self, token):
        with self.lock:
            row = self.db.execute('SELECT u.id,u.email,u.name FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.hash=? AND s.expires>?',
                                  (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return {'id': row[0], 'email': row[1], 'name': row[2]} if row else None

    def logout(self, token):
        with self.lock:
            self.db.execute('DELETE FROM sessions WHERE hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
            self.db.commit()

    def close(self):
        self.db.close()
