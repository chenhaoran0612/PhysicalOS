"""SSH through several jump hosts in a row (issue #15): the route of a request and its fingerprint steps.

A route is an ordered list of jump hosts and the device behind the last one, as in
``ssh -J a,b,c robot``. This machine logs in to the first hop, the first hop forwards to the
second, and so on (``device_access.Route``). Until issue #15 there was at most one jump host.

* **One step per hop.** ``scan`` reads one host key per call and stops for the user to confirm it:
  the first jump host's, then the next one's through the hops confirmed so far, and the device's
  last. No login goes to a hop before its own fingerprint is confirmed, and nothing is contacted
  beyond the hop being read. A step is only accepted for the route its check was made for.
* **Names.** A place after the first hop may be a name for an earlier jump host to resolve
  (``resolve_on``: its level), as ``ssh gate 'ssh mid ssh dog'`` does. The look-up is the fixed
  read-only ``ssh -G -- <name>`` on that hop, after its fingerprint is confirmed; what it answers
  is checked like typed input and shown with the fingerprint.
* **ProxyJump.** When the ssh config of that hop puts jump hosts in front of a name, they join
  the route and are confirmed one by one like typed ones. ProxyCommand is not followed.
* **Saved connections.** A connection record keeps the hops in ``jumps`` and their logins in the
  ``jumps`` of its secret, in the same order. ``migrate`` turns the one-hop records of before
  into lists of one; a request written the old way (``jump`` and ``jump_*``) is still accepted.
"""
from . import device_access

MAX_JUMPS = 5                      # jump hosts in a row; every hop adds a handshake and a login to each connection
ENDPOINT = ('host', 'port', 'username')
LEGACY_LOGIN = ('auth',) + device_access.SECRET_FIELDS     # a request of before issue #15: jump_auth, jump_password, ...
ROUTE_CHANGED = '跳板机信息已改变，请重新读取指纹'
NO_JUMP_TO_ASK = '只有经跳板机连接时，才能按跳板机上的 SSH 配置解析目标名称'
jump_label = device_access.jump_label


def canonical(p):
    """A request with its jump hosts as one list, each hop with its own login fields.

    The form of before issue #15, one ``jump`` and its login in ``jump_*`` fields, becomes a list
    of one. A request that already has ``jumps``, or names no jump host, is returned as it is."""
    if 'jumps' in p or not p.get('jump'):
        return p
    if not isinstance(p['jump'], dict):
        raise ValueError('跳板机配置无效')
    login = {key: p['jump_' + key] for key in LEGACY_LOGIN if 'jump_' + key in p}
    moved = {'jump', *('jump_' + key for key in login)}
    rest = {key: value for key, value in p.items() if key not in moved}
    return {**rest, 'jumps': [{**p['jump'], **login}]}


def _place(source, resolve_on, plain, position):
    """One place of a route, checked: an address, or a name for the jump host of level ``resolve_on`` to
    resolve. ``position`` counts the jump hosts from 1; the device comes after the last of them."""
    if resolve_on is None:
        return dict(zip(ENDPOINT, device_access.target(source)))
    if isinstance(resolve_on, bool) or not isinstance(resolve_on, int) or not 1 <= resolve_on < position:
        raise ValueError('名称只能交给排在它前面的某一级跳板机解析')
    place = {**dict(zip(ENDPOINT, device_access.named_target(source))), 'resolve_on': resolve_on}
    return {**place, 'plain': True} if plain else place


def request_route(p):
    """The route a request describes, checked before anything is contacted:
    ``{'jumps': [place, ...], 'target': place}``. ``p`` is a ``canonical`` request."""
    jumps = p.get('jumps') or []
    if not isinstance(jumps, list):
        raise ValueError('跳板机配置无效')
    total = len(jumps)
    if total > MAX_JUMPS:
        raise ValueError(f'跳板机最多 {MAX_JUMPS} 级，这里有 {total} 级')
    places = []
    for level, jump in enumerate(jumps, 1):
        if not isinstance(jump, dict):
            raise ValueError(jump_label(level, total) + '配置无效')
        try:
            places.append(_place(jump, jump.get('resolve_on'), jump.get('plain'), level))
        except ValueError as exc:
            raise ValueError(f'{jump_label(level, total)}：{exc}') from None
    resolve_on = p.get('resolve_on')
    if resolve_on is None and p.get('resolve_on_jump'):
        if not total:
            raise ValueError(NO_JUMP_TO_ASK)
        resolve_on = total             # "the jump host": the one right before the device
    return {'jumps': places, 'target': _place(p, resolve_on, p.get('resolve_plain'), total + 1)}


def _places(route):
    return [dict(place) for place in route['jumps']] + [dict(route['target'])]


def _same_route(route, confirmed):
    """True if a request describes the route its jump check was made for: the same places in the same
    order, and the same names still to be resolved. Logins and what a check recorded do not count."""
    shape = lambda place: (*(place.get(key) for key in ENDPOINT), place.get('resolve_on'), bool(place.get('plain')))
    return [shape(place) for place in _places(route)] == [shape(place) for place in _places(confirmed)]


def _confirmed_hops(p, check):
    """(the hops a jump check has behind it, their logins): each hop with the login method and the
    fingerprint the user confirmed. The hop the check itself read is confirmed by this request."""
    level, recorded = check['level'], check['route']['jumps']
    hops, secrets = [], []
    for index in range(level):
        hop, given = recorded[index], p['jumps'][index]
        auth, secret = device_access.credentials({**given, 'auth': hop.get('auth', given.get('auth'))}, '',
                                                 jump_label(index + 1, len(recorded)) + ' ')
        hops.append({**hop, 'auth': auth, 'fingerprint': hop.get('fingerprint', check['fingerprint'])})
        secrets.append(secret)
    return hops, secrets


def _resolved(chain, route, position):
    """Make the place at ``position`` an address, asking the jump host that its name belongs to.

    The jump hosts that host's ssh config puts in front of the name (ProxyJump) are inserted before
    it, as names for the same host to resolve; like ssh, only the first of them follows a ProxyJump
    of its own. Returns (the route, what was added: None, or ``{'at': the position of the first added
    jump host, 'count': how many, 'by': the level of the jump host whose config named them}``)."""
    places, index, added, on = _places(route), position - 1, 0, None
    while 'resolve_on' in places[index]:
        on = places[index]['resolve_on']
        where = jump_label(on, len(places) - 1)
        resolved, proxies = device_access.resolve_on_jump(chain.clients[on - 1], places[index], where)
        try:
            device_access.target(resolved)             # what a jump host answers is checked like typed input
        except ValueError as exc:
            raise ValueError(f'{where}的 SSH 配置给“{resolved["alias"]}”的地址不能用：{exc}') from None
        if len(places) - 1 + len(proxies) > MAX_JUMPS:
            raise ValueError(f'按{where}上的 SSH 配置，到“{resolved["alias"]}”还要再经过 {len(proxies)} 级跳板机，'
                             f'超过了最多 {MAX_JUMPS} 级')
        inserted = [{**spec, 'resolve_on': on, **({'plain': True} if number else {})}
                    for number, spec in enumerate(proxies)]
        # A later name that an earlier-numbered hop resolves keeps its level; one resolved by a place that moved, moves along.
        later = [{**place, 'resolve_on': place['resolve_on'] + len(inserted)} if place.get('resolve_on', 0) > index
                 else place for place in places[index + 1:]]
        places = places[:index] + inserted + [resolved] + later
        added += len(inserted)
    return {'jumps': places[:-1], 'target': places[-1]}, {'at': position, 'count': added, 'by': on} if added else None


def _next_key(chain, route, level):
    """With the hops up to ``level`` logged in (``chain``): read the host key of the place after them."""
    route, expanded = _resolved(chain, route, level + 1)
    jumps, target = route['jumps'], route['target']
    chain.total = total = len(jumps)
    if level < total:
        place = jumps[level]
        key = device_access.inspect_host(place, chain.tunnel(place, 'jump', level + 1), 'jump', True,
                                         level=level + 1, total=total)
        result = {**key, 'stage': 'jump', 'level': level + 1, 'total': total, 'route': route}
        if expanded:               # jump hosts nobody typed: the page says so and asks for their logins
            result['expanded'] = expanded
    else:
        place = target
        key = device_access.inspect_host(place, chain.tunnel(place), 'device', True, total=total)
        result = {**key, 'jumps': jumps}
    return {**result, 'alias': place['alias']} if 'alias' in place else result


def scan(engine, p):
    """Read the next host key of a route for the user to confirm (``Engineering.scan``).

    Direct: the device's key. Through jump hosts, one call per hop: the first call reads the first
    jump host's key and answers ``stage: 'jump'``, ``level: 1`` and the ``route``; each further call
    brings the check of the hop just confirmed (``jump_check_id``, ``trust_jump_fingerprint``), the
    logins of the hops confirmed so far and the route as the last answer gave it, and reads the key
    of the next hop through them. The call after the last jump host answers the device's key and the
    confirmed ``jumps``, which ``Engineering.connect`` saves."""
    p = canonical(p)
    route = request_route(p)
    total = len(route['jumps'])
    if not total:
        return device_access.inspect_host(route['target'])
    if not p.get('jump_check_id'):
        key = device_access.inspect_host(route['jumps'][0], hop='jump', level=1, total=total)
        return {**key, 'stage': 'jump', 'level': 1, 'total': total, 'route': route}
    check = engine._host_check(p['jump_check_id'], p.get('project_id'))
    if check.get('stage') != 'jump' or not _same_route(route, check['route']):
        raise ValueError(ROUTE_CHANGED)
    level = check['level']
    if p.get('trust_jump_fingerprint') != check['fingerprint']:
        raise ValueError(f'请核对并确认{jump_label(level, total)}主机指纹')
    hops, secrets = _confirmed_hops(p, check)
    chain = device_access.Route(hops, secrets, total)
    try:
        return _next_key(chain, {'jumps': hops + check['route']['jumps'][level:], 'target': check['route']['target']}, level)
    finally:
        chain.close()


def connect_logins(p, jumps):
    """The logins a connect request brings for the confirmed jump hosts of its host check, in their order."""
    given = canonical(p).get('jumps')
    if given is None:              # a connect request of before issue #15 names no jump host, only its login
        given = [{key: p.get('jump_' + key, '') for key in device_access.SECRET_FIELDS}]
    if not isinstance(given, list) or len(given) != len(jumps) or not all(isinstance(hop, dict) for hop in given):
        raise ValueError(ROUTE_CHANGED)
    return [device_access.credentials({**hop, 'auth': jump['auth']}, '', jump_label(level, len(jumps)) + ' ')[1]
            for level, (hop, jump) in enumerate(zip(given, jumps), 1)]


def migrate(store):
    """Turn the connections saved before issue #15 into the list form: the one jump host of a record
    (``jump``) becomes ``jumps`` of one, and its login (``jump_*`` in the secret) the ``jumps`` of the
    secret. Records that are already in the new form are not touched; running it again changes nothing."""
    with store.atomic():
        for connection in store.list('device_connections'):
            if 'jump' not in connection:
                continue
            jumps = device_access.jumps_of(connection)
            route = {'jumps': jumps} if jumps else {}
            try:
                secret = store.get('agent_secrets', connection['id'])
            except ValueError:         # a connection without saved logins: only its record changes
                secret = None
            if secret is not None:
                kept = {key: value for key, value in secret.items() if not key.startswith('jump_') and key != 'jumps'}
                logins = {'jumps': device_access.jump_secrets(secret, len(jumps))} if jumps else {}
                store.put('agent_secrets', {**kept, **logins})
            # The record goes last: while it still has ``jump``, an interrupted run is simply done again.
            store.put('device_connections', {**{key: value for key, value in connection.items() if key != 'jump'}, **route})
