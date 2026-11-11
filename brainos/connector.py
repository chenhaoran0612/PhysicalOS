"""Calls only endpoints explicitly configured by the workspace owner; never follows redirects."""
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint_url(value):
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError('请输入有效服务地址')
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('服务地址须为 HTTP(S)，不含用户名、密码、参数或片段')
    return value.rstrip('/')


def request(endpoint, path, token='', payload=None):
    """Call an endpoint; any failure raises :class:`DeviceError` (a ValueError).

    ux04 (Q5-03): the device's JSON error body (``error`` / ``code``) is kept, so a
    refusal such as 409 ``reset_in_progress`` reaches the user as what it is, not as
    "无法连接接入服务" or "请检查地址、凭据与协议".
    """
    return call(endpoint, path, token, payload, timeout=8)


def _http_message(status):
    if status == 401:
        return '接入服务拒绝了访问令牌（HTTP 401）；请检查令牌'
    if status == 403:
        return '接入服务拒绝了这个操作（HTTP 403）'
    if status == 404:
        return '接入服务没有提供这个接口（HTTP 404）；可能是较旧版本的接入服务'
    return f'接入服务返回 HTTP {status}'


class DeviceError(ValueError):
    """An endpoint call that did not return a usable JSON object.

    ``status`` is the HTTP status (``None`` for network failures), ``code`` and
    ``body`` come from a JSON error body when the device sent one, and ``sent``
    says whether the request may have reached the device: ``False`` only when
    the connection was refused before anything was sent.
    """

    def __init__(self, message, status=None, code=None, body=None, sent=None):
        super().__init__(message)
        self.status, self.code, self.body, self.sent = status, code, body or {}, sent


def call(endpoint, path, token='', payload=None, timeout=8):
    """Like :func:`request`, but keeps the JSON error body of HTTP errors.

    Used where the caller must tell a refusal (e.g. 409 ``incompatible``)
    apart from an unknown outcome. Redirects are never followed.
    """
    url = endpoint_url(endpoint) + path
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        data = json.dumps(payload, allow_nan=False).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        body = {}
        try:
            parsed = json.loads(exc.read(200_001) or b'{}')
            body = parsed if isinstance(parsed, dict) else {}
        except (ValueError, OSError):
            pass
        message = body.get('error') if isinstance(body.get('error'), str) and body['error'].strip() else _http_message(exc.code)
        if not re.search('[\u4e00-\u9fff]', message):
            message += f'（接入服务返回 HTTP {exc.code}）' if 'HTTP' not in message else ''
        code = body.get('code') if isinstance(body.get('code'), str) else None
        raise DeviceError(message[:2000], status=exc.code, code=code, body=body, sent=True) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise DeviceError(f'接入服务没有在 {timeout:g} 秒内响应；请求结果未知', code='device_timeout',
                              sent=None) from None
        refused = isinstance(exc.reason, ConnectionRefusedError)
        raise DeviceError('无法连接接入服务；请检查服务是否启动及网络是否可达', code='device_unreachable',
                          sent=False if refused else None) from None
    except (TimeoutError, socket.timeout):
        raise DeviceError(f'接入服务没有在 {timeout:g} 秒内响应；请求结果未知', code='device_timeout',
                          sent=None) from None
    except OSError:
        raise DeviceError('与接入服务的连接中断；请求结果未知', code='device_unreachable', sent=None) from None
    if len(raw) > 2_000_000:
        raise DeviceError('服务响应超过 2 MB', status=200, sent=True)
    try:
        result = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise DeviceError('接入服务未返回有效 JSON', status=200, sent=True) from None
    if not isinstance(result, dict):
        raise DeviceError('服务必须返回 JSON 对象', status=200, sent=True)
    return result
