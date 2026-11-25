# 资料解析（issue #34，R1-13）

资料库（[MATERIAL_LIBRARY.md](MATERIAL_LIBRARY.md)，issue #33）里的资料上传完成后自动解析。解析结果给人看、可以改，人确认后才标「可用」。资料属于一台设备，在「我的机器人」里那台设备的设备档案中上传和查看。

## 用户定下的做法（2026-11-21）

- 文档（Word、Excel、PDF、各种文本）交给 **WeKnora**（腾讯开源的知识库服务）解析和存储，再经它问大模型，把固定项目填出来。**厂家的资料会离开云平台，发给 WeKnora 里配置的模型服务**；用哪个模型、模型在哪里运行，在 WeKnora 里定。
- URDF 和驱动安装包在云平台自己用规则读，不发给任何外部服务。
- 云平台只配 WeKnora 的地址、API Key 和知识库编号，连已有的 WeKnora；不在云平台的 compose 里部署它。没配时，文档标「没解析出来」，可以手工填写后确认。
- 资料**只属于一台设备**：同一本手册给十台设备，要在十台设备的档案里各传一次（云端不再当成重复拒收）。同一个文件只送 WeKnora 一次、只问一次模型，其余几份沿用结果；每台设备各自确认。
- 设备档案和本体档案合并成一个，另开 issue 处理；「资料库」页暂时保留。

## 四个状态

资料的 `status`（云端 `library_materials.status`）：

| 取值 | 页面上 | 什么时候 |
|---|---|---|
| `parsing` | 解析中 | 上传完成后；“重新解析”后 |
| `parsed` | 待确认 | 解析完、必填项齐全，还没有人确认；人改过已确认的结果后也回到这里 |
| `usable` | 可用 | 有人核对并确认了。确认人和时间记在结果里（`confirmed_by`、`confirmed_at`），也进审计日志（`library.parse_confirmed`） |
| `failed` | 没解析出来 | 解析失败，或者缺必填项。`status_detail` 写明原因或缺哪一项（如“缺：关节 elbow 的位置下限、力矩上限”）。人填好（或把不适用的项目标为“不适用”）后确认，也变 `usable` |

只有 `usable` 的资料能被后面的流程使用：#31 把安装包送到设备时只送 `usable` 的；#30 的修复助手只读 `usable` 的。

## 提取什么

| 资料 | 怎么读 | 项目（* 必填） |
|---|---|---|
| 文档：Word、Excel、PDF、Markdown、文本、YAML、JSON、XML | WeKnora 解析后问模型三个问题（只限这一份文档，不联网搜索） | 驱动名称*、驱动版本*、从哪里取驱动*、适用的系统*、适用的 ROS 2 发行版*、安装步骤*、装好后应该出现的话题*、动作 |
| 网页（`.html`、`.htm`，issue #45） | 云端规则先取出网页里的文字，再把这些文字当作一份 Markdown 文档交给 WeKnora，之后和别的文档一样 | 同上 |
| URDF（`.urdf`、`.xacro`） | 云端规则 | 机器人名称、连杆*、关节*（每个关节的类型、父子连杆、位置下限和上限、速度上限、力矩上限）。转动关节和移动关节要四个上限，连续转动关节要速度和力矩上限 |
| 驱动安装包（deb、zip、tar.gz、tar.xz、tar.bz2、tar） | 云端规则，**不解析内容** | 校验值*（资料的 SHA-256，不能改）、名称*、版本*、适用架构*；选填：系统、ROS 2 发行版、安装步骤、说明 |

- URDF 的上限**只是填写设备安全限值时的参考，不会自动生效**（安全限值见 issue #22）。xacro 表达式（`${pi/2}`）不求值，按没填处理，页面上显示资料里原来写的内容；`xacro:include` 引用的别的文件不去读。
- 安装包的名称、版本、架构取自包里写明的地方：deb 的 `control`（`Package`、`Version`、`Architecture`，另有 `Description`、`Homepage`、`Depends` 写进“说明”）；zip / tar 里的 `DEBIAN/control` 或 ROS 的 `package.xml`；架构写不出来时按包里程序的机器类型判断（ELF 头），并提示核对。Ubuntu 22.04 默认用 zstd 压缩 deb 的 `control` 部分：云端镜像里用系统自带的 `dpkg-deb` 读（在同样的限制下）；没有 `dpkg-deb` 的环境里这一项标“没解析出来”，人来填。
- 模型的回答只是数据：不是要求的 JSON 格式的，那几项留空并说明；“资料中未提及”之类的回答按没填处理；每项有长度和条数上限，控制字符去掉。安装步骤是文字，云端和桌面端都不执行。

## 不可信的内容

上传的文件、WeKnora 的答复和模型的回答都当作不可信的输入：

- **规则解析在子进程里做**（`cloud/neuxmind_cloud/material_extract.py`，由 `material_sandbox.py` 启动）：`python -I -S` 运行，只用标准库；文件作为标准输入交给它（不跟随符号链接），环境变量几乎清空，工作目录是 `/`，单独的进程组。它在读第一个字节之前先给自己设上限：CPU 时间（默认 60 秒）、地址空间（Linux，默认 1 GiB）、写文件大小 0、打开文件数。父进程另有墙钟时间上限（默认 120 秒）和输出上限（4 MiB），超了就杀掉整个进程组。崩溃、卡死、超限都只让这一份资料变成“没解析出来”，云端进程本身从不解析文件。
- **解压炸弹**：压缩包只读成员名和必要的一小段内容（说明文件最多 1 MiB、程序只读前 20 字节），不解压到磁盘；解出的字节全部计入上限（默认 8 GiB），xz 解码器有内存上限（128 MiB）；ZIP 的压缩比异常高的成员不读，只报告；目录过大（成员数、目录字节数）在 `zipfile` 读它之前就拒绝。tar 用自己写的读取器，超过 1 MiB 的扩展头直接拒绝，不读进内存。
- **路径穿越**：成员名里有 `..`、绝对路径、盘符，或者链接指向包外的，在结果里列出来（解析时只看名字，从不解开）；#31 把包装到设备上时要拒绝这样的包。
- **XML（URDF、`package.xml`、Office 的关系部件）**：用 expat 读，**任何 DOCTYPE 都拒绝**，所以既没有外部实体，也没有实体膨胀；嵌套深度（64）和元素个数（20 万）有上限。
- **网页（HTML，issue #45）**：WeKnora 0.6.3 没有读 HTML 文件的解析器（只有 `.mhtml` 和按网址抓取），所以网页不原样送去。同一个子进程用标准库的 `html.parser` 把文字取出来（`material_page.py`）：`<script>`、`<style>`、`<template>`、`<svg>`、`<iframe>` 等的内容和注释丢掉，标题前加 `#`，列表项前加 `- `（按层级缩进），表格一行占一行、单元格之间用 ` | ` 隔开（单元格里有段落或换行也不拆行），`<pre>` 里的命令原样放在代码围栏里，指向别的网站的链接把网址写在文字后面。**网页引用的任何东西都不去取**（图片、样式、脚本、框架），也不运行脚本，所以打开网页时才由脚本生成的内容读不到：读不出文字的网页标“没解析出来”，不送 WeKnora，可以另存为 PDF 或文本再传。只有标题、正文没有字的网页也算读不出文字。网页最大读 32 MiB，取出的文字最多 1 MiB，超过的部分不解析并在结果里注明（重新解析、同一个文件沿用别的设备的结果时也注明）；读一个网页最多用 10 秒 CPU（标准库的解析器遇到某些残缺的标记很慢），超时只是这一份没解析出来；发给 WeKnora 的是这些文字（文件名换成 `.md`），不是网页原文，所以两边的 SHA-256 不同。
- **Office 文档的宏和外部引用**：送 WeKnora 之前先在同一个子进程里检查。有宏的（`vbaProject.bin`、启用宏的内容类型、旧格式里的 VBA 流）照常送去解析，只读文字，结果里注明“有宏，没有运行”。有外部引用的（关系部件里 `TargetMode="External"` 的图片、模板、OLE 对象等，以及 Excel 的外部链接和数据连接；旧格式 Word 里会去别处取内容的字段 `INCLUDEPICTURE`、`INCLUDETEXT`、`LINK`、`DDE`、`DDEAUTO`、`IMPORT`（只认字段代码的位置、不分大小写，正文里出现这些词不算），旧格式 Excel 里的 `WEBSERVICE`、`FILTERXML` 函数）**不送**，标“没解析出来”，原因里写明引用了哪里；普通超链接不算。关系部件超过 300 个的文档不检查完就不送（拒绝，而不是只查前一部分）。
- **超大或畸形的 PDF**：超过 `NEUXMIND_WEKNORA_MAX_BYTES`（默认 50 MB，即 WeKnora 自己的默认上限）的文档不送；送去的由 WeKnora 的解析容器处理，解析失败、超时（默认 30 分钟）都只影响这一份。
- **WeKnora 的答复**：不跟随重定向、不用代理、每个答复有字节上限和总时间上限（不只是每次读的超时，所以一点一点往外挤字节的服务也拖不住解析线程）、编号按固定格式检查后才拼进地址（不能是 `.`、`..`）；连不上或 5xx 时稍后重试（6 次，间隔从 30 秒到 15 分钟），密钥错误等明确的拒绝不重试。模型的回答里超出浮点范围的数字、过深的嵌套按没填处理。
- **任何一步出错都只影响这一份资料**：解析步骤里任何没有预料到的错误都变成这份资料的“没解析出来”（写明是程序问题），不会让任务卡住、反复调用模型。
- **zstd 压缩的 deb**：Python 读不了 zstd，由镜像里的 `dpkg-deb --ctrl-tarfile` 在同一个受限的子进程里把 `control` 部分解成 tar 输出（最多 32 MiB），再由这里的 tar 读取器读。这是唯一一处由 C 程序读上传内容的地方。
- **页面**：文件名、解析出的值、警告、模型的回答一律当作文字显示（`h()` 转义），测试里专门放了带标签的回答。

## 后台怎么跑

`cloud/neuxmind_cloud/material_parse.py`：

- 每个云平台进程有解析线程（`NEUXMIND_LIBRARY_PARSE_THREADS`，默认 2），每 5 秒找一次活（`NEUXMIND_LIBRARY_PARSE_INTERVAL_SECONDS`）。上传完成的资料在下一轮被找到，不需要别的触发。两个以上线程时，第一个只做 URDF 和安装包（几秒一份），其余只做文档（要等 WeKnora），所以 WeKnora 慢的时候规则解析照常进行。
- 一份资料一行记录（`library_material_parses`），一步一步走：`queued`（规则解析；或检查 Office 文档后送 WeKnora）→ `indexing`（等 WeKnora 解析完）→ `asking`（问模型）→ `done`。每一步只在拿到租约时运行，租约过期（进程死了）会由别的线程接着做；多个云平台进程共用一个数据库也不会把同一步跑两次。
- 资料被删除后，下一轮清理会删掉它在 WeKnora 里的副本（没有别的资料还在用它时）和这条记录。WeKnora 对同一个文件只留一份，所以删除的同时恰好有别的资料接上了这一份时：还在解析的那份重新送一次，已经解析完的保留结果。
- 清理每 60 秒一轮（`CLEANUP_INTERVAL`），所以删除后大约一分钟内，被删那份的记录和副本都还在。这期间再传同样内容的文件，云平台直接沿用被删那份的解析结果，不再送 WeKnora；被删那份没有可沿用的结果（例如解析出错）时才会送，这时 WeKnora 回 409（重复）并给出已有的那份，云平台接着用它。两种情况下清理时都因为还有资料在用这份副本而保留它，不算出错。
- 沿用的是机器解析出的结果（不含人改过的值），**缺项的也照样沿用**：一份文档第一次问的时候没问出来（例如 WeKnora 还没准备好，或模型的回答是空的），同一个文件后来给别的设备再传，也是同样的缺项，不会再问。在缺项的那份上点“重新解析”，会用它在 WeKnora 里的那份再问一次模型；其余沿用了它的各自再点一次。
- 人改结果时要带上读到的 `revision`；期间别人改过或重新解析过，就回答 409 `parse_changed`，不覆盖。

## 接口

云端（前缀 `/api/v1`，权限沿用资料库：读要 `library:read`，改、确认、重新解析要 `library:write` 和已验证的邮箱，operator 只能看）：

| 方法 路径 | 请求 → 响应 |
|---|---|
| GET `/library/materials/{id}/parse` | → `{material, kind, fields[], note, stage, status, status_detail, values, not_applicable[], missing[], warnings[], error, retrying, parser, revision, edited_by, edited_at, confirmed_by, confirmed_at, editable}` |
| PUT `/library/materials/{id}/parse` | `{revision, values, not_applicable?}` → 同上。改过的结果回到“待确认”（缺项时“没解析出来”）。400 `invalid_parse_values`（没有的项目、不能标不适用的项目）、409 `parse_changed`、409 `parse_running` |
| POST `/library/materials/{id}/parse/confirm` | 同上（可以同时带上修改）→ 状态 `usable`。409 `parse_incomplete`（带 `missing[]`） |
| POST `/library/materials/{id}/parse/retry` | → 状态 `parsing`，从头解析；已填写和已确认的内容被新结果替换 |

资料多了两个字段：`device_id`（桌面端机器人的编号）和 `device_name`，上传开始时给；列表可以按 `device_id` 过滤。

桌面端后台（`brainos/material_parse.py`，经 `MaterialLibrary._call` 加上云平台会话令牌后转发）：`library/parse`、`library/parse/save`、`library/parse/confirm`、`library/parse/retry`，参数是 `material_id` 加上面的字段。云端的答复只保留普通 JSON 值，深度、长度和条数有上限。

## 页面

`web/material_parse.js` 和 `web/material_parse.css`（在 `material_library.js` 和 `device_profile.js` 之后加载，包装它们的函数，不改它们的文件）：

- **设备档案的「资料」标签**（真机卡片上的“设备档案”）：这台设备的资料列表、状态（缺什么写在状态下面）；“上传资料”在标签里展开表单，本体类型和设备自动带上，厂家、型号、版本由人填；每份资料“查看解析结果”在原地展开。还没读到设备档案时，对话框下方有“查看这台设备的资料”。
- **解析结果**：每个项目一个输入框（列表每行一项；话题和动作每行“名称 类型”；关节是一张表，每个上限一个数字框，单位按关节类型显示 rad / m、rad/s / m/s、N·m / N）。必填项标星，缺的项标“缺”，必填项可以勾“不适用”。按钮：保存、确认可用、重新解析（要再点一次确认）。还在解析时显示进度，每 4 秒自动刷新这一份。
- **「资料库」页**：状态名换成 解析中 / 待确认 / 可用 / 没解析出来，缺什么写在状态下面；每行多一个“解析结果”，在对话框里打开同一个结果。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEUXMIND_WEKNORA_URL` | 空 | WeKnora 的地址（`http(s)://主机[:端口]`，到 `/api/v1` 之前）。和下面两项都配了才送文档 |
| `NEUXMIND_WEKNORA_API_KEY` | 空 | WeKnora 的 API Key（`X-API-Key`）。用空间 Key；平台 Key 还要配 `NEUXMIND_WEKNORA_TENANT_ID` |
| `NEUXMIND_WEKNORA_KB_ID` | 空 | 文档放进的知识库。这个知识库要在 WeKnora 里配好向量模型和问答模型 |
| `NEUXMIND_WEKNORA_MAX_BYTES` | `52428800` | 超过就不送（应与 WeKnora 的 `MAX_FILE_SIZE_MB` 一致） |
| `NEUXMIND_WEKNORA_PARSE_SECONDS` / `NEUXMIND_WEKNORA_ANSWER_SECONDS` | `1800` / `180` | 等 WeKnora 解析完一份文档 / 一个回答的最长时间 |
| `NEUXMIND_LIBRARY_PARSE_INTERVAL_SECONDS` / `NEUXMIND_LIBRARY_PARSE_THREADS` | `5` / `2` | 解析线程多久找一次活、几个线程；0 不启动线程 |
| `NEUXMIND_LIBRARY_PARSE_WALL_SECONDS` / `_CPU_SECONDS` / `_MEMORY_BYTES` / `_UNPACKED_BYTES` | `120` / `60` / `1073741824` / `8589934592` | 规则解析子进程的墙钟时间、CPU 时间、地址空间（Linux）、解出字节的上限 |

WeKnora 的部署按它自己的文档（Docker Compose 标准版是界面、主服务、文档解析、ParadeDB、Redis 五个容器）。它应当只在内网可达；云平台到它的连接不加任何代理。

## 测试

```bash
cd cloud/tests && python3 -m unittest test_material_extract test_material_parse    # 云端：规则解析、恶意输入、WeKnora 替身、编辑确认、权限、租约
NEUXMIND_TEST_DATABASE_URL=postgresql+psycopg://… python3 -m unittest discover -s cloud/tests   # 同一套在 PostgreSQL 上
python3 -m unittest tests.test_material_parse_desktop                               # 桌面端转发（需要云端依赖）
node tests/material_parse_logic.cjs                                                 # 页面逻辑
python3 tests/material_parse_browser_fixture.py CONFIG.json &                       # 浏览器验收的夹具（SSH 替身、真云端、WeKnora 替身）
node tests/material_parse_browser.cjs CONFIG.json                                   # BRAIN_BROWSER=webkit 换成 WebKit
cd cloud/tests && python3 -m unittest test_material_html                            # 网页（issue #45）：类型判定、取文字、送 WeKnora
python3 tests/material_html_browser_fixture.py CONFIG.json &                        # 网页的浏览器验收夹具（真云端、WeKnora 替身）
node tests/material_html_browser.cjs CONFIG.json                                    # 在「资料库」页上传网页、解析、看结果
```

测试里的文件都是测试自己生成的；WeKnora 用替身（`cloud/tests/fake_weknora.py`，只模仿它的五个接口）。2026-11-23 在本机 Docker 里的 WeKnora 0.6.3 上跑通过：对话模型 `qwen3.6-plus`、向量模型 `Qwen3-Embedding-4B`（都经 TokenFans），一份 Markdown 的驱动说明经云平台解析，8 项全部读出、状态「待确认」。

对真的 WeKnora 要注意：

- WeKnora 说 `finalizing`（还在做摘要）时文档还检索不到，这时提问得到的是「对话中并未附带任何文档」。云平台只在 `completed` 之后提问（9a34c46；替身里的 `polls_finalizing` 模拟这一段）。
- 会思考的模型（如千问 3.x）要让 WeKnora 能关掉思考：千问的服务商设成 `aliyun`，WeKnora 才会传 `enable_thinking: false`。按 `generic` 接时，第一个问题想了 6000 多字，正文为空。
- 云平台删除资料后约一分钟才删 WeKnora 里的副本；这段时间里再传同样内容的文件，WeKnora 回 409（重复）。
- 向量维度没有对应的索引时（`Qwen3-Embedding-4B` 是 2560 维）WeKnora 逐条比对，资料少时没有影响。

## 没有做的

- 网页（issue #45）还没有在真的 WeKnora 上跑过，只用替身验证了发过去的文字；`.mhtml`、`.xhtml` 和网页里引用的图片不处理。
- 在用户正式使用的 WeKnora 上验证：现在只在本机 Docker 里的 0.6.3 上跑过（见上）。接口按 WeKnora 的公开文档和客户端代码写成，0.6.3 上这几个接口都在。
- 修复助手按需检索文档全文（#30）：现在只能拿到确认过的结构化结果；文档在 WeKnora 里的副本编号只在云端。
- 设备档案和本体档案合并（另开 issue）。
- 资料不能改挂到另一台设备；没挂设备的资料（「资料库」页上传的）只在「资料库」页出现。
- 旧格式 Office 文档（`.doc`、`.xls`）按字段代码和函数名查外部引用，不认 Excel 97–2003 的外部工作簿链接（SUPBOOK 记录）；这类文件由 WeKnora 的解析容器处理。
