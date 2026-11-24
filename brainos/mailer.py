"""Sends account e-mails (verification codes).

Configuration, first match wins:
  1. environment: NEUXMIND_SMTP_HOST, NEUXMIND_SMTP_PORT, NEUXMIND_SMTP_USER, NEUXMIND_SMTP_PASSWORD,
     NEUXMIND_SMTP_FROM, NEUXMIND_SMTP_FROM_NAME, NEUXMIND_SMTP_SSL (1 = implicit TLS, default when port is 465)
  2. <state dir>/mail.json with the same keys in lower case without the prefix
     ({"host": ..., "port": 587, "user": ..., "password": ..., "from": ..., "from_name": ..., "ssl": false})

The sending account lives on this computer only (issue #37): it is in neither the repository nor the
installer. A settings file with a mistake in it is reported when a mail is sent; it never falls back
to the local file silently.

Without a configured SMTP server nothing leaves the machine: messages are appended to
<state dir>/mail-outbox.txt (mode 0600) so a local install can still be used and tested.
NEUXMIND_MAIL_DEV_ECHO=1 (automated tests only) additionally returns the code in the API response,
and only when no SMTP server is configured.

Command line (docs/MAIL_SERVICE.md):
  python -m brainos.mailer --state-dir <state dir> init | check | test <address>
"""
import argparse
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

log = logging.getLogger('brainos.mail')

KEYS = ('host', 'port', 'user', 'password', 'from', 'from_name', 'ssl')
DEFAULT_FROM_NAME = 'NeuxMind Robot OS'
SUBMISSION_PORT = 587
IMPLICIT_TLS_PORT = 465
SMTP_TIMEOUT_S = 20
ADDRESS_RE = re.compile(r'^[^\s<>@,;:"]+@[^\s<>@,;:"]+$')
TEMPLATE = {'host': '', 'port': SUBMISSION_PORT, 'user': '', 'password': '', 'from': '', 'from_name': DEFAULT_FROM_NAME}


class MailError(ValueError):
    pass


def explain(error):
    """One sentence a person can act on, for a failed connection or send."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return '邮件服务拒绝了登录：请检查发信账号和授权码，并确认这个邮箱已开启 SMTP 发信'
    if isinstance(error, smtplib.SMTPSenderRefused):
        return '邮件服务不允许用这个发件地址发信：from 要与登录账号一致，或是它有权代发的地址'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return '收件地址被邮件服务拒收，请确认邮箱填写正确'
    if isinstance(error, smtplib.SMTPNotSupportedError) and 'internationalized' in str(error):
        return '收件地址里有非英文字符，这个邮件服务不支持这样的地址'
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return '邮件服务器不支持加密连接或密码登录，请检查 host、port 和 ssl 设置'
    if isinstance(error, ssl.SSLError):
        return '与邮件服务器的加密连接失败，请检查 port 和 ssl 设置（465 用 ssl，587 不用）'
    if isinstance(error, smtplib.SMTPException):
        return '验证码邮件发送失败，请稍后再试'
    if isinstance(error, TimeoutError):
        return '邮件服务器响应超时，请稍后再试'
    if isinstance(error, ValueError):
        return '邮件配置或收件地址里有邮件服务不接受的字符，请检查 mail.json 和填写的邮箱'
    return '连接不上邮件服务器，请检查网络和 host、port 设置'


class Mailer:
    def __init__(self, root):
        self.root = Path(root)
        self.outbox = self.root / 'mail-outbox.txt'
        self.settings_file = self.root / 'mail.json'

    def _raw(self):
        """The settings as written, or None when there are none. Raises MailError for a file that cannot be read."""
        env = {k: os.environ.get('NEUXMIND_SMTP_' + k.upper(), '') for k in KEYS}
        if env['host']:
            return env
        try:
            text = self.settings_file.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        except OSError as e:
            raise MailError(f'读不了邮件配置 mail.json：{e.strerror or e}') from e
        try:
            cfg = json.loads(text)
        except ValueError as e:
            raise MailError(f'邮件配置 mail.json 不是合法的 JSON：{e}') from e
        if not isinstance(cfg, dict):
            raise MailError('邮件配置 mail.json 应该是一个 JSON 对象')
        return cfg if cfg.get('host') else None

    def config(self):
        """Checked settings, or None when no SMTP server is configured. Raises MailError for settings that cannot work."""
        cfg = self._raw()
        if cfg is None:
            return None
        try:
            port = int(cfg.get('port') or IMPLICIT_TLS_PORT)
        except (TypeError, ValueError):
            raise MailError('邮件配置的 port 应该是数字，例如 587 或 465') from None
        ssl_flag = cfg.get('ssl')
        use_ssl = port == IMPLICIT_TLS_PORT if ssl_flag in (None, '') else str(ssl_flag).lower() in ('1', 'true', 'yes')
        sender = str(cfg.get('from') or cfg.get('user') or '').strip()
        if not ADDRESS_RE.match(sender):
            raise MailError('邮件配置缺少发件地址：请把 from（或 user）填成完整的邮箱地址，只写地址，不带名称和尖括号')
        name = str(cfg.get('from_name') or DEFAULT_FROM_NAME).strip()
        if '\n' in name or '\r' in name:
            raise MailError('邮件配置的 from_name 不能换行')
        if not (str(cfg.get('user') or '') + str(cfg.get('password') or '')).isascii():
            raise MailError('邮件配置的 user 或 password 里有非英文字符（常见于粘贴时带进了全角字符或特殊空格）')
        return {'host': str(cfg['host']).strip(), 'port': port, 'user': str(cfg.get('user') or '').strip(),
                'password': str(cfg.get('password') or ''), 'from': sender,
                'from_name': name, 'ssl': use_ssl}

    @property
    def mode(self):
        try:
            return 'smtp' if self._raw() else 'outbox'
        except MailError:
            return 'smtp'    # settings exist but are broken: sending reports it, nothing falls back to the local file

    def dev_echo(self):
        return os.environ.get('NEUXMIND_MAIL_DEV_ECHO') == '1' and self.mode == 'outbox'

    def _session(self, cfg, work):
        """Connects, signs in and runs work(server). Every failure becomes a MailError; the detail goes to the log."""
        try:
            context = ssl.create_default_context()
            if cfg['ssl']:
                server = smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=SMTP_TIMEOUT_S, context=context)
            else:
                server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=SMTP_TIMEOUT_S)
            with server:
                if not cfg['ssl']:
                    server.starttls(context=context)
                if cfg['user']:
                    server.login(cfg['user'], cfg['password'])
                return work(server)
        except (OSError, smtplib.SMTPException, ValueError) as e:   # ValueError: a character smtplib cannot encode
            log.warning('smtp %s:%s failed: %s: %s', cfg['host'], cfg['port'], type(e).__name__, e)
            raise MailError(explain(e)) from e

    def check(self):
        """Signs in to the configured server without sending anything. Returns the settings in use, without the password."""
        cfg = self.config()
        if not cfg:
            raise MailError('未配置邮件服务')
        self._session(cfg, lambda server: server.noop())
        return {k: v for k, v in cfg.items() if k != 'password'}

    def send(self, to, subject, body, html=None):
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
        msg['From'] = formataddr((cfg['from_name'], cfg['from']))
        msg['To'] = to
        msg['Subject'] = subject
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid(domain=cfg['from'].rpartition('@')[2])
        msg['Auto-Submitted'] = 'auto-generated'         # RFC 3834: no out-of-office replies to a code mail
        msg['X-Auto-Response-Suppress'] = 'All'
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype='html')
        self._session(cfg, lambda server: server.send_message(msg))
        return {'delivery': 'smtp'}


def _init(mailer):
    path = mailer.settings_file
    if path.exists():
        print(f'已有配置文件，没有改动：{path}')
        return 1
    mailer.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + '\n')
    print(f'已生成配置模板（只有你的账户能读）：{path}\n填好 host、user、password、from 后运行 check。')
    return 0


def _report(mailer):
    info = mailer.check()
    print(f"登录成功：{info['host']}:{info['port']}（{'SSL' if info['ssl'] else 'STARTTLS'}），发件人 {info['from_name']} <{info['from']}>")
    path = mailer.settings_file
    if not os.environ.get('NEUXMIND_SMTP_HOST') and path.stat().st_mode & 0o077:
        print(f'提醒：这台电脑上的其他账户能读到这个文件里的密码，建议执行 chmod 600 "{path}"')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m brainos.mailer', description='配置并检查验证码邮件的发信账号')
    parser.add_argument('--state-dir', type=Path, required=True, help='应用的状态目录（mail.json 所在目录）')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='生成 mail.json 模板')
    commands.add_parser('check', help='登录邮件服务器，不发信')
    commands.add_parser('test', help='发一封测试验证码邮件').add_argument('to')
    args = parser.parse_args(argv)
    mailer = Mailer(args.state_dir)
    try:
        if args.command == 'init':
            return _init(mailer)
        _report(mailer)
        if args.command == 'test':
            from .mail_templates import verification_mail
            mailer.send(args.to, *verification_mail('register', '000000', 10))
            print(f'测试邮件已交给邮件服务器，请到 {args.to} 的收件箱（和垃圾邮件箱）查看。其中的验证码 000000 只是示例。')
    except (MailError, OSError) as e:
        print(f'失败：{e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
