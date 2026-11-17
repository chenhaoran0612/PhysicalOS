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
* Saving never waits for the robot (issue #12): an investigation, a repair or a hardware scan that is
  running does not stop it. A session that is already running keeps the SSH session it opened and ends
  on the machine it started on; see ``ConnectionEdit.session_login``.
"""
import time

from . import device_access
from .service import now, uid

SECRET_FIELDS = ('password', 'private_key', 'passphrase')
SECRET_OF_AUTH = {'password': ('password', '密码'), 'key': ('private_key', '私钥')}
AUDITED_FIELDS = ('host', 'port', 'username', 'auth', 'fingerprint', 'alias', 'read_roots', 'watch_services')
ADDRESS_CHANGED = '主机或跳板机的地址变了：要先重新读取并确认主机指纹，再保存'
CHANGED_MEANWHILE = '这台机器人的连接刚刚被改过；请关闭后重新打开“配置连接”再改'
MOVED_AWAY = ('这台机器人的 SSH 连接改过了，现在指向另一台机器（主机指纹和本次会话开始时不同）。'
              '本次会话不会换到那台机器上继续，新保存的登录信息也不会发给原来那台机器。请结束本次会话，再重新开始')
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


def _hide_too(device, secret):
    """From now on, nothing this SSH session reads may show these saved logins either."""
    device.secrets = list(dict.fromkeys([*device.secrets, *device_access.secret_values(secret)]))


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
        """(robot, saved connection) of an edit request. What the robot is doing does not matter (issue #12)."""
        if not p.get('edit_robot_id'):
            raise ValueError('请指定要修改连接的机器人')
        return self.platform.engineering._ssh_connection(
            {'robot_id': p['edit_robot_id'], 'project_id': p.get('project_id')})

    def prepare(self, p):
        """Called first by ``Engineering.scan`` and ``Engineering.connect``.

        In a request that edits a robot (``edit_robot_id``), each hop whose credential field was
        left empty gets its saved login. Any other request passes through untouched."""
        if not p.get('edit_robot_id'):
            return p
        with self.store.atomic():                         # the record and the logins of one and the same edit
            _, saved = self._editable(p)
            secret = self.store.get('agent_secrets', saved['id'])
        engine, p = self.platform.engineering, dict(p)
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

    def saved_login(self, connection_id):
        """(connection, saved logins) as one edit left them. Read apart, an edit saved in between would pair
        the address of one machine with the login of another: every SSH login from the store starts here."""
        with self.store.atomic():
            return self.store.get('device_connections', connection_id), self.store.get('agent_secrets', connection_id)

    def session_login(self, device, connection_id):
        """``saved_login`` for a session that is already running and logs in again, or needs the saved
        password for ``sudo``. ``device`` is the SSH session it opened when it started.

        The connection may have been edited since (issue #12). The newly saved one is used while it still
        leads to the machine the session started on; a session never follows its robot to another machine,
        and a login saved for another machine never reaches the one the session is connected to."""
        connection, secret = self.saved_login(connection_id)
        if device is None:
            return connection, secret
        if device.fingerprint != connection['fingerprint']:
            raise ValueError(MOVED_AWAY)
        _hide_too(device, secret)
        return connection, secret

    def _open_sessions(self, connection_id):
        """The SSH sessions that running investigations and repairs opened over this connection."""
        using = {s['id'] for s in self.store.list('agent_sessions') if s.get('connection_id') == connection_id}
        handles = [handle for sid, handle in self.platform.engineering.live.items() if sid in using]
        return [handle['device'] for handle in handles if handle.get('device') is not None]

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
        with engine.lock:                                 # one edit at a time: the check below and the write belong together
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
            for device in self._open_sessions(saved['id']):
                _hide_too(device, secret)                 # issue #12: they go on reading the device after this edit
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
