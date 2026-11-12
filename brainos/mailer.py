"""Sends account e-mails (verification codes).

Configuration, first match wins:
  1. environment: NEUXMIND_SMTP_HOST, NEUXMIND_SMTP_PORT, NEUXMIND_SMTP_USER, NEUXMIND_SMTP_PASSWORD,
     NEUXMIND_SMTP_FROM, NEUXMIND_SMTP_SSL (1 = implicit TLS, default when port is 465)
  2. <state dir>/mail.json with the same keys in lower case without the prefix
     ({"host": ..., "port": 465, "user": ..., "password": ..., "from": ..., "ssl": true})

Without a configured SMTP server nothing leaves the machine: messages are appended to
<state dir>/mail-outbox.txt (mode 0600) so a local install can still be used and tested.
NEUXMIND_MAIL_DEV_ECHO=1 (automated tests only) additionally returns the code in the API response,
and only when no SMTP server is configured.
"""
import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path


class MailError(ValueError):
    pass


class Mailer:
    def __init__(self, root):
        self.root = Path(root)
        self.outbox = self.root / 'mail-outbox.txt'

    def config(self):
        env = {k: os.environ.get('NEUXMIND_SMTP_' + k.upper(), '') for k in ('host', 'port', 'user', 'password', 'from', 'ssl')}
        if env['host']:
            cfg = env
        else:
            try:
                cfg = json.loads((self.root / 'mail.json').read_text())
            except (OSError, ValueError):
                return None
            if not isinstance(cfg, dict) or not cfg.get('host'):
                return None
        port = int(cfg.get('port') or 465)
        ssl_flag = cfg.get('ssl')
        use_ssl = port == 465 if ssl_flag in (None, '') else str(ssl_flag).lower() in ('1', 'true', 'yes')
        return {'host': str(cfg['host']), 'port': port, 'user': str(cfg.get('user') or ''),
                'password': str(cfg.get('password') or ''), 'from': str(cfg.get('from') or cfg.get('user') or ''), 'ssl': use_ssl}

    @property
    def mode(self):
        return 'smtp' if self.config() else 'outbox'

    def dev_echo(self):
        return os.environ.get('NEUXMIND_MAIL_DEV_ECHO') == '1' and self.mode == 'outbox'

    def send(self, to, subject, body):
        cfg = self.config()
        if not cfg:
            self.root.mkdir(parents=True, exist_ok=True)
            new = not self.outbox.exists()
            with open(self.outbox, 'a', encoding='utf-8') as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\nTo: {to}\nSubject: {subject}\n\n{body}\n\n")
            if new:
                self.outbox.chmod(0o600)
            return {'delivery': 'outbox', 'outbox': str(self.outbox)}
        msg = EmailMessage()
        msg['From'] = formataddr(('NeuxMind', cfg['from']))
        msg['To'] = to
        msg['Subject'] = subject
        msg['Message-ID'] = make_msgid()
        msg.set_content(body)
        try:
            context = ssl.create_default_context()
            if cfg['ssl']:
                server = smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=20, context=context)
            else:
                server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=20)
                server.starttls(context=context)
            with server:
                if cfg['user']:
                    server.login(cfg['user'], cfg['password'])
                server.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise MailError('验证码邮件发送失败，请稍后再试') from e
        return {'delivery': 'smtp'}
