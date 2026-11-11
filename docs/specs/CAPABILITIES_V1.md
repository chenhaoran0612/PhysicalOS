# NeuxMind 能力接口标准 v1

> 权威源码：`spec/neuxmind_spec/capabilities.py`（本表由源码生成，冲突时以源码为准）。
> 状态：v1.0 草案，第 01 轮进化引入。

## 为什么需要它

跨本体的前提是：技能和模型不认识"某品牌某型号"，只认识"能力"。一台四足狗和一台轮式底盘都提供 `base.navigate_to`，同一个巡逻技能就能在两者上运行；机械臂不提供它，安装时就被拒绝，而不是跑起来才出错。

## 规则

1. 能力 ID 形如 `领域.名称`，含义永不改变；破坏性修改升主版本 `major`。
2. 所有物理量用 SI 单位：米、秒、弧度；位姿 `[x,y,z,qx,qy,qz,qw]`，坐标系由 `frame` 指定。
3. 四种类型：**状态**（读取快照）、**数据流**（传感器最新帧或引用）、**命令**（短时设定值，立即返回）、**动作**（长时目标，有进度、结果、可取消）。
4. 三种安全级：**无**（不能让机器人动）、**运动**（需现场授权，并经安全监督器限幅）、**系统**（改变端侧软件）。
5. `safety.stop` 与 `system.health` 是每个本体的必备能力。
6. 厂商私有能力用 `x.<厂商>.<名称>` 命名空间，不进入标准，技能可以依赖但会失去可移植性。
7. 旧版自由字符串能力（`cmd_vel`、`camera`…）通过别名表自动映射。

## 能力表

`?` 表示可选字段。

| ID | 名称 | 类型 | 安全级 | 输入 | 输出 | 本体参数 |
|---|---|---|---|---|---|---|
| `safety.stop` **必备** | 停止 | 命令 | 无 | `reason`: str? | `stopped`: bool | `hardware_estop`: bool |
| `system.health` **必备** | 系统健康 | 状态 | 无 | — | `ok`: bool<br>`mode`: str<br>`errors`: object? | — |
| `sensor.battery` | 电池 | 状态 | 无 | — | `percent`: float (%)<br>`charging`: bool?<br>`voltage`: float (V)? | — |
| `sensor.imu` | IMU | 数据流 | 无 | — | `orientation`: vector<br>`angular_velocity`: vector (rad/s)<br>`linear_acceleration`: vector (m/s^2) | `rate_hz`: float (Hz)<br>`frame`: str |
| `sensor.camera.rgb` | 彩色相机 | 数据流 | 无 | — | `image`: blob_ref<br>`width`: int (px)<br>`height`: int (px)<br>`stamp`: float (s) | `name`: str<br>`frame`: str<br>`rate_hz`: float (Hz)<br>`intrinsics`: vector? |
| `sensor.camera.depth` | 深度相机 | 数据流 | 无 | — | `depth`: blob_ref<br>`width`: int (px)<br>`height`: int (px)<br>`stamp`: float (s) | `name`: str<br>`frame`: str<br>`range_max`: float (m) |
| `sensor.lidar` | 激光雷达 | 数据流 | 无 | — | `points`: blob_ref<br>`stamp`: float (s) | `frame`: str<br>`range_max`: float (m)<br>`dimensions`: enum |
| `base.odometry` | 里程计 | 状态 | 无 | — | `pose`: pose<br>`velocity`: vector | `frame`: str |
| `localization.pose` | 定位 | 状态 | 无 | — | `pose`: pose<br>`covariance`: floats?<br>`map_id`: str? | — |
| `joint.state` | 关节状态 | 状态 | 无 | — | `names`: object<br>`position`: floats (rad)<br>`velocity`: floats (rad/s)?<br>`effort`: floats (N·m)? | `dof`: int |
| `base.velocity` | 底盘速度 | 命令 | 运动 | `vx`: float (m/s)<br>`vy`: float (m/s)?<br>`wz`: float (rad/s)<br>`timeout_s`: float (s)? | `accepted`: bool | `max_vx`: float (m/s)<br>`max_vy`: float (m/s)<br>`max_wz`: float (rad/s)<br>`holonomic`: bool |
| `base.navigate_to` | 导航到目标 | 动作 | 运动 | `goal`: pose<br>`frame`: str?<br>`tolerance`: float (m)? | `reached`: bool<br>`final_pose`: pose? | `planner`: str? |
| `legged.posture` | 足式姿态 | 命令 | 运动 | `posture`: enum | `accepted`: bool | `supported`: object |
| `arm.joint_position` | 机械臂关节位置 | 动作 | 运动 | `position`: floats (rad)<br>`max_velocity`: float (rad/s)? | `reached`: bool<br>`position`: floats (rad) | `dof`: int<br>`lower`: floats (rad)<br>`upper`: floats (rad)<br>`max_velocity`: float (rad/s)<br>`joint_names`: strs?（v1.1） |
| `arm.ee_pose` | 末端位姿 | 动作 | 运动 | `pose`: pose<br>`frame`: str?<br>`max_velocity`: float (m/s)? | `reached`: bool<br>`pose`: pose | `frame`: str<br>`reach`: float (m)<br>`payload`: float (kg) |
| `gripper.position` | 夹爪 | 动作 | 运动 | `opening`: float (ratio)<br>`max_force`: float (N)? | `opening`: float (ratio)<br>`grasped`: bool? | `max_width`: float (m)<br>`max_force`: float (N) |
| `policy.action_chunk` | 策略动作块 | 动作 | 运动 | `actions`: object<br>`dt`: float (s) | `executed`: int | `action_space`: str<br>`dim`: int<br>`rate_hz`: float (Hz) |
| `model.install` | 端侧模型安装 | 动作 | 系统 | `package_ref`: str<br>`sha256`: str<br>`runtime`: str | `installed`: bool<br>`model_id`: str | `runtimes`: object<br>`accelerator`: str? |
| `audio.speak` | 语音播报 | 命令 | 无 | `text`: str<br>`language`: str? | `accepted`: bool | — |

## 三个核心对象

**Manifest v2**（本体自我声明）：`instance_id`、`family`、`vendor/model`、`simulated`、`capabilities[{id, major, params}]`、`model_runtimes`、`safety`。参数如 `base.velocity.max_vx`、`arm.joint_position.dof/lower/upper` 是安全监督与兼容性判定的依据。

**技能包 v1**：`id`、语义化 `version`、`entry`、`requires`、`optional`、`constraints`（如 `{"base.velocity": {"max_vx": {"min": 0.5}}}`）、`parameters`、`models`、`timeout_s`。

**兼容性判定** `check_compatibility(skill, manifest)` → `{compatible, missing, violations, optional_missing}`。运行时安装、云端模型过滤、SDK 检查、桌面展示都调用同一函数，保证各处结论一致。

## 演进计划

- v1.1：力/力矩传感器、移动操作（`mobile_manipulation.*`）、灵巧手、多相机标定与 TF 树查询。
- v1.x：观测/动作空间的标准化描述，对齐 LeRobot / Open X-Embodiment 数据格式，使 VLA 模型可按动作空间自动适配。
- 与 ROS 2 接口的双向映射表（见 `runtime/` ROS2 桥接）。

## v1.1 新增（2026-11-09，第 04 轮）

| ID | 名称 | 类型 | 安全级 | 输出 |
|---|---|---|---|---|
| `gripper.state` | 夹爪状态 | 状态 | 无 | `opening`: float (ratio)<br>`grasped`: bool?<br>`force`: float (N)? |
| `arm.ee_state` | 末端位姿状态 | 状态 | 无 | `pose`: pose<br>`frame`: str? |

用途：一致性测试套件据此自动验证夹爪与末端位姿相关动作的停止与最小动作；未声明时相应 motion 用例记为 `blocked`。

## 一致性测试（第 04 轮）

每项标准能力在 `static / dry / motion` 三级的测试用例由 `runtime/neuxmind_runtime/conformance.py` 定义，清单见 `runtime/README.md` §3.1。报告 `conformance.report.v1` 不等于现场验收证据。

## v1.1 补充：`arm.joint_position.params.joint_names`（第 04 轮复审 R4-01）

- 可选，字符串列表，长度等于 `dof`，互不相同，按 `position` 分量的顺序排列，必须与 `joint.state.names` 使用同一套名称。`validate_manifest_v2` 校验。
- **未声明时，任何组件不得假定 `joint.state` 的顺序就是指令顺序。** 一致性套件对此类本体的机械臂和动作块用例一律 blocked。
- 声明本身无法被软件验证；声明错误时，套件与守卫会按错误声明执行。
- v2 计划改为必填。
- （fbd9f37）未声明 `joint_names` 时，运行时**拒绝** joint_position 动作块；`joint.state` 关节名重复也拒绝。已有动作块但未声明的旧适配器需补声明。
