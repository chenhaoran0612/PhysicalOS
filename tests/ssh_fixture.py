"""Ephemeral SSH protocol fixture. Never runs received commands on any machine."""
import socket
import threading
import io
import os
import posixpath
import stat
import paramiko

class SSHFixture:
    def __init__(self,files=None,command_outputs=None):
        self.files=files or {};self.command_outputs=command_outputs or {};self.reads=[];self.links={}
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
            def check_auth_password(self,username,password):return paramiko.AUTH_SUCCESSFUL if username=='robot' and password=='fixture-password' else paramiko.AUTH_FAILED
            def get_allowed_auths(self,username):return 'password'
            def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
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
        t=paramiko.Transport(client);self.transports.append(t);t.add_server_key(self.key);ready=threading.Event()
        t.set_subsystem_handler('sftp',paramiko.SFTPServer,Files)
        try:
            t.start_server(server=Protocol())
            while not self.closed and t.is_active():
                channel=t.accept(.2)
                if channel is None:continue
                if ready.wait(2):
                    channel.sendall(owner.command_outputs.get(owner.commands[-1],b'Linux test-fixture\nCPU: ARM protocol fixture\nROS: test-only\n'))
                    channel.send_exit_status(0);channel.close();ready.clear()
        except (OSError,paramiko.SSHException,EOFError):pass
        finally:t.close()
    def close(self):
        self.closed=True;self.socket.close()
        for t in self.transports:t.close()
        self.thread.join(timeout=2)
