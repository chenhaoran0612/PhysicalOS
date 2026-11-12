"""Ephemeral SSH protocol fixture. Never runs received commands on any machine.

As a jump host it forwards direct-tcpip channels, only to the addresses listed in ``forward_to``."""
import socket
import threading
import io
import os
import posixpath
import stat
import paramiko

# The fixture's well-known login. A test value that never reaches a real machine, not a credential.
DEFAULT_LOGIN=('robot','fixture-password')

class SSHFixture:
    def __init__(self,files=None,command_outputs=None,username=None,password=None,forward_to=None,authorized_keys=None):
        self.files=files or {};self.command_outputs=command_outputs or {};self.reads=[];self.links={}
        self.username=username or DEFAULT_LOGIN[0];self.password=password or DEFAULT_LOGIN[1];self.auth_attempts=[]
        self.authorized_keys={key.asbytes() for key in authorized_keys or []};self.key_logins=0
        self.forward_to={tuple(x) for x in forward_to or []};self.forwarded=[];self.forwarding=True
        self.key=paramiko.RSAKey.generate(2048);self.closed=False;self.transports=[];self.commands=[]
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0));self.socket.listen();self.socket.settimeout(.2)
        self.port=self.socket.getsockname()[1];self.thread=threading.Thread(target=self.accept,daemon=True);self.thread.start()
    def accept(self):
        while not self.closed:
            try:client,_=self.socket.accept()
            except socket.timeout:continue
            except OSError:break
            threading.Thread(target=self.serve,args=(client,),daemon=True).start()
    def serve(self,client):
        owner=self
        class Protocol(paramiko.ServerInterface):
            def check_auth_password(self,username,password):
                owner.auth_attempts.append(username)
                return paramiko.AUTH_SUCCESSFUL if username==owner.username and password==owner.password else paramiko.AUTH_FAILED
            def check_auth_publickey(self,username,key):
                if username!=owner.username or key.asbytes() not in owner.authorized_keys:return paramiko.AUTH_FAILED
                owner.key_logins+=1;return paramiko.AUTH_SUCCESSFUL
            def get_allowed_auths(self,username):return 'password,publickey' if owner.authorized_keys else 'password'
            def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_direct_tcpip_request(self,chanid,origin,destination):
                if not owner.forwarding:return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
                if tuple(destination) not in owner.forward_to:return paramiko.OPEN_FAILED_CONNECT_FAILED
                forwards[chanid]=tuple(destination);return paramiko.OPEN_SUCCEEDED
            def check_channel_exec_request(self,channel,command):
                owner.commands.append(command.decode());ready.set();return True
        class Files(paramiko.SFTPServerInterface):
            def canonicalize(self,path):return owner.links.get(path,posixpath.normpath(path))
            def stat(self,path):
                attrs=paramiko.SFTPAttributes()
                if path in owner.files:attrs.st_mode=stat.S_IFREG|0o644;attrs.st_size=len(owner.files[path]);return attrs
                if any(p.startswith(path.rstrip('/')+'/') for p in owner.files):attrs.st_mode=stat.S_IFDIR|0o755;attrs.st_size=0;return attrs
                return paramiko.SFTP_NO_SUCH_FILE
            lstat=stat
            def list_folder(self,path):
                prefix=path.rstrip('/')+'/';names=sorted({p[len(prefix):].split('/')[0] for p in owner.files if p.startswith(prefix)})
                result=[]
                for name in names:
                    attrs=self.stat(prefix+name);attrs.filename=name;result.append(attrs)
                return result
            def open(self,path,flags,attr):
                if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):return paramiko.SFTP_PERMISSION_DENIED
                if path not in owner.files:return paramiko.SFTP_NO_SUCH_FILE
                owner.reads.append(path);handle=paramiko.SFTPHandle(flags);handle.readfile=io.BytesIO(owner.files[path]);return handle
        t=paramiko.Transport(client);self.transports.append(t);t.add_server_key(self.key);ready=threading.Event();forwards={}
        t.set_subsystem_handler('sftp',paramiko.SFTPServer,Files)
        try:
            t.start_server(server=Protocol())
            while not self.closed and t.is_active():
                channel=t.accept(.2)
                if channel is None:continue
                if channel.get_id() in forwards:self.pump(channel,forwards.pop(channel.get_id()));continue
                if ready.wait(2):
                    channel.sendall(owner.command_outputs.get(owner.commands[-1],b'Linux test-fixture\nCPU: ARM protocol fixture\nROS: test-only\n'))
                    channel.send_exit_status(0);channel.close();ready.clear()
        except (OSError,paramiko.SSHException,EOFError):pass
        finally:t.close()
    def pump(self,channel,destination):
        """Relay one forwarded channel to its destination (another fixture), byte for byte."""
        try:upstream=socket.create_connection(destination,timeout=5)
        except OSError:channel.close();return
        upstream.settimeout(None);self.forwarded.append(destination)
        def copy(read,write):
            try:
                while True:
                    data=read(32768)
                    if not data:break
                    write(data)
            except (OSError,EOFError,paramiko.SSHException):pass
            finally:
                channel.close()
                try:upstream.close()
                except OSError:pass
        threading.Thread(target=copy,args=(channel.recv,upstream.sendall),daemon=True).start()
        threading.Thread(target=copy,args=(upstream.recv,channel.sendall),daemon=True).start()
    def close(self):
        self.closed=True;self.socket.close()
        for t in self.transports:t.close()
        self.thread.join(timeout=2)
