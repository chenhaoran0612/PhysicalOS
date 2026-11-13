import argparse
import json
import os
import signal
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from .auth import CODE_RESEND_S, CODE_TTL_S, Identity
from .mailer import Mailer
from .errors import error_payload
from .httpd import FastBindHTTPServer
from .service import Platform
from .commerce import Commerce

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
        title='注册验证码' if purpose=='register' else '重置密码验证码'
        body=(f'你的 NeuxMind {title}是：{code}\n\n{CODE_TTL_S//60} 分钟内有效。'
              +('如果不是你本人操作，请忽略这封邮件。' if purpose=='register' else '如果不是你本人申请重置密码，请忽略这封邮件，你的密码不会改变。'))
        try:
            self.mailer.send(email,f'NeuxMind {title}：{code}',body)
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

        def do_GET(self):
            if not self.trusted():
                return
            # Issue #2: its own static files, kept apart from the shared table below.
            extra={'/hardware.js':('hardware.js','text/javascript; charset=utf-8'),'/hardware.css':('hardware.css','text/css; charset=utf-8')}.get(urlparse(self.path).path)
            if extra:
                return self.respond((ROOT/'web'/extra[0]).read_bytes(),mime=extra[1])
            path=urlparse(self.path).path
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/inventory.js':('inventory.js','text/javascript; charset=utf-8'),'/engineering.js':('engineering.js','text/javascript; charset=utf-8'),'/ssh_command.js':('ssh_command.js','text/javascript; charset=utf-8'),'/ssh_jump.js':('ssh_jump.js','text/javascript; charset=utf-8'),'/extensions.js':('extensions.js','text/javascript; charset=utf-8'),'/workflows.js':('workflows.js','text/javascript; charset=utf-8'),'/adaptation.js':('adaptation.js','text/javascript; charset=utf-8'),'/journey.js':('journey.js','text/javascript; charset=utf-8'),'/planner.js':('planner.js','text/javascript; charset=utf-8'),'/results.js':('results.js','text/javascript; charset=utf-8'),'/delivery.js':('delivery.js','text/javascript; charset=utf-8'),'/revisions.js':('revisions.js','text/javascript; charset=utf-8'),'/robot_detail.js':('robot_detail.js','text/javascript; charset=utf-8'),'/cloud.js':('cloud.js','text/javascript; charset=utf-8'),'/model_compute.js':('model_compute.js','text/javascript; charset=utf-8'),'/onsite.js':('onsite.js','text/javascript; charset=utf-8'),'/sim.js':('sim.js','text/javascript; charset=utf-8'),'/ux02.js':('ux02.js','text/javascript; charset=utf-8'),'/ux03.js':('ux03.js','text/javascript; charset=utf-8'),'/ux04_errors.js':('ux04_errors.js','text/javascript; charset=utf-8'),'/ux04.js':('ux04.js','text/javascript; charset=utf-8'),'/ux05.js':('ux05.js','text/javascript; charset=utf-8'),'/safety.js':('safety.js','text/javascript; charset=utf-8'),'/account.js':('account.js','text/javascript; charset=utf-8'),'/boot.js':('boot.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if path in files:
                filename,mime=files[path]
                return self.respond((ROOT/'web'/filename).read_bytes(),mime=mime)
            if path=='/api/health':
                return self.respond({'status':'ok','version':'0.16.0'})
            if path=='/api/me':
                me=application.identity.user(self.token())
                if not me:
                    return self.respond({'user':None})
                return self.respond({'user':{**me,'sessions':application.identity.sessions(me['id'])}})
            user=self.user()
            if not user:
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
                    return self.respond({'user':user,'session_seconds':lifetime},cookie=f'brain_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={lifetime}')
                if path in ('/api/register','/api/login'):
                    token,user,lifetime=application.identity.sign_in(payload,register=path=='/api/register',require_code=True)
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
                    return self.respond(application.identity.change_password(user['id'],self.token(),payload))
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
                        'projects':service.add_project,'robots':service.add_robot,'robots/connect':service.connect_robot,
                        'engineering/skills':service.engineering.extensions.save_skill,
                        'engineering/skills/detail':service.engineering.extensions.skill_detail,
                        'engineering/skills/toggle':service.engineering.extensions.toggle_skill,
                        'engineering/mcp':service.engineering.extensions.add_mcp,
                        'engineering/mcp/inspect':service.engineering.extensions.inspect_mcp,
                        'engineering/mcp/configure':service.engineering.extensions.configure_mcp,
                        'engineering/models':service.engineering.model_add,'engineering/models/use':service.engineering.use_offer,
                        'engineering/models/cloud':service.engineering.use_cloud_model,'engineering/cloud/close':service.engineering.cloud_close,
                        'engineering/ssh/scan':service.engineering.scan,'engineering/ssh/connect':service.engineering.connect,
                        'engineering/ssh/status':service.engineering.status,'engineering/ssh/services':service.engineering.watch_services,
                        'engineering/start':service.engineering.start,'engineering/resume':service.engineering.resume,
                        'engineering/work':service.engineering.start_work,'engineering/approve':service.engineering.approve,
                        'engineering/artifact':service.engineering.artifact,
                        'engineering/stop':service.engineering.stop,'engineering/answer':service.engineering.answer,
                        'engineering/evidence':service.engineering.evidence,
                        # Issue #2: hardware initialisation (read-only scan, repair sessions with the built-in Skill)
                        'engineering/hardware/scan':service.hardware.scan,'engineering/hardware/detail':service.hardware.scan_detail,
                        'engineering/hardware/repair':service.hardware.start_repair,'engineering/hardware/skill':service.hardware.skill,
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
                        # Round 02 (INTERFACES 4.1, 4.5)
                        'robots/detail':service.robot_detail,
                        'robots/stop':service.stop_robot,
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
                        'robots/run-skill':service.run_robot_skill,
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
                        # Issue #4: TokenFans models & compute (the key and password never come back).
                        'model-compute/key':service.model_compute.connect_key,
                        'model-compute/login':service.model_compute.login,
                        'model-compute/models':service.model_compute.refresh_models,
                        'model-compute/default':service.model_compute.set_default,
                        'model-compute/disconnect':service.model_compute.disconnect}
                if path.removeprefix('/api/') not in routes:
                    return self.respond({'error':'未知操作'},404)
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
    server=FastBindHTTPServer(('127.0.0.1',args.port),make_handler(app))
    server.daemon_threads=True
    print(f'NeuxMind Robot OS http://127.0.0.1:{server.server_port}',flush=True)
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
