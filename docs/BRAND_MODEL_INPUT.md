# 品牌和型号的输入（issue #61）

所有填写品牌、型号的地方现在是同一种控件：**一个框，可以下拉选，也可以直接输入**。原来的做法是一个下拉列表加「其他品牌」「其他型号」，选了「其他」再出现第二个输入框；issue 截图里四个框同时出现，是因为隐藏第二个输入框的样式被 `.field` 的 `display:flex` 盖掉了。现在没有第二个输入框，也没有「其他」这一项。

## 页面上是什么样

- 品牌一个框，型号一个框，并排（窄屏上下排）。
- 点框或框右边的箭头，展开完整列表；框里已有的值在列表里打勾。
- 输入时列表按输入内容收窄，品牌的其他写法也能找到（输入 `unitree` 会出现「宇树科技」）。没有匹配时列表收起，输入的内容原样保存。
- 账号里已有机器人的品牌、型号排在列表最前面，右边标「已有」（资料库的表单里是资料库已有的品牌型号）。框里的值是某个品牌的另一种写法时（例如 `unitree`），列表里那个品牌打勾。
- 型号列表跟着品牌框里的内容：清单里的品牌（任何写法）列出它的型号；清单外的品牌只有账号自己机器人的型号；都没有时列表写「没有可选的，直接输入」。
- 换了品牌：型号如果是旧品牌清单里的型号、新品牌又没有它，就清空；自己输入的型号保留，把品牌清空也不动型号。
- 键盘：↓ ↑ 展开并移动，Enter 选中（不会提交表单），Esc 只收起列表（不会关掉对话框），Tab 离开。输入法组字时的按键不处理。
- 列表画在对话框之上，不会被对话框裁掉；下方空间不够时向上展开。

## 用到的地方

| 位置 | 之前 | 现在 |
| --- | --- | --- |
| 添加机器人（服务地址 / SSH）、修改品牌型号、登记技能、云端技能「发布」「发布示例技能」「指定品牌型号」 | 两个下拉 + 两个输入框 | 两个框 |
| 资料库「上传资料」「上传机器人模型」「改品牌型号」 | 一个「品牌型号」下拉（含“其他品牌型号（手动填写）”）+ 两个输入框 | 同样的两个框；列表最前面是账号已有的品牌型号 |

资料库上传时的「本体类型」：框里的品牌型号是账号已有的，就用它自己的本体类型，不问；是新的才出现这一项。

没有改的：「添加仿真机器人」的品牌、型号仍是普通下拉。那里只能选内置的可仿真型号，手工输入一个没有模型的型号没有意义。资料库列表上方的「按品牌型号筛选」也仍是下拉（筛选只能选已有的）。

发送给后端的字段没有变，仍是 `vendor` 和 `model`；后端规则（`brainos/robot_model.py`、`brainos/model_library.py`）没有改。

## 代码

| 文件 | 内容 |
| --- | --- |
| `web/combo.js`、`web/combo.css` | 通用控件，不知道机器人。`comboInput({name, source, value, label, placeholder, required, attrs})` 生成标记；选项来自页面上的一个函数，名字写在输入框的 `data-combo-source` 上，返回 `[{value, keys: 其他写法, note}]`。整页共用一个列表元素 `#combo-list`（放在输入框所在的表单里，对话框重画时跟着消失）。选中一项会在输入框上触发 `input` 和 `change` |
| `web/robot_catalog.js` | `rcInputs(product, required, sources)` 画两个框；`rcVendorOptions(robots)`、`rcModelOptions(vendor, robots)` 给出选项；`rcKeepsModel` 决定换品牌时型号留不留。原来的 `RC_OTHER`、`rcStart`、`rcOwnBrands`、`rcChosenBrand` 已删除 |
| `web/model_library.js` | `mlProductFields` 改用 `rcInputs`（`ML_SOURCES`：品牌选项 `mlVendorChoices`，「已有」的范围 `mlOwned`，把资料库已有的品牌型号排在前面，换品牌时型号的去留也按它算）；`mlLabels` 按输入的品牌型号找账号已有的那一个；`ML_OTHER`、`mlProductSelect` 已删除 |

别的页面要用这个控件：写一个返回选项的顶层 `function`，调用 `comboInput({source: '函数名', ...})`。品牌型号直接用 `rmInputs(product, required)`（`web/robot_model.js` 里的名字，运行时就是 `rcInputs`）。

## 测试

- `node tests/combo_logic.cjs`：筛选、方向键、标记、选项来源。
- `node tests/robot_catalog_logic.cjs`：两个框的标记（没有下拉、没有第二个输入框）、品牌和型号的选项及顺序、换品牌时型号的去留。
- `node tests/model_library_logic.cjs`：资料库表单的标记、`mlLabels`、本体类型什么时候问。
- `tests/brand_combo_browser.cjs`（fixture：`tests/robot_model_browser_fixture.py`）：鼠标选、输入收窄、键盘选、Esc、清单外的品牌型号、自己的品牌排第一、列表不被对话框裁掉、375 / 768 / 1440 三个宽度。Chromium 和 WebKit 都要跑。
- 浏览器脚本里填品牌型号：`require('./robot_model_helper.cjs')` 的 `fillBrandAndModel(page)` / `pickBrandAndModel(scope, {vendor, model})`（输入并离开输入框），要从列表里点选用 `chooseFromList(page, box, text)`。直接 `fill()` 之后列表是开着的，会盖住下面的控件：先 `blur()`。
