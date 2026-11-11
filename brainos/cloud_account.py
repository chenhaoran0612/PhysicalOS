"""Cloud platform account for a workspace (INTERFACES §2 and §4.5).

* The cloud address must be HTTPS; plain HTTP is accepted only for loopback
  addresses (a cloud running on this computer for development).
* The session token lives only in the workspace credential file
  ``<workspace>/credentials/cloud-session.json`` (directory 700, file 600). It
  is never put in the registry, the state snapshot, audit events or responses.
  Passwords are passed through to the cloud and never stored.
* Round 03 (contract §5.3): the engineering assistant can use a cloud model
  through a metered gateway session (:meth:`CloudAccount.open_gateway_session`).
  Metering and charging happen only in the cloud; the local ledger
  (``commerce``) is never touched on that path. The session's limited access
  token is handed to the engineering runtime's environment and never stored.
"""
import http.client
import ipaddress
import json
import os
import re
import time
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import robot_caps
from .connector import NoRedirect

API_PREFIX = '/api/v1'
# Where a NeuxMind cloud runs when started on this computer (cloud/, sdk/ and the demo use the same).
# NEUXMIND_DEFAULT_CLOUD_URL overrides it (tests, or a team that ships its own default).
DEFAULT_BASE_URL = os.environ.get('NEUXMIND_DEFAULT_CLOUD_URL') or 'http://127.0.0.1:8800'
CREDENTIAL_FILE = 'cloud-session.json'

ERROR_MESSAGES = {
    'invalid_credentials': '邮箱或密码不正确',
    'rate_limited': '尝试过于频繁，请稍后再试',
    'email_taken': '该邮箱已注册，请直接登录',
    'email_pending_verification': '该邮箱已被一个尚未验证的账户占用，且云平台没有配置邮件发送；请联系平台运营人员',
    'verification_token_invalid': '验证令牌无效，请检查后重试',
    'verification_token_expired': '验证令牌已过期，请重新发送验证邮件',
    'verification_wrong_account': '这个令牌属于另一个账户；请用收到邮件的账户登录后再验证',
    'already_verified': '邮箱已经验证过了',
    'email_sender_not_configured': '云平台没有配置邮件发送，无法发送验证邮件；请联系平台运营人员人工核实',
    'unauthorized': '云平台登录已失效，请重新登录',
    'invalid_manifest': '机器人能力清单不符合标准',
    'email_unverified': '云平台账户的邮箱尚未验证；验证后才能调用云平台模型（在“云平台账户”页完成验证）',
    # Gateway sessions (round 03, §5.3).
    'insufficient_credits': '云平台钱包可用点数不足以预留本次预算；请降低 Token 预算或在云平台充值',
    'entitlement_required': '尚未获得该云平台模型的使用授权；请先在云平台获取授权',
    'model_not_found': '云平台上找不到该模型，可能已下架',
    'model_unavailable': '该云平台模型已下架或暂停云端调用',
    'not_gateway_model': '该模型不提供云端调用',
    'pricing_not_session_compatible': '该模型的计价方式不支持工程助手会话',
    'session_busy': '云端会话仍有一次调用在进行中（同一会话同一时刻只允许一个调用）；请稍后重试',
    'session_budget_exhausted': '云端会话剩余预算不足以容纳下一次模型请求；请提高 Token 预算后继续',
    'session_overage_locked': '某次调用的用量超过了预留，云端已锁定该会话（只按预留上限计费，超出部分由平台人工核对）；请继续以开启新会话',
    'session_closed': '云端会话已关闭',
    'session_expired': '云端会话已超过时间上限',
    'invalid_session_token': '云端会话令牌无效',
    'request_id_reused': '云端会话请求编号冲突，请重试',
    'parameter_not_allowed': '云端网关不接受工程引擎发送的某个请求参数',
}

GATEWAY_SESSION_ID = re.compile(r'[A-Za-z0-9_-]{1,100}')


# Cloud metadata services: never reachable, not even with the private-address opt-in.
METADATA_ADDRESSES = {ipaddress.ip_address('169.254.169.254'), ipaddress.ip_address('fd00:ec2::254'),
                      ipaddress.ip_address('100.100.100.200')}
ALLOW_PRIVATE_ENV = 'NEUXMIND_CLOUD_ALLOW_PRIVATE'


def _literal_ip(host):
    try:
        return ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return None


def private_allowed():
    """Local opt-in (this computer only) for a cloud on a private or link-local network."""
    return os.environ.get(ALLOW_PRIVATE_ENV) == '1'


def check_address(ip):
    """Address policy for the cloud platform (Q2-10).

    * loopback (127.0.0.0/8, ::1) and public addresses: allowed;
    * private, shared (100.64/10) and link-local addresses: refused unless this
      computer sets ``NEUXMIND_CLOUD_ALLOW_PRIVATE=1``;
    * cloud metadata, unspecified, multicast and reserved addresses: always refused.
    """
    if getattr(ip, 'ipv4_mapped', None):
        ip = ip.ipv4_mapped
    if ip in METADATA_ADDRESSES:
        raise ValueError(f'云平台地址 {ip} 是云主机元数据服务地址，不允许连接')
    if ip.is_loopback or ip.is_global:
        return ip
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        raise ValueError(f'云平台地址 {ip} 不是可连接的服务器地址')
    if private_allowed():
        return ip
    raise ValueError(f'云平台地址解析到内网或链路本地地址 {ip}；默认不允许。'
                     f'如确需连接内网云平台，请在本机设置环境变量 {ALLOW_PRIVATE_ENV}=1 后重启桌面端')


def cloud_url(value):
    """Validate the cloud address.

    https is required. Plain http is accepted only for a literal loopback IP;
    ``localhost`` is rewritten to ``127.0.0.1`` so that no name lookup decides
    where a session token is sent in clear text. Literal IPs are checked
    against :func:`check_address` here; host names are checked on every
    connection, against the exact address connected to.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError('请输入云平台地址')
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('云平台地址须以 https:// 开头')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('云平台地址不能包含用户名、密码、参数或片段')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('云平台地址的端口无效') from None
    host = parsed.hostname
    if host == 'localhost':
        host = '127.0.0.1'
    ip = _literal_ip(host)
    if parsed.scheme == 'http' and not (ip and ip.is_loopback):
        raise ValueError('云平台地址须使用 https；只有本机地址（127.0.0.1、::1、localhost）可以使用 http')
    if ip:
        check_address(ip)
    netloc = (f'[{host}]' if ip and ip.version == 6 else host) + (f':{port}' if port else '')
    path = parsed.path.rstrip('/')
    if path.endswith(API_PREFIX):
        path = path[:-len(API_PREFIX)]
    return f'{parsed.scheme}://{netloc}{path}'


def _connect_checked(host, port, timeout):
    """Resolve, check every candidate with :func:`check_address`, connect to a checked address.

    The address that is checked is the address that is connected to, so a DNS
    answer that changes between check and connect (rebinding) cannot reach a
    refused address.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('无法解析云平台地址') from None
    last = None
    for family, kind, proto, _, sockaddr in infos:
        check_address(ipaddress.ip_address(sockaddr[0].split('%')[0]))
    for family, kind, proto, _, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError('connect failed')


class _CheckedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect_checked(self.host, self.port, self.timeout)


class _CheckedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = _connect_checked(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _CheckedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_CheckedHTTPConnection, req)


class _CheckedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):
        return self.do_open(_CheckedHTTPSConnection, req, context=self._context)


class CloudError(ValueError):
    def __init__(self, message, status=None, code=None, body=None):
        super().__init__(message)
        self.status, self.code, self.body = status, code, body or {}


class CloudAccount:
    def __init__(self, platform):
        self.platform = platform
        self.dir = Path(platform.store.root) / 'credentials'

    # ------------------------------------------------------- local state
    def settings(self):
        try:
            return self.platform.store.get('settings', 'cloud')
        except ValueError:
            return {'id': 'cloud'}

    def _save_settings(self, values):
        self.platform.store.put('settings', {'id': 'cloud', **values})

    def _token(self):
        try:
            data = json.loads((self.dir / CREDENTIAL_FILE).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return ''
        settings = self.settings()
        # A token is only valid for the server it was issued by.
        if data.get('base_url') != settings.get('base_url'):
            return ''
        return data.get('session_token', '') if isinstance(data.get('session_token'), str) else ''

    def _store_token(self, token):
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        payload = json.dumps({'base_url': self.settings().get('base_url'), 'session_token': token})
        fd, tmp = tempfile.mkstemp(prefix='.cloud-', dir=self.dir)     # created with mode 600
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.dir / CREDENTIAL_FILE)

    def _drop_token(self):
        try:
            (self.dir / CREDENTIAL_FILE).unlink()
        except FileNotFoundError:
            pass

    def summary(self):
        """What the front end may know. Never contains the session token."""
        s = self.settings()
        logged_in = bool(s.get('base_url') and self._token())
        return {'configured': bool(s.get('base_url')), 'base_url': s.get('base_url', ''),
                'logged_in': logged_in, 'email': s.get('email') if logged_in else None,
                'name': s.get('name') if logged_in else None,
                'email_verified': s.get('email_verified') if logged_in else None,
                'insecure_http': s.get('base_url', '').startswith('http://'),
                # §5.3: logged-in accounts can run the engineering assistant on cloud models.
                'gateway_enabled': logged_in, 'default_base_url': DEFAULT_BASE_URL}

    # ------------------------------------------------------------- HTTP
    def _request(self, method, path, payload=None, auth=True, base=None, timeout=10):
        base = base or self.settings().get('base_url')
        if not base:
            raise ValueError('请先设置云平台地址')
        headers = {'Accept': 'application/json'}
        if auth:
            token = self._token()
            if not token:
                raise ValueError('请先登录云平台账户')
            headers['Authorization'] = 'Bearer ' + token
        data = None
        if payload is not None:
            data = json.dumps(payload, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(base + API_PREFIX + path, data=data, headers=headers, method=method)
        # No proxy: the address policy is applied to the server actually connected to.
        opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}),
                                             _CheckedHTTPHandler(), _CheckedHTTPSHandler())
        try:
            with opener.open(req, timeout=timeout) as response:
                status, raw = response.status, response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            body = {}
            try:
                parsed = json.loads(exc.read(200_001) or b'{}')
                body = parsed if isinstance(parsed, dict) else {}
            except (ValueError, OSError):
                pass
            code = body.get('code') if isinstance(body.get('code'), str) else None
            if exc.code == 401 and auth:
                self._drop_token()
                code = 'unauthorized'
            message = ERROR_MESSAGES.get(code) or (body.get('error') if isinstance(body.get('error'), str) else None)
            if exc.code == 422 and not message:
                message = '云平台认为请求格式无效'
            if code == 'invalid_manifest' and isinstance(body.get('error'), str):
                message = ERROR_MESSAGES[code] + '：' + body['error']
            raise CloudError(message or f'云平台返回 HTTP {exc.code}', exc.code, code, body) from None
        except ssl.SSLError:
            raise CloudError('云平台的 HTTPS 证书校验失败，已停止连接') from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise CloudError('云平台的 HTTPS 证书校验失败，已停止连接') from None
            raise CloudError('无法连接云平台；请检查地址和网络') from None
        except (TimeoutError, OSError):
            raise CloudError('无法连接云平台；请检查地址和网络') from None
        if len(raw) > 2_000_000:
            raise CloudError('云平台响应过大')
        try:
            result = json.loads(raw)
        except (UnicodeError, ValueError):
            raise CloudError('云平台未返回有效 JSON') from None
        if not isinstance(result, dict):
            raise CloudError('云平台返回格式无效')
        return status, result

    # ------------------------------------------------------- operations
    def probe(self, p=None):
        """Is a cloud reachable? Unauthenticated read-only GET; never changes settings.

        Checks the given address, else the saved one, else the address a cloud
        uses when started on this computer. The answer is written for a user
        who may not know what the cloud is.
        """
        p = p or {}
        saved = self.settings().get('base_url')
        base = cloud_url(p.get('base_url') or saved or DEFAULT_BASE_URL)
        result = {'base_url': base, 'configured': bool(saved), 'is_default': base == DEFAULT_BASE_URL,
                  'reachable': False}
        try:
            self._request('GET', '/capabilities/registry', auth=False, base=base, timeout=3)
            result['reachable'] = True
            result['message'] = '已连接到云平台，可以登录或注册。'
        except CloudError as exc:
            if exc.status is not None:
                result['message'] = f'这个地址有服务在运行，但不像 NeuxMind 云平台（HTTP {exc.status}）。请核对地址。'
            elif base == DEFAULT_BASE_URL:
                result['message'] = ('这台电脑上没有运行 NeuxMind 云平台，也还没有填写云平台地址。'
                                     '云平台只用于账户、钱包和模型市场；不登录也可以添加仿真机器人、安装和运行技能。'
                                     '如果你的团队或运营方提供了云平台地址，请填写它。')
            else:
                result['message'] = f'暂时连不上 {base}：{exc}。请检查地址和网络，或稍后重试。'
        except ValueError as exc:
            result['message'] = str(exc)
        return result

    def configure(self, p):
        base = cloud_url(p.get('base_url'))
        if base != self.settings().get('base_url'):
            self._drop_token()            # a session belongs to the server that issued it
            self._save_settings({'base_url': base})
        self.platform.audit('cloud.configured', {'base_url': base})
        return self.summary()

    @staticmethod
    def _credentials(p):
        email = p.get('email')
        password = p.get('password')
        if not isinstance(email, str) or '@' not in email or len(email.strip()) > 200:
            raise ValueError('请输入有效邮箱')
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise ValueError('密码需要 10–256 个字符')
        return email.strip(), password

    def _signed_in(self, token, user):
        self._store_token(token)
        s = self.settings()
        self._save_settings({'base_url': s.get('base_url'), 'email': user.get('email'), 'name': user.get('name'),
                             'email_verified': user.get('email_verified') is True})

    @staticmethod
    def _verification_view(result):
        """Verification hints. A dev token is shown only when the cloud marks it simulated."""
        verification = result.get('verification') if isinstance(result.get('verification'), dict) else {}
        view = {'status': verification.get('status'), 'simulated': verification.get('simulated') is True}
        token = result.get('dev_verification_token')
        if isinstance(token, str) and token and view['simulated']:
            view['dev_token'] = token
        return view

    def register(self, p):
        email, password = self._credentials(p)
        body = {'email': email, 'password': password}
        if isinstance(p.get('name'), str) and p['name'].strip():
            body['name'] = p['name'].strip()[:100]
        status, result = self._request('POST', '/auth/register', body, auth=False)
        if status == 202 or result.get('status') == 'claim_sent':
            return {'status': 'claim_sent', 'account': self.summary(),
                    'message': '该邮箱已被一个尚未验证的账户占用。云平台已向该邮箱发送认领邮件，'
                               '请按邮件完成认领（本页暂不支持认领）。'}
        if not isinstance(result.get('token'), str) or not isinstance(result.get('user'), dict):
            raise CloudError('云平台注册响应缺少会话')
        self._signed_in(result['token'], result['user'])
        self.platform.audit('cloud.registered', {'email': email})
        return {'status': 'registered', 'account': self.summary(), 'verification': self._verification_view(result)}

    def login(self, p):
        email, password = self._credentials(p)
        _, result = self._request('POST', '/auth/login', {'email': email, 'password': password}, auth=False)
        if not isinstance(result.get('token'), str) or not isinstance(result.get('user'), dict):
            raise CloudError('云平台登录响应缺少会话')
        self._signed_in(result['token'], result['user'])
        self.platform.audit('cloud.login', {'email': email})
        return {'status': 'logged_in', 'account': self.summary()}

    def logout(self, p=None):
        if self._token():
            try:
                self._request('POST', '/auth/logout', {})
            except ValueError:
                pass                      # the local session is dropped either way
        self._drop_token()
        self.platform.audit('cloud.logout', {})
        return {'status': 'logged_out', 'account': self.summary()}

    def verify(self, p):
        token = p.get('token')
        if not isinstance(token, str) or not token.strip() or len(token) > 500:
            raise ValueError('请输入邮件中的验证令牌')
        _, result = self._request('POST', '/auth/verify-email', {'token': token.strip()})
        s = self.settings()
        self._save_settings({**s, 'email_verified': result.get('email_verified') is True})
        return {'status': 'verified', 'account': self.summary()}

    def resend(self, p=None):
        _, result = self._request('POST', '/auth/verify-email/resend', {})
        return {'status': 'sent', 'account': self.summary(), 'verification': self._verification_view(result)}

    def overview(self, p=None):
        """Account, e-mail verification and wallet, read from the cloud."""
        if not self.summary()['logged_in']:
            return {'account': self.summary(), 'wallet': None}
        try:
            _, me = self._request('GET', '/me')
            _, wallet = self._request('GET', '/wallet')
        except CloudError as exc:
            if exc.code == 'unauthorized':
                return {'account': self.summary(), 'wallet': None, 'error': str(exc)}
            raise
        user = me.get('user') if isinstance(me.get('user'), dict) else {}
        s = self.settings()
        self._save_settings({**s, 'email': user.get('email', s.get('email')), 'name': user.get('name', s.get('name')),
                             'email_verified': me.get('email_verified') is True})
        keys = ('balance', 'reserved', 'available')
        wallet_view = {k: wallet.get(k) for k in keys if isinstance(wallet.get(k), int)}
        wallet_view['unit'] = wallet.get('unit', 'credits')
        return {'account': self.summary(), 'wallet': wallet_view,
                'organization': next((o.get('name') for o in me.get('organizations', [])
                                      if isinstance(o, dict) and o.get('id') == me.get('current_org_id')), None)}

    def models(self, p):
        """Models from the cloud catalogue that fit the chosen robot's manifest."""
        robot = self.platform.store.get('robots', p.get('robot_id'))
        if p.get('project_id') and robot.get('project_id') != p['project_id']:
            raise ValueError('该机器人不属于当前项目')
        manifest = robot_caps.cloud_manifest(robot)
        _, result = self._request('POST', '/catalog/models/compatible', {'manifest': manifest})
        models = result.get('models') if isinstance(result.get('models'), list) else []
        keep = ('id', 'name', 'version', 'source', 'kind', 'description', 'license', 'pricing',
                'requires_capabilities', 'families', 'gateway', 'entitled', 'package')
        return {'robot': {'id': robot['id'], 'name': robot['name'], 'simulated': manifest.get('simulated') is True,
                          'manifest_source': 'v2' if robot.get('manifest_v2') else 'v1'},
                'models': [{k: m.get(k) for k in keep if k in m} for m in models if isinstance(m, dict)]}

    # ------------------------------------------------ gateway (round 03, §5.3)
    def gateway_models(self, p=None):
        """Cloud LLMs that can be called through a metered session (engineering assistant)."""
        if not self.summary()['logged_in']:
            raise ValueError('请先登录云平台账户')
        _, result = self._request('GET', '/catalog/models?kind=llm')
        models = result.get('models') if isinstance(result.get('models'), list) else []
        keep = ('id', 'name', 'version', 'source', 'kind', 'description', 'pricing', 'entitled')
        return {'account': self.summary(),
                'models': [{k: m.get(k) for k in keep if k in m} for m in models
                           if isinstance(m, dict) and m.get('gateway') is True and isinstance(m.get('id'), str)
                           and (m.get('pricing') or {}).get('type') in ('per_1k_tokens', 'free')]}

    def session_base_url(self, session_id):
        """Where the engineering runtime sends model calls: built from the address
        this desktop is configured with (never from the server's reply), so the
        access token only goes to the cloud the user chose (contract §4.3)."""
        if not isinstance(session_id, str) or not GATEWAY_SESSION_ID.fullmatch(session_id):
            raise CloudError('云平台返回的会话编号无效')
        return self.settings()['base_url'] + API_PREFIX + '/gateway/sessions/' + session_id + '/v1'

    def open_gateway_session(self, request_id, model, max_tokens, max_minutes):
        """``POST /gateway/sessions``. Returns the grant; the access token is only
        in the return value (the caller hands it to the runtime and drops it)."""
        if not self.summary()['logged_in']:
            raise CloudError('云平台未登录，无法使用云平台模型；请先在“云平台账户”页登录', code='unauthorized')
        _, result = self._request('POST', '/gateway/sessions', {'request_id': request_id, 'model': model,
                                                                 'max_tokens': max_tokens, 'max_minutes': max_minutes})
        token = result.get('access_token')
        if not isinstance(token, str) or not token or len(token) > 4096 or any(c in token for c in '\r\n'):
            raise CloudError('云平台没有返回会话令牌（同一请求编号的会话已存在）')
        session_id = result.get('session_id')
        return {'session_id': session_id, 'base_url': self.session_base_url(session_id), 'access_token': token,
                'server_base_url': result.get('base_url') if isinstance(result.get('base_url'), str) else None,
                'reservation_id': result.get('reservation_id'), 'reserved_credits': result.get('reserved_credits'),
                'expires_at': result.get('expires_at'), 'max_tokens': result.get('max_tokens'),
                'request_id': request_id}

    METERING_KEYS = ('status', 'metering_status', 'usage_tokens', 'usage_units', 'usage_evidence_ref',
                     'credits_charged', 'overage_calls', 'overage_tokens', 'reservation_id')

    def close_gateway_session(self, session_id, request_id, wait_seconds=20.0):
        """``POST /gateway/sessions/{id}/close`` (idempotent by ``request_id``).

        A streamed call that the runtime abandoned keeps the session busy while
        the cloud drains and settles it, so 409 ``session_busy`` is retried for
        up to ``wait_seconds``. Returns the cloud's metering record."""
        self.session_base_url(session_id)                 # validates the id
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                _, result = self._request('POST', '/gateway/sessions/' + session_id + '/close',
                                          {'request_id': request_id})
                return {k: result.get(k) for k in self.METERING_KEYS if k in result}
            except CloudError as exc:
                if exc.code != 'session_busy' or time.monotonic() > deadline:
                    raise
            time.sleep(0.5)

    def gateway_session(self, session_id):
        """``GET /gateway/sessions/{id}``: state and usage of a session."""
        self.session_base_url(session_id)
        _, result = self._request('GET', '/gateway/sessions/' + session_id)
        keys = ('status', 'used_tokens', 'max_tokens', 'inflight_tokens', 'calls', 'unknown_calls',
                'overage_calls', 'overage_tokens', 'close_result', 'expires_at')
        return {k: result.get(k) for k in keys if k in result}
