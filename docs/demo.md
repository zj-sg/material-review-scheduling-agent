# HTTP 完整演示与讲解

## 一条命令复现

安装依赖后，在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 examples/verify_key_paths.py --cases normal conflict_stale
```

这会在空闲端口启动独立 Uvicorn/FastAPI，读取合成资料，初始化独立 SQLite，再实际提交 HTTP 请求。默认 mode=offline_test，不调用外部模型。
需要实际 DeepSeek 时，在本地 .env 填好模型、地址、密钥，并加 `--live`：

```powershell
.\.venv\Scripts\python.exe -X utf8 examples/verify_key_paths.py --live --cases normal conflict_stale
```

不需要停止原来 8000 端口的服务；演示服务退出时自动关闭，现有业务数据不变。日期自动设为北京时间明天，每次 request_id 与数据库独立。

## 建议展示顺序

1. 正常请求：M01 已授权、时间空闲。查看 `/schedule` 返回 completed、review=passed、persistence=saved，再 GET `/drafts/{request_id}` 核对数据库记录。
2. 时间冲突：18:00 已正式占用。查看 waiting_for_human、interrupt.kind=choose_time、19:00/20:00 候选和 not_attempted，说明原始 18:00 要求尚未改变。
3. 时间失效：脚本通过服务端测试夹具将 19:00 更新为正式占用，操作单独记录为 fixture:replace_schedule；这不是模型行为，也不是生产 API。
4. 选择 19:00：恢复时重新审核并复查占用，发现失效后返回新候选和新中断 ID。再次提交旧 ID 返回 409，不自动替用户换时间。
5. 选择最新候选：继续恢复、校验、保存。核对 accepted_at、原 requested_at、confirmed、saved，以及 publication_status=not_published。
6. 打开脚本打印的记录目录：request.json 是实际请求，events.json 是实际 HTTP 和合成夹具事件，response.json 是最后的 Agent 响应；trace.json 给出模型观察、工具参数、公开依据与后续动作。

终端会打印每次请求的路径、HTTP 状态、业务状态和中断类型；成功完成的场景显示 PASS。发生验收失败时保留失败记录并返回非零退出码，不自动换一次成功结果。

## 每一步由谁完成

| 模块 | 负责什么 | 演示时可打开的代码 |
| --- | --- | --- |
| FastAPI | 请求校验、查询运行、接收人工恢复 | [src/api.py](../src/api.py) |
| LangGraph Agent | 模型选择真实绑定工具，ToolNode 执行，工具结果进入下一轮 | [workflow.py](../src/agents/workflow.py)、[toolbox.py](../src/agents/toolbox.py) |
| RuleService | 字段、授权、禁用表述、渠道校验 | [rule_service.py](../src/services/rule_service.py) |
| SlotSearchService | 按正式占用与硬性要求计算候选 | [schedule_service.py](../src/services/schedule_service.py) |
| SchedulingRuns | 当前运行、中断 ID、人工输入及恢复路由 | [time_selection.py](../src/agents/time_selection.py) |
| DraftService / SQLiteStore | 保存前复查、幂等、真实事务提交和回滚 | [draft_service.py](../src/services/draft_service.py)、[storage.py](../src/services/storage.py) |

模型观察 passed 后选择 query_slots，可用模型步骤与工具调用 ID 证明。冲突时强制暂停、用户选择后重新检查由代码保证，不能把它们说成模型自主决策。

## Swagger 手动操作

启动常规服务并打开 http://127.0.0.1:8000/docs，具体配置见 [HTTP API 说明](http-api.md)。
默认合成数据的正式占用为 2026-11-02 18:00–19:00；日期过期后，需要使用未来日期更新请求及演示占用，或者直接使用上面的自动日期脚本。
已有数据库以其中的正式排期为准，修改 JSON 不会覆盖已有占用。新演示数据库可在启动前设置 `MATERIAL_AGENT_DATABASE_PATH` 为未使用过的文件名。

正常请求在 POST /schedule 中输入：

```json
{
  "request_id": "REQ-DEMO-NORMAL-001",
  "material_id": "M01",
  "channel": "wechat",
  "time_requirement": {"mode": "fixed", "requested_at": "2026-11-02T19:00:00+08:00"},
  "search_window": {"start": "2026-11-02T19:00:00+08:00", "end": "2026-11-02T21:00:00+08:00"},
  "save_draft": true
}
```

冲突请求使用新的 request_id，将 requested_at 和 search_window.start 都改为 18:00。返回 waiting_for_human 后，把实际 run_id 填入 `/runs/{run_id}/resume`，选择 time 示例，复制实际 interrupt_id 和候选 start_at 提交。
Swagger 演示不注入新占用；“选择前已被占用”的分支通过上面的隔离脚本演示。正常草稿不占用正式时段，另一份草稿仍可能提出相同时间；本项目不执行发布或正式时段预留。

## 其余关键路径与结果统计

```powershell
# 先离线验证演示入口，运行全部六类
.\.venv\Scripts\python.exe -X utf8 examples/verify_key_paths.py
# 真实模型，每类两次
.\.venv\Scripts\python.exe -X utf8 examples/verify_key_paths.py --live --repeats 2
```

除 normal、conflict_stale 外，还包含：

- supplement_success：补查返回有效凭证，模型观察后选择 check_rules，重审通过后选择 query_slots。
- human_metadata：补查 empty 后模型请求补充，人工补齐后恢复同一运行，重新审核与保存。
- human_review：授权存疑暂停，有凭证人工通过后恢复，重新审核与保存。
- tool_failure：合成资料补查源抛出 OSError，实际工具转换为 TOOL_ERROR；验证模型观察 error 后结束，不查询、不保存、不重试。

每次最多按项目配置调用模型，默认 12 次。`--repeats` 为 1–5，所有尝试均进入 summary.json，保存验收通过率、模型调用次数、模型工具调用次数、失败检查和业务错误。
工具故障用例以“明确失败且正确停止”为通过标准，不要求业务保存成功。仅在使用 --live 后才能将该批记录称为真实模型验收。
脚本默认写入 data/key-paths/日期-随机号；可用 `--output` 指定一个尚不存在的目录。已归档的实际记录入口见 [评估文档](03-evaluation.md)。
