"""The wording and look of account e-mails (issue #37).

Each mail has a plain-text part and an HTML part with the same sentences. The HTML uses tables and
inline styles only (mail clients drop style sheets), loads nothing from the network and has no links:
a verification mail that asks for nothing but reading a code is the least likely to be taken for phishing.
"""
import time
from html import escape

PRODUCT = 'NeuxMind Physical OS'

PURPOSES = {
    'register': {'title': '注册验证码', 'lead': f'你正在注册 {PRODUCT} 账号，请在注册页面输入下面的验证码：',
                 'ignore': '如果不是你本人操作，请忽略这封邮件，不会有账号被创建。'},
    'reset': {'title': '重置密码验证码', 'lead': f'你正在重置 {PRODUCT} 账号的密码，请在重置页面输入下面的验证码：',
              'ignore': '如果不是你本人操作，请忽略这封邮件，你的密码不会改变。'},
}

INK, MUTED, BRAND, ACCENT, PAGE, PANEL, LINE = '#1c2b27', '#6b7a75', '#1b2b27', '#d7f5b1', '#f2f4f1', '#f4f7f4', '#e3e8e4'
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"


def verification_mail(purpose, code, ttl_minutes):
    """Returns (subject, text, html) for a verification code."""
    if purpose not in PURPOSES:
        raise ValueError(f'unknown mail purpose: {purpose}')
    words = PURPOSES[purpose]
    validity = f'验证码 {ttl_minutes} 分钟内有效，只能使用一次。请不要把它告诉任何人。'
    footer = '此邮件由系统自动发送，请勿直接回复。'
    rights = f'© {time.strftime("%Y")} NeuxMind'
    subject = f'NeuxMind {words["title"]}：{code}'
    text = '\n'.join([PRODUCT, '', '你好，', '', words['lead'], '', f'    {code}', '', validity, words['ignore'], '', footer, rights, ''])
    html = _HTML.format(title=escape(subject), preheader=escape(f'{words["title"]}，{ttl_minutes} 分钟内有效'), lead=escape(words['lead']),
                        code=escape(code), validity=escape(validity), ignore=escape(words['ignore']), footer=escape(footer),
                        rights=escape(rights), ink=INK, muted=MUTED, brand=BRAND, accent=ACCENT, page=PAGE, panel=PANEL, line=LINE, font=FONT)
    return subject, text, html


_HTML = '''<!doctype html>
<html lang="zh-CN">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:{page};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{page};">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{page};">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="520" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:520px;background:#ffffff;border:1px solid {line};border-radius:12px;">
<tr><td style="background:{brand};border-radius:12px 12px 0 0;padding:22px 32px;font-family:{font};">
<span style="font-size:19px;font-weight:700;color:#ffffff;">NeuxMind</span>
<span style="font-size:11px;letter-spacing:3px;color:{accent};padding-left:8px;">PHYSICAL OS</span>
</td></tr>
<tr><td style="padding:32px 32px 8px;font-family:{font};color:{ink};font-size:15px;line-height:1.7;">
<p style="margin:0 0 12px;">你好，</p>
<p style="margin:0;">{lead}</p>
</td></tr>
<tr><td style="padding:16px 32px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td align="center" style="background:{panel};border:1px solid {line};border-radius:10px;padding:20px 12px 20px 22px;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:34px;font-weight:700;letter-spacing:10px;color:{ink};">{code}</td></tr>
</table>
</td></tr>
<tr><td style="padding:8px 32px 28px;font-family:{font};color:{ink};font-size:14px;line-height:1.7;">
<p style="margin:0 0 8px;">{validity}</p>
<p style="margin:0;color:{muted};">{ignore}</p>
</td></tr>
<tr><td style="border-top:1px solid {line};padding:18px 32px 22px;font-family:{font};color:{muted};font-size:12px;line-height:1.7;">
{footer}<br>{rights}
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
'''
