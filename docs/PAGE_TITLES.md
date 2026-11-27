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

- 标题下面的那句说明和「这个页面怎么用」还在。issue 只说了标题的写法；要像「工作台」那样连说明也去掉，是 `web/app.js` 里 `heading()` 的一处改动。
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
