"""Workspace control plane: projects, robot adapters, skills, jobs, evidence and releases."""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from . import connector, robot_caps
from . import cloud_auto_login          # issue #36
from . import robot_scope               # issue #42: robots belong to the account; every task can use them
from . import task_robot                # issue #42: the robot a task is done with
from .errors import CodedError, refusal_fields
from .contracts import FAMILIES, evaluation_simulation_reasons, required_text, string_list, validate_manifest
from .store import Store


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return prefix + '-' + uuid.uuid4().hex[:12]


# ux05-fix2 (Q7b-05/06): how long a reset may take before the robot voids it, and how long the desktop waits.
RESET_EXPIRE_MIN_S, RESET_EXPIRE_MAX_S, RESET_EXPIRE_EXTRA_S = 8, 30, 5
RESET_HTTP_MARGIN_S = 4


def _positive_seconds(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 < v < 1e6 else None


def reset_policy(status, manifest=None):
    """What the desktop sends with ``/v1/reset`` and how it words a hung reset (INTERFACES §9.2).

    * ``reset_expire``: the robot voids a reset that returns after ``expire_s`` -- only when ``/v1/status``
      advertises ``capabilities.reset_expire === true``. Older runtimes ignore ``expire_s`` (Q7b-05).
    * ``expire_s = clamp(max(8, reset_expected_s + 5), 8, 30)`` with ``reset_expected_s`` from ``/v1/status``
      (manifest ``safety.reset_expected_s``); 8 when the robot does not say (Q7b-06).
    * ``timeout_s``: the HTTP timeout of the reset request, always beyond ``expire_s`` so the desktop never gives
      up before the robot does.
    """
    status = status if isinstance(status, dict) else {}
    caps = status.get('capabilities') if isinstance(status.get('capabilities'), dict) else {}
    expected = _positive_seconds(status.get('reset_expected_s'))
    if expected is None:
        safety = status.get('safety') if isinstance(status.get('safety'), dict) else {}
        expected = _positive_seconds(safety.get('reset_expected_s'))
    if expected is None and isinstance(manifest, dict) and isinstance(manifest.get('safety'), dict):
        expected = _positive_seconds(manifest['safety'].get('reset_expected_s'))
    expire = RESET_EXPIRE_MIN_S if expected is None else max(RESET_EXPIRE_MIN_S, expected + RESET_EXPIRE_EXTRA_S)
    expire = min(max(expire, RESET_EXPIRE_MIN_S), RESET_EXPIRE_MAX_S)
    expire = int(expire) if float(expire).is_integer() else round(expire, 1)
    return {'reset_expire': caps.get('reset_expire') is True, 'expire_s': expire,
            'reset_expected_s': expected, 'timeout_s': expire + RESET_HTTP_MARGIN_S}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


AGENTS = [
    {'id': 'solution', 'name': '方案工程师', 'role': '检查项目需求、设备和能力缺口', 'local_tool': 'project.inspect'},
    {'id': 'integration', 'name': '本体集成工程师', 'role': '检查本体连接和应用所需能力', 'local_tool': 'capability.check'},
    {'id': 'application', 'name': '应用开发工程师', 'role': '验证技能流程、输入和模型绑定', 'local_tool': 'application.validate'},
    {'id': 'testing', 'name': '测试工程师', 'role': '生成和执行应用静态 Harness 检查', 'local_tool': 'harness.run'},
    {'id': 'release', 'name': '交付工程师', 'role': '核对应用版本、设备与现场测试证据', 'local_tool': 'release.inspect'},
]


class Platform:
    def __init__(self, root, commerce=None, user_id=None):
        self.store = Store(root)
        self.commerce, self.user_id = commerce, user_id
        from . import ssh_route
        ssh_route.migrate(self.store)               # issue #15: saved one-hop connections become a list of jump hosts, before anything reads them
        try:
            robot_scope.migrate(self.store)         # issue #42: robots, and what is kept about them, belong to the account, not to a task
        except Exception as error:                  # nothing was moved: the workspace opens as it was, and the log says so
            self.audit('robot_scope.migration_failed', {'error': str(error)[:500]})
        from .engineering import Engineering
        self.engineering = Engineering(self)
        from .workflows import Workflows
        self.workflows = Workflows(self)
        from .cloud_account import CloudAccount
        self.cloud = CloudAccount(self)
        from .material_library import MaterialLibrary
        self.library = MaterialLibrary(self)        # issue #33: documents and driver packages kept in the cloud
        from .model_library import ModelLibrary
        self.model_library = ModelLibrary(self)     # issue #51: a material belongs to a brand and model; old ones are moved
        from .package_delivery import PackageDelivery
        self.package_delivery = PackageDelivery(self)   # issue #31: a usable driver package goes from the library to a device, verified there
        from .model_compute import ModelCompute
        self.model_compute = ModelCompute(self)
        self.model_compute.ensure_profile(robot_scope.HOME)     # issue #56: with no task, a robot is investigated on the default model kept with the robots
        from .body_types import BodyTypes
        self.body_types = BodyTypes(self)           # issue #32: the platform's list of body types; a device has the type a person chose
        from .robot_model import RobotModels
        self.robot_models = RobotModels(self)       # issue #48: a robot's brand and model; applications and skills belong to one (after body_types)
        from .task_apps import TaskApps
        self.task_apps = TaskApps(self)             # issue #50: applications belong to the account; a task binds them (after robot_models)
        from .sim_robots import SimRobots
        self.sims = SimRobots(self, self.store.root)
        from .mujoco_sim import MujocoSim
        self.mujoco = MujocoSim(self)               # issue #51: simulation environments for applications, run with MuJoCo
        from .sim_profiles import SimProfiles
        self.sim_profiles = SimProfiles(self)       # issue #54: built-in profiles by type, brand and model; simulated robots are added from them
        from .duck_kit import DuckKit
        self.duck = DuckKit(self)                   # issue #63: the Microduck's model, skill models and training
        from .hardware_init import HardwareInit
        self.hardware = HardwareInit(self)      # issue #2: hardware scan and repair
        from .device_profile import DeviceProfile
        self.device_profile = DeviceProfile(self)   # issue #27: hardware, system, ROS 2 and resources in one record
        from .motion_drivers import MotionDrivers
        self.motion_drivers = MotionDrivers(self)   # issue #28: the motion interfaces a body type should have, against the profile
        from .investigation import Investigation
        self.investigation = Investigation(self)    # issue #6: investigation states, full re-runs, fix chats
        from .heartbeat import Heartbeat
        self.heartbeat = Heartbeat(self)            # issue #24: a heartbeat a minute over SSH; 未接入 / 接入中 / 在线 / 离线
        from .connection_verify import ConnectionVerify
        self.connection_verify = ConnectionVerify(self)     # issue #16: 「验证连接」 logs in over the saved SSH connection, hop by hop
        from .ssh_edit import ConnectionEdit
        self.ssh_edit = ConnectionEdit(self)        # issue #9: change the SSH connection of a connected robot
        from .runtime_link import RuntimeLinks
        self.runtime_link = RuntimeLinks(self)      # issue #17: the management layer over the robot's SSH connection, no tunnel by hand
        from .robot_remove import RobotRemoval
        self.robot_remove = RobotRemoval(self)      # issue #11: delete a robot with its connection and investigations
        from .task_remove import TaskRemoval
        self.task_remove = TaskRemoval(self)        # issue #44: delete a task (任务项目) with what it holds
        from .safety_limits import SafetyLimits
        self.safety_limits = SafetyLimits(self)     # issue #22: safety limits a person fills in per device
        from .runtime_install import RuntimeInstall
        self.runtime_install = RuntimeInstall(self)     # issue #19: install the management layer from the console (after runtime_link, robot_remove)
        from .robot_body import RobotBody
        self.robot_body = RobotBody(self)           # issue #38: a robot's body, simulated or its own ROS 2, and its device configuration
        from .cloud_skills import CloudSkills
        self.cloud_skills = CloudSkills(self)       # issue #43: skill packages kept in the cloud; publish, download, install on a robot
        from .remote_calls import RemoteCalls
        self.remote_calls = RemoteCalls(self)       # issue #43: the owner's "允许远程调用" switch per installed skill
        from .robot_bundle import RobotBundles
        self.robot_bundle = RobotBundles(self)      # issue #43: a robot's programs and scene data, to the cloud and back
        from .run_records import RunRecords
        self.run_records = RunRecords(self)         # issue #70: a run is followed to its end; its result, events and samples are kept (after everything that audits a run)
        # Do not replay physical or remote operations after a process interruption.
        for kind in ('jobs', 'deployments', 'operations', 'model_deployments'):
            for obj in self.store.list(kind):
                if obj.get('status') == 'dispatching':
                    obj.update(status='unknown', error='请求期间服务中断；请到执行端核对结果，不会自动重试')
                    self.store.put(kind, obj)
        self.sim_profiles.remove_old_sims()         # issue #54: once, the simulated robots from before the profiles go
        self.sim_profiles.align_types()             # issue #58: a simulated robot is of its profile's type
        # Simulated robots that were on when the app closed come back (in the background).
        self.sims.resume_all()

    def audit(self, action, details, project_id=None):
        self.store.put('events', {'id': uid('event'), 'action': action, 'details': details, 'project_id': project_id, 'created_at': now()})

    def project(self, key):
        return self.store.get('projects', key)

    def same_project(self, kind, key, project_id):
        obj = self.store.get(kind, key)
        # Issue #42: what is kept at the robots' home (every robot is) is for every task; a request for the home may name a task's asset.
        shared = robot_scope.HOME in (project_id, obj.get('project_id'))
        if obj.get('project_id') != project_id and not shared:
            raise ValueError('所选资产不属于当前项目')
        return obj

    def snapshot(self):
        result = {k: self.store.list(k) for k in ('projects', 'robots', 'services', 'models', 'datasets', 'skills', 'applications', 'jobs', 'evaluations', 'deployments', 'operations', 'scenes', 'model_deployments')}
        for kind in ('robots', 'services'):
            for obj in result[kind]:
                obj['has_token'] = bool(obj.pop('token', ''))
        result.update(projects=robot_scope.tasks(result['projects']), robot_home=robot_scope.HOME)     # issue #42: the robots' home is not a task
        for obj in result['robots']:
            if obj.get('sim'):
                obj['sim'] = self.sims.process_view(obj)
            if 'body' in obj or 'body_applied' in obj:      # issue #38: as the page shows it, with how it relates to the robot
                from . import robot_body
                obj['body'] = robot_body.view(obj)
                obj.pop('body_applied', None)
        result['sim_kinds'] = self.sims.kinds()
        result['sim_catalog'] = self.sim_profiles.catalog()     # issue #54
        result['duck_kit'] = self.duck.catalog()                # issue #63
        from . import sim_profiles
        result['product_aliases'] = sim_profiles.aliases()      # issue #54: other spellings of a brand and model
        result.update(events=self.store.list('events')[:80], agents=AGENTS, families=FAMILIES,
                      system={'version': '0.21.16', 'mode': 'local-preview', 'identity': 'workspace-isolated'})
        result.update(self.engineering.snapshot())
        result.update(self.hardware.snapshot())
        result.update(self.heartbeat.snapshot())    # issue #24: the workspace is shown, so its heartbeat runs; the states are asked for apart
        result.update(self.connection_verify.snapshot(result['robots'], result['agent_sessions']))    # issue #16: robots being onboarded, where 「验证连接」 is not offered
        result.update(self.investigation.snapshot())
        result.update(self.workflows.snapshot())
        # The cloud session token stays in the credential file; only a summary is shown.
        result['cloud_account'] = self.cloud.summary()
        # Issue #36: why the automatic cloud sign-in did not happen, or that it has lapsed (None: never ran).
        result['cloud_account']['auto_login'] = cloud_auto_login.view(self, result['cloud_account']['logged_in'])
        # Issue #4: the TokenFans connection that tasks without a chosen model use (key never included).
        result['model_compute'] = self.model_compute.summary()
        result.update(self.body_types.snapshot())   # issue #32: the list of body types and the interfaces a type can name
        result.update(self.device_profile.snapshot())   # issue #27: which robots have a device profile (for the deletion)
        result.update(self.motion_drivers.snapshot())   # issue #28: per SSH robot, which motion interfaces of its type are missing
        result.update(self.task_apps.snapshot())    # issue #50: which applications each task has bound
        return result

    def add_project(self, p):
        obj = {'id': uid('project'), 'name': required_text(p.get('name'), '项目名称', 100),
               'objective': required_text(p.get('objective'), '交付目标', 2000), 'created_at': now()}
        task_robot.keep(self, obj, p)               # issue #42: the robot the task is done with, chosen when it is created
        self.store.put('projects', obj)
        self.audit('project.created', {'name': obj['name']}, obj['id'])
        self.model_compute.ensure_profile(obj['id'])
        return obj

    def add_robot(self, p):
        project = self.project(p.get('project_id'))
        self.body_types.require(p.get('family'))    # issue #32: a type from the platform's list, not any value of the standard
        endpoint = connector.endpoint_url(p['endpoint']) if p.get('endpoint') else ''
        obj = {'id': uid('robot'), 'project_id': project['id'], 'name': required_text(p.get('name'), '本体名称', 100),
               'family': p['family'], 'endpoint': endpoint, 'token': self.token(p), 'status': 'unconnected',
               'capabilities': [], 'created_at': now()}
        obj.update(self.robot_models.initial(p))    # issue #48: the brand and model, when the caller names them
        with self.store.atomic():                   # issue #32: the type is not removed between the check and the save
            self.body_types.require(obj['family'])
            self.store.put('robots', obj)
        self.audit('robot.registered', {'robot_id': obj['id'], 'family': obj['family']}, project['id'])
        return self.redact(obj)

    @staticmethod
    def token(p):
        value = p.get('token', '')
        if not isinstance(value, str) or len(value) > 2048 or '\n' in value or '\r' in value:
            raise ValueError('接入令牌格式无效')
        return value

    @staticmethod
    def redact(obj):
        return {k: v for k, v in obj.items() if k != 'token'}

    def configure_connection(self, p):
        kind = p.get('kind')
        if kind not in ('robots', 'services'):
            raise ValueError('未知连接类型')
        obj = self.store.get(kind, p.get('id'))
        if obj.get('managed_resource_id'):
            raise ValueError('平台资源凭据由运营端管理')
        if obj.get('sim'):
            raise ValueError('仿真机器人的地址和令牌由应用自动管理，不需要手工配置')
        obj['endpoint'] = connector.endpoint_url(p.get('endpoint'))
        if p.get('token'):
            obj['token'] = self.token(p)
        if p.get('clear_token') is True:
            obj['token'] = ''
        obj['status'] = 'unconnected'
        obj.pop('manifest', None)
        obj.pop('manifest_digest', None)
        obj.pop('manifest_v2', None)
        obj.pop('manifest_v2_error', None)
        if kind == 'robots':
            obj['capabilities'] = []
        self.store.put(kind, obj)
        self.audit('connection.updated', {'kind': kind, 'id': obj['id']}, obj['project_id'])
        return self.redact(obj)

    def connect_robot(self, p):
        obj = self.store.get('robots', p.get('robot_id'))
        if not obj['endpoint']:
            raise ValueError('请先配置设备端适配服务地址')
        try:
            manifest = validate_manifest(connector.request(obj['endpoint'], '/v1/manifest', obj['token']))
            health = connector.request(obj['endpoint'], '/v1/status', obj['token'])
            if health.get('instance_id') != manifest['instance_id']:
                raise ValueError('设备身份不一致')
            # Issue #32: ``family`` stays what a person chose (the store keeps it, see ``Store.owned``); what the
            # device reports is in ``manifest['family']``.
            obj.update(manifest=manifest, manifest_digest=digest(manifest), capabilities=manifest['capabilities'],
                       device_status=health, status='connected', checked_at=now())
            obj['reset_policy'] = reset_policy(health, manifest)
            obj.pop('error', None)
            if isinstance(health.get('estop_latched'), bool):
                self.note_estop(obj, health['estop_latched'], health.get('latch_reason'))
        except ValueError as exc:
            obj.update(status='error', error=str(exc), checked_at=now())
            self.store.put('robots', obj)
            raise
        self.store.put('robots', obj)
        obj = self.robot_models.note_device(obj)    # issue #48: what the device reports is the robot's brand and model
        self.audit('robot.connected', {'robot_id': obj['id'], 'capabilities': obj['capabilities']}, obj['project_id'])
        return self.redact(obj)

    @staticmethod
    def note_estop(robot, latched, reason=None):
        # Record what the device itself reported about its e-stop latch.
        robot.update(estop_latched=latched, estop_checked_at=now())
        if latched and isinstance(reason, str) and reason.strip():
            robot['estop_reason'] = reason.strip()[:500]
        elif not latched:
            robot.pop('estop_reason', None)

    @staticmethod
    def validation_latched(validation):
        if validation.get('estop_latched') is True:
            return True
        cases = validation.get('cases') if isinstance(validation.get('cases'), list) else []
        return any(isinstance(c, dict) and c.get('passed') is False and '急停' in str(c.get('name', ''))
                   for c in cases)

    def add_service(self, p):
        self.project(p.get('project_id'))
        if p.get('kind') not in ('training', 'inference', 'agent', 'evaluation', 'simulation'):
            raise ValueError('服务类型无效')
        obj = {'id': uid('service'), 'project_id': p['project_id'], 'name': required_text(p.get('name'), '服务名称', 100),
               'kind': p['kind'], 'endpoint': connector.endpoint_url(p.get('endpoint')), 'token': self.token(p),
               'status': 'unconnected', 'created_at': now()}
        self.store.put('services', obj)
        self.audit('service.registered', {'id': obj['id'], 'kind': obj['kind']}, p['project_id'])
        return self.redact(obj)

    def connect_service(self, p):
        obj = self.store.get('services', p.get('service_id'))
        if obj.get('managed_resource_id'):
            worker = self.resolve_service(obj)
            connector.request(worker['endpoint'], '/v1/manifest', worker['token'])
            return self.redact(obj)
        try:
            manifest = connector.request(obj['endpoint'], '/v1/manifest', obj['token'])
            if manifest.get('schema') != 'platform.service.v1' or manifest.get('kind') != obj['kind']:
                raise ValueError('服务类型或 platform.service.v1 协议不匹配')
            required_text(manifest.get('version'), '服务版本')
            from .recipes import validate_recipes
            validate_recipes(manifest)
            obj.update(status='connected', manifest=manifest, manifest_digest=digest(manifest), checked_at=now())
            obj.pop('error', None)
        except ValueError as exc:
            obj.update(status='error', error=str(exc), checked_at=now())
            self.store.put('services', obj)
            raise
        self.store.put('services', obj)
        return self.redact(obj)

    def resolve_service(self,service):
        if service.get('managed_resource_id'):
            if not self.commerce:
                raise ValueError('平台资源管理未配置')
            resource=self.commerce.store.get('resources',service['managed_resource_id'])
            if resource['status']!='available':
                raise ValueError('平台资源暂不可用')
            return {**service,'endpoint':resource['endpoint'],'token':resource['token']}
        return service

    def add_model(self, p):
        self.project(p.get('project_id'))
        service = self.same_project('services', p.get('service_id'), p['project_id'])
        if service['kind'] != 'inference':
            raise ValueError('请选择推理服务')
        source = None
        if p.get('source_model_id'):
            source = self.same_project('models', p['source_model_id'], p['project_id'])
            if source.get('status') != 'artifact' or p.get('loaded_confirm') is not True:
                raise ValueError('请确认训练产物已由所选推理服务加载')
            if service['status'] != 'connected':
                raise ValueError('请先检查推理服务连接')
        obj = {'id': uid('model'), 'project_id': p['project_id'], 'name': required_text(p.get('name'), '模型名称', 100),
               'model_ref': required_text(p.get('model_ref'), '模型版本引用', 500), 'service_id': service['id'],
               'task_type': required_text(p.get('task_type'), '能力类型'), 'input_contract': required_text(p.get('input_contract'), '输入契约', 1000),
               'output_contract': required_text(p.get('output_contract'), '输出契约', 1000), 'created_at': now()}
        if source:
            obj.update(source_model_id=source['id'], source_model_digest=source['digest'],
                       provenance=source['provenance'], provenance_digest=source['provenance_digest'])
        obj['digest'] = digest({k:v for k,v in obj.items() if k not in ('id','created_at')})
        self.store.put('models', obj)
        self.audit('model.registered', {'model_id': obj['id']}, p['project_id'])
        return obj

    def preview_training_model(self, p):
        from .results import preview
        return preview(self, p)

    def save_training_model(self, p):
        from .results import save
        return save(self, p)

    def add_scene(self,p):
        self.project(p.get('project_id'))
        obj={'id':uid('scene'),'project_id':p['project_id'],'name':required_text(p.get('name'),'场景名称',100),
             'engine':required_text(p.get('engine'),'仿真引擎',100),'scene_ref':required_text(p.get('scene_ref'),'场景版本引用',1000),
             'version':required_text(p.get('version'),'版本'),'description':required_text(p.get('description'),'场景说明',2000),
             'created_at':now()}
        self.store.put('scenes',obj)
        self.audit('scene.registered',{'scene_id':obj['id']},p['project_id'])
        return obj

    def infer(self, p):
        model = self.store.get('models', p.get('model_id'))
        if model.get('status') == 'artifact':
            raise ValueError('这是训练产物；请先配置已加载该产物的推理服务，再测试调用')
        if not model.get('service_id'):
            raise ValueError('该模型为端侧部署包；请先部署至兼容设备，云端调用需绑定推理服务')
        service = self.same_project('services', model['service_id'], model['project_id'])
        if service['status'] != 'connected':
            raise ValueError('模型推理服务未连接')
        if not isinstance(p.get('inputs'), dict):
            raise ValueError('模型输入需要 JSON 对象，具体字段由模型契约决定')
        service = self.resolve_service(service)
        request_id=uid('infer')
        reservation=None
        if service.get('managed_resource_id'):
            reservation=self.commerce.reserve(self.user_id,service['managed_resource_id'],p.get('max_units'),request_id)
        start = time.monotonic()
        result = connector.request(service['endpoint'], '/v1/infer', service['token'], {'model_ref': model['model_ref'], 'inputs': p['inputs'], 'request_id': request_id,
                                   'max_units':reservation['max_units'] if reservation else None})
        if reservation:
            units=result.get('usage_units')
            if isinstance(units,int) and not isinstance(units,bool) and 0<=units<=reservation['max_units'] and result.get('usage_evidence_ref'):
                self.commerce.settle({'reservation_id':reservation['id'],'actual_units':units,'evidence_ref':result['usage_evidence_ref']},'provider:'+service['managed_resource_id'])
            # Missing/invalid metering keeps the reservation for operator reconciliation.
        self.audit('model.invoked', {'model_id': model['id'], 'duration_ms': (time.monotonic()-start)*1000}, model['project_id'])
        return {'model_id': model['id'], 'response': result, 'duration_ms': (time.monotonic()-start)*1000, 'reservation_id':reservation['id'] if reservation else None}

    def use_catalog_model(self,p):
        self.project(p.get('project_id'))
        if not self.commerce:raise ValueError('平台模型目录未配置')
        entry=self.commerce.store.get('model_catalog',p.get('catalog_model_id'))
        model={**entry,'id':uid('model'),'project_id':p['project_id'],'catalog_model_id':entry['id'],'created_at':now()}
        if entry.get('resource_id'):
            resource=self.commerce.store.get('resources',entry['resource_id'])
            service={'id':uid('managed-service'),'project_id':p['project_id'],'name':resource['name'],'kind':'inference',
                     'managed_resource_id':resource['id'],'endpoint':'platform://'+resource['id'],'token':'','status':'connected','created_at':now()}
            self.store.put('services',service)
            model['service_id']=service['id']
        model['digest']=digest(model)
        self.store.put('models',model)
        self.audit('catalog.model_added',{'model_id':model['id'],'catalog_model_id':entry['id']},p['project_id'])
        return model

    def deploy_model(self,p):
        model=self.store.get('models',p.get('model_id'))
        robot=self.same_project('robots',p.get('robot_id'),model['project_id'])
        if not model.get('package_ref') or not model.get('package_digest'):
            raise ValueError('该模型没有已登记的端侧部署包')
        self.connect_robot({'robot_id':robot['id']})
        robot=self.store.get('robots',robot['id'])
        # Issue #32: a model package names values of the standard, so it is matched against what the device declares.
        if robot['manifest']['family'] not in model.get('families',[]) or model.get('runtime') not in robot['manifest'].get('model_runtimes',[]):
            raise ValueError('本体形态或端侧推理运行时不兼容此模型包')
        if 'model.install' not in robot['capabilities']:
            raise ValueError('设备未声明模型安装能力')
        item={'id':uid('model-release'),'project_id':model['project_id'],'model_id':model['id'],'robot_id':robot['id'],
              'package_digest':model['package_digest'],'status':'dispatching','created_at':now()}
        self.store.put('model_deployments',item)
        try:
            result=connector.request(robot['endpoint'],'/v1/models/install',robot['token'],{'request_id':item['id'],'model':model})
            if result.get('accepted') is not True or result.get('package_digest')!=model['package_digest']:
                raise ValueError('设备未确认安装精确模型版本')
            item.update(status='deployed',response=result)
        except ValueError as exc:
            item.update(status='unknown',error=str(exc)+'；请核对设备端安装状态')
        self.store.put('model_deployments',item)
        return item

    def add_dataset(self, p):
        self.project(p.get('project_id'))
        obj = {'id': uid('dataset'), 'project_id': p['project_id'], 'name': required_text(p.get('name'), '数据集名称', 100),
               'uri': required_text(p.get('uri'), '数据版本地址', 1000), 'format': required_text(p.get('format'), '格式'),
               'version': required_text(p.get('version'), '数据版本'), 'source': required_text(p.get('source'), '来源说明', 1000),
               'created_at': now(), 'status': 'registered'}
        self.store.put('datasets', obj)
        self.audit('dataset.registered', {'dataset_id': obj['id']}, p['project_id'])
        return obj

    def add_skill(self, p):
        self.project(p.get('project_id'))
        capabilities = string_list(p.get('requires'), '依赖的设备能力')
        models = p.get('model_ids', [])
        if not isinstance(models, list):
            raise ValueError('模型绑定须为列表')
        model_digests = {}
        for model_id in models:
            model_digests[model_id] = self.same_project('models', model_id, p['project_id'])['digest']
        obj = {'id': uid('skill'), 'project_id': p['project_id'], 'name': required_text(p.get('name'), '技能名称', 100),
               'requires': capabilities, 'entrypoint': required_text(p.get('entrypoint'), '执行器技能标识'),
               'success_condition': required_text(p.get('success_condition'), '完成条件', 1000),
               'stop_condition': required_text(p.get('stop_condition'), '停止条件', 1000), 'model_ids': models,
               'model_digests': model_digests, 'version': required_text(p.get('version'), '版本'), 'created_at': now()}
        # ux03: optional parameter declarations (skill.json ``parameters``) so application steps get the
        # same generated form as the run dialog. Descriptive only: the device still validates parameters.
        # Stored only when present, so skills registered without them keep their digest.
        decls = p.get('parameters')
        if isinstance(decls, str):
            # ux05-fix2 (Q7b-04): the "登记技能" form sends the declaration text as typed, so ``1.0`` is parsed
            # here (Python keeps it a float) and not by the browser (where it becomes ``1``).
            if not decls.strip():
                decls = None
            else:
                try:
                    decls = json.loads(decls)
                except ValueError:
                    raise ValueError('参数说明不是合法 JSON（应为 skill.json 里 parameters 那样的列表）') from None
        if decls is not None:
            if not isinstance(decls, list) or len(decls) > 50:
                raise ValueError('技能参数说明须为列表（最多 50 项）')
            decls = robot_caps.parameter_views(decls, typed=True)   # Q7b-04: stored with their declared types
            if decls:
                obj['parameters'] = decls
        obj.update(self.robot_models.skill_fields(p))   # issue #48: the brand and model the skill is for
        obj['digest'] = digest(obj)
        self.store.put('skills', obj)
        self.audit('skill.registered', {'skill_id': obj['id']}, p['project_id'])
        return obj

    def application_manifest(self, p):
        self.project(p.get('project_id'))
        robot = self.same_project('robots', p.get('robot_id'), p['project_id'])
        steps = p.get('steps')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
            raise ValueError('应用需要 1–50 个技能步骤')
        normalized, used = [], []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError('每个步骤须为 JSON 对象')
            skill = self.same_project('skills', step.get('skill_id'), p['project_id'])
            params, timeout = step.get('parameters', {}), step.get('timeout_s', 60)
            if not isinstance(params, dict) or isinstance(timeout, bool) or not isinstance(timeout, (int,float)) or not 0 < timeout <= 86400:
                raise ValueError('步骤需要 JSON 参数及 0–86400 秒超时')
            params = robot_caps.typed_parameters(skill.get('parameters'), params)   # ux05-fix (Q7-04)
            used.append(skill)
            normalized.append({'skill_id': skill['id'], 'skill_digest': skill['digest'], 'entrypoint': skill['entrypoint'],
                               'parameters': params, 'timeout_s': timeout, 'requires': skill['requires'],
                               'success_condition': skill['success_condition'], 'stop_condition': skill['stop_condition'],
                               'model_digests': skill['model_digests']})
        return {'project_id': p['project_id'], 'name': required_text(p.get('name'), '应用名称',100),
               'robot_id': robot['id'], 'steps': normalized, 'failure_policy': 'stop_and_report', 'schema': 'robot.application.v1',
               'version': required_text(p.get('version'), '版本'),
               **self.robot_models.application_fields(robot, used)}     # issue #48: the brand and model it belongs to

    def add_application(self, p):
        obj = {**self.application_manifest(p), 'id': uid('app'), 'created_at': now()}
        obj['digest'] = digest(obj)
        self.store.put('applications', obj)
        self.robot_models.bind_skills(obj)          # issue #48
        self.audit('application.created', {'application_id': obj['id']}, p['project_id'])
        return obj

    def preview_revision(self, p):
        from .revisions import preview
        return preview(self, p)

    def save_revision(self, p):
        from .revisions import save
        return save(self, p)

    def application_checks(self, app, robot_id=None):
        robot = self.robot_models.target(app, robot_id)     # issue #48: any robot of the application's brand and model
        required = set(c for s in app['steps'] for c in s['requires'])
        missing = sorted(required-set(robot['capabilities']))
        models = [self.store.get('models', m) for s in app['steps'] for m in s['model_digests']]
        unavailable = [m['name'] for m in models if
                       (self.store.get('services',m['service_id'])['status']!='connected' if m.get('service_id') else
                        not any(d['model_id']==m['id'] and d['robot_id']==robot['id'] and d['status']=='deployed' for d in self.store.list('model_deployments')))]
        cases = [
            {'name':'机器人已建立协议连接', 'passed':robot['status']=='connected', 'detail':robot.get('error','请连接设备端适配服务') if robot['status']!='connected' else robot['endpoint']},
            {'name':'设备能力覆盖所有步骤', 'passed':not missing, 'detail':', '.join(missing) if missing else '能力声明覆盖应用需求'},
            {'name':'技能与模型版本已固定', 'passed':all(s['skill_digest'] for s in app['steps']), 'detail':f"{len(app['steps'])} 个步骤绑定固定摘要"},
            {'name':'所需模型服务已连接', 'passed':not unavailable, 'detail':', '.join(unavailable) if unavailable else '无缺失模型连接'},
            {'name':'步骤有超时、结果和停止条件', 'passed':all(s['timeout_s']>0 and s['success_condition'] and s['stop_condition'] for s in app['steps']), 'detail':'失败处理：停止并报告'},
        ]
        return robot, cases

    def delivery_readiness(self, p):
        from .delivery import readiness
        return readiness(self, p)

    def harness(self, p):
        app = self.store.get('applications', p.get('application_id'))
        robot, cases = self.application_checks(app, p.get('robot_id'))
        obj = {'id':uid('check'), 'project_id':app['project_id'], 'application_id':app['id'], 'application_digest':app['digest'],
               'robot_id':robot['id'], 'robot_manifest_digest':robot.get('manifest_digest'), 'cases':cases,
               'passed':all(c['passed'] for c in cases), 'scope':'static', 'created_at':now()}
        self.store.put('evaluations',obj)
        self.audit('harness.completed', {'report_id':obj['id'],'passed':obj['passed'],'scope':'static'},app['project_id'])
        return obj

    def run_agent(self, p):
        project = self.project(p.get('project_id'))
        agent = next((a for a in AGENTS if a['id']==p.get('agent_id')),None)
        if not agent:
            raise ValueError('Agent 类型无效')
        # Local checks are deterministic tools, explicitly separate from external LLM jobs.
        findings=[]
        robots=[r for r in self.store.list('robots') if r['project_id'] in (project['id'],robot_scope.HOME)]     # issue #42: the robots of the account
        apps=[a for a in self.store.list('applications') if a['project_id']==project['id']]
        apps=apps+[a for a in self.task_apps.applications(project['id']) if a not in apps]     # issue #50: the applications the task has bound
        if not robots:
            findings.append('项目尚未登记机器人：添加本体并连接设备端适配服务。')
        for robot in robots:
            if robot['status']!='connected':
                findings.append(f"{robot['name']} 尚未连接；需要适配服务地址与能力清单。")
        if not apps:
            findings.append('项目尚无应用：先注册技能，再编排技能步骤。')
        reports=[]
        if agent['id'] in ('integration','application','testing','release'):
            for app in apps:
                try:
                    report=self.harness({'application_id':app['id']})
                except CodedError as exc:                # issue #48: no robot of its brand and model: said, and the rest goes on
                    findings.append(f"{app['name']}：{exc}")
                    continue
                reports.append(report['id'])
                findings.extend(f"{app['name']}：{c['name']} — {c['detail']}" for c in report['cases'] if not c['passed'])
        if agent['id']=='release':
            for app in apps:
                onsite=[r for r in self.store.list('evaluations') if r.get('application_digest')==app['digest'] and r['scope']=='onsite' and r['passed']]
                if not onsite:
                    findings.append(f"{app['name']} 尚无该版本的现场测试证据。")
        obj={'id':uid('agent'), 'project_id':project['id'], 'kind':'local-agent', 'name':agent['name'], 'status':'succeeded',
             'created_at':now(), 'finished_at':now(), 'outputs':{'findings':findings or ['当前配置检查未发现缺项；现场执行能力仍需实测。'], 'report_ids':reports},
             'logs':[{'tool':agent['local_tool'],'message':'执行确定性项目检查；未生成或运行代码，未操作机器人'}]}
        self.store.put('jobs',obj)
        self.audit('agent.completed',{'job_id':obj['id']},project['id'])
        return obj

    def preview_job(self,p):
        from .recipes import prepare
        return prepare(self,p)

    def add_job(self,p,*,prepared=None):
        if p.get('recipe_id') and prepared is None:
            from .recipes import prepare
            prepared=prepare(self,p,verify=True)
            p={**p,'kind':prepared['kind']}
        self.project(p.get('project_id'))
        kind=p.get('kind')
        if kind not in ('training','agent','evaluation','simulation'):
            raise ValueError('任务类型无效')
        worker=self.same_project('services',p.get('service_id'),p['project_id'])
        if worker['kind']!=kind:
            raise ValueError('执行服务类型不匹配')
        config=prepared['config'] if prepared else p.get('config')
        if not isinstance(config,dict) or not config:
            raise ValueError('执行配置须为非空 JSON 对象')
        # Keep resource and tool policy explicit; no platform-wide hardware assumption.
        budget=p.get('max_minutes')
        if isinstance(budget,bool) or not isinstance(budget,int) or not 1<=budget<=43200:
            raise ValueError('执行时长上限须为 1–43200 分钟')
        obj={'id':uid('job'),'schema':'platform.job.v1','project_id':p['project_id'],'kind':kind,
             'name':required_text(p.get('name'),'任务名称',100),'service_id':worker['id'],'config':config,
             'max_minutes':budget,'status':'draft','created_at':now(),'outputs':{},'logs':[]}
        if prepared:
            obj.update(recipe=prepared['recipe'],input_summary=prepared['input_summary'],recipe_inputs=prepared['inputs'],
                       provider_name=prepared['provider_name'],provider_fingerprint=prepared['provider_fingerprint'],preview_digest=prepared['preview_digest'])
        obj['manifest_digest']=digest(obj)
        self.store.put('jobs',obj)
        return obj

    def add_managed_job(self,p,user_id,commerce):
        resource=commerce.store.get('resources',p.get('resource_id'))
        if resource['kind'] not in ('training','agent','evaluation','simulation'):
            raise ValueError('该资源不是作业执行资源')
        self.project(p.get('project_id'))
        # Managed credentials stay in the operator's catalog, never in tenant snapshots.
        prepared=None
        if p.get('recipe_id'):
            from .recipes import prepare
            prepared=prepare(self,p,verify=True,resource=resource)
        service={'id':uid('managed-service'),'project_id':p['project_id'],'name':resource['name'],
                 'kind':resource['kind'],'managed_resource_id':resource['id'],'status':'connected','created_at':now(),
                 'endpoint':'platform://'+resource['id'],'token':''}
        # Reserve before creating a dispatchable job. Insufficient credit leaves
        # no accidentally usable unmetered platform service.
        job_id=uid('job')
        reservation=commerce.reserve(user_id,resource['id'],p.get('max_units'),job_id)
        self.store.put('services',service)
        try:
            job=self.add_job({**p,'kind':resource['kind'],'service_id':service['id']},prepared=prepared)
        except Exception:
            commerce.settle({'reservation_id':reservation['id'],'actual_units':0,'evidence_ref':'validation-rejected:'+job_id},'platform')
            raise
        reservation['job_id']=job['id']
        commerce.store.put('reservations',reservation)
        job.update(managed_resource_id=resource['id'],reservation_id=reservation['id'],max_units=reservation['max_units'])
        self.store.put('jobs',job)
        return job

    def submit_job(self,p):
        with self.store.atomic():
            job=self.store.get('jobs',p.get('job_id'))
            if job['status']!='draft':
                raise ValueError('只允许提交草稿；不自动重试状态未知的请求')
            worker=self.resolve_service(self.store.get('services',job['service_id']))
            if worker['status']!='connected':
                raise ValueError('执行服务未连接')
            if worker.get('managed_resource_id') and not job.get('reservation_id'):
                raise ValueError('平台作业缺少预算预留，请从资源中心创建')
            from .recipes import check_dispatch
            check_dispatch(worker,job)
            job.update(status='dispatching',submitted_at=now())
            self.store.put('jobs',job)
        try:
            result=connector.request(worker['endpoint'],'/v1/jobs',worker['token'],{**job,'request_id':job['id']})
            remote_id=required_text(result.get('job_id'),'执行端任务 ID')
            job.update(status='submitted',remote_id=remote_id,logs=[{'tool':'executor.submit','message':'执行端已接收任务，等待状态同步'}])
        except ValueError as exc:
            job.update(status='unknown',error=str(exc)+'；请在执行端核对是否接收，不会自动重试')
        self.store.put('jobs',job)
        self.audit('job.submitted',{'job_id':job['id'],'status':job['status']},job['project_id'])
        return job

    def sync_job(self,p):
        job=self.store.get('jobs',p.get('job_id'))
        if not job.get('remote_id'):
            raise ValueError('没有执行端任务 ID；请先提交并确认接收')
        worker=self.resolve_service(self.store.get('services',job['service_id']))
        result=connector.request(worker['endpoint'],'/v1/jobs/'+quote(job['remote_id'],safe=''),worker['token'])
        if result.get('status') not in ('queued','running','succeeded','failed','cancelled'):
            raise ValueError('执行端任务状态无效')
        if not isinstance(result.get('outputs',{}),dict) or not isinstance(result.get('logs',[]),list):
            raise ValueError('执行端日志或产物格式无效')
        job.update(status=result['status'],outputs=result.get('outputs',{}),logs=result.get('logs',[]),synced_at=now())
        self.store.put('jobs',job)
        if job.get('reservation_id') and job['status'] in ('succeeded','failed','cancelled'):
            reservation=self.commerce.store.get('reservations',job['reservation_id'])
            units=result.get('usage_units')
            if reservation['status']=='held' and isinstance(units,int) and not isinstance(units,bool) and 0<=units<=reservation['max_units'] and result.get('usage_evidence_ref'):
                self.commerce.settle({'reservation_id':reservation['id'],'actual_units':units,'evidence_ref':result['usage_evidence_ref']},'provider:'+job['managed_resource_id'])
        return job

    def cancel_job(self,p):
        job=self.store.get('jobs',p.get('job_id'))
        if job['status']=='draft':
            job['status']='cancelled'
            if job.get('reservation_id'):
                self.commerce.settle({'reservation_id':job['reservation_id'],'actual_units':0,'evidence_ref':'cancelled-before-submit:'+job['id']},'platform')
        elif job.get('remote_id') and job['status'] in ('submitted','queued','running'):
            worker=self.resolve_service(self.store.get('services',job['service_id']))
            connector.request(worker['endpoint'],'/v1/jobs/'+quote(job['remote_id'],safe='')+'/cancel',worker['token'],{'request_id':uid('cancel')})
            job['status']='cancelling'
        else:
            raise ValueError('当前状态不能取消；未知请求请在执行端核对')
        self.store.put('jobs',job)
        return job

    def onsite_test(self,p):
        if p.get('hardware_authorized') is not True:
            raise ValueError('现场测试可能产生机器人动作，需要明确确认现场条件')
        app=self.store.get('applications',p.get('application_id'))
        static=self.harness({'application_id':app['id'],'robot_id':p.get('robot_id')})
        if not static['passed']:
            raise ValueError('先修复静态 Harness 检查中的缺项')
        robot=self.store.get('robots',static['robot_id'])
        # Refresh identity and capability manifest before every physical request.
        self.connect_robot({'robot_id':robot['id']})
        robot=self.store.get('robots',robot['id'])
        if robot['manifest_digest']!=static['robot_manifest_digest']:
            raise ValueError('设备能力版本已改变，请重新检查应用')
        validation=connector.request(robot['endpoint'],'/v1/validate',robot['token'],{'application':robot_caps.typed_application(self,app)})
        if validation.get('accepted') is not True:
            if self.validation_latched(validation):
                self.note_estop(robot, True, '设备执行前检查报告急停已锁存')
                self.store.put('robots', robot)
                raise ValueError('设备急停已锁存；请在现场确认安全后“解除急停”，再进行现场测试')
            # ux02: say which device-side check failed instead of a bare refusal.
            failed = [c for c in (validation.get('cases') or []) if isinstance(c, dict) and c.get('passed') is False]
            detail = '；'.join(f"{str(c.get('name', ''))[:120]}：{str(c.get('detail', ''))[:200]}" for c in failed[:5])
            raise ValueError('设备端未通过应用执行前检查' + ('（' + detail + '）' if detail else ''))
        operation={'id':uid('onsite'),'project_id':app['project_id'],'kind':'onsite','status':'dispatching','application_id':app['id'],
                   'application_digest':app['digest'],'robot_id':robot['id'],'robot_manifest_digest':robot['manifest_digest'],'created_at':now()}
        self.store.put('operations',operation)
        # INTERFACES 4.1: asynchronous on-site test, with fallback for older devices.
        from .evaluations import dispatch
        operation=dispatch(self,app,robot,operation)
        self.audit('onsite.requested',{'operation_id':operation['id'],'status':operation['status']},app['project_id'])
        return operation

    def deploy(self,p):
        app=self.store.get('applications',p.get('application_id'))
        report=self.same_project('evaluations',p.get('report_id'),app['project_id'])
        robot=self.robot_models.target(app,p.get('robot_id') or report.get('robot_id'))   # issue #48: the robot the evidence is from
        if robot['status']!='connected':
            raise ValueError('机器人未连接')
        self.connect_robot({'robot_id':robot['id']})
        robot=self.store.get('robots',robot['id'])
        from .delivery import require_real_evidence
        require_real_evidence(robot, report)
        if report['scope']!='onsite' or not report['passed'] or report['application_digest']!=app['digest'] or report.get('robot_id')!=robot['id'] or report.get('robot_manifest_digest')!=robot.get('manifest_digest'):
            raise ValueError('部署需要该应用与该本体当前版本通过的现场测试证据')
        from .delivery import require_current_evidence
        require_current_evidence(self, app, robot, report)
        check=self.harness({'application_id':app['id'],'robot_id':robot['id']})
        if not check['passed']:
            raise ValueError('当前依赖检查未通过，不能发布')
        obj={'id':uid('release'),'project_id':app['project_id'],'application_id':app['id'],'application_digest':app['digest'],
             'application_name':app['name'],'robot_id':robot['id'],'report_id':report['id'],'created_at':now(),'status':'dispatching'}
        self.store.put('deployments',obj)
        try:
            response=connector.request(robot['endpoint'],'/v1/deploy',robot['token'],{'request_id':obj['id'],'application':robot_caps.typed_application(self,app),'report_id':report['id']})
            if response.get('accepted') is not True:
                raise ValueError('设备未确认部署')
            obj.update(status='deployed',remote_release_id=required_text(response.get('release_id'),'设备端版本 ID'))
        except ValueError as exc:
            obj.update(status='unknown',error=str(exc)+'；请核对设备端版本')
        self.store.put('deployments',obj)
        self.audit('release.dispatched',{'release_id':obj['id'],'status':obj['status']},app['project_id'])
        return obj

    def operate(self,p):
        release=self.store.get('deployments',p.get('release_id'))
        if release['status']!='deployed':
            raise ValueError('请先完成设备部署')
        action=p.get('action')
        if action not in ('start','stop'):
            raise ValueError('未知运行操作')
        if action=='start' and p.get('hardware_authorized') is not True:
            raise ValueError('启动会产生机器人动作，需要明确确认现场条件')
        robot=self.store.get('robots',release['robot_id'])
        if action=='start':
            self.connect_robot({'robot_id':robot['id']})
            robot=self.store.get('robots',robot['id'])
            report=self.store.get('evaluations',release['report_id'])
            from .delivery import require_real_evidence
            require_real_evidence(robot, report)
            if robot.get('estop_latched') is True:
                raise ValueError('设备急停已锁存；请在现场确认安全后“解除急停”，再启动工作')
            if robot.get('manifest_digest')!=report.get('robot_manifest_digest'):
                raise ValueError('机器人版本已变化，请重新测试并部署')
            app=self.store.get('applications',release['application_id'])
            from .delivery import require_current_evidence
            require_current_evidence(self, app, robot, report)
            _, cases=self.application_checks(app,robot['id'])
            if not all(c['passed'] for c in cases):
                raise ValueError('当前应用依赖检查未通过，请修复后再启动')
        obj={'id':uid('operation'),'project_id':release['project_id'],'release_id':release['id'],'robot_id':robot['id'],
             'kind':action,'status':'dispatching','created_at':now()}
        self.store.put('operations',obj)
        try:
            response=connector.request(robot['endpoint'],'/v1/'+action,robot['token'],{'request_id':obj['id'],'release_id':release['remote_release_id'],'hardware_authorized':action=='start'})
            if response.get('accepted') is not True:
                raise ValueError('设备未确认操作')
            obj.update(status='accepted',response=response)
        except ValueError as exc:
            obj.update(status='unknown',error=str(exc)+'；请核对机器人状态')
        self.store.put('operations',obj)
        self.audit('robot.'+action,{'operation_id':obj['id'],'status':obj['status']},release['project_id'])
        return obj

    def reset_robot(self, p):
        """Clear a latched e-stop through POST /v1/reset. Needs on-site confirmation;
        the request_id is idempotent and an unconfirmed outcome is never retried.

        ux04 (Q5-03): a refusal (e.g. 409 ``reset_in_progress`` while the adapter's
        previous reset has not returned) is recorded as ``rejected`` with the device's
        own reason and ``code``; a reset that does not answer in time is checked
        against ``/v1/status`` so the user sees "复位仍在进行" instead of a network error.
        """
        if p.get('hardware_authorized') is not True:
            raise CodedError('解除急停后机器人可以再次运动，需要在现场确认安全并明确授权', 'authorization_required')
        request_id = p.get('request_id')
        if request_id is not None:
            request_id = required_text(request_id, '请求标识', 100)
            existing = next((o for o in self.store.list('operations') if o['id'] == 'reset-' + request_id), None)
            if existing:
                if existing.get('robot_id') != p.get('robot_id'):
                    raise ValueError('请求标识已用于其他操作')
                # Same request again: report the recorded outcome, never resend.
                return existing
        robot = self.store.get('robots', p.get('robot_id'))
        self.connect_robot({'robot_id': robot['id']})
        robot = self.store.get('robots', robot['id'])
        policy = reset_policy(robot.get('device_status'), robot.get('manifest'))
        obj = {'id': 'reset-' + request_id if request_id else uid('reset'), 'project_id': robot['project_id'],
               'robot_id': robot['id'], 'kind': 'reset', 'status': 'dispatching', 'created_at': now(),
               # Q7b-05/06: what was promised to the robot, so the outcome is worded for this runtime.
               'expire_s': policy['expire_s'], 'reset_expire': policy['reset_expire']}
        if robot.get('sim'):
            obj['simulated'] = True
        self.store.put('operations', obj)
        try:
            started = time.monotonic()
            # Q7-10 / Q7b-06: a reset that returns after expire_s is void on robots that support it; the desktop
            # waits longer than that, so it never gives up before the robot does.
            response = connector.call(robot['endpoint'], '/v1/reset', robot['token'],
                                      {'request_id': obj['id'], 'hardware_authorized': True,
                                       'expire_s': policy['expire_s']}, timeout=policy['timeout_s'])
            if response.get('latched') is True or (response.get('accepted') is not True and response.get('latched') is not False):
                raise ValueError('设备未确认解除急停')
            obj.update(status='accepted', response=response)
            robot = self.store.get('robots', robot['id'])
            self.note_estop(robot, False)
            self.store.put('robots', robot)
        except connector.DeviceError as exc:
            obj.update(self._reset_failure(robot, exc, policy, time.monotonic() - started))
        except ValueError as exc:
            obj.update(status='unknown', error=str(exc) + '；请在现场核对急停状态，不会自动重试')
        self.store.put('operations', obj)
        self.audit('robot.reset', {'operation_id': obj['id'], 'robot_id': robot['id'], 'status': obj['status'],
                                   **({'code': obj['code']} if obj.get('code') else {})}, robot['project_id'])
        return obj

    def _reset_failure(self, robot, exc, policy=None, elapsed=0.0):
        """What a failed /v1/reset means, from the device's status code and error code (never from text)."""
        policy = policy or reset_policy(None)
        out = {'code': exc.code} if exc.code else {}
        if exc.status is not None:
            out['device_status'] = exc.status
        # ux05 (Q6-08): the runtime's reset_in_progress_s, cause and hint reach the browser.
        out.update({k: v for k, v in refusal_fields(exc.body).items() if k in ('reset_in_progress_s', 'cause', 'hint')})
        if exc.status is not None and 400 <= exc.status < 500 and exc.code and exc.code != 'outcome_unknown':
            # A structured refusal (runtime error code): nothing was done, the e-stop stays latched and a
            # later retry is valid. A 4xx without a code (legacy gateways answer 400 for an unconfirmed
            # execution too) stays "unknown" below.
            return {**out, 'status': 'rejected', 'error': str(exc) + ('' if '锁存' in str(exc) else '；急停保持锁存')}
        if exc.sent is False:
            return {**out, 'status': 'failed', 'error': '请求未发出：' + str(exc)}
        # No answer in time (or a 5xx): the reset may still be running on the robot. Ask it.
        try:
            status = connector.call(robot['endpoint'], '/v1/status', robot['token'], timeout=3)
        except connector.DeviceError:
            status = {}
        # Q7b-08: this very request outlived expire_s (the desktop waits longer than that): on a robot that
        # supports it, this reset is now void and the e-stop stays latched whatever it does later.
        expired = exc.status is None and elapsed >= policy['expire_s']
        late = {'own_reset': True, 'expired': True} if expired else {}
        if status.get('reset_in_progress') is True:
            return {'status': 'unknown', 'code': 'reset_in_progress', 'device_status': None, **late,
                    # Q7b-08: once this reset has outlived its limit the runtime's "请稍候" hint misleads; the
                    # desktop says what happens instead.
                    **refusal_fields({'reset_in_progress_s': status.get('reset_in_progress_s'),
                                      'hint': None if expired else status.get('reset_hint')}),
                    'error': ('这次复位（适配器 reset）超过 %g 秒仍没有完成；' % policy['expire_s'] if expired else
                              '复位请求已发给机器人，但机器人上一次复位（适配器 reset）一直没有完成；') +
                             '急停' + ('仍然锁存' if status.get('estop_latched') is not False else '状态待核对')}
        if status.get('estop_latched') is False:
            return {**out, 'status': 'unknown', 'error': str(exc) + '；机器人报告急停已不再锁存，请在现场核对'}
        return {**out, **late, 'status': 'unknown', 'error': str(exc) + '；请在现场核对急停状态，不会自动重试'}

    # ---- round 02: robot detail, skill packages, async on-site test, robot stop
    def robot_detail(self, p):
        from .robot_caps import detail
        return detail(self, p)

    def preview_skill_package(self, p):
        from .skill_packages import preview
        return preview(self, p)

    def install_skill_package(self, p):
        from .skill_packages import install
        return install(self, p)

    def poll_onsite(self, p):
        from .evaluations import poll
        return poll(self, p)

    def stop_robot(self, p):
        """Robot-level stop (POST /v1/stop), e.g. while an on-site test runs.

        Always available: no identity refresh, evidence or e-stop check comes
        first, so nothing can delay or block it. The outcome is never retried.
        """
        robot = self.store.get('robots', p.get('robot_id'))
        if robot_scope.elsewhere(robot, p.get('project_id')):      # issue #42: a robot of the account is every task's
            raise ValueError('该机器人不属于当前项目')
        if not robot.get('endpoint'):
            raise ValueError('这台机器人没有配置设备端地址，无法发送停止请求；请使用现场硬件急停')
        obj = {'id': uid('stop'), 'project_id': robot['project_id'], 'robot_id': robot['id'], 'kind': 'stop',
               'status': 'dispatching', 'created_at': now()}
        if p.get('application_id'):
            obj['application_id'] = str(p['application_id'])[:100]
        self.store.put('operations', obj)
        try:
            response = connector.request(robot['endpoint'], '/v1/stop', robot['token'],
                                         {'request_id': obj['id'], 'reason': '桌面端请求停止'})
            if response.get('accepted') is not True:
                raise ValueError('设备未确认停止')
            obj.update(status='accepted', response=response)
        except ValueError as exc:
            obj.update(status='unknown', error=str(exc) + '；请立即在现场确认机器人已停止，必要时使用硬件急停')
        self.store.put('operations', obj)
        self.audit('robot.stop', {'operation_id': obj['id'], 'robot_id': robot['id'], 'status': obj['status']},
                   robot['project_id'])
        return obj

    # ---- ux01: one-click simulated robots, live status, run a skill, example gallery
    def add_sim_robot(self, p):
        if p.get('profile_id') is not None:         # issue #54: by type, brand and model
            return self.sim_profiles.add(p)
        return self.sims.add(p)

    def robot_live(self, p):
        from .sim_robots import live
        return live(self, p)

    def robot_safety(self, p):
        from .safety_watch import watch
        return watch(self, p)

    def run_robot_skill(self, p):
        from .sim_robots import run_skill
        return run_skill(self, p)

    def skill_examples(self, p):
        from .sim_robots import gallery
        return gallery(self, p)

    def close(self):
        self.heartbeat.close()      # issue #24: the timer, its SSH sessions and its checks end before the store is closed
        self.runtime_link.close()   # issue #17: the SSH sessions of the links to the robots' management layers
        self.runtime_install.close()   # issue #19: installs under way stop waiting (what a robot does goes on there)
        self.mujoco.close()         # issue #51: scenes being generated or checked stop waiting
        self.sims.close()
        self.workflows.close()
        self.engineering.close()
        self.package_delivery.close()   # issue #31: package deliveries a person started stop before the store is closed
        self.robot_bundle.close()       # issue #43: collecting or installing a bundle stops before the store is closed
        self.run_records.close()        # issue #70: runs being followed are left before the store is closed
        self.store.close()
