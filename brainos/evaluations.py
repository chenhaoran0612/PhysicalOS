"""On-site test results, synchronous or asynchronous (INTERFACES §4.1).

``POST /v1/evaluate`` is sent with ``async: true``. A current runtime answers
at once with ``{accepted, evaluation_id, status: "running"}``; the desktop then
polls ``GET /v1/evaluations/{id}`` until ``completed`` or ``unknown``. An older
device ignores the flag and returns the full result in the same response; that
result is recorded exactly as before.

Rules kept from round 01 (ADR-011): a simulated body or any evidence that is
not ``evidence_kind == "device_run"`` is stored as ``scope: "simulation"`` and
never counts for publish/start. An ``unknown`` outcome is recorded as such and
never retried.
"""
from urllib.parse import quote

from . import connector
from .contracts import evaluation_simulation_reasons, required_text

UNKNOWN_EVALUATION = '设备报告本次现场测试结果未知（例如运行时重启）；不会自动重试，请在设备端核对'


def record_result(platform, app, robot, operation, response):
    """Store the evaluation result as a report and finish the operation.

    Raises ValueError when the result does not match this application and body
    version; the caller then marks the operation ``unknown``.
    """
    from .service import now, uid
    if not isinstance(response, dict):
        raise ValueError('设备返回的现场测试结果格式无效')
    if (response.get('application_digest') != operation['application_digest']
            or response.get('robot_manifest_digest') != operation['robot_manifest_digest']):
        raise ValueError('设备返回证据与应用或本体版本不匹配')
    evidence = required_text(response.get('evidence_ref'), '现场证据引用', 1000)
    if not isinstance(response.get('passed'), bool):
        raise ValueError('现场结果缺少 passed 布尔字段')
    report = {'id': uid('onsite-report'), 'project_id': app['project_id'], 'application_id': app['id'],
              'application_digest': app['digest'], 'robot_id': robot['id'],
              'robot_manifest_digest': operation['robot_manifest_digest'], 'scope': 'onsite',
              'passed': response['passed'], 'evidence_ref': evidence, 'details': response, 'created_at': now()}
    # INTERFACES 1.1: simulated bodies or non device_run evidence are kept,
    # with passed unchanged, but never as on-site evidence.
    reasons = evaluation_simulation_reasons(robot.get('manifest'), response)
    if reasons:
        from .delivery import SIMULATION_EVIDENCE_MESSAGE
        report.update(id=uid('simulation-report'), scope='simulation', simulated=True,
                      simulation_reasons=reasons, evidence_note=SIMULATION_EVIDENCE_MESSAGE)
    platform.store.put('evaluations', report)
    operation.update(status='succeeded', report_id=report['id'], scope=report['scope'], finished_at=now())
    operation.pop('poll_error', None)
    if reasons:
        operation['simulated'] = True
    return report


def dispatch(platform, app, robot, operation):
    """Send the evaluation. Leaves the operation ``running`` (async) or finished (sync)."""
    from .robot_caps import typed_application
    payload = {'application': typed_application(platform, app), 'request_id': operation['id'], 'hardware_authorized': True, 'async': True}
    try:
        response = connector.request(robot['endpoint'], '/v1/evaluate', robot['token'], payload)
        evaluation_id = response.get('evaluation_id')
        if evaluation_id is not None:
            evaluation_id = required_text(evaluation_id, '现场测试编号', 200)
            if response.get('accepted') is not True:
                raise ValueError('设备未受理现场测试')
            operation.update(mode='async', evaluation_id=evaluation_id, status='running')
            # A device may already have finished (e.g. stopped at once).
            if response.get('status') == 'completed' and isinstance(response.get('result'), dict):
                record_result(platform, app, robot, operation, response['result'])
            elif response.get('status') == 'unknown':
                operation.update(status='unknown', error=UNKNOWN_EVALUATION)
        else:
            operation['mode'] = 'sync'        # older device: the result came back in this response
            record_result(platform, app, robot, operation, response)
    except ValueError as exc:
        operation.update(status='unknown', error=str(exc) + '；请在设备端核对执行情况')
    platform.store.put('operations', operation)
    return operation


def poll(platform, p):
    """One status read for a running asynchronous on-site test (GET only)."""
    from .service import now
    operation = platform.store.get('operations', p.get('operation_id'))
    if p.get('project_id') and operation.get('project_id') != p['project_id']:
        raise ValueError('该现场测试不属于当前项目')
    if operation.get('kind') != 'onsite':
        raise ValueError('这不是现场测试记录')
    if operation.get('status') != 'running' or not operation.get('evaluation_id'):
        return operation                      # finished or unknown: nothing to read, never re-sent
    robot = platform.store.get('robots', operation['robot_id'])
    unexpected = None
    try:
        result = connector.request(robot['endpoint'], '/v1/evaluations/' + quote(operation['evaluation_id'], safe=''),
                                   robot['token'])
        if result.get('evaluation_id') not in (None, operation['evaluation_id']):
            raise ValueError('设备返回了另一个现场测试的状态')
        status = result.get('status')
        if not isinstance(status, str) or not status:
            raise ValueError('设备返回的现场测试状态无效')
        if status not in ('running', 'completed', 'unknown'):
            # INTERFACES 4.1 allows only three states. Anything else (e.g. an
            # internal "rejected") is treated as unknown: recorded, never retried.
            unexpected = status[:50]
            status = 'unknown'
    except ValueError as exc:
        # The test may still be running on the device: keep it running, show why
        # the status could not be read, and let the user stop it at any time.
        with platform.store.atomic():
            operation = platform.store.get('operations', operation['id'])
            if operation.get('status') == 'running':
                operation.update(poll_error=str(exc), polled_at=now())
                platform.store.put('operations', operation)
        return operation
    with platform.store.atomic():
        operation = platform.store.get('operations', operation['id'])
        if operation.get('status') != 'running':
            return operation                  # another poll finished it first
        if status == 'running':
            operation.update(polled_at=now())
            operation.pop('poll_error', None)
            if isinstance(result.get('progress'), (int, float)) and not isinstance(result.get('progress'), bool):
                operation['progress'] = max(0.0, min(1.0, float(result['progress'])))
        elif status == 'unknown':
            note = UNKNOWN_EVALUATION
            if unexpected:
                note = f'设备返回了契约之外的状态“{unexpected}”，按结果未知处理；不会自动重试，请在设备端核对'
            operation.update(status='unknown', error=note, finished_at=now())
        else:
            app = platform.store.get('applications', operation['application_id'])
            try:
                record_result(platform, app, robot, operation, result.get('result'))
            except ValueError as exc:
                operation.update(status='unknown', error=str(exc) + '；请在设备端核对执行情况')
        platform.store.put('operations', operation)
    if operation['status'] != 'running':
        platform.audit('onsite.finished', {'operation_id': operation['id'], 'status': operation['status']},
                       operation['project_id'])
    return operation
