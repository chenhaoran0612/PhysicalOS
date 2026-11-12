"""Models & compute: TokenFans as the workspace's default large-model provider (issue #4).

TokenFans (https://tokenfans.ai/integrations/api) is an OpenAI-compatible
gateway. The engineering engine (Codex app-server, Responses API) uses its
Codex route ``<origin>/codex/v1``; the model list is ``/codex/v1/models``.

Credentials
* An API key pasted by the user (``sk-tkf-…``), verified against the model list; or
* the TokenFans account's email + password: the app signs in once
  (``/auth/login``), reads the account key (``/auth/my-key``) and the balance,
  then signs out. These are the endpoints the TokenFans web console uses; they
  are not in the public API docs and may change. The password and the console
  token are used for that one exchange and never stored.
* The key lives only in ``<workspace>/credentials/tokenfans.json`` (directory
  700, file 600), bound to the TokenFans address it was verified against. It is
  never put in the registry, the state snapshot, audit events or responses;
  the engine receives it through its environment at run time.

Default model
Every project gets one managed agent model profile (``billing='tokenfans'``)
that follows the default model chosen here. A discovery, engineering task or
workflow stage that names no model uses it (``Engineering.resolve_model``).
"""
import http.client
import ipaddress
import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from urllib.parse import urlparse

from .connector import NoRedirect
from .service import now

DEFAULT_ORIGIN = 'https://tokenfans.ai'
# Tests (or a team running its own TokenFans-compatible deployment) point the app elsewhere.
ORIGIN_ENV = 'NEUXMIND_TOKENFANS_URL'
DOCS_URL = DEFAULT_ORIGIN + '/integrations/api'
ENGINE_PATH = '/codex/v1'
CREDENTIAL_FILE = 'tokenfans.json'
SETTINGS_ID = 'tokenfans'
PROFILE_PREFIX = 'agent-model-tokenfans-'
MAX_MODELS = 300
MAX_RESPONSE_BYTES = 2_000_000
TIMEOUT_SECONDS = 20          # per connect / socket read
TOTAL_SECONDS = 60            # whole response, so a trickling server cannot hold a request open

KEY = re.compile(r'[A-Za-z0-9._-]{16,512}')
EMAIL = re.compile(r'[^@\s]{1,128}@[^@\s]{1,253}')
MODEL_ID = re.compile(r'[^\s\x00-\x1f\x7f]{1,200}')
CURRENCY = re.compile(r'[A-Z]{3,10}')

NOT_CONNECTED = 'TokenFans 尚未连接：请在“平台模型与算力”页连接 TokenFans，或为任务选择其他模型'
NO_MODEL = '请选择工程模型，或在“平台模型与算力”页连接 TokenFans 作为默认模型'
NO_DEFAULT = '尚未选择 TokenFans 默认模型：请在“平台模型与算力”页选择默认模型，或为任务选择其他模型'
MODEL_GONE = '所选 TokenFans 模型已不在可用列表中：请在“平台模型与算力”页重新选择默认模型'
KEY_REJECTED = 'TokenFans 拒绝了该 API Key：请检查 Key 是否完整、是否已在 TokenFans 控制台重新生成'
NO_ACCOUNT_KEY = '该 TokenFans 账户还没有 API Key：请先在 TokenFans 控制台的账户总览中生成 Key，再回来登录或直接粘贴 Key'
STATUS_MESSAGES = {
    402: 'TokenFans 账户余额不足，请先在 TokenFans 充值',
    403: 'TokenFans 账户无权使用该接口或模型',
    404: 'TokenFans 接口不存在（服务可能已变更），请稍后重试或改用 API Key 连接',
    429: 'TokenFans 请求过于频繁，请稍后再试',
}


def origin():
    """The TokenFans address: https, or plain http only for this computer (tests)."""
    from .engineering import model_url
    return model_url(os.environ.get(ORIGIN_ENV) or DEFAULT_ORIGIN)


def key_hint(key):
    """Only the public prefix and the last 4 characters, whatever the key's length."""
    return ('sk-tkf-' if key.startswith('sk-tkf-') else '') + '…' + key[-4:]


def _loopback(url):
    host = urlparse(url).hostname or ''
    try:
        return host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def clean_key(value):
    if not isinstance(value, str) or not KEY.fullmatch(value.strip()):
        raise ValueError('API Key 格式无效：请粘贴 TokenFans 控制台中的完整 Key（通常以 sk-tkf- 开头）')
    return value.strip()


def parse_models(result):
    data = result.get('data')
    if not isinstance(data, list):
        raise ValueError('TokenFans 模型列表格式无效')
    models = []
    for item in data:
        model = item.get('id') if isinstance(item, dict) else None
        if isinstance(model, str) and MODEL_ID.fullmatch(model) and model not in models:
            models.append(model)
        if len(models) >= MAX_MODELS:
            break
    if not models:
        raise ValueError('该 TokenFans 账户当前没有可供工程助手使用的模型')
    return models


def parse_balance(result):
    data = result.get('data') if isinstance(result.get('data'), dict) else {}
    minor = data.get('balance_minor')
    if isinstance(minor, str) and re.fullmatch(r'-?\d{1,20}', minor):
        minor = int(minor)
    if isinstance(minor, bool) or not isinstance(minor, int):
        return None
    wallet = data.get('wallet') if isinstance(data.get('wallet'), dict) else {}
    currency = str(data.get('currency') or wallet.get('currency') or 'CNY').strip().upper()
    return {'minor': minor, 'currency': currency if CURRENCY.fullmatch(currency) else 'CNY'}


def _read_bounded(response):
    deadline, chunks, size = time.monotonic() + TOTAL_SECONDS, [], 0
    while size <= MAX_RESPONSE_BYTES:
        chunk = response.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError('TokenFans response too slow')
    return b''.join(chunks)


def request(method, path, token=None, payload=None, unauthorized='TokenFans 拒绝了请求'):
    """Call TokenFans; every failure is a ValueError in plain words. Redirects are refused
    so that a Bearer credential is never forwarded to another address."""
    headers = {'Accept': 'application/json', 'User-Agent': 'NeuxMind-Robot-OS'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    base = origin()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    handlers = [NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context())]
    if _loopback(base):
        handlers.append(urllib.request.ProxyHandler({}))    # plain-http loopback: never through a proxy
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
            raw = _read_bounded(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError(unauthorized) from None
        fallback = f'TokenFans 服务暂时不可用（HTTP {exc.code}），请稍后重试' if exc.code >= 500 else f'TokenFans 拒绝了请求（HTTP {exc.code}）'
        raise ValueError(STATUS_MESSAGES.get(exc.code, fallback)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise ValueError('TokenFans 的 HTTPS 证书校验失败，已停止连接') from None
        raise ValueError('无法连接 TokenFans；请检查网络后重试') from None
    except ssl.SSLError:
        raise ValueError('TokenFans 的 HTTPS 证书校验失败，已停止连接') from None
    except (TimeoutError, OSError, http.client.HTTPException):
        raise ValueError('无法连接 TokenFans；请检查网络后重试') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError('TokenFans 响应过大')
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):     # RecursionError: absurdly deep nesting
        raise ValueError('TokenFans 未返回有效 JSON') from None
    if not isinstance(result, dict):
        raise ValueError('TokenFans 返回格式无效')
    return result


class ModelCompute:
    def __init__(self, platform):
        self.platform = platform
        self.store = platform.store
        self.dir = Path(platform.store.root) / 'credentials'
        self.lock = threading.RLock()

    # ------------------------------------------------------------ local state
    def settings(self):
        try:
            return self.store.get('settings', SETTINGS_ID)
        except ValueError:
            return {'id': SETTINGS_ID}

    def _save_settings(self, values):
        self.store.put('settings', {**values, 'id': SETTINGS_ID})

    def api_key(self):
        """The saved key, only while it belongs to the TokenFans address in use."""
        try:
            data = json.loads((self.dir / CREDENTIAL_FILE).read_text(encoding='utf-8'))
            current = origin()
        except (OSError, ValueError):
            return ''
        key = data.get('api_key') if isinstance(data, dict) else None
        return key if isinstance(key, str) and data.get('origin') == current else ''

    def _store_key(self, key):
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        payload = json.dumps({'origin': origin(), 'api_key': key})
        fd, tmp = tempfile.mkstemp(prefix='.tokenfans-', dir=self.dir)     # created with mode 600
        try:
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, payload.encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.dir / CREDENTIAL_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)    # never leave a stray copy of the key behind
            raise

    def _drop_key(self):
        try:
            (self.dir / CREDENTIAL_FILE).unlink()
        except FileNotFoundError:
            pass

    def engine_base_url(self):
        return origin() + ENGINE_PATH

    def summary(self):
        """What the front end may know. Never contains the key."""
        s, connected = self.settings(), bool(self.api_key())
        try:
            engine = self.engine_base_url()
        except ValueError:
            engine = None
        shown = (lambda k: s.get(k)) if connected else (lambda k: None)
        return {'provider': 'tokenfans', 'name': 'TokenFans', 'connected': connected,
                'email': shown('email'), 'key_hint': shown('key_hint'),
                'models': s.get('models', []) if connected else [], 'default_model': shown('default_model'),
                'balance': shown('balance'), 'balance_at': shown('balance_at'),
                'connected_at': shown('connected_at'), 'models_checked_at': shown('models_checked_at'),
                'engine_base_url': engine, 'console_url': DEFAULT_ORIGIN, 'docs_url': DOCS_URL}

    # ------------------------------------------------------------- operations
    def connect_key(self, p):
        key = clean_key(p.get('api_key'))
        return self._connect(key, self._fetch_models(key), email=None, balance=None)

    def login(self, p):
        email = p.get('email')
        if not isinstance(email, str) or not EMAIL.fullmatch(email.strip()):
            raise ValueError('请输入 TokenFans 账户邮箱')
        password = p.get('password')
        if not isinstance(password, str) or not password or len(password) > 512:
            raise ValueError('请输入 TokenFans 账户密码')
        email = email.strip().lower()
        result = request('POST', '/auth/login', payload={'email': email, 'password': password}, unauthorized='邮箱或密码不正确')
        token = result.get('token')
        if not isinstance(token, str) or not token or len(token) > 8192 or any(c in token for c in '\r\n'):
            raise ValueError('TokenFans 登录结果格式无效')
        try:
            key = request('GET', '/auth/my-key', token, unauthorized='TokenFans 登录已失效，请重试').get('key')
            if not isinstance(key, str) or not key.strip():
                raise ValueError(NO_ACCOUNT_KEY)
            key = clean_key(key)
            balance = self._balance(token)
        finally:
            self._logout(token)
        return self._connect(key, self._fetch_models(key), email=email, balance=balance)

    def refresh_models(self, p=None):
        key = self.api_key()
        if not key:
            raise ValueError(NOT_CONNECTED)
        models = self._fetch_models(key)
        with self.lock:
            if self.api_key() != key:     # disconnected or switched account while the list was loading
                raise ValueError('TokenFans 连接在刷新期间发生了变化，请重试')
            s = self.settings()
            default = s.get('default_model') if s.get('default_model') in models else None
            self._save_settings({**s, 'models': models, 'models_checked_at': now(), 'default_model': default})
            self.sync_profiles()
        return self.summary()

    def set_default(self, p):
        with self.lock:
            if not self.api_key():
                raise ValueError(NOT_CONNECTED)
            s = self.settings()
            model = p.get('model')
            if not isinstance(model, str) or model not in s.get('models', []):
                raise ValueError('请选择 TokenFans 模型列表中的模型；列表可能已变化，请先刷新模型列表')
            self._save_settings({**s, 'default_model': model})
            self.platform.audit('model_compute.default_model', {'provider': 'tokenfans', 'model': model})
            self.sync_profiles()
        return self.summary()

    def disconnect(self, p=None):
        with self.lock:
            self._drop_key()
            self._save_settings({})
            self.platform.audit('model_compute.disconnected', {'provider': 'tokenfans'})
            self.sync_profiles()
        return self.summary()

    def _fetch_models(self, key):
        return parse_models(request('GET', ENGINE_PATH + '/models', key, unauthorized=KEY_REJECTED))

    def _balance(self, token):
        try:
            return parse_balance(request('GET', '/v1/me/billing/balance', token))
        except ValueError:
            return None    # the balance is informational; connecting does not depend on it

    def _logout(self, token):
        # Best effort: the console session also expires on its own and nothing of it was stored.
        try:
            request('POST', '/auth/logout', token, payload={})
        except ValueError as exc:
            self.platform.audit('model_compute.logout_failed', {'provider': 'tokenfans', 'reason': str(exc)[:200]})

    def _connect(self, key, models, email, balance):
        with self.lock:
            old = self.settings()
            default = old.get('default_model') if old.get('default_model') in models else models[0]
            self._store_key(key)
            self._save_settings({'connected_at': now(), 'email': email, 'key_hint': key_hint(key), 'models': models,
                                 'models_checked_at': now(), 'default_model': default,
                                 'balance': balance, 'balance_at': now() if balance else None})
            self.platform.audit('model_compute.connected', {'provider': 'tokenfans', 'via': 'login' if email else 'api_key',
                                                            'key_hint': key_hint(key), 'default_model': default})
            self.sync_profiles()
        return self.summary()

    # ------------------------------------------------- default model profiles
    def sync_profiles(self):
        for project in self.store.list('projects'):
            self.ensure_profile(project['id'])

    def ensure_profile(self, project_id):
        """The project's managed TokenFans profile, following the default model.
        Kept (marked unavailable) when there is no default, because sessions refer to it."""
        with self.lock:
            return self._ensure_profile(project_id)

    def _ensure_profile(self, project_id):
        pid = PROFILE_PREFIX + project_id
        try:
            existing = self.store.get('agent_models', pid)
        except ValueError:
            existing = None
        model = self.settings().get('default_model') if self.api_key() else None
        if not model:
            if existing and existing.get('available') is not False:
                self.store.put('agent_models', {**existing, 'available': False, 'updated_at': now()})
            return None
        wanted = {'id': pid, 'project_id': project_id, 'name': 'TokenFans · ' + model, 'model': model,
                  'base_url': self.engine_base_url(), 'billing': 'tokenfans', 'protocol': 'responses',
                  'managed_by': 'model_compute', 'available': True}
        if existing and all(existing.get(k) == v for k, v in wanted.items()):
            return existing
        profile = {**wanted, 'created_at': (existing or {}).get('created_at') or now(), 'updated_at': now()}
        self.store.put('agent_models', profile)
        return profile

    def default_profile(self, project_id):
        if not self.api_key():
            raise ValueError(NO_MODEL)
        profile = self.ensure_profile(project_id)
        if not profile:
            raise ValueError(NO_DEFAULT)
        return profile

    def check(self, model):
        """Fail before a run starts when a TokenFans profile cannot be used now."""
        if model.get('billing') != 'tokenfans':
            return
        if not self.api_key():
            raise ValueError(NOT_CONNECTED)
        if model.get('available') is False:
            raise ValueError(MODEL_GONE)

    def default_llm(self):
        """Entry point for other desktop modules that call a large model directly (e.g. hardware scan).

        Returns the workspace default: ``model``, ``base_url`` (the OpenAI-compatible root ``/v1``:
        chat/completions, responses, messages), ``engine_base_url`` (the Codex route) and ``api_key``.
        The key is for the request being made: never store it, log it or return it to the UI.
        Raises ValueError in plain words when TokenFans is not connected or has no default model.
        Runs of the engineering engine use ``Engineering.resolve_model`` / ``start_work`` without a
        ``model_id`` instead.
        """
        key = self.api_key()
        if not key:
            raise ValueError(NOT_CONNECTED)
        model = self.settings().get('default_model')
        if not model:
            raise ValueError(NO_DEFAULT)
        return {'provider': 'tokenfans', 'model': model, 'base_url': origin() + '/v1',
                'engine_base_url': self.engine_base_url(), 'api_key': key}

    def session_model(self, model, pinned=None):
        """The model name a new run records and uses. A TokenFans profile follows the default model,
        but a workflow stage keeps the model it was defined with (``pinned``)."""
        if model.get('billing') != 'tokenfans' or not pinned or pinned == model['model']:
            return model['model']
        if pinned not in self.settings().get('models', []):
            raise ValueError(f'该流程阶段定义时使用的 TokenFans 模型 {pinned} 已不在可用列表中：请刷新模型列表，或新建流程以使用当前默认模型')
        return pinned

    def engine_profile(self, model, session):
        """Model, address and key for one run. The session keeps the model it started with."""
        key = self.api_key()
        if not key:
            raise ValueError(NOT_CONNECTED)
        return {'model': session.get('model') or model['model'], 'base_url': self.engine_base_url(), 'api_key': key}
