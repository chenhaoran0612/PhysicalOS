"""Device read-only transport. SSH credentials never enter model context."""
import base64
import hashlib
import io
import re
import socket
import stat
import time
from pathlib import PurePosixPath

# Commands are product-owned constants. The model selects a probe identifier,
# never supplies shell text. No package installation, sudo, source, or actuators.
PROBES={
 'system':('系统与处理器',"uname -a; cat /etc/os-release 2>/dev/null; hostname; command -v lscpu >/dev/null && lscpu; head -c 16000 /proc/cpuinfo 2>/dev/null; cat /proc/device-tree/model 2>/dev/null"),
 'memory_storage':('内存与存储',"cat /proc/meminfo 2>/dev/null; df -h; command -v lsblk >/dev/null && lsblk -o NAME,TYPE,SIZE,MODEL"),
 'accelerators':('GPU 与计算加速器',"command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader; command -v lspci >/dev/null && lspci; ls /dev/dri /dev/nvidia* /dev/accel* 2>/dev/null"),
 'buses_sensors':('总线、传感器与设备节点',"command -v lsusb >/dev/null && lsusb; ls -l /dev/video* /dev/ttyUSB* /dev/ttyACM* /dev/i2c-* /dev/spidev* /sys/bus/iio/devices 2>/dev/null; ls /sys/class/net /sys/class/input 2>/dev/null"),
 'network':('网络接口',"ip -brief address 2>/dev/null; ip route 2>/dev/null; ss -lnt 2>/dev/null"),
 'services':('已安装服务与运行进程',"systemctl list-units --type=service --all --no-pager 2>/dev/null; ps -eo comm 2>/dev/null"),
 'software':('软件包与开发环境',"command -v python3 python pip3 ros2 roscore rosversion docker colcon catkin_make gcc cmake 2>/dev/null; python3 --version 2>/dev/null; dpkg-query -W -f='${Package} ${Version}\\n' 2>/dev/null | head -n 1500; rpm -qa 2>/dev/null | head -n 1500"),
 'robotics':('机器人中间件与部署目录',"ls -ld /opt/* /usr/local/share/* /etc/ros /etc/robot* 2>/dev/null; find /opt/ros /etc/ros -maxdepth 3 -type f 2>/dev/null | head -n 250; cat /proc/modules 2>/dev/null"),
 'containers':('已有容器',"command -v docker >/dev/null && docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}}' 2>/dev/null"),
}
DEFAULT_ROOTS=['/opt/ros','/etc/ros','/etc/robot','/usr/local/share']
SENSITIVE=re.compile(r'(?:^|/)(?:\.ssh|\.aws|\.config|\.env[^/]*|\.git|credentials?[^/]*|secrets?[^/]*|[^/]*(?:private[._-]?key|id_rsa|id_ed25519)[^/]*)(?:/|$)',re.I)


def redact(text):
    text=str(text)
    text=re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[已隐藏私钥]',text)
    text=re.sub(r'(?im)^([^\n]*(?:password|passwd|api[_-]?key|access[_-]?token|secret|authorization)\s*[:=]\s*).+$',r'\1[已隐藏]',text)
    text=re.sub(r'(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*','Bearer [已隐藏]',text)
    text=re.sub(r'\bsk-[A-Za-z0-9_-]{8,}','[已隐藏模型密钥]',text)
    return text


def target(p):
    host=p.get('host','').strip();user=p.get('username','').strip();port=p.get('port',22)
    if not host or len(host)>253 or not re.fullmatch(r'[A-Za-z0-9._:%-]+',host) or host.startswith('-'):
        raise ValueError('请输入主机名或 IP 地址，不包含命令或 URL')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}',user) or user.startswith('-'):raise ValueError('SSH 用户名无效')
    if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535:raise ValueError('SSH 端口无效')
    return host,port,user


def fingerprint(key):return 'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


def inspect_host(p):
    import paramiko
    host,port,user=target(p)
    transport=None
    try:
        transport=paramiko.Transport(socket.create_connection((host,port),timeout=8))
        transport.start_client(timeout=8);key=transport.get_remote_server_key()
        return {'host':host,'port':port,'username':user,'fingerprint':fingerprint(key),'key_type':key.get_name()}
    except (OSError,paramiko.SSHException):raise ValueError('无法读取 SSH 主机密钥，请检查地址和端口') from None
    finally:
        if transport:transport.close()


def validate_roots(roots):
    if not isinstance(roots,list) or not 1<=len(roots)<=12:raise ValueError('请指定 1–12 个允许调查的机器人配置目录')
    valid=[]
    for root in roots:
        if not isinstance(root,str) or not root.startswith('/') or '\x00' in root:raise ValueError('配置目录必须为绝对路径')
        path=PurePosixPath(root)
        if '..' in path.parts or len(path.parts)<3 or SENSITIVE.search(root) or str(path).startswith(('/proc/','/sys/','/dev/')):
            raise ValueError('配置目录范围过大或包含敏感位置')
        valid.append(str(path))
    return valid


def command_request(args):
    """Validate shape, not shell safety. Every arbitrary command needs approval."""
    command=args.get('command');purpose=args.get('purpose');timeout=args.get('timeout_seconds',20)
    if not isinstance(command,str) or not command.strip() or len(command)>12000 or '\x00' in command:
        raise ValueError('SSH 命令必须为 1–12000 字符的文本')
    if not isinstance(purpose,str) or not purpose.strip() or len(purpose)>1000:
        raise ValueError('请说明这条命令要确认什么信息')
    if isinstance(timeout,bool) or not isinstance(timeout,int) or not 1<=timeout<=60:
        raise ValueError('命令超时须为 1–60 秒')
    return {'command':command,'purpose':purpose,'timeout_seconds':timeout}


class SSHDevice:
    def __init__(self,connection,secret):
        import paramiko
        host,port,user=target(connection)
        pinned=connection['fingerprint']
        class PinnedKey(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self,client,hostname,key):
                if fingerprint(key)!=pinned:raise paramiko.SSHException('设备密钥与已确认指纹不一致')
        self.client=paramiko.SSHClient();self.client.set_missing_host_key_policy(PinnedKey())
        self.roots=list(connection['read_roots']);self.stop_event=None
        self.secrets=[v for v in secret.values() if isinstance(v,str) and v]
        kwargs=dict(hostname=host,port=port,username=user,timeout=8,banner_timeout=8,auth_timeout=8,
                    allow_agent=connection['auth']=='agent',look_for_keys=False)
        if connection['auth']=='password':kwargs['password']=secret.get('password','')
        elif connection['auth']=='key':
            key=None
            for cls in (paramiko.Ed25519Key,paramiko.ECDSAKey,paramiko.RSAKey):
                try:key=cls.from_private_key(io.StringIO(secret.get('private_key','')),password=secret.get('passphrase') or None);break
                except (ValueError,paramiko.SSHException):continue
            if not key:raise ValueError('无法解析 SSH 私钥或解密口令不正确')
            kwargs['pkey']=key
        try:self.client.connect(**kwargs)
        except (OSError,paramiko.SSHException):
            self.client.close();raise ValueError('SSH 认证失败、连接超时或主机指纹改变；请核对连接') from None

    def probe(self,name):
        if name not in PROBES:raise ValueError('不允许的调查项')
        return {'probe':name,'label':PROBES[name][0],**self.execute(PROBES[name][1],18)}

    def clean(self,text):
        for value in self.secrets:text=text.replace(value,'[已隐藏连接凭据]')
        return redact(text)

    def execute(self,command,timeout=20):
        """Only product probes or an individually approved command may call this."""
        channel=self.client.get_transport().open_session(timeout=8)
        try:
            channel.settimeout(2);channel.exec_command(command);deadline=min(time.monotonic()+timeout,getattr(self,'deadline',float('inf')))
            output=bytearray();error=bytearray();limited=False
            while time.monotonic()<deadline:
                if self.stop_event and self.stop_event.is_set():break
                for ready,read,buffer in [(channel.recv_ready,channel.recv,output),(channel.recv_stderr_ready,channel.recv_stderr,error)]:
                    if ready():
                        data=read(8192)
                        room=max(0,48000-len(buffer));buffer.extend(data[:room])
                        if len(data)>room:limited=True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():break
                if limited:break
                time.sleep(.02)
            timed_out=not channel.exit_status_ready()
            code=channel.recv_exit_status() if not timed_out else None
            return {'stdout':self.clean(output.decode('utf-8','replace')),
                    'stderr':self.clean(error.decode('utf-8','replace')),'exit_code':code,'truncated':limited,'timed_out':timed_out,
                    'cancelled':bool(self.stop_event and self.stop_event.is_set())}
        finally:channel.close()

    def config(self,path,listing=False,offset=0):
        if not isinstance(path,str) or not path.startswith('/') or '\x00' in path or len(path)>1000 or '..' in PurePosixPath(path).parts or SENSITIVE.search(path):
            raise ValueError('配置路径无效或属于敏感文件')
        if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=20_000_000:raise ValueError('读取偏移无效')
        sftp=self.client.open_sftp()
        try:
            sftp.get_channel().settimeout(8)
            canonical=sftp.normalize(path)
            if SENSITIVE.search(canonical) or not any(canonical==root or canonical.startswith(root+'/') for root in self.roots):
                raise ValueError('配置路径或符号链接超出授权目录')
            if listing:
                if offset>10000:raise ValueError('目录分页最多读取 10000 项')
                from itertools import islice
                entries=[x for x in islice(sftp.listdir_iter(canonical),offset,offset+201)]
                return {'path':canonical,'entries':[{'name':x.filename,'size':x.st_size,'mode':x.st_mode} for x in entries[:200] if not SENSITIVE.search(canonical+'/'+x.filename)],
                        'offset':offset,'next_offset':offset+200 if len(entries)>200 else None,'truncated':len(entries)>200}
            info=sftp.stat(canonical)
            if not stat.S_ISREG(info.st_mode):raise ValueError('只允许读取普通文本文件，不能读取设备或管道')
            with sftp.open(canonical,'rb') as f:
                f.seek(offset);data=f.read(48001)
            if b'\0' in data:raise ValueError('该文件为二进制数据')
            return {'path':canonical,'text':self.clean(data[:48000].decode('utf-8','replace')),'offset':offset,
                    'next_offset':offset+48000 if len(data)>48000 else None,'truncated':len(data)>48000}
        except OSError:raise ValueError('配置路径不可读或不存在') from None
        finally:sftp.close()

    def grant_root(self,path):
        root=validate_roots([path])[0]
        sftp=self.client.open_sftp()
        try:
            sftp.get_channel().settimeout(8)
            canonical=sftp.normalize(root)
            if canonical!=root:raise ValueError('目录是链接或别名，请针对实际路径重新申请：'+canonical)
            if not stat.S_ISDIR(sftp.stat(root).st_mode):raise ValueError('申请的路径不是目录')
        finally:sftp.close()
        if root not in self.roots:self.roots.append(root)
        return root

    def close(self):self.client.close()
