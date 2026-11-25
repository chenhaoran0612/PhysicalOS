# 技能在云端：控制台这一侧（issue #43）

各部分之间的接口约定在 [CLOUD_SKILLS.md](CLOUD_SKILLS.md)。本文只讲控制台（桌面端后台 `brainos/` 和页面 `web/`）做了什么：用户看到什么、桌面端后台多了哪些接口、在机器人上执行什么、哪些验证过、哪些没有。

三块，各自一组文件：

| 用户看到的 | 后台 | 页面 |
| --- | --- | --- |
| 侧栏「云端技能」页；机器人页面“已安装技能包”旁的「从云端安装」 | `brainos/cloud_skills.py` | `web/cloud_skills.js`、`.css` |
| 机器人页面的「远程调用」一节 | `brainos/remote_calls.py` | `web/remote_calls.js`、`.css` |
| 机器人页面的「程序和场景数据」一节 | `brainos/robot_bundle.py`、`brainos/robot_bundle_device.py` | `web/robot_bundle.js`、`.css` |

页面不直接访问云平台，也不直接访问机器人：云平台的会话令牌只在工作空间的凭据文件里，三块都经桌面端后台。三个页面脚本排在 `web/index.html` 最后，自己把页面加进侧栏、自己监听 `data-cloudskill-*` / `data-remote-*` / `data-bundle-*`，并包住 `robotDetailRender` 把各自的一节放进机器人页面；`web/robot_detail.js`、`web/sim.js`、`web/app.js` 没有改动。

## 1. 云端技能

### 用户看到什么

- 没有登录云平台：一句“技能库保存在云平台”和去登录的入口，没有发布或安装的按钮。
- 登录后：技能库里每个技能一张卡片（名称、编号、最新版本、还有几个版本、需要机器人具备的能力、会不会让机器人运动），下面列出应用自带的示例技能和它们各自是否已经在云端。
- **发布技能**：第 1 步填技能文件夹的完整路径；第 2 步看到读出来的内容和结论，三种之一：
  - 可以发布（云端还没有这个版本）→ 可以写一句说明，点「发布到云端」；
  - 云端已经有完全相同的这个版本 → 不需要再发布，没有发布按钮；
  - 这个版本在云端已经有了，内容不同 → 不能发布，写明“在 skill.json 里提高版本号”。
- **一键发布示例技能**：把 `runtime/examples/skills` 里的示例逐个发到自己的技能库，结果逐个列出（已发布 / 云端的同一版本内容不同 / 示例本身有问题）。再点一次不会多出东西。
- **版本**：一个技能在云端的各个版本（时间、发布人、文件数和大小、摘要的前 12 位、说明），可以删除某个版本（先确认；已经装到机器人上的不受影响）。
- **安装到机器人**：第 1 步选技能版本和当前项目里一台已连接的机器人；第 2 步是和“从本地文件夹安装”同一张兼容性说明（缺哪项能力、哪个参数不满足），来源一行写明“云端技能库 · 摘要 … 已在这台电脑上核对”；兼容才有「确认安装到机器人」。安装成功后交给 `web/robot_detail.js` 的 `skillInstallResult`，之后和任何一次安装一样：仿真机器人进入运行表单，真机回到机器人页面。

### 后台怎么做

- 读文件夹、校验、兼容性说明都是 `brainos/skill_packages.py` 原有的函数（`read_source`、`validate`、`explain`），没有另写一套。
- **摘要**：一个版本的摘要按机器人管理层安装时的算法算（`runtime/neuxmind_runtime/skills.py`：对 `{"package": 校验后的 skill.json, "files": {...}}` 取 SHA-256），不是 `skill_packages.package_digest`。这样云端记录的、控制台算的、机器人装完后报告的是同一个值。
  - 发布：预览时给出摘要，发布时重新读文件夹，摘要变了就拒绝（`skill_changed_since_preview`）。云端回答里的摘要和本机算的不一样时，发布结果里 `digest_matches` 为假，页面会说明（这种版本以后安装时会被拒绝）。
  - 安装：下载 → 按技能包的规则检查（文件数、单个文件和总大小、都是文本、编号和版本就是所要的那个）→ 本机重算摘要，必须等于云端 `Release.digest`，否则 `skill_digest_mismatch`，不联系机器人 → 读机器人现在的能力清单做兼容性说明 → 原有的 `POST /v2/skills/install`。
  - 确认安装时再下载一次并再核对一次，摘要必须是预检时看到的那个（`skill_changed_since_preview`）。
  - 安装成功后，机器人报告的摘要和这个值不一致时，操作记录里 `digest_matches` 为假。
- **安装要三样东西**（`install`）：机器人所在的项目（`project_id` 必须给，而且就是这台机器人的项目，和远程调用、程序和场景数据的接口一样用 `platform.same_project`）；这个版本对这台机器人做过预检（应用这次运行期间调用过 `install-preview`，后台记着“机器人、技能、版本、摘要”，没做过的回答 `skill_preview_required`）；人的确认（`authorized === true`，只认真正的 `true`，页面在点「确认安装到机器人」时发出，否则 `skill_install_not_authorized`）。只拿列表里的摘要直接调 `install` 是装不上的。
- 安装的幂等和对结果的判读与本地安装相同：同一个 `request_id` 返回已有的记录，不再下载、不再发送；结果未知不自动重试。操作记录多一个 `source: "cloud"`。
- 云端回答里的字段逐个取、逐个截断（`release_view`），不认识的字段不往页面传；标题、说明这些别人写的文字在页面上一律转义。
- 一次回答最多读 8 MiB（技能包文字最大 1.5 MiB，JSON 转义后有余量）；其余和所有访问云平台的调用一样：不走代理、不跟随跳转、对实际连到的地址应用地址策略。

### 桌面端后台的接口（POST，JSON）

| 路径 | 作用 |
| --- | --- |
| `/api/cloud-skills/overview` | 账户摘要、技能库、自带示例及其是否已在云端（未登录时都是空的） |
| `/api/cloud-skills/detail` | `{skill_id}` → 这个技能的各个版本 |
| `/api/cloud-skills/publish-preview` | `{path}` 或 `{example}` → 读取、校验、和云端比对；不发送 |
| `/api/cloud-skills/publish` | `{path 或 example, digest, notes?}` → 发布预览过的那份内容 |
| `/api/cloud-skills/publish-examples` | 发布全部自带示例 |
| `/api/cloud-skills/delete` | `{skill_id, version}` |
| `/api/cloud-skills/install-preview` | `{robot_id, skill_id, version, project_id}` → 下载、核对、兼容性说明；不改动机器人 |
| `/api/cloud-skills/install` | 同上加 `package_digest`、`request_id`、`authorized: true` → 安装（要先预检过） |

## 2. 远程调用（机器人页面的一节）

### 用户看到什么

- 这台机器人和云端连没连上：已连上云端（云平台地址、上次同步是多久以前）/ 正在连接 / 没有连上（原因）/ 没有配置云端连接 / 云端连接已关闭。
- 每个已安装技能一行：名称、会不会让机器人运动、一个「允许远程调用」开关；打开着的写明是谁、什么时候打开的；技能重装成别的内容后开关失效，行上写明原因。
- **打开要确认**：点开关先弹出确认框（在机器人页面之上，另一个对话框，机器人页面不会被换掉）。会让机器人运动的技能，确认框里写明“这个技能会让机器人运动。打开后，第三方可以经云平台让这台机器人运动，不需要有人每次确认。”，要勾选“我了解第三方可以让这台机器人运动，并确认它所在的场地适合这样做”才能确认。不会运动的技能是另一段话。确认框还写明会记下是谁打开的。
- **关闭不用确认**，点一下就关。
- 最近的远程调用：时间、技能、状态（运行中 / 已完成 / 失败 / 已取消 / 已停止 / 机器人拒绝了）、说明（用时或拒绝的原因）。
- 管理层没有 `/v2/remote`（旧版本）：只有一句“这台机器人的管理层还不支持远程调用”，不是报错，也没有开关。
- 机器人没有连上管理层：一句“连上这台机器人的管理层并通过连接检查后，才能在这里设置远程调用”。

### 后台和页面怎么做

- 读 `GET /v2/remote`、写 `POST /v2/remote/skills`，走控制台访问 `/v2` 的同一条路（`connector.call`：直接地址，或经机器人的 SSH 连接转发）。
- 打开时后台要求 `confirmed === true`（只认真正的 `true`），向机器人发 `hardware_authorized: true`、`set_by` 和 `digest`；`digest` 是确认框打开时那个已安装技能的内容摘要，机器人发现技能在这期间被重装成别的内容就回答 409 `skill_changed`，页面在确认框里说明、去掉确认按钮并重新读取列表。`set_by` 是登录账号的名称（没有名称时是邮箱），由后台按会话决定，页面改不了。关闭时发 `hardware_authorized: false`。每次修改记一条审计（`remote.skill_switched`）。
- **会不会让机器人运动**按技能可能用到的全部能力判断：`requires` 和 `optional` 都算（管理层允许技能使用它列为可选的能力）。这一节自己读 `/v2/skills`（`remote_calls.installed`），因为公用的 `robot_caps.installed_skills` 不带 `optional`。能力不在标准里、或者列表读不懂的，一律按会运动处理。
- 一次读取在路上的时候发了开关：那次读取的回答比开关的结果旧，页面把它丢掉（`remoteAnswerOutdated`），开关不会跳回旧状态。
- 404 且没有错误码（或 `not_found`）算“不支持”；404 `skill_not_found` 是机器人的原话，照样显示。
- **不进状态快照**：连接状态和最近的调用会自己变，控制台只要 `/api/state` 的内容变了就重画整个 `#main`。所以这些数据只从自己的接口 `robots/remote` 来，页面每 5 秒问一次，原地更新连接那一行、调用列表和状态有变化的开关；开关元素不重建，焦点、打开着的确认框、滚动位置都不动。浏览器验收里检查了：调用到达前后，开关是同一个元素、焦点还在它上面、背后的页面没有重画。
- 一次读取暂时失败（机器人一时连不上）时保留上次显示的内容，不把它说成“不支持”。

### 桌面端后台的接口（POST，JSON）

| 路径 | 作用 |
| --- | --- |
| `/api/robots/remote` | `{robot_id, project_id}` → `{supported: true / false / null, link, skills: [{id, version, title, motion, allowed, stale, set_by, set_at}], calls}`；只读 |
| `/api/robots/remote/skill` | `{robot_id, project_id, skill_id, allowed, confirmed, digest（打开时）, request_id}` → 同上（修改后的状态） |

## 3. 程序和场景数据（机器人页面的一节）

只对有 SSH 连接的真机显示（仿真机器人没有自己的电脑，不显示这一节）。

### 用户看到什么

- **收进云端**：确认框里填机器人上的目录（例如 `~/walk_programs`），写明不会收进来的内容、大小上限、只读取不改动；勾选确认后开始。进度一行原地更新：正在机器人上打包 → 正在从机器人取回（百分比）→ 正在送到云平台（百分比）→ 已收进云端：文件名（几个文件，多大）。可以停止。
  - **没有收的是哪些**，结束后列出来：像登录凭据的文件和目录有几个、前几个的名字；按约定跳过的（运行记录、缓存、备份）有几项、前几个的名字。看了才知道云端这一份里缺什么。
  - 内容和云端已有的一份完全相同（同一台机器人，SHA-256 相同）时，说明“云端已经有内容完全相同的一份”，不取回、不上传、不多存一份。
  - 目录里有指向目录之外的链接时，结束后会说明有几个：链接原样收了，它们指向的内容没有收，这样的一份装回机器人时会被拒绝。
  - 打包时有文件正在被改写（GNU tar 以 1 结束）时也会说明。
- 云端里这台机器人的程序和场景数据：文件名、大小、校验值前 12 位、收进来的时间，每行一个「装到机器人」。每行还写明来历：“从这台机器人的 ~/… 收进来”（这台电脑上的控制台收的，依据是本机的审计记录，资料编号和校验值都对得上），或者“不是这台电脑上的控制台收的（云端记录的上传人：…）”。组织里的成员都可以往一台机器人名下上传，所以后一种在「装到机器人」的确认框里另有一段提醒，请人先确认来源。
- **装到机器人**：确认框写明送到哪里、解到哪里、`current` 指向它，**不会覆盖机器人上原来的目录，也不改适配器的配置**；要让管理层用这一份，装完后由人把 `ros2-config.json` 里的路径改到 `current`。勾选确认后开始：正在从云平台取 → 正在送到机器人 → 正在机器人上解开 → 已装到机器人的 `~/neuxmind-bundles/<名字>/<16 位>`。机器人上原来解开的这一份被改动过时，结果里写明它被另存到了哪里。
- “做过的操作”：最近 20 次收和装，来自审计记录。
- 没有 SSH 连接、没有登录云平台时各有一句说明，没有按钮。

### 规则

- **人的确认**：两个动作都要 `authorized === true`（只认真正的 `true`），没有它不联系云端也不联系机器人。
- **目录**（`robot_bundle_device.directory`，连接之前就检查）：`/` 开头或 `~/` 开头；最多 300 个字符；不能有控制字符、换行、改变文字方向的字符；不能有 `.`、`..` 或空的段；不能是主目录或根目录本身；路径里不能有存放登录凭据的目录或文件名（下面“不收的凭据”那张清单，按完整的名字认：`~/secrets_nav`、`~/credentials_helper` 是普通目录）；不能在 `~/neuxmind-bundles` 里。这只看得到人填的文字；目录在设备上解析成实际路径之后，脚本再查一遍（`ln -s .ssh ~/tools` 再填 `~/tools` 会被拒绝）。空格、中文、引号、`$()` 这些字符都允许。
- **人填的文字不进命令行，也不进脚本**：设备上只执行 `robot_bundle_device.py` 里两段固定的 POSIX sh 脚本，脚本文本经标准输入交给 `sh -s`（和 #18 的通道一样）。目录作为一个只含十六进制数字的参数传过去，由脚本自己用 shell 内建的算术和 `printf` 还原；其余参数只有名字（字母、数字和 `. _ + -`）、16 位十六进制和数字。测试里用带空格、引号、分号、`$(…)`、反引号、`|&>` 的目录名实际跑过，没有任何一个被当成命令。
- **打包**（`PACK_SCRIPT`）：
  - 目录在设备上解析成实际路径；是主目录或它的上级、是 `/`、在 `~/neuxmind-bundles` 里、或者实际路径经过凭据目录（`.ssh`、`.git`、`.config` 等）的都拒绝。
  - 收哪些由脚本自己用 `find` 列出来交给 `tar --null --no-recursion -T -`，不用 tar 的 `--exclude`（GNU tar 的排除模式会跨 `/`、在任何一层匹配，`secrets*` 会把 `secrets_loader/__init__.py` 也丢掉）。每条规则只看路径的最后一段，是完整的名字或者一个后缀：
    - 约定跳过的：名叫 `runs`、`__pycache__` 的目录；`*.pyc`、`*.bak*` 的文件（只对文件：名叫 `maps.bak.d` 的目录照收）。
    - 不收的凭据，目录：`.ssh`、`.aws`、`.gnupg`、`.git`、`.config`、`.kube`、`.docker`、`.azure`。
    - 不收的凭据，文件：`.netrc`、`.git-credentials`、`.npmrc`、`.pypirc`、`.env`、`.env.*`、`.envrc`、`authorized_keys`、`known_hosts`、`id_rsa` / `id_dsa` / `id_ecdsa` / `id_ed25519`（和带后缀的，如 `.pub`）、`credentials`、`credentials.json`、`token.json`、`*.pem`、`*.key`。
    - 名字只是像的不算：`secrets_loader/`、`credentials_helper.py`、`secrets.yaml`、`id_card_reader.py`、`token.json.tmpl` 都照收。清单之外的凭据文件（自己起名的密钥、写在配置文件里的口令）认不出来，会被收进云端；确认框里写明了不收的是哪些名字。
  - 答复里带上没有收的：两类各自的总数和前 20 个名字（被整个跳过的目录算一个）。
  - 不跟随符号链接（链接原样存）。目录里有设备文件、管道、套接字（跳过的目录之外）时拒绝。有读不了的子目录时算失败，不收半份。
  - GNU tar 上加 `--hard-dereference`：硬链接按普通文件存。
  - 输出经 `head -c` 截在上限加 1 字节：超过 64 MiB 的不会写满磁盘，超了就删掉并报告。
  - `~/neuxmind-bundles` 和 `.outgoing` 先确认不是符号链接、实际路径就是它自己，然后才 `mkdir`：`~/neuxmind-bundles` 被链接到程序目录时什么都不会建在里面。
  - 压缩用 `tar -cf - | gzip -n`：gzip 头里没有文件名和打包时间，同样的文件什么时候打包都是同样的字节、同样的校验值，“云端已经有这一份”才判断得准（macOS 的 bsdtar 自己压缩时会写进当前时间；0.21.7 起改成这样）。gzip 的结果单独记下：设备上没有 gzip 或 gzip 失败时，打包报失败（`bundle_pack_failed`，说明里是 gzip 的原话），不会把空包当成打好的包送上云端。
  - 包放在 `~/neuxmind-bundles/.outgoing/<随机 16 位>.tar.gz`（目录 700），在设备上算 SHA-256；用完删除。会话没了（停止、断线）时脚本收到信号或写答复失败，自己把包删掉；控制台这边不管在哪一步结束（包括打包时被停止），都会再连一次把它删掉；仍然留下的，下次打包时把超过一天的清掉。
- **取回**：经 SFTP 读回本机 `<工作空间>/bundle-cache/`，连接断了从已取到的位置接着读（连续 7 次没有进展才放弃）；读完算 SHA-256，必须等于设备上算出的那个，否则不上传。本机副本用完即删。
- **去重**：打完包先问资料库这台机器人名下有没有 SHA-256 相同的一份（同样的文件打出来的包逐字节相同）。有 → 结果里 `already_in_library` 为真，不取回、不上传。
- **上传**：资料库的分片上传，`kind: "bundle"`，`device_id` / `device_name` 是这台机器人；文件名 `<名字>-<UTC 日期时间>.tar.gz`。资料库要求的标注这样填：本体类型用机器人的类型，厂家写“机器人上的程序”，型号写名字，版本写日期时间，说明里写来自哪台机器人的哪个目录。云端回答 409 `upload_kind_conflict`（同一个文件有一次当作驱动安装包、没传完的上传）时：没传完的上传只有发起它的账号看得到，所以它是这个用户自己的，控制台放弃那一次再重新开始，结果里 `cancelled_other_upload` 为真，页面上说明。
- **名字**：`<名字>` 取目录的最后一段；它不是合法名字（中文、空格开头等），或者 #18 的通道不肯写（`secrets_nav` 这类），就用 `bundle-` 加整个目录的 SHA-256 前 8 位，两个这样的目录不会共用一个位置。
- **装到机器人**：资料必须是 `bundle`、状态 `usable`、属于这台机器人（另一台机器人的不装，`bundle_other_device`）、不超过 64 MiB；`package_fetch` 取回并核对 SHA-256；经 #18 的通道送到 `~/neuxmind-bundles/<名字>/<SHA-256 前 16 位>.tar.gz`（通道允许的目录换成 `~/neuxmind-bundles`，权限 600，设备上核对过）；然后执行解开的脚本。`<名字>` 取收的时候记下的名字（资料的“型号”），不是合法名字时从文件名去掉后缀和日期得到。
- **解开**（`EXTRACT_SCRIPT`）：
  1. `~/neuxmind-bundles/<名字>` 必须是它自己（不是链接）；目标目录的位置被文件或链接占着、`current` 是真实的目录或文件时拒绝，不覆盖。
  2. 解之前看清单：有绝对路径或 `..` 的拒绝；条目类型只允许普通文件、目录、符号链接，设备文件、管道、硬链接都拒绝；解开后的大小超过 1 GiB 的拒绝。
  3. 解到同一目录下的临时目录（`--no-same-owner --no-same-permissions`，不保留属主），去掉 setuid / setgid 和组、其他人的写权限。
  4. 解完再查一遍：有设备文件、管道、套接字，或者有指向目录之外的符号链接（绝对目标；目标或目标所在目录按文件系统实际解析后在目录之外；目标所在目录不存在而路径里有 `..`）→ 整个临时目录删掉，什么都不留。指向目录之内的链接保留，包括目标没被收进来的（例如指向 `runs/` 里）。
  5. **已有的同名目录不直接相信**：程序从 `current` 运行，可能改过它。把它和刚解开的逐个文件比（`diff -r`）：一样 → 不动它，`already=yes`；不一样 → 把它改名成 `<16 位>.changed-<日期时间>` 留着，换上云端的这一份，答复里带上改名后的名字。不会把改动过的内容报告成“已装好”，也不会删掉它。
  6. 到这里为止什么都没有改。先写一行答复：会话已经没了（停止、超时、断线）时这一行写不出去，脚本清掉临时目录后退出，`current` 不动。脚本还捕获 HUP、INT、PIPE、TERM，同样清理后退出。
  7. 目录就位之后，最后一步是一次改名把 `current` 换成新的链接（`mv -T`，BSD 上 `mv -h`）：`current` 要么是旧的要么是新的，不会有一刻不存在。
  - 全程只写 `~/neuxmind-bundles/<名字>/` 之内，机器人上原来的目录没有任何一步会碰到。
- **记录**：不按机器人另存记录；做过什么写在审计记录里（`bundle.collected`、`bundle.installed`），页面的“做过的操作”从那里读。所以 `robot_remove.ROBOT_RECORDS` 没有变，删除机器人后审计记录照旧保留（和其他审计记录一样）。
- 同一台机器人同一时间只进行一个收或装；应用退出时正在进行的会停下。收到一半中断不续传，重新开始即可（包最大 64 MiB）；装的一段沿用 `package_fetch` 和 #18 通道各自的续传。

### 桌面端后台的接口（POST，JSON）

| 路径 | 作用 |
| --- | --- |
| `/api/robots/bundles` | `{robot_id, project_id}` → 有没有 SSH、云端里这台机器人的 bundle、进行中的和做过的 |
| `/api/robots/bundles/collect` | `{robot_id, project_id, directory, name?, authorized: true}` → 后台开始收，返回这一次的记录 |
| `/api/robots/bundles/install` | `{robot_id, project_id, material_id, authorized: true}` → 后台开始装 |
| `/api/robots/bundles/status` | `{job_id, project_id}` → `{status: running / done / failed / stopped, phase, done, total, result, error, code}` |
| `/api/robots/bundles/stop` | `{job_id, project_id}` |

### 设备需要什么

SFTP、`sh`、`tar`、`gzip`、`find`、`head`、`grep`、`sort`、`tr`、`cut`、`wc`、`readlink`、`dirname`、`ln`、`mv`、`rm`、`chmod`、`mkdir`，以及 `sha256sum`、`shasum`、`openssl` 三者之一。脚本按 GNU tar（Ubuntu 22.04）写，bsdtar（macOS）上也能跑。

## 错误码

都在 `web/ux04_errors.js` 的 `UX_CODES` 里有白话说明（“skills kept in the cloud”“remote calls”“a robot's programs and scene data”三段），包括约定里云端和管理层的错误码。控制台自己新增的：

- 云端技能：`skill_changed_since_preview`、`skill_download_invalid`、`skill_digest_mismatch`
- 远程调用：`remote_unsupported`、`remote_no_runtime`、`remote_confirm_required`、`remote_switch_unconfirmed`
- 云端技能（审查后加的）：`skill_install_not_authorized`、`skill_preview_required`；另外补了云端和管理层的 `skill_changed`、`upload_kind_conflict`
- 程序和场景数据：`bundle_not_authorized`、`bundle_no_ssh`、`bundle_directory_invalid`、`bundle_directory_missing`、`bundle_directory_refused`、`bundle_too_large`、`bundle_pack_failed`、`bundle_device_unreachable`、`bundle_fetch_interrupted`、`bundle_checksum_mismatch`、`bundle_cancelled`、`bundle_send_failed`、`bundle_not_bundle`、`bundle_not_usable`、`bundle_other_device`、`bundle_unsafe`、`bundle_occupied`、`bundle_extract_failed`、`bundle_busy`

## 测试

```sh
PYTHONPATH=spec:runtime:sdk:.:tests python -m unittest test_cloud_skills test_cloud_skills_real test_remote_calls \
    test_robot_bundle test_robot_bundle_scripts
node tests/cloud_skills_logic.cjs && node tests/remote_calls_logic.cjs && node tests/robot_bundle_logic.cjs
python tests/cloud_skills_browser_fixture.py CONFIG.json &      # 另开一个终端
node tests/cloud_skills_browser.cjs CONFIG.json artifacts/issue43      # BRAIN_BROWSER=webkit 换内核
```

测试替身（都只在本机，什么都不连真服务、真机器）：

- `tests/skill_cloud_fixture.py`：`FakeSkillCloud`，按约定写的假云端（技能库，和资料库里 bundle 用到的那部分）。它算版本摘要用的是管理层自己的函数，不是控制台的。
- `tests/remote_runtime_fixture.py`：`RemoteRuntime`，带 `/v2/remote` 的假管理层，可以切成“旧版本”（两条路径都 404）。
- `tests/bundle_fixture.py`：`BundleDevice`，#18 的 SSH 测试替身加上这两段脚本：标准输入里的脚本必须和产品的逐字节相同、每个参数符合那段脚本的形状才执行，执行时 `HOME` 移到临时目录、`PATH` 里只有允许的工具；绝对路径的目录先移到临时目录之下。可以让 SFTP 读到一半断开、读出一个错字节。

## 在哪里验证过，哪里没有

| 内容 | 怎么验证的 |
| --- | --- |
| 云端技能（列表、预览、发布、冲突、删除、示例一键发布） | 假云端（`test_cloud_skills`）；**真云端**（`test_cloud_skills_real`：本检出的 `cloud/` 应用跑在本机 SQLite 上）也跑通，控制台算的摘要和云端记录的一致 |
| 从云端安装 | 假云端 + **真管理层**（仿真四足，真的 `neuxmind_runtime` 子进程，真的 `/v2/skills/install`）；真云端 + 真管理层也跑通，机器人报告的摘要等于云端记录的 |
| 下载被改动、不合规、版本在预检后变了、不兼容 | 假云端注入 |
| 远程调用的读和写 | 假管理层（各种状态）；**真管理层**的 `/v2/remote`、`/v2/remote/skills`（`test_remote_calls.RealManagementLayer`，仿真四足：打开、读回、关闭、没装的技能被拒绝）。真管理层没有这两个接口时这一类自动跳过 |
| 连上云端时的连接状态、最近的调用 | 只有假管理层：仿真机器人没有配置云端连接，状态是“没有配置” |
| 打包和解开的两段脚本 | 真的执行：macOS（bsdtar，`/bin/sh`）；**Linux**：`python:3.13-slim`（Debian，GNU tar 1.35，dash）和 **Ubuntu 22.04**（`ros:humble-ros-base-jammy`，GNU tar 1.34，dash 和 bash，root 和普通用户各一遍），容器都是 `--network none`。35 个用例（0.21.7 加了“相隔 1.2 秒打两次包，字节相同”和“gzip 失败时报失败”），Linux 上全部通过；macOS 上跳过 1 个（硬链接按普通文件存只有 GNU tar 才做） |
| 收进云端、装到机器人的整条流程 | `BundleDevice`（脚本在本机真的执行）+ 假云端；+ **真云端**的资料库（`kind=bundle`、状态直接 `usable`、驱动安装包的送达不接受它） |
| 页面 | 逻辑检查 3 个脚本；浏览器验收在 Chromium 和 WebKit 上各一遍，截图看过宽窗口和 600 px 窄窗口 |

没有验证的：

- **真机**。没有连过任何真实机器人：真机上的 `tar`、`find`、磁盘、权限、经跳板机的 SFTP 读取速度都没有实测。SFTP 读回没有并行请求，一次读 256 KiB；往返时延大的链路上 64 MiB 会比较慢。
- **公网云平台**。真云端只是本检出的应用跑在本机；没有访问过任何部署着的云平台（包括本机 8800 端口上的那个）。
- 机器人真的连上云端时 `/v2/remote` 的 `link` 和 `calls`（需要云端和管理层的联调，不在控制台这一侧）。
- busybox 的 `tar`：脚本没有在 busybox 上跑过。
- 弱网（丢包、限速）下的收和装。

## 没有做的

- 技能文件夹用系统的文件夹选择框来选（现在只能粘贴路径；本地安装那边有选择框）。
- 收进云端之后删除云端的某一份（要删去「资料库」页删）。
- “先预检过”只记在这次运行的内存里：应用重启后要重新预检一次才能安装。
- 来历只分得出“这台电脑上的控制台收的”和“别的”：云端记录的上传人是账号编号，控制台不知道它对应谁，原样显示。
- 收到一半中断后的续传；一次收多个目录。
- 把 `ros2-config.json` 的路径改到 `current`：按约定由人来改，控制台不碰适配器的配置。
- 发放开放平台的 API Key、查看云端一侧的调用记录：在云平台那一侧。
