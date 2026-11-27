# 页面标题统一（issue #57）

侧栏里每个页面的标题都按「工作台」的写法：上面一行小号的英文名，下面是中文名，中文名就是侧栏里的那个名字。

## 改之前

只有「工作台」是这个写法。其余页面的标题各不相同：

- 大多数页面的大标题是一句话（「检查机器人能否完成任务」「把验证过的任务交给机器人」），上面一行是所在步骤的中文（「验证效果」「交付使用」），和侧栏里的名字对不上。
- 「我的机器人」上面一行是机器人数量（「1 台真机 · 2 台仿真」），没有机器人时没有这一行。
- 「云平台账户」「资料库」「技能发布」「技能加载」已经是英文在上、名字在下，但英文是各页自己起的。

## 改之后

| 侧栏 | 上面一行 | 标题 |
|---|---|---|
| 工作台 | WORKBENCH | 工作台 |
| 我的机器人 | MY ROBOTS | 我的机器人 |
| 机器人应用 | ROBOT APPLICATIONS | 机器人应用 |
| 仿真试运行 | SIMULATION TRIALS | 仿真试运行 |
| 验证与测试 | VERIFICATION & TESTING | 验证与测试 |
| 部署与运行 | DEPLOYMENT & OPERATIONS | 部署与运行 |
| 模型库 | MODEL LIBRARY | 模型库 |
| 训练与改进 | TRAINING & IMPROVEMENT | 训练与改进 |
| 训练与测试数据 | TRAINING & TEST DATA | 训练与测试数据 |
| 平台模型与算力 | PLATFORM MODELS & COMPUTE | 平台模型与算力 |
| 用量与账单 | USAGE & BILLING | 用量与账单 |
| 云平台账户 | CLOUD ACCOUNT | 云平台账户 |
| 资料库 | MATERIAL LIBRARY | 资料库 |
| 技能发布 | SKILL PUBLISHING | 技能发布 |
| 技能加载 | SKILL LOADING | 技能加载 |

暂时隐藏的四个页面（issue #49）也在表里，重新显示时是同样的写法：工程助手 ENGINEERING ASSISTANT、协作流程 ASSISTANT WORKFLOWS、助手工具 ASSISTANT TOOLS、自有服务 OWN SERVICES。

没有动的：

- 标题下面的那句说明和「这个页面怎么用」当时还在；issue #59 把它们从所有页面去掉了，见下面一节。
- 「我的机器人」的机器人数量从标题上面移到了标题下面（`.rp-count`），没有机器人时不显示。
- 某一个任务的页面（`#task`）标题仍然只有任务名（issue #52），它不是侧栏里的页面。
- 对话框、登录页的标题。

## 做法

`web/page_titles.js` 里一张表 `PAGE_TITLES`（页面 → 英文名、中文名）和两个函数：`pageTitle(route)` 返回 `{en, zh}`，`pageTitleMarkup(route)` 返回标题的两行标记。

- 普通页面都用 `web/app.js` 的 `heading(en, title, sub, buttons)` 画页头。它现在先问 `pageTitleMarkup(current)`：页面在表里就用表里的两行，页面自己传进来的英文和标题、`web/journey.js` 里的步骤名都不再显示；不在表里的页面照原样画。
- 「我的机器人」有自己的页头（`web/robots_page.js` 的 `rpHead`），用 `pageTitleMarkup('robots')`。
- 「工作台」（`web/task_workbench.js`）是这个写法的出处，自己写这两行。

以后新增侧栏页面：在 `PAGE_TITLES` 里加一行，中文名和侧栏里的一致，页头用 `heading()`。改侧栏里的名字时表里也要改，检查会指出两处不一致。

## 检查

- `tests/page_titles_logic.cjs`（由 `tests/test_page_titles.py` 运行）：侧栏每一项在表里有英文名、中文名和侧栏一致、英文名不重复；`heading()` 对每个页面画出这两行、原来的标题不再出现、说明和按钮还在；「我的机器人」的页头；不在表里的页面不受影响。
- `tests/page_titles_browser.cjs`（夹具 `tests/ux01_browser_fixture.py`）：从侧栏逐个打开 15 个页面，标题等于侧栏名字，上面一行是英文，字号、字重、字距、颜色、位置和「工作台」逐项相同；直接按地址打开也一样；任务页面仍只有任务名。
- 跟着改的检查：`tests/robots_page_browser.cjs`（数量在 `.rp-count`）、`tests/hidden_pages_browser.cjs`（「工程助手」页面按标题和说明认）。

## 标题下面不再有说明（issue #59）

issue 的截图是「机器人应用」页面，圈出了两处：标题下面的一句说明，和它下面可以展开的「这个页面怎么用」（需要准备 / 完成后得到）。要求是所有页面都去掉。

改之后，每个页面的页头只有三样：英文名、中文名、右边的按钮。和「工作台」一样。

- `web/app.js` 的 `heading(en, title, sub, buttons)` 不再画 `sub`，也不再在页头后面接「这个页面怎么用」。参数 `sub` 还在（各页面照旧传），只是不显示；不在表里的页面同样不显示。
- `web/journey.js` 的 `journeyHelp()` 删掉了。`pagePurposes` 里每页的后三项（说明、需要准备、完成后得到）现在没有地方用，文字留着没删。
- `web/ux02.js` 的 `uxInsertAfterHead()` 原来把仿真提示接在「这个页面怎么用」后面，现在直接接在页头后面（「仿真试运行」「验证与测试」「部署与运行」三页）。
- 暂时隐藏的四个页面（issue #49）重新显示时也没有这两处。

没有动的：

- 「我的机器人」标题下面的机器人数量（「1 台真机 · 2 台仿真」）。它是数据，不是说明。
- 页面里各小节标题右边的短语（「版本固定 · 能力匹配 · 失败停止」）和空列表里的引导文字。截图没有圈它们。
- 「模型库」页面底部的「工程助手使用的大模型」、对话框里的折叠项。它们用同一个样式（`.page-purpose`），但不是「这个页面怎么用」。
- 被去掉的说明里有两句是别处没有的提示：「技能发布」的「发布不会改动任何机器人；要把技能装到机器人上，去“技能加载”页」，和「技能加载」的「发布和删除版本在“技能发布”页」。按 issue 一并去掉了。

检查：`tests/page_titles_logic.cjs` 对每个页面核对页头只有两行标题和按钮；`tests/journey_browser.cjs` 逐页确认标题下没有说明、页面里没有「这个页面怎么用」；`tests/hidden_pages_browser.cjs` 原来靠「工程助手」的那句说明认页面，改成按标题认。
