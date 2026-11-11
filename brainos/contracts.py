"""Versioned extension contracts. No robot family, model vendor or compute owner is assumed."""
from typing import Protocol

FAMILIES = {'manipulator': '机械臂', 'mobile': '移动机器人', 'quadruped': '四足机器人',
            'humanoid': '人形机器人', 'aerial': '无人机', 'composite': '复合机器人', 'custom': '其他 / 自定义'}


class RobotAdapter(Protocol):
    """Implement on the user's edge machine. All hardware-specific semantics live here."""
    def manifest(self) -> dict: ...
    def status(self) -> dict: ...
    def validate(self, application: dict) -> dict: ...
    def evaluate(self, request: dict) -> dict: ...
    def deploy(self, request: dict) -> dict: ...
    def start(self, request: dict) -> dict: ...
    def stop(self, request: dict) -> dict: ...
    def install_model(self, request: dict) -> dict: ...
    # Optional: clear a latched emergency stop (POST /v1/reset).
    # def reset(self, request: dict) -> dict: ...


class ModelProvider(Protocol):
    def describe(self) -> dict: ...
    def infer(self, inputs: dict) -> dict: ...


class JobExecutor(Protocol):
    def submit(self, manifest: dict) -> dict: ...
    def inspect(self, job_id: str) -> dict: ...
    def cancel(self, job_id: str) -> dict: ...


def required_text(value, label, max_length=200):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f'{label}需要 1–{max_length} 个字符')
    return value.strip()


def string_list(value, label, maximum=100):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f'{label}需要 1–{maximum} 项')
    return list(dict.fromkeys(required_text(x, label) for x in value))


def validate_manifest(payload):
    if not isinstance(payload, dict) or payload.get('schema') != 'robot.adapter.v1':
        raise ValueError('设备端需要提供 robot.adapter.v1 能力清单')
    for key in ('instance_id', 'name', 'adapter_version'):
        required_text(payload.get(key), key)
    if payload.get('family') not in FAMILIES:
        raise ValueError('设备形态声明无效')
    caps = string_list(payload.get('capabilities'), '设备能力')
    result={k: payload[k] for k in ('schema', 'instance_id', 'name', 'adapter_version', 'family')} | {'capabilities': caps}
    if 'model_runtimes' in payload:
        result['model_runtimes']=string_list(payload['model_runtimes'],'端侧模型运行时')
    # Optional v1 identity fields (INTERFACES 1.1). They are kept only when the
    # device declares them, so legacy manifests keep the same digest; any other
    # unknown field is still dropped. ``simulated`` takes part in the manifest
    # digest, so a simulated body never shares evidence with real hardware.
    if 'simulated' in payload:
        if not isinstance(payload['simulated'], bool):
            raise ValueError('simulated 须为布尔值')
        result['simulated'] = payload['simulated']
    for key in ('vendor', 'model'):
        if key in payload:
            result[key] = required_text(payload[key], key)
    return result


def evaluation_simulation_reasons(manifest, response):
    """Why an on-site evaluation result cannot count as real-device evidence.

    An empty list means the device positively declared a real run
    (``evidence_kind == "device_run"``) on a body not declared as simulated.
    """
    reasons = []
    if (manifest or {}).get('simulated') is True:
        reasons.append('本体能力清单声明 simulated=true（仿真本体）')
    if response.get('simulated') is True:
        reasons.append('设备返回 simulated=true')
    kind = response.get('evidence_kind')
    if kind != 'device_run':
        reasons.append(f'证据类型为 {kind}，不是 device_run' if kind else '设备未声明 evidence_kind=device_run')
    ref = response.get('evidence_ref')
    if isinstance(ref, str) and ref.strip().lower().startswith('simulation://'):
        reasons.append('证据引用以 simulation:// 开头')
    return reasons
