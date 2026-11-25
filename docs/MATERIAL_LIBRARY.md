# 资料库（issue #33，R1-12）

机器人的手册、参数表、URDF 和驱动安装包存在云平台，每份资料标明本体类型、厂家、型号和版本。
这份文档说明三段之间怎么配合；云端的接口、接收哪些类型、安全做法和配置在 `cloud/README.md` 的“资料库”一节。

## 三段

```
「资料库」页（web/material_library.js）
   │  JSON：POST /api/library/…          二进制：PUT /api/library/chunk?upload=<id>&index=<n>
   ▼
桌面端后台（brainos/material_library.py）     只在本机，加上云平台会话令牌后转发
   │  /api/v1/library/…（见 cloud/README.md）
   ▼
云平台（cloud/neuxmind_cloud/library*.py）    决定收不收、存到磁盘卷
```

- 页面不直接访问云平台：云平台的会话令牌只在工作空间的凭据文件里（`brainos/cloud_account.py`），页面拿不到。
- 登录应用时会用同一邮箱和密码同时登录云平台，不用再单独登录（issue #36，见 [CLOUD_AUTO_LOGIN.md](CLOUD_AUTO_LOGIN.md)）。
- 收不收、多大算超限、谁能上传，都由云端决定。页面只是把云端一定会拒绝的文件提前挡下（后缀不在清单里、超过大小上限、空文件），省得白传。
- 文件不经过本机磁盘：页面把文件切成云端规定大小的分片，桌面端后台收到一片转发一片，并附上这一片的 SHA-256。

## 一次上传

1. 页面按 1 MiB 一段读文件，算出整个文件的 SHA-256（`MaterialSha256`，纯 JS，可以一段一段喂；WebCrypto 只能对整块数据算，2 GB 的安装包放不进内存）。
2. `library/upload/start`：文件名、大小、SHA-256 和标注。云端返回这次上传的编号、分片大小和**已经收到的分片**。
3. 页面只发缺的分片（`materialMissing`），每片最多试 3 次。
4. `library/upload/complete`：云端核对长度、SHA-256 和内容类型后保存，资料进入“解析中”。云端同时只核对少数几个文件，忙的时候回答 `library_busy`，页面自己等一会儿再问；桌面端后台为这一步留了 15 分钟（大文件要读完才知道结果）。

中断后续传：连接断了、页面关了、应用重启了都一样——再选同一个文件、填同样的标注，第 2 步会拿回同一次上传和云端已有的分片，接着发剩下的。页面上的“继续”按钮做的就是从第 2 步重来（文件的 SHA-256 已经算过，不重算）。云端把没有新分片的上传保留 24 小时。

页面上一次只传一个文件，其余排队。离开「资料库」页不影响正在进行的上传；关闭应用会中断它（之后可以续传）。

能不能“继续”由失败的性质决定（`materialResumable`）：连不上、登录失效、云端出错（5xx）或忙，可以继续；云端明确拒绝（类型不支持、超限、校验值不符、重复等）是这份文件的最终结果，行上写明原因。被拒绝或中断的行从列表移除时，页面会让云端释放这次上传占着的空间。

## 桌面端后台的接口

| 路径（POST，JSON） | 作用 |
|---|---|
| `/api/library/overview` | 账户摘要、云端支持的类型和上限、第一页资料（未登录云平台时 `formats: null`） |
| `/api/library/list` | 列表；只转发认识的过滤项（`body_type`、`vendor`、`model`、`version`、`kind`、`format`、`status`、`q`、`limit`、`offset`） |
| `/api/library/upload/start`、`/status`、`/complete`、`/cancel` | 对应云端的开始、查询、完成、取消 |
| `/api/library/delete` | 删除一份资料 |

`PUT /api/library/chunk?upload=<id>&index=<n>`（`application/octet-stream`）是桌面端接口里唯一不是 JSON 的请求，由 `Handler.do_PUT` 交给 `material_library.handle_chunk`。它和其他接口一样要求请求来自页面本身（`Handler.trusted`）并且已登录工作空间。

页面给的上传编号、资料编号在拼进云端地址之前按固定格式检查；云端的答复只挑认识的字段、只要普通值再交给页面。云端的拒绝原样带着 `code` 回到页面（HTTP 400，`device_status` 是云端的状态码）。

## 页面

- 导航最后一项“资料库”（`#library`）。未登录云平台时只显示去登录的提示。
- “上传资料”：选文件（可多选，同一组标注）、本体类型、厂家、型号、版本、说明。有不支持的文件时整批不发，并写明支持哪些类型。
- “本次上传”：每个文件一行，显示计算校验值和上传的进度；中断的可以“继续”或“取消”，被拒绝的写明原因；云端正在核对的那一步不能取消。
- “已有资料”：文件名、类型和大小、本体类型、厂家 / 型号、版本、状态（解析中）、SHA-256 的前 12 位、上传时间；可以按本体类型筛选、按文件名或厂家型号搜索、删除。
- 页面自己的文件是 `web/material_library.js` 和 `web/material_library.css`；它往导航表里加一项、监听自己的 `data-library-*` 属性，没有改 `web/app.js`。

## 给后续 issue 的接口

- 给 #34 和 #31 都适用的一条：云端只是认出文件是不是声称的类型（压缩包只看开头），没有打开它。解析和安装时要把内容当作不可信的输入。
- #34（解析）：已经做了，见 [MATERIAL_PARSING.md](MATERIAL_PARSING.md)。状态多了 `usable`（可用，人确认后）；资料多了所属设备 `device_id` / `device_name`，列表可以按它过滤。`format` 是 `library_types.FORMATS` 里的标识（`docx`、`xlsx`、`doc`、`xls`、`pdf`、`markdown`、`text`、`yaml`、`json`、`xml`、`urdf`、`zip`、`tar.gz`、`tar.xz`、`tar.bz2`、`tar`、`deb`）。
- #45（网页）：`.html`、`.htm` 也是文档，`format` 是 `html`。页面没有为它改代码：能选哪些后缀、提示里写什么都来自云端的 `library/formats`。页面只显示它的文件名，从不显示它的内容；云端只把它当附件下载。
- #31（把驱动安装包送到设备）：`GET /library/materials?kind=package&body_type=…&vendor=…&model=…` 找包，`…/content` 取文件（支持 `Range`，响应头 `X-Checksum-SHA256` 和详情里的 `sha256` 是整个文件的摘要），API Key 需要 `library:read`。
- #32（本体类型清单）：资料上的 `body_type` 存的是类型的标识，云端现在只校验格式；页面的下拉来自 `state.families`。清单定了以后在 `library._upload_fields` 里对照它。

## 测试

```bash
python3 -m unittest discover -s cloud/tests -p "test_library_*.py"     # 云端：验收、续传、权限、限制、类型判定、异常路径
python3 -m unittest tests.test_material_library                        # 桌面端转发（需要云端依赖，否则跳过）
node tests/material_library_logic.cjs                                  # 页面逻辑：SHA-256、预检、缺片、转义、上传状态机
python3 tests/material_library_browser_fixture.py CONFIG.json &        # 浏览器验收的夹具
node tests/material_library_browser.cjs CONFIG.json                    # BRAIN_BROWSER=webkit 换成 WebKit
```

测试里的文件都是测试自己生成的（只有一行字的 Word / Excel 容器、几十字节的 PDF、随机字节的压缩包），没有真实的厂商资料。

## 没有做的

- 解析是 #34（[MATERIAL_PARSING.md](MATERIAL_PARSING.md)）：上传完成后自动解析，「资料库」页每行有“解析结果”。按设备上传、查看在设备档案的「资料」标签里。
- 在页面上下载资料、修改标注。
- 对象存储：现在只有磁盘卷一种存储。
- 真机：这个 issue 不涉及设备端。
