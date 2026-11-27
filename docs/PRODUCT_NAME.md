# 产品名称与登录页（issue #55）

## 名称

产品叫 **NeuxMind Physical OS**。用户能读到的地方都用这个名字：

| 位置 | 文字 |
|------|------|
| 浏览器标题 | `NeuxMind · Physical OS` |
| 登录页、工作台首屏标签 | `PHYSICAL OS`、`NEUXMIND / PHYSICAL OS` |
| 等待提示 | 正在连接 Physical OS… |
| 桌面应用 | 窗口标题、菜单（关于 / 退出）、提示框、启动提示、`CFBundleName`、`CFBundleDisplayName` |
| 应用与安装包文件名 | `NeuxMind Physical OS.app`、`NeuxMind-Physical-OS-<版本>-arm64.dmg` / `.pkg`、安装盘卷名 |
| 验证码邮件 | 发件人名称、页眉、正文落款 |
| 命令行与包说明 | `nx`、`neuxmind-runtime`、设备网关的帮助文字，四个 Python 包的说明 |

### 有意不改的地方

下面几处仍是旧拼写。它们是标识或位置，不是给人读的名字，改了会影响已有数据或权限：

| 位置 | 原因 |
|------|------|
| 应用标识 `com.neuxmind.` + `robotos` | macOS 按它记本地网络权限和窗口状态。改了要重新授权。 |
| 工程运行时上报的客户端名 `neuxmind_` + `robot_os` | 模型服务端的记录和云平台测试样本里是这个名字。 |
| 历史记录（`docs/evolution/`、`docs/HANDOFF.md`、两份带日期的架构图） | 记录当时的名字。 |

`tests/test_physical_os_name.py` 扫描源码、页面和文档：除了上表列出的行，任何地方再出现旧名字都会失败；表里的某一项不存在了也会失败。

### 数据目录

数据目录跟着改名：`~/Library/Application Support/NeuxMind Physical OS/`。规则在 `desktop/DataFolder.swift`，每次启动判断一次：

| 启动时的情况 | 做什么 |
|------|------|
| 新目录已经在 | 直接用。旧目录如果也在，不动它。 |
| 只有旧目录 | 把旧目录整个改名为新目录（同一个磁盘上的改名，不复制），再在旧位置留一个指向新目录的链接。 |
| 两个都没有 | 用新目录（首次安装）。 |
| 改名做不成（没有权限、新位置被别的东西占着） | 继续用旧目录，原因写进系统日志。不会在数据旁边另起一个空目录。 |

旧位置留链接，是因为数据里的记录可能写着旧路径，旧版应用和一些脚本也还在用旧路径；有了链接，它们读到的是同一份数据。

`tests/test_data_folder.py` 把这个文件编译出来，在临时目录上跑上表每一种情况（需要 Mac 上的 Swift 编译器，没有就跳过）。

### 升级时要知道的

- 应用文件名变了。把新版拖进「应用程序」不会覆盖旧的 `.app`，旧的要先退出、再手动删掉。
- 第一次打开新版前先退出旧版：旧版开着的时候数据目录也能改名，但两个应用同时写同一份数据没有保护。
- 发件人名称只在 `mail.json` 没写 `from_name` 时才用默认值。已经写了旧名字的要自己改。

## 登录页

左侧只留一句口号「让你的机器人，准备上岗。」，原来的四个步骤（接入本体、开发应用、测试验证、部署上岗）已删除。

- 口号是左侧最大的文字，第二行用渐变色点亮；上方一行小字写产品名，底部是一行只有描边的大字 `PHYSICAL OS`。
- 背景有三团缓慢移动的光、三圈带光点的轨道和一层细网格。动的只有位移、旋转和透明度。
- 系统开了「减少动态效果」时全部静止，口号直接显示。
- 窗口宽度不到 861px 时左侧隐藏，只显示表单；窗口很矮（不到 620px）时去掉底部大字、口号缩小。
- 在登录、创建账号、验证码、重置密码之间切换，只换右侧表单，左侧不重画，动画不会从头再来。

样式在 `web/login_hero.css`，结构在 `web/account.js` 的 `acctGateHtml()`，切换表单的逻辑在 `acctDrawGate()`。

## 验证

```bash
python -m unittest tests/test_physical_os_name.py
node tests/login_page_browser.cjs http://127.0.0.1:<端口> <截图目录>
```

浏览器脚本需要一个用临时数据目录启动的后端（`NEUXMIND_MAIL_DEV_ECHO=1 NEUXMIND_CLOUD_AUTO_LOGIN=0`）；设 `BRAIN_BROWSER=webkit` 用 WebKit 跑。
