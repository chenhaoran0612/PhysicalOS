"""Change the SSH connection of a robot that is already connected (issue #9).

The robot's "配置连接" opens the form that connected it. Saving replaces the robot's one
connection record in place: the robot, its investigations, inventories and hardware scans stay.

* Validation, fingerprint confirmation, the login test and every error message are those of a
  new connection. An edit goes through ``Engineering.scan`` and ``Engineering.connect``; both
  call ``prepare`` first, and ``connect`` hands the tested connection to ``apply`` instead of
  creating a robot.
* A hop whose address changed has its fingerprint confirmed again, exactly as when connecting
  (both hops through a jump host). ``update`` saves without that only while no address changed,
  and then the hops stay pinned to the fingerprints confirmed before.
* Credentials never come back. A credential field left empty keeps the saved login, and a saved
  login is only ever sent to the machine it was saved for: the host key of that hop must be the
  one confirmed before. Any other machine gets a login only if it is typed again.
* Nothing changes while an investigation, a repair or a hardware scan of the robot is running.
"""
import time

from . import device_access
from .engineering import ACTIVE
from .service import now, uid

SECRET_FIELDS = ('password', 'private_key', 'passphrase')
SECRET_OF_AUTH = {'password': ('password', '密码'), 'key': ('private_key', '私钥')}
SESSION_NAMES = {'discovery': '调查', 'repair': '修复'}
AUDITED_FIELDS = ('host', 'port', 'username', 'auth', 'fingerprint', 'alias', 'read_roots', 'watch_services')
ADDRESS_CHANGED = '主机或跳板机的地址变了：要先重新读取并确认主机指纹，再保存'
CHANGED_MEANWHILE = '这台机器人的连接刚刚被改过；请关闭后重新打开“配置连接”再改'
ROUTE_ONLY_FIELDS = ('jump', 'alias')      # present only on some routes: never carried over from the old record
REVISION = '_edit_revision'        # set by prepare: the saved connection's last change, as this request saw it


def _endpoint(hop):
    return f"{hop['username']}@{hop['host']}:{hop['port']}" if hop else None


def _address(hop):
    return (hop['host'], hop['port']) if hop else None


def _device_secret(secret):
    return {key: secret.get(key, '') for key in SECRET_FIELDS}


def _used_fields(auth):
    """The credential fields a login method reads: nothing else is worth keeping on disk."""
    return {'password': ('password',), 'key': ('private_key', 'passphrase')}.get(auth, ())


def _secret_to_save(connection, secret):
    """The logins of a connection as they are stored: per hop, only what its login method uses."""
    kept = {key: secret.get(key, '') if key in _used_fields(connection['auth']) else '' for key in SECRET_FIELDS}
    jump = connection.get('jump')
    if jump:
        kept.update({'jump_' + key: secret.get('jump_' + key, '') if key in _used_fields(jump['auth']) else ''
                     for key in SECRET_FIELDS})
    return kept


class ConnectionEdit:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    def _editable(self, p):
        """(robot, saved connection) of an edit request, or the reason the connection cannot change now."""
        if not p.get('edit_robot_id'):
            raise ValueError('请指定要修改连接的机器人')
        robot, saved = self.platform.engineering._ssh_connection(
            {'robot_id': p['edit_robot_id'], 'project_id': p.get('project_id')})
        running = next((s for s in self.store.list('agent_sessions')
                        if s.get('robot_id') == robot['id'] and s['status'] in ACTIVE), None)
        if running:
            kind = SESSION_NAMES.get(running.get('mode'), '工程')
            raise ValueError(f'这台机器人有{kind}会话正在运行，现在改连接会让它中途断开；请等它结束，或先停止它，再修改连接')
        if robot['id'] in self.platform.hardware.scanning:
            raise ValueError('这台机器人正在扫描硬件；请等扫描结束，再修改连接')
        return robot, saved

    def prepare(self, p):
        """Called first by ``Engineering.scan`` and ``Engineering.connect``.

        A request that edits a robot (``edit_robot_id``) is refused while the robot is busy, and
        each hop whose credential field was left empty gets its saved login. Any other request
        passes through untouched."""
        if not p.get('edit_robot_id'):
            return p
        _, saved = self._editable(p)
        engine, secret, p = self.platform.engineering, self.store.get('agent_secrets', saved['id']), dict(p)
        p[REVISION] = saved.get('updated_at')
        saved_jump = saved.get('jump') or {}
        if p.get('host_check_id'):                        # connect: the device login, and the jump login once more
            check = engine._host_check(p['host_check_id'], p.get('project_id'))
            if check.get('stage') != 'jump' and p.get('trust_fingerprint') == check['fingerprint']:
                self._keep_login(p, '', '设备', p.get('auth'), saved, check, secret)
                if check.get('jump'):
                    self._keep_login(p, 'jump_', '跳板机', check['jump']['auth'], saved_jump, check['jump'], secret)
        elif p.get('jump_check_id'):                      # second scan: reads the device key through the jump host
            first = engine._host_check(p['jump_check_id'], p.get('project_id'))
            if first.get('stage') == 'jump' and p.get('trust_jump_fingerprint') == first['fingerprint']:
                self._keep_login(p, 'jump_', '跳板机', p.get('jump_auth'), saved_jump, first, secret)
        return p

    @staticmethod
    def _keep_login(p, prefix, label, auth, saved_hop, confirmed_hop, secret):
        """Fill one hop's empty credential field with its saved login, if the hop is still the same machine.

        The caller pins the connection to ``confirmed_hop``'s fingerprint, so the saved login can only
        reach a machine that holds the host key it was saved for."""
        field, name = SECRET_OF_AUTH.get(auth, (None, None))
        if not field or p.get(prefix + field) or auth != saved_hop.get('auth'):
            return                                        # no secret needed, a new one was typed, or none is saved
        if confirmed_hop['fingerprint'] != saved_hop.get('fingerprint'):
            raise ValueError(f'{label}的主机指纹和原来保存的不同（换了一台机器，或重装过系统），'
                             f'已保存的{name}不会发给它；请重新输入{label}的{name}')
        for key in SECRET_FIELDS:
            p[prefix + key] = p.get(prefix + key) or secret.get(prefix + key, '')

    def update(self, p):
        """Save an edit of a robot's SSH connection.

        With ``host_check_id`` the fingerprints were read and confirmed again, as for a new connection.
        Without it the request is accepted only while no hop's address changed."""
        _, saved = self._editable(p)
        if p.get('host_check_id'):
            return self.platform.engineering.connect(p)
        check = self.store.put('host_key_checks', self._confirmed_before(p, saved))
        try:
            return self.platform.engineering.connect({**p, 'host_check_id': check['id'],
                                                      'trust_fingerprint': check['fingerprint']})
        finally:
            self.store.delete('host_key_checks', check['id'])     # it stood in for this one request only

    @staticmethod
    def _confirmed_before(p, saved):
        """A host check for a request that keeps every address: the fingerprints confirmed when these
        addresses were connected. ``Engineering.connect`` treats it like a check that was just confirmed."""
        jump = device_access.jump_target(p)
        if p.get('resolve_on_jump'):
            raise ValueError(ADDRESS_CHANGED)             # asking the jump host again is a new look-up
        host, port, user = device_access.target(p)
        saved_jump = saved.get('jump')
        if (host, port) != _address(saved) or _address(jump) != _address(saved_jump):
            raise ValueError(ADDRESS_CHANGED)
        check = {'id': uid('ssh-key'), 'project_id': saved['project_id'], 'host': host, 'port': port, 'username': user,
                 'fingerprint': saved['fingerprint'], 'scanned_at': time.time(), 'confirmed_for': saved['id']}
        if saved.get('alias'):
            check['alias'] = saved['alias']
        if jump:
            check['jump'] = {**jump, 'auth': p.get('jump_auth'), 'fingerprint': saved_jump['fingerprint']}
        return check

    def apply(self, p, connection, secret):
        """Called by ``Engineering.connect`` in place of creating a robot, once the new route and its
        logins have been tested: replaces the robot's connection record and its saved logins."""
        engine = self.platform.engineering
        with engine.lock:                                 # no session of this robot can start before the write
            robot, saved = self._editable(p)
            if saved.get('updated_at') != p.get(REVISION):
                raise ValueError(CHANGED_MEANWHILE)       # another edit was saved while this one was being tested
            old_secret = self.store.get('agent_secrets', saved['id'])
            secret = _secret_to_save(connection, secret)
            # Fields this module does not know stay; a jump host or a name the new route no longer has goes.
            kept = {key: value for key, value in saved.items() if key not in ROUTE_ONLY_FIELDS}
            updated = {**kept, **connection, 'id': saved['id'], 'robot_id': robot['id'],
                       'created_at': saved['created_at'], 'updated_at': now()}
            with self.store.atomic():
                self.store.put('agent_secrets', {'id': saved['id'], **secret})
                self.store.put('device_connections', updated)
        engine.status_sessions.forget(saved['id'])        # the live-status session of the old route or login
        self.platform.audit('device.ssh_updated', self._audit(robot, saved, updated, old_secret, secret),
                            robot['project_id'])
        return updated

    @staticmethod
    def _audit(robot, old, new, old_secret, new_secret):
        """What changed, without any credential: only whether a saved login is a different one now."""
        changed = [key for key in AUDITED_FIELDS if old.get(key) != new.get(key)]
        if _device_secret(old_secret) != _device_secret(new_secret):
            changed.append('credentials')
        if old.get('jump') != new.get('jump'):
            changed.append('jump')
        if device_access.jump_secret(old_secret) != device_access.jump_secret(new_secret):
            changed.append('jump_credentials')
        return {'robot_id': robot['id'], 'connection_id': new['id'], 'changed': changed,
                'before': {'target': _endpoint(old), 'jump': _endpoint(old.get('jump'))},
                'after': {'target': _endpoint(new), 'jump': _endpoint(new.get('jump'))},
                'fingerprint_changed': old['fingerprint'] != new['fingerprint']}
