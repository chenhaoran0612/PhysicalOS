"""Pinned open-source agent app-server adapter; no application-global login/config reuse."""
import json
import os
from pathlib import Path
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time

RUNTIME_VERSION='0.146.1'
ROOT=Path(__file__).resolve().parents[1]


def binary_path():
    candidates=[ROOT/'runtime/bin/codex', ROOT/'.build/agent-runtime/bin/codex']
    if getattr(sys,'frozen',False):
        candidates.insert(0,Path(sys.executable).parent.parent/'agent-runtime/bin/codex')
    return next((p for p in candidates if p.is_file()),None)


def available():
    return {'available':binary_path() is not None,'name':'NeuxMind Engineering Engine','version':RUNTIME_VERSION,
            'transport':'stdio','capabilities':['persistent_threads','streamed_events','dynamic_tools','interrupt','resume','token_usage','tool_questions','workspace_commands','file_edits','multi_agent','versioned_skills','http_mcp','mcp_approvals'],
            'discovery_policy':'automatic_probes_and_approved_ssh','model_protocol':'responses'}


class StopRequested(ValueError):
    """A user stop ended a synchronous wait on the runtime (G-2)."""


class AgentProcess:
    def __init__(self,home,work,profile,engineering=False,extensions=None,analysis_tools=False,cancel=None):
        # Default cancel event for every request, including the start-up ones
        # (initialize, skills/list) issued from this constructor.
        self.cancel=cancel
        binary=binary_path()
        if not binary:raise ValueError('工程引擎未安装，请安装完整桌面版本')
        extensions=extensions or {'skills':[],'mcp':[]}
        local_tools=engineering or analysis_tools
        # Device sessions may load only product-owned built-in Skills (issue #2 hardware-init), never project extensions.
        if not engineering and (extensions['mcp'] or any(not x.get('builtin') for x in extensions['skills'])):raise ValueError('调查会话不能加载工程扩展')
        self.events=queue.Queue(maxsize=4096)
        self.pending={};self.lock=threading.RLock();self.counter=0;self.closed=False
        home,work=Path(home),Path(work)
        home.mkdir(parents=True,exist_ok=True);work.mkdir(parents=True,exist_ok=True)
        home.chmod(0o700);work.chmod(0o700);self.home=home
        skill_root=home/'skills'
        if skill_root.is_symlink():raise ValueError('技能目录不能为符号链接')
        if skill_root.exists():shutil.rmtree(skill_root)
        allowed_skills=[]
        for skill in extensions['skills']:
            directory=skill_root/skill['slug']
            for relative,content in skill['files'].items():
                path=directory/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
            allowed_skills.append(str((directory/'SKILL.md').resolve()))
        # Values are TOML strings encoded with JSON-compatible escaping. Secrets
        # go only into the child environment, never CLI arguments or config.
        config='\n'.join([
            'model_provider = "neuxmind"','model = '+json.dumps(profile['model']),
            'approval_policy = '+('"on-request"' if local_tools else '"never"'),'default_permissions = "robot-discovery"','web_search = "disabled"',
            'model_reasoning_effort = "medium"','tool_output_token_limit = 6000','check_for_update_on_startup = false',
            '[model_providers.neuxmind]','name = "NeuxMind model connection"',
            'base_url = '+json.dumps(profile['base_url']),'env_key = "NEUXMIND_MODEL_KEY"',
            'wire_api = "responses"','requires_openai_auth = false','supports_websockets = false',
            'request_max_retries = 0','stream_max_retries = 0','stream_idle_timeout_ms = 120000',
            '[analytics]','enabled = false','[shell_environment_policy]','inherit = "none"',
            '[shell_environment_policy.set]','PATH = '+json.dumps(str(binary.parent.resolve())+':/usr/bin:/bin:/usr/sbin:/sbin'),
            '[features]',
            'shell_tool = '+str(local_tools).lower(),'unified_exec = '+str(local_tools).lower(),'apply_patch_freeform = '+str(local_tools).lower(),
            'code_mode = false','code_mode_host = false','apps = false','connectors = false',
            'browser_use = false','computer_use = false','multi_agent = '+str(local_tools).lower(),
            # Review Q3-03: the shell snapshot dumps the app-server environment
            # (model key, MCP tokens) as `declare -x` lines into CODEX_HOME.
            'shell_snapshot = false',
            # Review Q3-04: no plugin marketplace sync (a ~25 MB git clone from
            # GitHub per session), no remote plugins, no update checks.
            'plugins = false','remote_plugin = false','plugin_sharing = false','in_app_updates = false',
            'skill_mcp_dependency_install = false',
            '[skills.bundled]','enabled = false',
            '[permissions.robot-discovery.filesystem]','":root" = "deny"','":minimal" = "read"',
            json.dumps(str(binary.parent.resolve()))+' = "read"',
            json.dumps(str(skill_root.resolve()))+' = "read"',
            '[permissions.robot-discovery.filesystem.":workspace_roots"]','"." = '+('"write"' if local_tools else '"read"'),
            '[permissions.robot-discovery.network]','enabled = false',
        ])+'\n'
        mcp_env={}
        for index,server in enumerate(extensions['mcp']):
            config+='\n[mcp_servers.'+json.dumps(server['runtime_name'])+']\n'
            config+='url = '+json.dumps(server['url'])+'\nstartup_timeout_sec = 12\ntool_timeout_sec = 30\n'
            config+='default_tools_approval_mode = "prompt"\n'
            if server.get('enabled_tools') is not None:config+='enabled_tools = '+json.dumps(server['enabled_tools'])+'\n'
            if server.get('token'):
                key='NEUXMIND_MCP_'+str(index);mcp_env[key]=server['token'];config+='bearer_token_env_var = '+json.dumps(key)+'\n'
        (home/'config.toml').write_text(config);(home/'config.toml').chmod(0o600)
        env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','TMPDIR','SYSTEMROOT')}
        env.update(CODEX_HOME=str(home),NEUXMIND_MODEL_KEY=profile['api_key']);env.update(mcp_env)
        # macOS system proxies may intercept loopback traffic unless explicitly
        # excluded. Local model gateways and MCP servers must stay on this Mac.
        env['NO_PROXY']=','.join(filter(None,[os.environ.get('NO_PROXY',os.environ.get('no_proxy','')),'localhost','127.0.0.1','::1']))
        env['PATH']=str(binary.parent)+os.pathsep+env.get('PATH','/usr/bin:/bin')
        self.process=subprocess.Popen([str(binary),'app-server','--listen','stdio://'],cwd=work,env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
        self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()
        try:
            self.request('initialize',{'clientInfo':{'name':'neuxmind_robot_os','title':'NeuxMind Robot OS','version':'0.16.0'},
                                       'capabilities':{'experimentalApi':True}})
            self.send({'method':'initialized','params':{}})
            listing=self.request('skills/list',{'cwds':[str(work.resolve())],'forceReload':True})
            self.loaded_skills=[]
            for entry in listing['data']:
                for skill in entry['skills']:
                    selected=skill['path'] in allowed_skills
                    if skill['enabled']!=selected:self.request('skills/config/write',{'path':skill['path'],'enabled':selected})
                    if selected:self.loaded_skills.append(skill)
                for error in entry.get('errors',[]):
                    if error.get('path') in allowed_skills:raise ValueError('Skill 格式错误：'+error['message'])
            if len(self.loaded_skills)!=len(allowed_skills):raise ValueError('工程技能未被运行时完整加载')
        except Exception:
            self.close();raise

    def send(self,value):
        with self.lock:
            if self.closed:raise ValueError('工程会话已关闭')
            try:
                self.process.stdin.write((json.dumps(value,ensure_ascii=False)+'\n').encode());self.process.stdin.flush()
            except (OSError,BrokenPipeError):raise ValueError('工程引擎连接已中断') from None

    def request(self,method,params,timeout=25,cancel=None):
        """Synchronous JSON-RPC call. ``cancel`` (a threading.Event) ends the
        wait early with StopRequested (round 04, G-2): a stop must not wait for
        a slow ``thread/start``/``turn/start`` reply. The request itself may
        still be processed by the runtime, which is then closed by the caller."""
        # cancel=False: never cancelled (used by turn/interrupt, sent *because* of a stop).
        cancel=self.cancel if cancel is None else (cancel or None)
        with self.lock:
            self.counter+=1;key=self.counter;result=queue.Queue(maxsize=1);self.pending[key]=result
        try:
            self.send({'id':key,'method':method,'params':params})
            deadline=time.monotonic()+timeout
            while True:
                if cancel is not None and cancel.is_set():raise StopRequested('停止请求中断了等待中的工程引擎调用：'+method)
                remaining=deadline-time.monotonic()
                if remaining<=0:raise ValueError('工程引擎响应超时')
                try:reply=result.get(timeout=min(.1,remaining));break
                except queue.Empty:continue
            if 'error' in reply:
                message=str(reply['error'].get('message','请求失败'))
                raise ValueError('工程引擎：'+message[:600])
            return reply.get('result',{})
        finally:
            with self.lock:self.pending.pop(key,None)

    def _read(self):
        try:
            while not self.closed:
                raw=self.process.stdout.readline(4_000_001)
                if not raw:break
                if len(raw)>4_000_000:break
                try:message=json.loads(raw)
                except (ValueError,UnicodeError):continue
                if 'id' in message and 'method' not in message:
                    with self.lock:pending=self.pending.get(message['id'])
                    if pending:pending.put_nowait(message)
                else:
                    try:self.events.put(message,timeout=1)
                    except queue.Full:break
        finally:
            with self.lock:
                for pending in self.pending.values():
                    if pending.empty():pending.put_nowait({'error':{'message':'运行时已退出'}})
            try:self.events.put_nowait({'method':'runtime/closed','params':{}})
            except queue.Full:pass

    def respond(self,request_id,result):self.send({'id':request_id,'result':result})

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid,signal.SIGTERM)
                # A stopped (SIGSTOP) or hung runtime only acts on SIGTERM once continued.
                os.killpg(self.process.pid,signal.SIGCONT);self.process.wait(timeout=3)
            except (ProcessLookupError,subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=3)
        for pipe in (self.process.stdin,self.process.stdout):
            try:pipe.close()
            except OSError:pass
        if threading.current_thread()!=self.reader:self.reader.join(timeout=2)
        self.scrub()

    # Regenerable runtime state that may carry secrets (shell environment
    # snapshots) or bulk (plugin caches); removed after every run as a second
    # line behind the config switches above (review Q3-03 / Q3-04).
    SCRUB=('shell_snapshots','.tmp','plugins')

    def scrub(self):
        for name in self.SCRUB:
            path=self.home/name
            try:
                if path.is_symlink() or path.is_file():path.unlink()
                elif path.is_dir():shutil.rmtree(path)
            except OSError:pass
