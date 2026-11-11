# 服务声明的训练、仿真与测试方案

0.6.0 在 platform.service.v1 清单中增加可选 job_recipes。未提供此字段的服务继续使用原有 config 接口，不会自动获得猜测生成的训练或仿真参数。服务方需要实际支持其声明的方案；安装包不内置真实训练器或仿真执行器。

## 服务清单示例

下面是协议示例，不是随产品上架的真实训练方案：

```json
{
  "schema": "platform.service.v1",
  "kind": "training",
  "version": "1.0",
  "job_recipes": [{
    "schema": "platform.job-recipe.v1",
    "id": "task-finetune",
    "version": "1.0",
    "name": "使用任务数据微调",
    "description": "服务方说明支持的模型、数据格式与适用范围。",
    "outputs": ["模型检查点", "评测指标"],
    "config": {"recipe": "task-finetune-v1"},
    "fields": [
      {"key": "objective", "label": "希望达到什么效果", "type": "text", "required": true, "prefill": "project_objective"},
      {"key": "dataset", "label": "训练数据", "type": "asset", "asset_kind": "datasets", "required": true},
      {"key": "model", "label": "基础模型", "type": "asset", "asset_kind": "models", "required": true},
      {"key": "epochs", "label": "训练轮数", "type": "integer", "min": 1, "max": 100, "default": 3, "required": true, "advanced": true}
    ]
  }]
}
```

支持 text、integer、number、choice、boolean、asset。choice 使用 options=[{value,label}]；数值可限制 min/max，文本可限制 max_length（最多 8000）。高级必填字段必须有有效默认值，避免隐藏必填项阻塞用户。资产不能设置默认 ID。字段键必须是小写字母开头的字母/数字/下划线，不能重复、使用保留字或覆盖 config 中的固定参数。每服务最多 20 个配方，每配方最多 20 个字段，固定配置最多 64 KB。

asset_kind 允许 datasets、models、scenes、applications、robots，选择范围限定当前项目。配置中写入所选记录的白名单快照，而不是只发送本机无法解析的资产 ID：

| 类型 | 可发送字段 |
| --- | --- |
| datasets | id、name、uri、version、format、source |
| models | id、name、model_ref、version、digest、package_ref、package_digest、runtime |
| scenes | id、name、scene_ref、version、engine、digest |
| applications | id、name、version、digest、robot_id、steps |
| robots | id、name、family、manifest_digest、capabilities |

仅包含存在的字段。服务令牌、模型 API Key 和机器人 SSH 凭据不进入资产快照。原始数据不自动上传；执行服务需要能够读取相应资产地址，并自行核验内容、格式、许可证和兼容性。登记版本不等于远端 URI 内容不可变。方案清单是公开信息，禁止在 config 或说明中放入密钥。

## 用户交互与 API

用户选择服务和方案 → 选择资产、填写参数和时间/用量上限 → 查看确认页 → 确认开始 → 查看状态、结果、日志与费用。平台与自有服务使用同一条路径。缺少资产时提供准备入口；不支持配方的服务提示选择其他服务或进入高级配置。

POST /api/jobs/preview 接收 project_id、name、recipe_id、values、max_minutes，以及 service_id 或 resource_id 二选一。平台资源另需 max_units。此接口只读取当前记录，校验字段、类型、数值和项目归属，返回生成的 config、输入摘要、配方快照、预算和 preview_digest；不会创建任务、预留额度或调用执行端。

确认时向原 /api/jobs 或 /api/managed/jobs 发送相同参数和 preview_digest。服务端重新计算，资产、参数、配方、价格或服务地址变化时拒绝创建，要求重新查看确认页；校验失败发生在平台额度预留之前。通过后保存任务和配方快照，前端再用既有 /api/jobs/submit 提交同一任务 ID。

配方任务执行前重新读取执行端清单，核对服务类型、配方摘要与已确认的目标地址。变化时任务保持 draft，不发送作业；用户可取消草稿释放预留后重新配置。请求已发送但结果不明时保持原有 unknown 语义，不自动重试；创建请求结果不明时前端要求检查任务记录，禁止直接重复确认。

服务端作业仍为 platform.job.v1，config 包含固定配置与表单值，另附 recipe 快照与摘要。执行端应核验 recipe.id/version/digest，实施 max_minutes、max_units 硬限额，并按原协议返回 job_id、状态、产物与可信计量证据。预期产物只是方案声明，只有执行端返回的 outputs 才作为结果显示；任务成功不自动注册模型、获得实机资格或部署机器人。

## 兼容性与边界

原始 JSON 配置模式继续保留，没有配方的旧服务和旧任务不触发新的配方检查。当前没有通用 JSON Schema 编辑器、跨字段条件、嵌套字段映射、文件上传或模型兼容性推断。对于这些需求，服务方可提供适当的明确方案，复杂设置仍由高级入口处理。服务升级后旧草稿不会静默改用新配方；已经发生的外部操作不能因界面关闭而撤销。

## 0.6.1：训练结果登记为模型资产

训练服务通过既有 outputs.checkpoint 返回非空字符串地址（最长 500 字符），outputs.metrics 返回指标；本版不猜测其他产物布局。只有 kind=training 且 status=succeeded 的任务允许登记，仿真、失败任务与对象形式检查点不符合此接口。旧服务不必增加 job_recipes；没有配方的任务仅保留可用的来源记录。

POST /api/jobs/model-preview 接收 project_id、job_id，返回来源快照与 preview_digest，不写入模型。POST /api/jobs/save-model 接收相同字段以及 preview_digest、name、task_type。服务端校验项目归属和当前训练结果，结果变化须重新预览。保存模型状态为 artifact，不含推理服务或部署包。模型 ID 按项目、任务和检查点地址确定，并在工作空间锁内去重；同一结果重试返回原记录，不覆盖原名称或指标。服务后来修订同一地址的内容或指标不更新已保存资产；真实版本应使用新的检查点地址。

provenance 保留任务 ID/名称/摘要、服务 ID、检查点、配方 ID/版本/摘要、输入摘要及执行时资产白名单快照、保存时指标。快照不会跟随源数据登记变化，不包含服务或设备凭据。登记摘要校验的是记录，不是模型权重内容；平台不会下载权重或证明 URI 不变。

POST /api/models 可选 source_model_id 指向同项目训练产物；必须 loaded_confirm=true 且推理服务已连接。产生新的可调用模型记录，保留 source_model_digest 和 provenance；原产物不变。用户确认的是服务已经加载产物，平台仍需调用测试和后续真实验证，不自动发布或发起设备操作。
