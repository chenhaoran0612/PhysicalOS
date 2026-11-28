"""Issue #73 「应用编排」: what is published to the cloud for a brand and model is a skill of that brand and model.

The owner's words: 「应用编排现在无法正确关联到已经发布的能力。需要关联上，能力是发布给品牌型号的」. A release in the
cloud skill library (「技能发布」, issue #43) names the brand and model it is for (issue #48). The form that creates an
application (issue #68, ``model_skills.ModelSkills.ready``) offered only the skills on connected robots of the brand
and model and the bundled ones that fit them; with no robot of it connected it offered nothing at all.

* :func:`releases_for`: every release the organisation's library holds for a brand and model (all versions; another
  spelling of the same brand and model is the same one, ``robot_model.key``). The library lists a skill with its
  newest release only, so a skill with more versions is read on its own page. Signed out, the cloud is not asked.
* :func:`install`: one of them onto a robot, through the path of 「技能加载」 (``cloud_skills.CloudSkills``): the
  package is downloaded and checked against the digest the cloud recorded, the release must be for the robot's
  brand and model and the package must fit the robot's live manifest, and the operation is recorded as there. It is
  called before an on-site test (``ModelSkills.install_missing``), which a person has confirmed for this robot.

A release is offered for its brand and model as published: whether it fits a robot is judged when it is installed.
"""
import http.client
import secrets
import threading
import time
from urllib.parse import quote

from . import cloud_skills as library, robot_model
from .cloud_account import CloudError

READ_TIMEOUT_S = 8          # the longest the form waits for the cloud, all reads together; the console's own pages wait cloud_skills.TIMEOUT_S
READERS = 8                 # pages of skills with several versions read at the same time
TRANSPORT = (http.client.HTTPException, OSError)    # what a broken answer raises besides CloudError (a body cut off, a reset)
SIGNED_OUT = ('cloud_login_required', 'unauthorized')
CHANGED = '云端的这个版本和列出它时不一样了（内容或需要的设备能力变了），没有安装'


def _item(release):
    """A release as ``model_skills`` handles a skill (its ``_plain`` shape), with the digest it was published under."""
    return {'id': release['skill_id'], 'version': release['version'], 'title': release['title'],
            'requires': [c['id'] for c in release['requires']], 'parameters': release['parameters'],
            'digest': release['digest']}


def _read(skills, path, key, deadline):
    """The rows under ``key`` of one GET to the cloud. Its socket waits at most what is left of the form's wait; a
    cloud that drips its answer is cut off by :func:`_together`."""
    try:
        found = skills._call('GET', path, timeout=max(0.1, deadline - time.monotonic())).get(key)
    except TRANSPORT as error:
        raise CloudError('云平台的回答不完整：%s' % (str(error)[:200] or type(error).__name__), code='cloud_unreachable') from None
    return [row for row in found[:library.MAX_LISTED] if isinstance(row, dict)] if isinstance(found, list) else []


def _together(deadline, jobs):
    """The results of ``jobs`` (callables), run at most ``READERS`` at a time, waited for until ``deadline`` in all.

    Daemon threads, not a pool: a read still going when the wait is over is left behind and must not hold up quitting
    the app. The first error of a job is raised."""
    results, gate = [None] * len(jobs), threading.BoundedSemaphore(READERS)

    def run(index, job):
        with gate:
            try:
                results[index] = (True, job())
            except Exception as error:          # handed to the caller below
                results[index] = (False, error)

    threads = [threading.Thread(target=run, args=(i, job), daemon=True, name='published-skills-read') for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        raise CloudError('云平台回答太慢，%d 秒内没有读完' % READ_TIMEOUT_S, code='cloud_unreachable')
    for done, value in results:
        if not done:
            raise value
    return [value for _, value in results]


def _readable(parse, rows):
    """What ``parse`` makes of each row; a row the console cannot read is left out, not the others with it."""
    out = []
    for row in rows:
        try:
            out.append(parse(row))
        except ValueError:
            continue
    return out


def _releases(skills):
    """Every release of the organisation's library: the list holds the newest of each skill, its page the others.
    Read as ``CloudSkills.list`` and ``detail`` read them, within ``READ_TIMEOUT_S`` in all: a person is looking at a form."""
    deadline = time.monotonic() + READ_TIMEOUT_S
    listed, = _together(deadline, [lambda: _read(skills, '/skills', 'skills', deadline)])
    rows = _readable(library.CloudSkills._skill_row, listed)
    out = [row['latest'] for row in rows if row['versions'] == [row['latest']['version']]]
    several = [row['id'] for row in rows if row['versions'] != [row['latest']['version']]]
    pages = _together(deadline, [lambda key=key: _read(skills, '/skills/' + quote(key, safe=''), 'releases', deadline) for key in several])
    return out + [release for page in pages for release in _readable(library.release_view, page)]


def releases_for(platform, product):
    """``{'status': 'ok' | 'signed_out' | 'failed', 'items': [...], 'error': text or None}``: the releases for ``product``.

    Never raises for the cloud's sake: what could not be read is said in ``status`` and ``error``."""
    if product is None:
        return {'status': 'ok', 'items': [], 'error': None}
    if not platform.cloud.summary()['logged_in']:
        return {'status': 'signed_out', 'items': [], 'error': None}
    wanted = robot_model.key(product)
    try:
        releases = _releases(platform.cloud_skills)
    except ValueError as error:                 # CloudError and CodedError: unreachable, signed out meanwhile, a bad answer
        if getattr(error, 'code', None) in SIGNED_OUT:
            return {'status': 'signed_out', 'items': [], 'error': None}
        return {'status': 'failed', 'items': [], 'error': str(error)[:300]}
    items = [_item(r) for r in releases if r.get('product') and robot_model.key(r['product']) == wanted]
    return {'status': 'ok', 'items': sorted(items, key=lambda i: (i['id'], i['version'])), 'error': None}


def install(platform, robot, item):
    """Install the release ``item`` names on ``robot``; the operation record as 「技能加载」 keeps it, or
    ``{'status': 'refused', 'error': ...}`` when it was not sent (the release does not fit the robot, the cloud refused)."""
    cloud = platform.cloud_skills
    asked = {'robot_id': robot['id'], 'project_id': robot.get('project_id'), 'skill_id': item['id'], 'version': item['version']}
    try:
        preview = cloud.install_preview(asked)
        needs = sorted(r['id'] for r in preview['skill']['requires'])
        if preview['package_digest'] != item.get('digest') or needs != sorted(item['requires']):
            return {'status': 'refused', 'error': CHANGED}
        if not preview['can_install']:
            compat = preview['compatibility']
            reasons = [m['text'] for m in compat.get('missing_detail', [])] + list(compat.get('violation_texts', []))
            return {'status': 'refused', 'error': '技能包与这台机器人不兼容，不能安装：' + ('；'.join(reasons) or '设备能力不符')}
        return cloud.install({**asked, 'package_digest': preview['package_digest'], 'authorized': True,
                              'request_id': 'auto-' + secrets.token_hex(12)})
    except (ValueError, *TRANSPORT) as error:   # CloudError, CodedError, a broken download: nothing was sent, or the robot said no
        return {'status': 'refused', 'error': str(error)[:500]}

