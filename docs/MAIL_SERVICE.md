# 验证码邮件（issue #37）

注册和重置密码要用邮件里的 6 位验证码。发信账号配置在**这台电脑**上：它不在代码仓库里，也不在安装包里。
没有配置时邮件不会发出，验证码写在本机的 `mail-outbox.txt` 里，注册页会显示它的位置。

## 配置

状态目录：安装版是 `~/Library/Application Support/NeuxMind Physical OS/state`，源码运行是 `--state-dir` 指的目录。

```bash
python3 -m brainos.mailer --state-dir "<状态目录>" init
```

它生成 `<状态目录>/mail.json`（权限 600，已存在时不改动）。填写：

| 键 | 含义 |
|---|---|
| `host` | SMTP 服务器地址。留空表示未配置 |
| `port` | `587`（STARTTLS，模板里的值）或 `465`（SSL）。删掉这一项时按 `465` 处理 |
| `user` | 登录账号，一般就是发信邮箱地址 |
| `password` | 邮箱的密码或授权码 |
| `from` | 发件地址，留空时用 `user`。只写地址，不带名称。要与登录账号一致，或是它有权代发的地址 |
| `from_name` | 收件人看到的发件人名称，默认 `NeuxMind Physical OS` |
| `ssl` | 可不写：端口是 465 时为 `true`，否则为 `false` |

设置了环境变量 `NEUXMIND_SMTP_HOST` 时，整套配置改用环境变量（`NEUXMIND_SMTP_HOST`、`_PORT`、`_USER`、`_PASSWORD`、`_FROM`、`_FROM_NAME`、`_SSL`），`mail.json` 不再读取；没有设置 `HOST` 时其余几个变量不起作用。

改完不用重启应用，下一封邮件就按新配置发。内置测试账号只在启动时没有配置邮件服务的情况下可用，所以它要到下次启动才停用。

## 检查

```bash
python3 -m brainos.mailer --state-dir "<状态目录>" check
```

只登录邮件服务器，不发信。成功时打印服务器、加密方式和发件人；失败时说明该改哪一项。

```bash
python3 -m brainos.mailer --state-dir "<状态目录>" test you@example.com
```

发一封与真实注册邮件相同样式的测试邮件（验证码是示例 `000000`）。

## 邮件的样子

- 发件人显示为 `NeuxMind Physical OS <发件地址>`，标题是 `NeuxMind 注册验证码：123456` 或 `NeuxMind 重置密码验证码：123456`。
- 同一封邮件带纯文本和 HTML 两个版本，内容相同。HTML 不加载任何外部资源，没有链接和按钮。
- 带 `Date`、发件域名下的 `Message-ID`、`Auto-Submitted: auto-generated`（不触发自动回复）。
- 文字和版式在 `brainos/mail_templates.py`。

## 发送失败时

页面上显示一句能照着处理的话，细节（服务器返回的原文）由本地服务写到标准错误输出（logger `brainos.mail`）；安装版看不到这个输出时，用上面的 `check` 重现：

| 页面上的话 | 常见原因 |
|---|---|
| 邮件服务拒绝了登录 | 密码或授权码不对；邮箱没有开启 SMTP 发信 |
| 邮件服务不允许用这个发件地址发信 | `from` 与登录账号不一致，且没有代发权限 |
| 收件地址被邮件服务拒收 | 注册时填的邮箱不存在 |
| 与邮件服务器的加密连接失败 | 端口和 `ssl` 不匹配 |
| 连接不上邮件服务器 / 响应超时 | 网络不通，或 `host`、`port` 写错 |
| 收件地址里有非英文字符 | 邮件服务不支持这样的地址 |
| 邮件配置 mail.json 不是合法的 JSON / port 应该是数字 / 缺少发件地址 / user 或 password 里有非英文字符 | 配置文件写错。写错的配置不会被当成“未配置” |

发送失败时这次验证码作废，不占用“60 秒后才能重发”的等待。

## Microsoft 365（Exchange Online）企业邮箱

- `host` 填 `smtp.office365.com`，`port` 填 `587`。
- 这个邮箱要由管理员开启“经过身份验证的 SMTP”（Microsoft 365 管理中心 → 用户 → 邮件 → 管理电子邮件应用）。租户开了“安全默认值”或这个账号要求多重验证时，密码登录会被拒绝。
- 微软在 2026 年 1 月公布的时间表：2026 年 12 月底起，密码方式的 SMTP 登录对已有租户默认关闭，管理员可以重新打开；最终停用日期将在 2027 年下半年公布。停用后要换成 OAuth，或换用专门的发信服务（例如 Azure Communication Services 邮件、阿里云邮件推送、腾讯云 SES，它们也走 SMTP，本配置不用改代码）。本应用目前只支持密码方式登录 SMTP。
- 来源：<https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835>

## 测试

`tests/test_mail_service.py`：邮件内容、两种加密方式、各种失败的提示、写错的配置、命令行、失败后可立即重发。
SMTP 服务器在测试里是替身；对真实邮件服务器的检查用上面的 `check` 和 `test`。
