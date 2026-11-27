import argparse
import json
import os
import signal
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import CODE_RESEND_S, CODE_TTL_S, Identity
from .mailer import Mailer
from .errors import error_payload
from .httpd import FastBindHTTPServer
from . import robot_pictures               # issue #53: pictures of robot models
from .service import Platform
from .commerce import Commerce
from .material_parse import MaterialParse   # issue #34: parsing results of library materials
from . import cloud_auto_login              # issue #36: an app sign-in signs into the cloud too
from . import robot_scope                   # issue #42: robots belong to the account, not to the task that is open
from . import task_robot                    # issue #42: the robot a task is done with

ROOT=Path(__file__).resolve().parents[1]


class Application:
    def __init__(self, root, operator_email=''):
        self.root=Path(root)
        self.identity=Identity(self.root)
        self.mailer=Mailer(self.root)
        # Built-in test account (see auth.TEST_EMAIL) while no mail server is configured; NEUXMIND_TEST_ACCOUNT=0 turns it off.
        self.identity.enable_test_account(self.mailer.mode=='outbox' and os.environ.get('NEUXMIND_TEST_ACCOUNT','1')!='0')
        self.commerce=Commerce(self.root/'platform-catalog')
        self.operator_email=operator_email.strip().lower()
        self.workspaces={}
        self.lock=threading.Lock()
        # Issue #36: off here, so a test server never reaches a cloud on this computer; main() turns it on.
        self.cloud_auto_login=False
        self.cloud_login=cloud_auto_login.CloudAutoLogin(self)

    def workspace(self,user):
        with self.lock:
            if user['id'] not in self.workspaces:
                self.workspaces[user['id']]=Platform(self.root/'workspaces'/user['id'],self.commerce,user['id'])
            return self.workspaces[user['id']]

    def send_code(self,email,purpose,code):
        """Mails a verification code. code=None (unknown address on password reset) answers exactly like a sent code."""
        result={'sent':True,'email':email,'resend_after':CODE_RESEND_S,'expires_in':CODE_TTL_S,'delivery':self.mailer.mode}
        if self.mailer.mode=='outbox':
            outbox=self.mailer.outbox
            try:
                outbox='~/'+str(outbox.resolve().relative_to(Path.home().resolve()))
            except ValueError:
                pass
            result['outbox']=str(outbox)
        if code is None:
            return result
        from .mail_templates import verification_mail      # issue #37: a formal mail, plain text and HTML
        subject,body,html=verification_mail(purpose,code,CODE_TTL_S//60)
        try:
            self.mailer.send(email,subject,body,html=html)
        except Exception:
            self.identity.discard_code(email,purpose)
            raise
        if self.mailer.dev_echo():
            result['dev_code']=code
        return result

    def close(self):
        for workspace in self.workspaces.values():
            workspace.close()
        self.identity.close()
        self.commerce.close()


def make_handler(application):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            pass

        def respond(self,data,status=200,mime='application/json; charset=utf-8',cookie=None):
            body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','same-origin')
            self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            if cookie is not None:
                self.send_header('Set-Cookie',cookie)
            self.end_headers()
            self.wfile.write(body)

        def trusted(self):
            allowed=[f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}']
            if self.headers.get('Host') not in allowed or self.headers.get('Sec-Fetch-Site')=='cross-site':
                self.respond({'error':'不允许跨站请求'},403)
                return False
            origin=self.headers.get('Origin')
            if origin and origin not in ['http://'+h for h in allowed]:
                self.respond({'error':'不允许跨站请求'},403)
                return False
            return True

        def token(self):
            cookie=SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie',''))
                return cookie['brain_session'].value if 'brain_session' in cookie else ''
            except Exception:
                return ''

        def user(self):
            user=application.identity.user(self.token())
            if not user:
                self.respond({'error':'请先登录工作空间','code':'unauthorized'},401)
            return user

        def do_PUT(self):
            # Issue #33: one binary chunk of a material upload; every other operation stays JSON over POST.
            if not self.trusted():
                return
            from .material_library import handle_chunk
            return handle_chunk(self,application)

        def do_GET(self):
            if not self.trusted():
                return
            # Issue #2: its own static files, kept apart from the shared table below.
            extra={'/hardware.js':('hardware.js','text/javascript; charset=utf-8'),'/hardware.css':('hardware.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #7: shared look (dropdowns, hover states, readable data views), also kept apart.
            extra=extra or {'/dropdown.js':('dropdown.js','text/javascript; charset=utf-8'),'/dropdown.css':('dropdown.css','text/css; charset=utf-8'),'/hover.css':('hover.css','text/css; charset=utf-8'),'/login_hero.css':('login_hero.css','text/css; charset=utf-8'),'/readable.css':('readable.css','text/css; charset=utf-8'),'/readable.js':('readable.js','text/javascript; charset=utf-8'),'/readable_dom.js':('readable_dom.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #6: its own static files, likewise.
            extra=extra or {'/investigation.js':('investigation.js','text/javascript; charset=utf-8'),'/investigation.css':('investigation.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #32: the list of body types and the type of a device, likewise.
            extra=extra or {'/body_types.js':('body_types.js','text/javascript; charset=utf-8'),'/body_types.css':('body_types.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #13: 默认允许执行 and problems waiting for on-site work, likewise.
            extra=extra or {'/repair_policy.js':('repair_policy.js','text/javascript; charset=utf-8'),'/repair_policy.css':('repair_policy.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #8: the status page of a real machine, likewise.
            extra=extra or {'/live_status.js':('live_status.js','text/javascript; charset=utf-8'),'/live_status.css':('live_status.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #27: the device profile (hardware, system, ROS 2, resources), likewise.
            extra=extra or {'/device_profile.js':('device_profile.js','text/javascript; charset=utf-8'),'/device_profile.css':('device_profile.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #28: the motion driver check (the interfaces a body type should have, against the profile), likewise.
            extra=extra or {'/motion_drivers.js':('motion_drivers.js','text/javascript; charset=utf-8'),'/motion_drivers.css':('motion_drivers.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #29: approvals of a repair that installs and starts the motion driver, likewise.
            extra=extra or {'/repair_driver.js':('repair_driver.js','text/javascript; charset=utf-8'),'/repair_driver.css':('repair_driver.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #24: the connection state and the last heartbeat on the cards and the status page, likewise.
            extra=extra or {'/heartbeat.js':('heartbeat.js','text/javascript; charset=utf-8'),'/heartbeat.css':('heartbeat.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #16: 「验证连接」 of a robot's saved SSH connection, likewise.
            extra=extra or {'/connection_verify.js':('connection_verify.js','text/javascript; charset=utf-8'),'/connection_verify.css':('connection_verify.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #9: its own static file, likewise.
            extra=extra or {'/ssh_edit.js':('ssh_edit.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #17: the link to a robot's management layer over SSH, likewise.
            extra=extra or {'/runtime_link.js':('runtime_link.js','text/javascript; charset=utf-8'),'/runtime_link.css':('runtime_link.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #19: installing the management layer from the console, likewise.
            extra=extra or {'/runtime_install.js':('runtime_install.js','text/javascript; charset=utf-8'),'/runtime_install.css':('runtime_install.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #20: autostart (the confirmation, the card line), likewise.
            extra=extra or {'/runtime_autostart.js':('runtime_autostart.js','text/javascript; charset=utf-8'),'/runtime_autostart.css':('runtime_autostart.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #38: a robot's body (simulated or its own ROS 2) and its device configuration, likewise.
            extra=extra or {'/robot_body.js':('robot_body.js','text/javascript; charset=utf-8'),'/robot_body.css':('robot_body.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #33: the material library page, likewise.
            extra=extra or {'/material_library.js':('material_library.js','text/javascript; charset=utf-8'),'/material_library.css':('material_library.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #34: parsing results, and the materials of a device in its profile, likewise.
            extra=extra or {'/material_parse.js':('material_parse.js','text/javascript; charset=utf-8'),'/material_parse.css':('material_parse.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #30: what a repair session found in the library, likewise.
            extra=extra or {'/repair_materials.js':('repair_materials.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # issue #51: the library by brand and model, and MuJoCo simulation environments
            extra=extra or {'/model_library.js':('model_library.js','text/javascript; charset=utf-8'),'/model_library.css':('model_library.css','text/css; charset=utf-8'),
                            '/mujoco_sim.js':('mujoco_sim.js','text/javascript; charset=utf-8'),'/mujoco_sim.css':('mujoco_sim.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #36: why the cloud is not signed in (the automatic sign-in), likewise.
            extra=extra or {'/cloud_auto_login.js':('cloud_auto_login.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #42: running an installed skill on a real robot (the confirmations of the run form), likewise.
            extra=extra or {'/skill_run.js':('skill_run.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #42: the robot a task is done with (chosen when the task is created), likewise.
            extra=extra or {'/task_robot.js':('task_robot.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #56: a robot is added and looked after with no task, likewise.
            extra=extra or {'/robot_no_task.js':('robot_no_task.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #11: deleting a robot, likewise.
            extra=extra or {'/robot_remove.js':('robot_remove.js','text/javascript; charset=utf-8'),'/robot_remove.css':('robot_remove.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #49: the pages that are hidden for now, likewise.
            extra=extra or {'/hidden_pages.js':('hidden_pages.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #41: the layout of "我的机器人", likewise.
            extra=extra or {'/robots_page.js':('robots_page.js','text/javascript; charset=utf-8'),'/robots_page.css':('robots_page.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #41: the one frame of a robot's dialogs, likewise.
            extra=extra or {'/robot_dialogs.js':('robot_dialogs.js','text/javascript; charset=utf-8'),'/robot_dialogs.css':('robot_dialogs.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #44: 任务工作台 as a dashboard of the account's tasks, likewise.
            extra=extra or {'/task_workbench.js':('task_workbench.js','text/javascript; charset=utf-8'),'/task_workbench.css':('task_workbench.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #46: the skills of a task and how they are configured, on the page of the task, likewise.
            extra=extra or {'/task_skills.js':('task_skills.js','text/javascript; charset=utf-8'),'/task_skills.css':('task_skills.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #22: safety limits per device, likewise.
            extra=extra or {'/safety_limits.js':('safety_limits.js','text/javascript; charset=utf-8'),'/safety_limits.css':('safety_limits.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #53: the brands and models a robot can be chosen as, and the pictures of the models.
            extra=extra or {'/robot_catalog.js':('robot_catalog.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path) or robot_pictures.static(urlparse(self.path).path,ROOT/'web')
            # Issue #61: the one control a brand or a model is chosen or typed in, likewise.
            extra=extra or {'/combo.js':('combo.js','text/javascript; charset=utf-8'),'/combo.css':('combo.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #54: adding a simulated robot by type, brand and model, likewise.
            extra=extra or {'/sim_profiles.js':('sim_profiles.js','text/javascript; charset=utf-8'),'/sim_profiles.css':('sim_profiles.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #63: the Microduck's skill models and their training, on the training page.
            extra=extra or {'/duck.js':('duck.js','text/javascript; charset=utf-8'),'/duck.css':('duck.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #43: skills kept in the cloud, remote calls of a robot's skills, its programs and scene data, likewise.
            extra=extra or {'/cloud_skills.js':('cloud_skills.js','text/javascript; charset=utf-8'),'/cloud_skills.css':('cloud_skills.css','text/css; charset=utf-8'),'/remote_calls.js':('remote_calls.js','text/javascript; charset=utf-8'),'/remote_calls.css':('remote_calls.css','text/css; charset=utf-8'),'/robot_bundle.js':('robot_bundle.js','text/javascript; charset=utf-8'),'/robot_bundle.css':('robot_bundle.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #48: a robot's brand and model; applications and skills by brand and model.
            extra=extra or {'/robot_model.js':('robot_model.js','text/javascript; charset=utf-8'),'/robot_model.css':('robot_model.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #50: the page of a task (its bound applications), and binding on 机器人应用.
            extra=extra or {'/task_apps.js':('task_apps.js','text/javascript; charset=utf-8'),'/task_apps.css':('task_apps.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #57: every page of the sidebar is titled the way 工作台 is.
            extra=extra or {'/page_titles.js':('page_titles.js','text/javascript; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #60: 机器人应用 as a waterfall of applications, the page of one application, the page of the skills.
            extra=extra or {'/app_gallery.js':('app_gallery.js','text/javascript; charset=utf-8'),'/app_gallery.css':('app_gallery.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #68: 机器人应用 shows applications only; one is created for a task and a brand and model.
            extra=extra or {'/app_create.js':('app_create.js','text/javascript; charset=utf-8'),'/app_create.css':('app_create.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            # Issue #62: why a skill cannot be installed on a robot that is not connected, and the step that fixes it.
            extra=extra or {'/skill_install_guide.js':('skill_install_guide.js','text/javascript; charset=utf-8'),'/skill_install_guide.css':('skill_install_guide.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            if extra:
                return self.respond((ROOT/'web'/extra[0]).read_bytes(),mime=extra[1])
            path=urlparse(self.path).path
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/inventory.js':('inventory.js','text/javascript; charset=utf-8'),'/engineering.js':('engineering.js','text/javascript; charset=utf-8'),'/ssh_command.js':('ssh_command.js','text/javascript; charset=utf-8'),'/ssh_jump.js':('ssh_jump.js','text/javascript; charset=utf-8'),'/extensions.js':('extensions.js','text/javascript; charset=utf-8'),'/workflows.js':('workflows.js','text/javascript; charset=utf-8'),'/adaptation.js':('adaptation.js','text/javascript; charset=utf-8'),'/journey.js':('journey.js','text/javascript; charset=utf-8'),'/planner.js':('planner.js','text/javascript; charset=utf-8'),'/results.js':('results.js','text/javascript; charset=utf-8'),'/delivery.js':('delivery.js','text/javascript; charset=utf-8'),'/revisions.js':('revisions.js','text/javascript; charset=utf-8'),'/robot_detail.js':('robot_detail.js','text/javascript; charset=utf-8'),'/cloud.js':('cloud.js','text/javascript; charset=utf-8'),'/model_compute.js':('model_compute.js','text/javascript; charset=utf-8'),'/onsite.js':('onsite.js','text/javascript; charset=utf-8'),'/sim.js':('sim.js','text/javascript; charset=utf-8'),'/ux02.js':('ux02.js','text/javascript; charset=utf-8'),'/ux03.js':('ux03.js','text/javascript; charset=utf-8'),'/ux04_errors.js':('ux04_errors.js','text/javascript; charset=utf-8'),'/ux04.js':('ux04.js','text/javascript; charset=utf-8'),'/ux05.js':('ux05.js','text/javascript; charset=utf-8'),'/safety.js':('safety.js','text/javascript; charset=utf-8'),'/account.js':('account.js','text/javascript; charset=utf-8'),'/boot.js':('boot.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if path in files:
                filename,mime=files[path]
                return self.respond((ROOT/'web'/filename).read_bytes(),mime=mime)
            if path=='/api/health':
                return self.respond({'status':'ok','version':'0.21.16'})
            if path=='/api/me':
                me=application.identity.user(self.token())
                if not me:
                    return self.respond({'user':None})
                # Issue #36: a session without a cloud sign-in (made before this version, or lapsed) is signed out once.
                stale=application.cloud_login.stale_session(self.token(),me)
                if stale:
                    return self.respond({'user':None,'signed_out':stale},cookie='brain_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
                return self.respond({'user':{**me,'sessions':application.identity.sessions(me['id'])}})
            user=self.user()
            if not user:
                return
            if path in ('/api/sims/frame','/api/sim/mujoco/preview'):       # issue #51: the picture of a simulation (PNG)
                wanted=parse_qs(urlparse(self.path).query).get('id',[''])[0]
                mujoco=application.workspace(user).mujoco
                picture=mujoco.frame(wanted) if path=='/api/sims/frame' else mujoco.preview(wanted)
                if picture is None:
                    return self.respond({'error':'还没有画面'},404)
                return self.respond(picture,mime='image/png')
            if path=='/api/duck/replay':                                    # issue #63: the recorded replay of a skill model (a video)
                from .duck_kit import replay_response
                wanted=parse_qs(urlparse(self.path).query).get('show',[''])[0]
                answer=replay_response(application.workspace(user).duck.replay(wanted),self.headers.get('Range'))
                if answer is None:
                    return self.respond({'error':'这个技能模型没有录像'},404)
                status,headers,body=answer
                self.send_response(status)
                for name,value in headers.items():
                    self.send_header(name,value)
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.end_headers()
                self.wfile.write(body)
                return
            if path=='/api/state':
                operator=bool(application.operator_email and user['email']==application.operator_email)
                data={**application.workspace(user).snapshot(),'user':{**user,'operator':operator},
                      'catalog':application.commerce.catalog(),'model_catalog':application.commerce.model_catalog(),
                      'billing':application.commerce.account(user['id'])}
                if operator:
                    data['operator']={'reservations':application.commerce.store.list('reservations'),
                                      'orders':application.commerce.store.list('orders')}
                return self.respond(data)
            return self.respond({'error':'未找到资源'},404)

        def do_POST(self):
            if not self.trusted():
                return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                return self.respond({'error':'请使用 application/json'},415)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=2_000_000:
                    raise ValueError('请求需为 1 字节到 2 MB')
                payload=json.loads(self.rfile.read(length),parse_constant=lambda x: (_ for _ in ()).throw(ValueError('JSON 不允许非有限数值')))
                if not isinstance(payload,dict):
                    raise ValueError('请求必须为 JSON 对象')
                path=urlparse(self.path).path
                if path=='/api/register/start':
                    email,code=application.identity.start_registration(payload)
                    return self.respond(application.send_code(email,'register',code))
                if path=='/api/password/forgot':
                    email,code=application.identity.request_reset(payload)
                    return self.respond(application.send_code(email,'reset',code))
                if path=='/api/password/reset':
                    token,user,lifetime=application.identity.reset_password(payload)
                    application.cloud_login.signed_in(token,user,payload,lifetime)     # issue #36
                    return self.respond({'user':user,'session_seconds':lifetime},cookie=f'brain_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={lifetime}')
                if path in ('/api/register','/api/login'):
                    token,user,lifetime=application.identity.sign_in(payload,register=path=='/api/register',require_code=True)
                    application.cloud_login.signed_in(token,user,payload,lifetime)     # issue #36
                    return self.respond({'user':user,'session_seconds':lifetime},cookie=f'brain_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={lifetime}')
                if path=='/api/logout':
                    application.identity.logout(self.token())
                    return self.respond({'ok':True},cookie='brain_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
                user=self.user()
                if not user:
                    return
                if path=='/api/account/profile':
                    return self.respond({'user':application.identity.update_profile(user['id'],payload)})
                if path=='/api/account/password':
                    changed=application.identity.change_password(user['id'],self.token(),payload)
                    application.cloud_login.password_changed(user)     # issue #36: the cloud keeps its own password
                    return self.respond(changed)
                service=application.workspace(user)
                if path.startswith('/api/operator/'):
                    if not application.operator_email or user['email']!=application.operator_email:
                        return self.respond({'error':'需要平台运营权限'},403)
                    operations={'resources':application.commerce.publish,'models':application.commerce.publish_model,
                                'grant':lambda p:application.commerce.grant(p,user['id']),
                                'settle':lambda p:application.commerce.settle(p,user['id'])}
                    operation=path.removeprefix('/api/operator/')
                    if operation not in operations:return self.respond({'error':'未知运营操作'},404)
                    return self.respond(operations[operation](payload))
                routes={'engineering/workflows':service.workflows.create,
                        'engineering/workflows/start':service.workflows.start,
                        'engineering/workflows/review':service.workflows.review,
                        'engineering/workflows/retry':service.workflows.retry,
                        'engineering/workflows/stop':service.workflows.stop,
                        'engineering/workflows/continue':service.workflows.continue_review,
                        'engineering/workflows/artifact':service.workflows.artifact,
                        'engineering/adaptation/start':service.workflows.start_adaptation,
                        'projects':service.add_project,'robots':service.robot_models.register,'robots/model':service.robot_models.assign,'robots/connect':service.connect_robot,
                        # Issue #32: the platform's list of body types; change the type of a registered device
                        'body-types':service.body_types.add,'body-types/remove':service.body_types.remove,
                        'robots/body-type':service.body_types.assign,
                        'engineering/skills':service.engineering.extensions.save_skill,
                        'engineering/skills/detail':service.engineering.extensions.skill_detail,
                        'engineering/skills/toggle':service.engineering.extensions.toggle_skill,
                        'engineering/mcp':service.engineering.extensions.add_mcp,
                        'engineering/mcp/inspect':service.engineering.extensions.inspect_mcp,
                        'engineering/mcp/configure':service.engineering.extensions.configure_mcp,
                        'engineering/models':service.engineering.model_add,'engineering/models/use':service.engineering.use_offer,
                        'engineering/models/cloud':service.engineering.use_cloud_model,'engineering/cloud/close':service.engineering.cloud_close,
                        'engineering/ssh/scan':service.engineering.scan,'engineering/ssh/connect':service.robot_models.connect_ssh,
                        'engineering/ssh/status':service.engineering.status,'engineering/ssh/services':service.engineering.watch_services,
                        # Issue #24: every robot's connection state and last heartbeat; ask one robot now instead of at the next round
                        'engineering/ssh/links':service.heartbeat.overview,'engineering/ssh/heartbeat':service.heartbeat.check,
                        # Issue #9: change the SSH connection of a robot that is already connected
                        'engineering/ssh/update':service.ssh_edit.update,
                        # Issue #16: log in to every hop of a robot's saved SSH connection once; read the system and the architecture
                        'engineering/ssh/verify':service.connection_verify.verify,
                        'engineering/start':service.engineering.start,'engineering/resume':service.engineering.resume,
                        'engineering/work':service.engineering.start_work,'engineering/approve':service.engineering.approve,
                        'engineering/artifact':service.engineering.artifact,
                        'engineering/stop':service.engineering.stop,'engineering/answer':service.engineering.answer,
                        'engineering/evidence':service.engineering.evidence,
                        # Issue #2: hardware initialisation (read-only scan, repair sessions with the built-in Skill)
                        'engineering/hardware/scan':service.hardware.scan,'engineering/hardware/detail':service.hardware.scan_detail,
                        'engineering/hardware/repair':service.hardware.start_repair,'engineering/hardware/skill':service.hardware.skill,
                        # Issue #27: the device profile; `read` runs the fixed read-only commands, `profile` returns the one kept
                        'engineering/profile':service.device_profile.saved,'engineering/profile/read':service.device_profile.read,
                        # Issue #13: mark a problem as handled on site; switch a repair between asking and running unasked
                        'engineering/hardware/mark':service.hardware.policy.mark,'engineering/hardware/auto_approve':service.hardware.policy.set_auto,
                        # Issue #29: a person on site for the motion driver's start waiting for approval
                        'engineering/hardware/onsite':service.hardware.driver.confirm_onsite,
                        # Issue #6: investigation states, full re-investigation, fix chats from the report
                        'engineering/investigation/restart':service.investigation.restart,
                        'engineering/investigation/fix':service.investigation.start_fix,
                        'engineering/investigation/fix/message':service.investigation.fix_message,
                        'connections/configure':service.configure_connection,'services':service.add_service,'services/connect':service.connect_service,
                        'models':service.add_model,'models/infer':service.infer,'datasets':service.add_dataset,
                        'models/use':service.use_catalog_model,'models/deploy':service.deploy_model,
                        'scenes':service.add_scene,'billing/purchase':lambda p:application.commerce.purchase_request(user['id'],p),
                        'managed/jobs':lambda p:service.add_managed_job(p,user['id'],application.commerce),
                        'skills':service.add_skill,'applications':service.add_application,'harness':service.harness,
                        'applications/readiness':service.delivery_readiness,
                        'applications/revision-preview':service.preview_revision,'applications/revise':service.save_revision,
                        'agents/run':service.run_agent,'jobs':service.add_job,'jobs/preview':service.preview_job,'jobs/submit':service.submit_job,
                        'jobs/sync':service.sync_job,'jobs/cancel':service.cancel_job,
                        'jobs/model-preview':service.preview_training_model,'jobs/save-model':service.save_training_model,
                        'onsite':service.onsite_test,'deployments':service.deploy,'operations':service.operate,
                        'robots/reset':service.reset_robot,
                        # Issue #11: delete a real robot with its SSH connection, saved logins and investigations
                        'robots/remove':service.robot_remove.remove,
                        # Issue #44: delete a task (任务项目) of the signed-in account; refused while it has robots or running work
                        'projects/remove':service.task_remove.remove,
                        # Issue #42: choose or change the robot a task is done with
                        'projects/robot':lambda p:task_robot.choose(service,p),
                        # Issue #50: a task binds applications of the account (and lets go of them); it does not make them
                        'task-apps/bind':service.task_apps.bind,'task-apps/unbind':service.task_apps.unbind,
                        # Issue #68: the skills of a brand and model, ready to be chosen; an application created for a task
                        'model-skills/ready':service.model_skills.ready,'model-skills/create':service.model_skills.create,
                        # Issue #22: safety limits a person fills in per device; the signed-in account is recorded as who
                        'robots/limits':service.safety_limits.read,
                        'robots/limits/save':lambda p:service.safety_limits.save(p,user),
                        # Round 02 (INTERFACES 4.1, 4.5)
                        'robots/detail':service.robot_detail,
                        'robots/stop':service.stop_robot,
                        # Issue #17: reach a robot's management layer over its SSH connection (no tunnel, no address)
                        'robots/runtime-link':service.runtime_link.attach,
                        'robots/runtime-link/remove':service.runtime_link.detach,
                        'robots/runtime-link/state':service.runtime_link.overview,
                        'robots/runtime-link/retry':service.runtime_link.retry,
                        # Issue #19: install the management layer on a robot over its SSH connection, then link it
                        'robots/runtime-install/start':service.runtime_install.start,'robots/runtime-install/status':service.runtime_install.status,
                        'robots/runtime-install/stop':service.runtime_install.stop,'robots/runtime-install/clean':service.runtime_install.clean,
                        # Issue #20: start the management layer at boot (listed, then confirmed; sudo password only on stdin)
                        'robots/runtime-install/autostart':service.runtime_install.autostart,
                        'robots/runtime-install/autostart-off':service.runtime_install.autostart_off,
                        'robots/runtime-install/answer':service.runtime_install.answer,
                        # Issue #38: a robot's body (simulated or its own ROS 2) and its device configuration
                        'robots/body/save':service.robot_body.save,'robots/body/check':service.robot_body.check,
                        'robots/body/detect':service.robot_body.detect,
                        'robots/runtime-install/apply-body':service.runtime_install.apply_body,
                        'skills/package-preview':service.preview_skill_package,
                        'skills/package-install':service.install_skill_package,
                        'onsite/poll':service.poll_onsite,
                        # ux01: simulated robots, gallery, live status, run a skill
                        'sims':service.add_sim_robot,
                        'sims/start':service.sims.api_start,
                        'sims/stop':service.sims.api_stop,
                        'sims/restart':service.sims.api_restart,
                        'sims/remove':service.sims.remove,
                        'robots/live':service.robot_live,
                        # issue #51: simulation environments (the task's) and the MuJoCo robot started in one (the robots' home)
                        'sim/mujoco/overview':service.mujoco.overview,'sim/mujoco/generate':service.mujoco.generate,
                        'sim/mujoco/empty':service.mujoco.empty,'sim/mujoco/scene':service.mujoco.scene,
                        'sim/mujoco/review':service.mujoco.review,'sim/mujoco/remove':service.mujoco.remove,
                        'sims/mujoco':service.mujoco.add_robot,'sims/view':service.mujoco.view,
                        'robots/run-skill':service.run_robot_skill,
                        # issue #63: a simulated Microduck performs a trained skill model; the training progress
                        'sims/duck/perform':service.duck.perform,'sims/duck/stop':service.duck.stop,'sims/duck/status':service.duck.status,
                        'duck/training':service.duck.training,
                        # ux05-fix (Q7-03): global safety watch (stop not obeyed, latch guard, calibration)
                        'robots/safety':service.robot_safety,
                        'skills/examples':service.skill_examples,
                        'cloud/probe':service.cloud.probe,
                        'cloud/configure':service.cloud.configure,
                        'cloud/register':service.cloud.register,
                        'cloud/login':service.cloud.login,
                        'cloud/logout':service.cloud.logout,
                        'cloud/verify':service.cloud.verify,
                        'cloud/resend':service.cloud.resend,
                        'cloud/overview':service.cloud.overview,
                        'cloud/models':service.cloud.models,
                        'cloud/gateway-models':service.cloud.gateway_models,
                        # Issue #34: what the cloud parsed out of a material; a person edits and confirms it
                        'library/parse':MaterialParse(service.library).get,'library/parse/save':MaterialParse(service.library).save,
                        'library/parse/confirm':MaterialParse(service.library).confirm,'library/parse/retry':MaterialParse(service.library).retry,
                        # Issue #33: materials and driver packages in the cloud library (file bytes go through do_PUT)
                        'library/overview':service.library.overview,'library/list':service.library.list,
                        'library/upload/start':service.library.start,'library/upload/status':service.library.status,
                        'library/upload/complete':service.library.complete,'library/upload/cancel':service.library.cancel,
                        'library/delete':service.library.remove,
                        # issue #51: the library by brand and model (overview with the brands and models, a robot's materials, one material moved)
                        'library/products':service.model_library.overview,'library/robot':service.model_library.robot,
                        'library/move':service.model_library.move,
                        # Issue #31: send a usable driver package from the library to a robot and verify it there
                        'library/deliver/start':service.package_delivery.start,'library/deliver/status':service.package_delivery.status,
                        'library/deliver/stop':service.package_delivery.stop,'library/deliver/list':service.package_delivery.list,
                        # Issue #43: the cloud skill library (list, publish a local folder or the bundled examples, install a release on a robot)
                        'cloud-skills/overview':service.cloud_skills.overview,'cloud-skills/detail':service.cloud_skills.detail,
                        'cloud-skills/publish-preview':service.cloud_skills.publish_preview,'cloud-skills/publish':service.cloud_skills.publish,
                        'cloud-skills/publish-examples':service.cloud_skills.publish_examples,'cloud-skills/bind':service.cloud_skills.bind,'cloud-skills/delete':service.cloud_skills.remove,
                        'cloud-skills/install-preview':service.cloud_skills.install_preview,'cloud-skills/install':service.cloud_skills.install,
                        # Issue #43: the owner's switch for remote calls per installed skill; the signed-in account is recorded as who
                        'robots/remote':service.remote_calls.state,
                        'robots/remote/skill':lambda p:service.remote_calls.set_skill(p,user),
                        # Issue #43: a robot's programs and scene data go to the cloud library and come back (both confirmed by a person)
                        'robots/bundles':service.robot_bundle.overview,'robots/bundles/collect':service.robot_bundle.start_collect,
                        'robots/bundles/install':service.robot_bundle.start_install,'robots/bundles/status':service.robot_bundle.status,
                        'robots/bundles/stop':service.robot_bundle.stop,
                        # Issue #4: TokenFans models & compute (the key and password never come back).
                        'model-compute/key':service.model_compute.connect_key,
                        'model-compute/login':service.model_compute.login,
                        'model-compute/models':service.model_compute.refresh_models,
                        'model-compute/default':service.model_compute.set_default,
                        'model-compute/disconnect':service.model_compute.disconnect}
                if path.removeprefix('/api/') not in routes:
                    return self.respond({'error':'未知操作'},404)
                payload=robot_scope.request(path.removeprefix('/api/'),payload)     # issue #42: a request about a robot is answered for the robots' home, whichever task is open
                return self.respond(routes[path.removeprefix('/api/')](payload))
            except ValueError as exc:
                # ux04: a structured body (code, device_status, field) so the browser never has to guess.
                return self.respond(error_payload(exc),400)
            except (TypeError,KeyError,AttributeError) as exc:
                return self.respond({'error':str(exc)},400)
            except RecursionError:
                # Q6-08: a pathologically nested body (e.g. 5000 levels of parameters) is a bad request.
                return self.respond({'error':'请求内容嵌套层数太多','code':'payload_too_deep'},400)
            except Exception:
                # ux05 (Q6-08): the outcome is unknown, so the browser says "check the robot first", not "retry".
                return self.respond({'error':'内部错误，操作未确认；请查看本地服务','code':'desktop_internal_error'},500)
    return Handler


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8788)
    parser.add_argument('--state-dir',type=Path,default=ROOT/'.state'/'platform')
    parser.add_argument('--operator-email',default=os.environ.get('BRAIN_OPERATOR_EMAIL',''))
    args=parser.parse_args()
    app=Application(args.state_dir,args.operator_email)
    app.cloud_auto_login=cloud_auto_login.enabled_by_environment()     # issue #36
    server=FastBindHTTPServer(('127.0.0.1',args.port),make_handler(app))
    server.daemon_threads=True
    print(f'NeuxMind Physical OS http://127.0.0.1:{server.server_port}',flush=True)
    def terminate(_signum,_frame):
        raise KeyboardInterrupt      # the app quits with SIGTERM: close workspaces (stops simulated robots)
    try:
        signal.signal(signal.SIGTERM,terminate)
    except ValueError:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__=='__main__':
    main()
