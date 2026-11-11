"""Read-only delivery guidance and shared current-evidence requirements."""
from .contracts import evaluation_simulation_reasons

SIMULATION_EVIDENCE_MESSAGE = '仿真本体的测试结果不能作为现场证据；请在真实本体上进行现场测试'


def report_simulation_reasons(report):
    """Reasons a stored report is simulation evidence (also catches pre-fix records)."""
    if report.get('scope') == 'simulation':
        return report.get('simulation_reasons') or ['报告范围为仿真']
    if report.get('scope') != 'onsite':
        return []
    return evaluation_simulation_reasons({'simulated': report.get('robot_simulated')}, report.get('details') or {})


def require_real_evidence(robot, report):
    reasons = report_simulation_reasons(report)
    if (robot.get('manifest') or {}).get('simulated') is True:
        reasons = reasons or ['本体能力清单声明 simulated=true（仿真本体）']
    if reasons:
        from .errors import CodedError
        raise CodedError(SIMULATION_EVIDENCE_MESSAGE + '（' + '；'.join(reasons) + '）', 'simulated_evidence')


def estop_state(platform, robot):
    """Latched e-stop: the device's own report wins; otherwise inferred from the last stop request.

    ``robot['estop_latched']`` is set by the control plane from /v1/status,
    from a /v1/validate case such as "急停未锁存", and cleared by an accepted reset.
    """
    # A reset whose outcome is unknown does not count as cleared.
    last = next((o for o in platform.store.list('operations')
                 if o.get('robot_id') == robot['id'] and o.get('kind') in ('stop', 'start')
                 or o.get('robot_id') == robot['id'] and o.get('kind') == 'reset' and o.get('status') == 'accepted'),
                None)
    reason = (robot.get('device_status') or {}).get('latch_reason') or robot.get('estop_reason')
    if robot.get('estop_latched') is True:
        return {'latched': True, 'source': 'device', 'reason': reason or '设备报告急停已锁存', 'last_operation': last}
    device_checked_after_stop = (robot.get('estop_latched') is False and last
                                 and (robot.get('estop_checked_at') or '') > last['created_at'])
    if last and last['kind'] == 'stop' and not device_checked_after_stop:
        return {'latched': True, 'source': 'last_stop', 'last_operation': last,
                'reason': '最近一次操作是停止；设备可能已锁存急停，需现场确认后解除'}
    return {'latched': False, 'source': None, 'reason': None, 'last_operation': last}


def evidence(platform, app, robot):
    reports = [e for e in platform.store.list('evaluations')
               if e.get('application_id') == app['id'] and e.get('application_digest') == app['digest']
               and e.get('robot_id') == robot['id'] and e.get('robot_manifest_digest') == robot.get('manifest_digest')
               and e.get('scope') == 'onsite' and not report_simulation_reasons(e)]
    reports.sort(key=lambda e: e['created_at'], reverse=True)
    latest = reports[0] if reports else None
    unresolved = next((o for o in platform.store.list('operations')
                       if o.get('application_id') == app['id'] and o.get('kind') == 'onsite'
                       and o.get('status') in ('dispatching', 'running', 'unknown')
                       and (not latest or o['created_at'] >= latest['created_at'])), None)
    return latest, unresolved


def require_current_evidence(platform, app, robot, report):
    latest, unresolved = evidence(platform, app, robot)
    if unresolved and unresolved.get('status') == 'running':
        raise ValueError('现场测试进行中；结果返回前不能发布或启动')
    if unresolved:
        raise ValueError('较新的现场测试结果尚待核对，不能使用旧报告发布或启动')
    if not latest or latest['id'] != report['id'] or not latest.get('passed'):
        raise ValueError('需要当前应用与本体最新一次通过的现场测试证据；旧报告不能覆盖后续失败')


def latest_simulation(platform, app, robot):
    reports = [e for e in platform.store.list('evaluations')
               if e.get('application_id') == app['id'] and e.get('application_digest') == app['digest']
               and e.get('robot_id') == robot['id'] and e.get('scope') in ('onsite', 'simulation')
               and report_simulation_reasons(e)]
    reports.sort(key=lambda e: e['created_at'], reverse=True)
    return reports[0] if reports else None


def readiness(platform, p):
    app = platform.same_project('applications', p.get('application_id'), p.get('project_id'))
    robot, cases = platform.application_checks(app)
    onsite, unresolved = evidence(platform, app, robot)
    static = next((e for e in platform.store.list('evaluations') if e.get('scope') == 'static'
                   and e.get('application_id') == app['id'] and e.get('application_digest') == app['digest']
                   and e.get('robot_manifest_digest') == robot.get('manifest_digest') and e.get('robot_id') == robot['id']), None)
    releases = [r for r in platform.store.list('deployments') if r.get('application_id') == app['id']
                and r.get('application_digest') == app['digest'] and r.get('robot_id') == robot['id']]
    releases.sort(key=lambda r: r['created_at'], reverse=True)
    release = releases[0] if releases else None
    prepared = all(c['passed'] for c in cases)
    verified = bool(prepared and onsite and onsite.get('passed') and not unresolved)
    published = bool(verified and release and release['status'] == 'deployed' and release['report_id'] == onsite['id'])
    uncertain_release = bool(release and release['status'] in ('dispatching', 'unknown'))
    # INTERFACES 4.1: an asynchronous on-site test that is still running.
    running = unresolved if unresolved and unresolved.get('status') == 'running' else None
    if running:
        next_step = 'testing'
    elif unresolved or uncertain_release:
        next_step = 'reconcile'
    elif not prepared:
        next_step = 'prepare'
    elif not static or not static['passed']:
        next_step = 'check'
    elif not verified:
        next_step = 'onsite'
    elif not published:
        next_step = 'publish'
    else:
        next_step = 'start'
    operations = [o for o in platform.store.list('operations') if o.get('application_id') == app['id']
                  or (release and o.get('release_id') == release['id'])
                  or (o.get('kind') == 'reset' and o.get('robot_id') == robot['id'])]
    simulated = (robot.get('manifest') or {}).get('simulated') is True
    simulation = latest_simulation(platform, app, robot)
    estop = estop_state(platform, robot)
    return {'application': app,
            'robot': {**{k: robot.get(k) for k in ('id', 'name', 'status', 'family', 'manifest_digest')},
                      'simulated': simulated, 'vendor': (robot.get('manifest') or {}).get('vendor'),
                      'model': (robot.get('manifest') or {}).get('model')},
            'simulation_report': simulation,
            'simulation_notice': SIMULATION_EVIDENCE_MESSAGE if simulated or simulation else None,
            'estop': estop, 'can_reset': estop['latched'] and robot.get('status') == 'connected',
            'checks': cases, 'prepared': prepared, 'static_report': static, 'onsite_report': onsite,
            'unresolved_onsite': unresolved, 'onsite_running': running, 'release': release, 'operations': operations[:10], 'next_step': next_step,
            'can_test': prepared and not unresolved, 'can_publish': verified and not uncertain_release,
            'can_start': published, 'can_stop': bool(release and release['status'] == 'deployed'),
            # Robot-level stop needs only a device address; it is never blocked.
            'can_stop_robot': bool(robot.get('endpoint')),
            # Round 01 P3: a latched e-stop blocks "准备启动工作" until it is reset.
            'start_blocked': 'estop_latched' if estop['latched'] else None}
