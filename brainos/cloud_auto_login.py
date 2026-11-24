"""Issue #36: signing into the app signs into the cloud (the material library) as well.

The app account (``auth.Identity``) and the cloud account (``cloud_account.CloudAccount``) are two
e-mail + password accounts. This module ties them at the one moment the app holds the password, an app
sign-in (login, registration, password reset):

* the same e-mail and password sign into the cloud; when the cloud has no such account, one is registered
  (a development cloud hands back the verification token, which is redeemed at once; a production cloud
  mails it, and the pages say what is still to do);
* "保持登录" is passed on as ``remember``, so the cloud session lasts as long as the app session;
* the password is used for those requests only. It is never stored, logged or put into a result.

A cloud sign-in the user made by hand (another account, or signing out) is respected and never replaced.

Sessions. An app session made before this version has no cloud sign-in and there is no password to make
one with. :meth:`CloudAutoLogin.stale_session` finds it when the app starts (``GET /api/me``), signs it
out, and the sign-in page says why; signing in again does both. The same happens when the cloud sign-in
made at login has lapsed. A session is never signed out twice for a reason signing in cannot fix: every
sign-in is marked (by the hash of its session token) whatever the cloud answered.

The switch. ``Application.cloud_auto_login`` is off unless the launcher (``server.main``) turns it on, so
a test server built from ``Application`` never reaches a cloud that happens to run on this computer.
``NEUXMIND_CLOUD_AUTO_LOGIN=0`` turns it off for the launcher too.
"""
import hashlib
import os
import threading
import time

from . import cloud_account           # CloudError is read from it at call time: the two modules import each other

SETTINGS_ID = 'cloud_auto_login'
ENVIRONMENT_SWITCH = 'NEUXMIND_CLOUD_AUTO_LOGIN'
SIGN_IN_TIMEOUT_S = 6
CHECK_TIMEOUT_S = 3
LIFETIME_SLACK_S = 3600             # clocks differ; an hour less than the app session still counts as matching
MESSAGE_MAX = 300                   # a cloud's own error text is shown, never at any length
# Two sign-ins of one account at once (two windows) would both register and the loser would record a
# password mismatch. Sign-ins are rare: one at a time for the whole app is enough.
_SIGN_IN_LOCK = threading.Lock()

# The cloud sign-in was made by an app sign-in and should still be there.
SIGNED_IN = ('logged_in', 'registered')
# Signing in again would not help: asking the cloud once more would only send another claim e-mail.
NEEDS_THE_USER = ('password_mismatch', 'claim_sent', 'email_pending_verification')

MESSAGES = {
    'logged_in': '已用同一账号登录云平台。',
    'registered': '已用同一邮箱和密码在云平台注册并登录。',
    'registered_unverified': '已用同一邮箱和密码在云平台注册并登录。云平台已向这个邮箱发送验证邮件，'
                             '验证后才能上传资料（在“云平台账户”页填写邮件里的验证令牌）。',
    'manual': '',
    'signed_out_by_hand': '',
    'password_mismatch': '没有自动登录云平台：云平台上已有这个邮箱的账户，但密码和应用密码不同。'
                         '请用云平台的密码登录一次。',
    'claim_sent': '没有自动登录云平台：这个邮箱在云平台上被一个尚未验证的账户占用，'
                  '云平台已向该邮箱发送认领邮件，请按邮件完成认领后再登录云平台。',
    'email_pending_verification': '没有自动登录云平台：这个邮箱在云平台上被一个尚未验证的账户占用，'
                                  '请联系平台运营人员。',
    'cloud_unreachable': '没有自动登录云平台：暂时连不上云平台。云平台恢复后，重新登录应用即可同时登录。',
    'rate_limited': '没有自动登录云平台：云平台暂时限制了登录尝试，请稍后重新登录应用，或在这里直接登录。',
    'failed': '没有自动登录云平台。',
    'expired': '云平台登录已到期。重新登录应用即可同时登录云平台。',
}
SIGNED_OUT = {
    'cloud_login_required': '从这个版本起，登录应用时会同时登录云平台（资料库）。请重新登录一次。',
    'cloud_session_expired': '云平台登录已到期，请重新登录（会同时登录云平台）。',
}


def enabled_by_environment(environ=None):
    """What the launcher asks: on, unless NEUXMIND_CLOUD_AUTO_LOGIN=0."""
    return (os.environ if environ is None else environ).get(ENVIRONMENT_SWITCH, '1') != '0'


def _session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _saved(platform):
    try:
        return platform.store.get('settings', SETTINGS_ID)
    except ValueError:
        return None


def _save(platform, status, email='', message=None, short=False):
    # ``short``: the cloud gave a session that ends well before the app session (a cloud from before
    # ``remember``). Its lapse is expected, so it must not sign the app out at every start.
    record = {'id': SETTINGS_ID, 'status': status, 'email': email, 'short': bool(short),
              'message': (MESSAGES.get(status, '') if message is None else message)[:MESSAGE_MAX], 'at': time.time()}
    platform.store.put('settings', record)
    return record


def note_manual(platform, signed_out=False):
    """The user signed into or out of the cloud, or changed its address, by hand: theirs from now on.

    A sign-out is kept apart: it is a decision to stay signed out, so it never signs the app out. A hand-made
    sign-in that has died is an old sign-in like any other.
    """
    _save(platform, 'signed_out_by_hand' if signed_out else 'manual')


def view(platform, logged_in):
    """For the state snapshot: why the cloud is not signed in, and whether signing in again fixes it.

    None while the automatic sign-in has never run in this workspace.
    """
    saved = _saved(platform)
    if saved is None:
        return None
    relogin = saved['status'] in SIGNED_IN and not logged_in
    message = MESSAGES['expired'] if relogin else '' if logged_in else saved.get('message', '')
    # ``at`` changes only with a sign-in or sign-out, never on a timer: pages read their cloud data anew then.
    return {'status': saved['status'], 'message': message, 'relogin': relogin, 'at': saved.get('at')}


# ------------------------------------------------------------- the cloud
def _ask(account, method, path, payload=None, auth=True, timeout=SIGN_IN_TIMEOUT_S):
    return account._request(method, path, payload, auth=auth, timeout=timeout)[1]


def _answers_as_cloud(account, base):
    """Is a NeuxMind cloud at ``base``? Its capability registry, by its schema name, not just any JSON."""
    try:
        registry = account._request('GET', '/capabilities/registry', auth=False, base=cloud_account.cloud_url(base),
                                    timeout=CHECK_TIMEOUT_S)[1]
    except ValueError:
        return False
    return str(registry.get('schema') or '').startswith('neuxmind.capabilities.')


def _has_session(result):
    return isinstance(result.get('token'), str) and isinstance(result.get('user'), dict)


def _current_cloud_user(account):
    """The e-mail of the cloud session this workspace holds, '' when it holds none (or a dead one)."""
    if not account.summary()['logged_in']:
        return ''
    try:
        me = _ask(account, 'GET', '/me', timeout=CHECK_TIMEOUT_S)
    except cloud_account.CloudError as exc:
        if exc.code == 'unauthorized':          # the account dropped the dead token
            return ''
        raise
    user = me.get('user') if isinstance(me.get('user'), dict) else {}
    return str(user.get('email') or account.settings().get('email') or '?').strip().lower()


def _end_session(account):
    try:
        _ask(account, 'POST', '/auth/logout', {}, timeout=CHECK_TIMEOUT_S)
    except ValueError:
        pass                            # it lapses by itself


def _register(account, credentials, name, lifetime=0):
    """Returns (status, session ends early). A development cloud returns the verification token: redeem it."""
    body = {**credentials, **({'name': name[:100]} if name else {})}
    status_code, result = account._request('POST', '/auth/register', body, auth=False, timeout=SIGN_IN_TIMEOUT_S)
    if status_code == 202 or result.get('status') == 'claim_sent':
        return 'claim_sent', False
    if not _has_session(result):
        raise cloud_account.CloudError('云平台注册响应缺少会话')
    account._signed_in(result['token'], result['user'])
    short = _ends_early(result, lifetime)
    token = account._verification_view(result).get('dev_token')
    if not token:
        return 'registered_unverified', short
    try:
        account.verify({'token': token})
    except ValueError:
        return 'registered_unverified', short
    return 'registered', short


def _ends_early(result, lifetime):
    expires = result.get('expires_at')
    return bool(lifetime) and isinstance(expires, (int, float)) and expires < time.time() + lifetime - LIFETIME_SLACK_S


def sign_in(platform, email, password, name='', remember=False, lifetime=0):
    """Sign this workspace into the cloud with the app's credentials. Never raises; returns the record."""
    with _SIGN_IN_LOCK:
        return _sign_in(platform, email.strip().lower(), password, name, remember, lifetime)


def _sign_in(platform, email, password, name, remember, lifetime):
    account = platform.cloud
    previous = _saved(platform) or {}
    try:
        if not account.settings().get('base_url'):
            default = cloud_account.DEFAULT_BASE_URL
            if not _answers_as_cloud(account, default):
                return _save(platform, 'cloud_unreachable', email)
            # Not ``configure``: that is the user's own act (and marks the sign-in as theirs).
            account._save_settings({'base_url': cloud_account.cloud_url(default)})
        current = _current_cloud_user(account)
        if current and current != email:
            return _save(platform, 'manual', email)
        credentials = {'email': email, 'password': password, **({'remember': True} if remember else {})}
        try:
            result = _ask(account, 'POST', '/auth/login', credentials, auth=False)
            if not _has_session(result):
                raise cloud_account.CloudError('云平台登录响应缺少会话')
            if current:
                _end_session(account)           # the session this one replaces
            account._signed_in(result['token'], result['user'])
            status, short = 'logged_in', _ends_early(result, lifetime)
        except cloud_account.CloudError as exc:
            if exc.code != 'invalid_credentials':
                raise
            if current:
                # Signed in already (by hand, or before the app password changed): that session stays.
                return _save(platform, 'password_mismatch', email)
            if previous.get('status') in NEEDS_THE_USER and previous.get('email') == email:
                return _save(platform, previous['status'], email)
            try:
                status, short = _register(account, credentials, name, lifetime)
            except cloud_account.CloudError as registration:
                if registration.code == 'email_taken':
                    return _save(platform, 'password_mismatch', email)
                if registration.code == 'email_pending_verification':
                    return _save(platform, 'email_pending_verification', email)
                raise
        record = _save(platform, 'registered' if status == 'registered_unverified' else status, email,
                       MESSAGES[status], short=short)
        platform.audit('cloud.auto_login', {'email': email, 'status': status})
        return record
    except cloud_account.CloudError as exc:
        if exc.status is None:
            return _save(platform, 'cloud_unreachable', email)
        if exc.code == 'rate_limited':
            return _save(platform, 'rate_limited', email)
        return _save(platform, 'failed', email, f'{MESSAGES["failed"]}云平台的回答：{exc}')
    except Exception as exc:            # an app sign-in must never fail because of the cloud
        return _save(platform, 'failed', email, f'{MESSAGES["failed"]}（{type(exc).__name__}）')


# ----------------------------------------------------------- app sessions
class CloudAutoLogin:
    """Which app sessions went through the automatic cloud sign-in, and what to do with the others."""

    def __init__(self, application):
        self.application = application
        identity = application.identity
        with identity.lock:
            identity.db.execute('CREATE TABLE IF NOT EXISTS cloud_login_sessions (hash TEXT PRIMARY KEY)')
            identity.db.commit()

    @property
    def enabled(self):
        return bool(self.application.cloud_auto_login)

    def _mark(self, token):
        identity = self.application.identity
        with identity.lock:
            identity.db.execute('DELETE FROM cloud_login_sessions WHERE hash NOT IN (SELECT hash FROM sessions)')
            identity.db.execute('INSERT OR IGNORE INTO cloud_login_sessions VALUES (?)', (_session_hash(token),))
            identity.db.commit()

    def _marked(self, token):
        identity = self.application.identity
        with identity.lock:
            return identity.db.execute('SELECT 1 FROM cloud_login_sessions WHERE hash=?',
                                       (_session_hash(token),)).fetchone() is not None

    def password_changed(self, user):
        """The app password changed; the cloud's did not (it can only be reset by e-mail). Signing in again
        would not bring a lapsed cloud sign-in back, so the lapse must not sign the app out."""
        if not self.enabled or not user:
            return
        platform = self.application.workspace(user)
        saved = _saved(platform) or {}
        if saved.get('status') in SIGNED_IN:
            _save(platform, 'password_mismatch', saved.get('email', ''))

    def signed_in(self, token, user, payload, lifetime=0):
        """After an app sign-in that carried the password. Never raises."""
        if not self.enabled:
            return None
        password = payload.get('password')
        record = None
        if isinstance(password, str) and user:
            record = sign_in(self.application.workspace(user), user['email'], password, user.get('name') or '',
                             remember=payload.get('remember', True) is not False, lifetime=lifetime)
        self._mark(token)
        return record

    def stale_session(self, token, user):
        """At start-up: None to keep the session, else ``{'code', 'message'}`` after signing it out."""
        if not self.enabled or not token or not user:
            return None
        try:
            code = self._verdict(token, self.application.workspace(user))
        except Exception:               # never lock the user out of the app over a cloud problem
            return None
        if code is None:
            return None
        self.application.identity.logout(token)
        return {'code': code, 'message': SIGNED_OUT[code]}

    def _verdict(self, token, platform):
        account = platform.cloud
        marked = self._marked(token)
        try:
            signed_in = bool(_current_cloud_user(account))
        except cloud_account.CloudError:
            return None                 # the cloud does not answer: nothing can be decided now
        if signed_in:
            if not marked:
                self._mark(token)
            return None
        saved = _saved(platform) or {}
        if marked:
            return 'cloud_session_expired' if saved.get('status') in SIGNED_IN and not saved.get('short') else None
        if saved.get('status') == 'signed_out_by_hand':
            self._mark(token)           # decided before this session was first seen
            return None
        base = account.settings().get('base_url') or cloud_account.DEFAULT_BASE_URL
        if not _answers_as_cloud(account, base):
            return None                 # no cloud to sign into: asked again at the next start
        return 'cloud_login_required'
