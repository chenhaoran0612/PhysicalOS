"""Skills kept in the cloud (issue #43): the desktop's side of the cloud skill library.

A skill package (``skill.package.v1`` with its files) used to exist only as a folder on this
computer and as an install on a robot. The cloud now keeps releases of it, private to the
organisation (docs/CLOUD_SKILLS.md, section 1). This module is what the console does with them:

* ``overview`` / ``detail``: what the library holds;
* ``publish_preview`` then ``publish``: a local skill folder (or one of the app's own examples) is
  read and validated by the rules of ``skill_packages`` and sent as a release. Publishing insists on
  the digest the person saw in the preview;
* ``publish_examples``: the app's bundled examples go to the user's own library in one action;
* ``install_preview`` then ``install``: one release is downloaded, its digest is computed here again
  and must be the one the cloud recorded, the package is checked against the robot's live manifest,
  and only then it goes to the robot through the existing ``POST /v2/skills/install``. Installing
  needs the preview to have been asked for (in this run of the app), a person's consent
  (``authorized``) and the project the robot belongs to.

The page never talks to the cloud: the session token stays in the workspace credential file and
every call goes through here (as in ``material_library``).

The digest of a release is the one the robot's management layer computes when it installs a package
(``runtime/neuxmind_runtime/skills.py``): SHA-256 over ``{"package": <validated skill.json>,
"files": {...}}``. The cloud is expected to use the same; a downloaded package whose digest differs
from its release is never sent to a robot.
"""
import hashlib
import json
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import connector, robot_caps, skill_packages
from .cloud_account import API_PREFIX, ERROR_MESSAGES, CloudError, _CheckedHTTPHandler, _CheckedHTTPSHandler
from .contracts import required_text
from .errors import CodedError

SKILL_ID = re.compile(r'[a-z0-9][a-z0-9_.\-]{0,99}')
VERSION = re.compile(r'\d+\.\d+\.\d+(?:[\-+][0-9A-Za-z.\-]+)?')
DIGEST = re.compile(r'[0-9a-f]{64}')
MAX_ANSWER_BYTES = 8 * 1024 * 1024      # a package of 1.5 MiB of text, JSON-escaped, with room to spare
MAX_NOTES_CHARS = 2000
MAX_PREVIEWS = 200
MAX_LISTED = 500                        # releases an organisation can hold
TIMEOUT_S = 30
RELEASE_TEXT = (('skill_id', 100), ('version', 50), ('title', 100), ('description', 2000), ('digest', 64),
                ('notes', MAX_NOTES_CHARS), ('created_by', 200))
# Refusals of the cloud that read better in the words of this page.
MESSAGES = {
    'forbidden': '你在这个组织里的角色只能查看和安装技能，不能发布或删除',
    'scope_missing': '当前凭据没有技能库的权限',
    'email_unverified': '云平台账户的邮箱尚未验证；验证后才能发布或删除技能（在“云平台账户”页完成验证）',
    'skill_version_exists': '云端已经有这个技能的这个版本，但内容不同。版本发布后不能改：请在 skill.json 里提高版本号后再发布',
    'skill_not_found': '云端技能库里没有这个技能或这个版本，可能刚被删除',
    'skill_quota_exceeded': '本组织在云端技能库里的份数已经到上限；删除不再需要的版本后再发布',
    'invalid_skill_package': '云平台没有接受这个技能包',
    'payload_too_large': '这个技能包超过了云平台允许的大小',
}
LOGIN_REQUIRED = '技能库保存在云平台；请先在“云平台账户”页登录'


def release_digest(package):
    """The digest of a package as the management layer computes it at install (see the module text).
    Raises ValueError when the package is not a valid ``skill.package.v1``."""
    skill = skill_packages.validate(package)
    body = {'package': skill, 'files': package['files']}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ''


def _cap_views(items):
    ids = [x.get('id') if isinstance(x, dict) else x for x in (items if isinstance(items, list) else [])]
    return [{'id': x[:100], 'title': robot_caps.capability_row(x[:100])['title']} for x in ids if isinstance(x, str) and x]


def moves(requires, optional=()):
    """True when one of the capabilities makes the robot move (or is unknown to the standard)."""
    ids = [x['id'] for x in [*requires, *optional]]
    return any(robot_caps.capability_row(cap_id)['safety'] != 'none' for cap_id in ids)


def release_view(raw):
    """A release as the page gets it: known fields only, text cut to size, nothing passed on blindly."""
    if not isinstance(raw, dict):
        raise CloudError('云平台返回格式无效')
    view = {key: _text(raw.get(key), limit) for key, limit in RELEASE_TEXT}
    if not SKILL_ID.fullmatch(view['skill_id']) or not VERSION.fullmatch(view['version']):
        raise CloudError('云平台返回的技能记录不完整（编号或版本不对）')
    view['title'] = view['title'] or view['skill_id']
    view['summary'] = robot_caps.plain_text(view['description'][:500])
    view['requires'], view['optional'] = _cap_views(raw.get('requires')), _cap_views(raw.get('optional'))
    view['parameters'] = robot_caps.parameter_views(raw.get('parameters'))
    # The cloud's own word, or ours when it does not say: a skill is never shown as harmless by default.
    view['motion'] = raw.get('motion') is True or (raw.get('motion') is not False and moves(view['requires'], view['optional']))
    files = raw.get('files') if isinstance(raw.get('files'), list) else []
    view['files'] = [{'path': _text(f.get('path'), 300), 'size': f['size']} for f in files[:skill_packages.MAX_FILES]
                     if isinstance(f, dict) and isinstance(f.get('size'), int) and not isinstance(f['size'], bool)]
    for key in ('size', 'timeout_s', 'created_at'):
        value = raw.get(key)
        view[key] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if isinstance(raw.get('created_at'), str):          # a cloud that writes times as text (ISO 8601)
        view['created_at'] = raw['created_at'][:40]
    return view


def _skill_id(value):
    if not isinstance(value, str) or not SKILL_ID.fullmatch(value):
        raise ValueError('技能编号无效')
    return value


def _version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError('技能版本无效')
    return value


def _release_path(p):
    return '/skills/%s/releases/%s' % (urllib.parse.quote(_skill_id(p.get('skill_id')), safe=''),
                                       urllib.parse.quote(_version(p.get('version')), safe=''))


class CloudSkills:
    def __init__(self, platform):
        self.platform = platform
        self.cloud = platform.cloud
        self.lock = threading.Lock()
        self.previewed = []             # (robot, skill, version, digest) a person was shown since the app started

    # ------------------------------------------------------------- cloud calls
    def _require_login(self):
        if not self.cloud.summary()['logged_in']:
            raise CodedError(LOGIN_REQUIRED, 'cloud_login_required')

    def _call(self, method, path, payload=None):
        """One JSON call to the cloud. Like ``CloudAccount._request`` (no proxy, no redirect, the
        address policy on the real peer), with room for an answer that holds a whole package."""
        self._require_login()
        headers = {'Authorization': 'Bearer ' + self.cloud._token(), 'Accept': 'application/json'}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.cloud.settings()['base_url'] + API_PREFIX + path, data=data,
                                         headers=headers, method=method)
        opener = urllib.request.build_opener(connector.NoRedirect, urllib.request.ProxyHandler({}),
                                             _CheckedHTTPHandler(), _CheckedHTTPSHandler())
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:
                raw = response.read(MAX_ANSWER_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise self._refusal(exc) from None
        except ssl.SSLError:
            raise CloudError('云平台的 HTTPS 证书校验失败，已停止连接') from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise CloudError('云平台的 HTTPS 证书校验失败，已停止连接') from None
            raise CloudError('无法连接云平台；请检查地址和网络', code='cloud_unreachable') from None
        except (TimeoutError, OSError):
            raise CloudError('无法连接云平台；请检查地址和网络', code='cloud_unreachable') from None
        try:
            result = json.loads(raw) if len(raw) <= MAX_ANSWER_BYTES else None
        except (UnicodeError, ValueError):
            result = None
        if not isinstance(result, dict):
            raise CloudError('云平台未返回有效 JSON')
        return result

    def _refusal(self, exc):
        body = {}
        try:
            parsed = json.loads(exc.read(200_001) or b'{}')
            body = parsed if isinstance(parsed, dict) else {}
        except (ValueError, OSError):
            pass                        # an error without a readable body is told by its status below
        code = body.get('code') if isinstance(body.get('code'), str) else None
        if exc.code == 401:
            self.cloud._drop_token()
            code = 'unauthorized'
        said = body.get('error') if isinstance(body.get('error'), str) else None
        message = MESSAGES.get(code) or ERROR_MESSAGES.get(code) or said
        if code == 'invalid_skill_package' and said:
            message = '%s：%s' % (MESSAGES[code], said[:500])
        return CloudError(message or '云平台返回 HTTP %d' % exc.code, exc.code, code, body)

    # ------------------------------------------------------------- reading the library
    @staticmethod
    def _skill_row(raw):
        latest = release_view(raw.get('latest'))
        versions = [v[:50] for v in (raw.get('versions') if isinstance(raw.get('versions'), list) else [])
                    if isinstance(v, str) and VERSION.fullmatch(v[:50])]
        return {'id': latest['skill_id'], 'title': latest['title'], 'summary': latest['summary'], 'latest': latest,
                'versions': versions or [latest['version']]}

    def list(self):
        result = self._call('GET', '/skills')
        rows = result.get('skills') if isinstance(result.get('skills'), list) else []
        return [self._skill_row(row) for row in rows[:MAX_LISTED] if isinstance(row, dict)]

    def overview(self, p=None):
        """What the page needs to draw itself: the account, the library and the app's own examples."""
        account = self.cloud.summary()
        if not account['logged_in']:
            return {'account': account, 'skills': [], 'examples': []}
        try:
            skills = self.list()
        except CloudError as exc:
            if exc.code == 'unauthorized':          # the session ended: the page shows the sign-in notice
                return {'account': self.cloud.summary(), 'skills': [], 'examples': [], 'error': str(exc)}
            raise
        return {'account': self.cloud.summary(), 'skills': skills, 'examples': self._examples(skills)}

    def _examples(self, skills):
        """The app's bundled examples and whether this content is in the library already."""
        held = {(row['id'], version) for row in skills for version in row['versions']}
        latest = {row['id']: row['latest'] for row in skills}
        out = []
        for example in bundled_examples():
            if not example['valid']:
                out.append(example)
                continue
            key, newest = (example['skill_id'], example['version']), latest.get(example['skill_id'])
            # Known only when this version is the newest one: the list carries the digest of that one.
            same = newest['digest'] == example['digest'] if newest and newest['version'] == example['version'] else None
            out.append({**example, 'published': key in held, 'same_content': same})
        return out

    def detail(self, p):
        skill_id = _skill_id(p.get('skill_id'))
        result = self._call('GET', '/skills/' + urllib.parse.quote(skill_id, safe=''))
        releases = result.get('releases') if isinstance(result.get('releases'), list) else []
        return {'id': skill_id, 'releases': [release_view(r) for r in releases[:MAX_LISTED] if isinstance(r, dict)]}

    def _existing(self, skill_id, version):
        """The release the cloud holds under this id and version, or None."""
        try:
            return release_view(self._call('GET', _release_path({'skill_id': skill_id, 'version': version})))
        except CloudError as exc:
            if exc.status == 404:
                return None
            raise

    # ------------------------------------------------------------- publishing
    @staticmethod
    def _local(p):
        """(package, info, validated skill, release digest) of a folder or a bundled example."""
        package, info = skill_packages.read_source(p)
        skill = skill_packages.validate(package)
        return package, info, skill, release_digest(package)

    def publish_preview(self, p):
        """Read and validate a local skill; say what publishing it would do. Sends nothing."""
        self._require_login()
        package, info, skill, digest = self._local(p)
        existing = self._existing(skill['id'], skill['version'])
        state = 'new' if existing is None else 'same' if existing['digest'] == digest else 'conflict'
        view = skill_packages._skill_view(skill)
        return {'path': info['path'], 'example': p.get('example') or None, 'digest': digest, 'skill': view,
                'motion': moves(view['requires'], view['optional']),
                'files': [{'path': k, 'bytes': v} for k, v in sorted(info['sizes'].items())],
                'total_bytes': info['total_bytes'], 'state': state, 'can_publish': state != 'conflict',
                'existing': existing}

    @staticmethod
    def _notes(value):
        if value in (None, ''):
            return ''
        if not isinstance(value, str) or len(value) > MAX_NOTES_CHARS:
            raise ValueError('说明须为不超过 %d 个字符的文字' % MAX_NOTES_CHARS)
        return value.strip()

    def publish(self, p):
        """Publish the previewed skill: the folder is read again and must still have the digest the
        person saw. The same content published twice is one release (the cloud answers the first)."""
        expected = required_text(p.get('digest'), '技能包摘要', 100)
        notes = self._notes(p.get('notes'))
        package, _info, skill, digest = self._local(p)
        if digest != expected:
            raise CodedError('技能文件夹在预览之后发生了变化；请重新预览，确认后再发布', 'skill_changed_since_preview')
        return self._send(package, skill, digest, notes)

    def _send(self, package, skill, digest, notes=''):
        body = {'package': package}
        if notes:
            body['notes'] = notes
        release = release_view(self._call('POST', '/skills/releases', body))
        matches = release['digest'] == digest and (release['skill_id'], release['version']) == (skill['id'], skill['version'])
        self.platform.audit('cloud_skill.published', {'skill': '%s@%s' % (skill['id'], skill['version']), 'digest': digest,
                                                      'digest_matches': matches})
        return {'release': release, 'digest': digest, 'digest_matches': matches}

    def publish_examples(self, p=None):
        """Publish every bundled example to the user's own library. One that is there already with the
        same content counts as done; one whose version holds other content is reported, not replaced."""
        self._require_login()
        results = []
        for example in bundled_examples():
            row = {'example': example['example'], 'title': example.get('title') or example['example']}
            if not example['valid']:
                results.append({**row, 'status': 'invalid', 'error': example['error']})
                continue
            try:
                package, _info, skill, digest = self._local({'example': example['example']})
                sent = self._send(package, skill, digest)
                results.append({**row, 'status': 'published', 'skill_id': skill['id'], 'version': skill['version'],
                                'digest_matches': sent['digest_matches']})
            except CloudError as exc:
                if exc.code in ('unauthorized', 'cloud_unreachable', 'forbidden', 'email_unverified', 'skill_quota_exceeded'):
                    raise               # the same for every example: say it once
                status = 'conflict' if exc.code == 'skill_version_exists' else 'refused'
                results.append({**row, 'status': status, 'error': str(exc), 'code': exc.code})
            except ValueError as exc:
                results.append({**row, 'status': 'invalid', 'error': str(exc)})
        return {'results': results, 'published': sum(r['status'] == 'published' for r in results)}

    def remove(self, p):
        path = _release_path(p)
        result = self._call('DELETE', path)
        if result.get('deleted') is not True:
            raise CloudError('云平台没有确认删除')
        self.platform.audit('cloud_skill.deleted', {'skill': '%s@%s' % (p['skill_id'], p['version'])})
        return {'deleted': True, 'skill_id': p['skill_id'], 'version': p['version']}

    # ------------------------------------------------------------- installing on a robot
    def download(self, p):
        """(release, package) of one release, after the package was checked against it: the digest
        computed here must be the one the cloud recorded, and the id and version the ones asked for."""
        skill_id, version = _skill_id(p.get('skill_id')), _version(p.get('version'))
        result = self._call('GET', _release_path(p) + '/package')
        release, package = release_view(result.get('release')), result.get('package')
        if not isinstance(package, dict) or not isinstance(package.get('files'), dict):
            raise CodedError('云平台给出的技能包不完整，没有继续', 'skill_download_invalid')
        try:
            self._check_files(package['files'])
            skill = skill_packages.validate(package)
            digest = release_digest(package)
        except ValueError as exc:
            raise CodedError('从云端下载的技能包不符合技能包的规则，没有继续：%s' % exc, 'skill_download_invalid') from None
        if (skill['id'], skill['version']) != (skill_id, version) or (release['skill_id'], release['version']) != (skill_id, version):
            raise CodedError('云平台答复的不是所要的那个技能版本，没有继续', 'skill_download_invalid')
        if not DIGEST.fullmatch(release['digest']) or digest != release['digest']:
            raise CodedError('下载到的技能包和云端记录的摘要不一致，没有安装：云端记录 %s，这台电脑算出 %s。'
                             '再试一次会重新下载；仍然不一致请删除这个版本后重新发布' % (release['digest'][:16] or '（空）', digest[:16]),
                             'skill_digest_mismatch')
        return release, package, skill, digest

    @staticmethod
    def _check_files(files):
        """The limits a local folder is read under, applied to what came from the cloud."""
        if not files or len(files) > skill_packages.MAX_FILES:
            raise ValueError('文件数不对')
        total = 0
        for path, text in files.items():
            if not isinstance(path, str) or not isinstance(text, str):
                raise ValueError('文件内容须为文本')
            size = len(text.encode('utf-8'))
            if size > skill_packages.MAX_FILE_BYTES:
                raise ValueError('%s 超过单个文件的大小上限' % path[:100])
            total += size
        if total > skill_packages.MAX_TOTAL_BYTES:
            raise ValueError('技能包超过总大小上限')

    def _robot(self, p):
        """The robot of the project the page is in. Unlike the install of a local folder, the project is
        not optional here: a robot is only reached from its own project."""
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        if robot.get('status') != 'connected':
            raise ValueError('请先对这台机器人做连接检查')
        return robot

    def _remember_preview(self, key):
        with self.lock:
            self.previewed.append(key)
            del self.previewed[:-MAX_PREVIEWS]

    def install_preview(self, p):
        """Download one release, verify it and explain its compatibility with the robot. Sends
        nothing that changes the robot."""
        robot = self._robot(p)
        release, package, skill, digest = self.download(p)
        manifest = robot_caps.fetch_manifest_v2(robot)
        compat = skill_packages.explain(skill, manifest)
        sizes = {path: len(text.encode('utf-8')) for path, text in package['files'].items()}
        self._remember_preview((robot['id'], skill['id'], skill['version'], digest))
        return {'robot': {'id': robot['id'], 'name': robot['name'], 'simulated': manifest.get('simulated') is True},
                'source': 'cloud', 'path': '', 'example': None, 'release': release, 'package_digest': digest,
                'skill': skill_packages._skill_view(skill),
                'files': [{'path': k, 'bytes': v} for k, v in sorted(sizes.items())],
                'total_bytes': sum(sizes.values()), 'compatibility': compat, 'can_install': compat['compatible']}

    def install(self, p):
        """Install the previewed release. ``request_id`` is idempotent as in ``skill_packages.install``:
        the same id returns the recorded outcome and nothing is sent twice."""
        request_id = required_text(p.get('request_id'), '请求标识', 100)
        op_id = 'skill-install-' + request_id
        expected = required_text(p.get('package_digest'), '技能包摘要', 100)
        store = self.platform.store
        robot = self._robot(p)
        existing = next((o for o in store.list('operations') if o['id'] == op_id), None)
        if existing:
            if existing.get('robot_id') != robot['id'] or existing.get('package_digest') != expected:
                raise ValueError('请求标识已用于另一次安装')
            return existing
        if p.get('authorized') is not True:
            raise CodedError('把技能装到机器人上需要你的确认', 'skill_install_not_authorized')
        with self.lock:
            shown = (robot['id'], p.get('skill_id'), p.get('version'), expected) in self.previewed
        if not shown:
            raise CodedError('请先预检这个版本和这台机器人是否兼容，看过结果后再安装', 'skill_preview_required')
        release, package, skill, digest = self.download(p)
        if digest != expected:
            raise CodedError('云端的这个版本在预检之后变了；请重新预检，确认后再安装', 'skill_changed_since_preview')
        compat = skill_packages.explain(skill, robot_caps.fetch_manifest_v2(robot))
        if not compat['compatible']:
            raise ValueError('技能包与这台机器人不兼容，不能安装：' + '；'.join(
                [m['text'] for m in compat['missing_detail']] + compat['violation_texts']))
        from .service import now
        op = {'id': op_id, 'project_id': robot['project_id'], 'robot_id': robot['id'], 'kind': 'skill_install',
              'skill_id': skill['id'], 'version': skill['version'], 'package_digest': expected, 'path': '',
              'example': None, 'source': 'cloud', 'status': 'dispatching', 'created_at': now()}
        store.put('operations', op)
        self._dispatch(robot, op, package)
        store.put('operations', op)
        self.platform.audit('skill.install', {'operation_id': op['id'], 'robot_id': robot['id'], 'source': 'cloud',
                                              'skill': '%s@%s' % (skill['id'], skill['version']), 'status': op['status']},
                            robot['project_id'])
        return {**op, 'skills': robot_caps.installed_skills(robot)}

    @staticmethod
    def _dispatch(robot, op, package):
        """``POST /v2/skills/install`` and what its answer means; the same reading as a local install.
        An unknown outcome is never retried."""
        try:
            response = connector.call(robot['endpoint'], '/v2/skills/install', robot.get('token', ''),
                                      {'request_id': op['id'], 'package': package}, timeout=15)
            if response.get('installed') is not True:
                raise connector.DeviceError('设备未确认安装', status=200, sent=True)
            op.update(status='succeeded', response=skill_packages._installed_summary(response))
            # The robot computed the digest of what it stored: it should be the release's.
            op['digest_matches'] = response.get('digest') == op['package_digest']
        except connector.DeviceError as exc:
            if exc.code == 'incompatible':
                device_compat = exc.body.get('compatibility') if isinstance(exc.body.get('compatibility'), dict) else None
                op.update(status='rejected', error='设备判定技能包与本体不兼容，未安装', compatibility=device_compat)
            elif exc.status is not None and 400 <= exc.status < 500 and exc.code != 'outcome_unknown':
                op.update(status='rejected', error='设备拒绝安装：%s' % exc)
            elif exc.sent is False:
                op.update(status='failed', error='请求未发出：' + str(exc))
            else:
                op.update(status='unknown', error='%s；安装结果未知，不会自动重试，请查看设备上的已安装技能' % exc)


def bundled_examples():
    """The app's own example skills (``runtime/examples/skills``) with the digest each would be
    published under. An example that does not validate is listed with the reason."""
    from . import runtime_bridge
    root = runtime_bridge.examples_dir()
    out = []
    for folder in sorted(root.iterdir()) if root else []:
        if not (folder / 'skill.json').is_file():
            continue
        try:
            package, _info = skill_packages.read_folder(str(folder), bundled=True)
            skill = skill_packages.validate(package)
            out.append({'example': folder.name, 'valid': True, 'skill_id': skill['id'], 'version': skill['version'],
                        'title': skill['title'], 'summary': robot_caps.plain_text(skill['description']),
                        'digest': release_digest(package)})
        except ValueError as exc:
            out.append({'example': folder.name, 'valid': False, 'error': str(exc)})
    return out
