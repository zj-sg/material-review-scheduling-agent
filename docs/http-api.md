# HTTP API 使用说明

HTTP 接口复用现有 LangGraph Agent、审核工具和 SQLite 保存服务。默认调用 .env 中配置的 DeepSeek，保留原命令行入口。
Agent 的五个工具已改为 @tool + bind_tools() + ToolNode；HTTP 地址、请求体和暂停恢复操作保持不变。

## 启动

在项目根目录的 PowerShell 执行；当前工作区依赖已经安装，新环境先按 usage.md 安装 requirements.txt。

~~~powershell
cd D:\ProgramData\material-review-scheduling-agent
.\.venv\Scripts\python.exe -X utf8 -m uvicorn src.api:app --host 127.0.0.1 --port 8000
~~~

启动后访问 http://127.0.0.1:8000/docs。在 POST /schedule 中点击 Try it out，可直接修改示例并 Execute。
健康检查地址：http://127.0.0.1:8000/health。按 Ctrl+C 停止服务。

.env 配置：

~~~dotenv
MATERIAL_AGENT_BASE_URL=https://api.deepseek.com
MATERIAL_AGENT_MODEL=deepseek-flash
MATERIAL_AGENT_API_KEY=在本地填写密钥
MATERIAL_AGENT_DATABASE_PATH=data/drafts.sqlite3
MATERIAL_AGENT_API_DATA_PATH=data/api-data.json
MATERIAL_AGENT_API_OFFLINE=false
MATERIAL_AGENT_MAX_MODEL_CALLS=12
~~~

前三项用于真实模型；最后一项为 false 时，缺少模型配置会启动失败，不会自动降级。
API Key 只由服务端读取，调用本项目的 HTTP 接口时无需在请求体中携带。

临时离线演示可在新的 PowerShell 窗口执行：

~~~powershell
$env:MATERIAL_AGENT_API_OFFLINE="true"
.\.venv\Scripts\python.exe -X utf8 -m uvicorn src.api:app --host 127.0.0.1 --port 8000
# Ctrl+C 停止后，清除临时配置，下次启动恢复读取 .env：
Remove-Item Env:MATERIAL_AGENT_API_OFFLINE
~~~

离线模式返回 mode=offline_test，实际执行工具与数据库，模型为可控测试模型。
真实模型返回 mode=api；健康检查仅说明服务已启动，不验证供应商权限或额度。

## 提交请求

在服务保持运行时，另开一个 PowerShell 窗口：

~~~powershell
cd D:\ProgramData\material-review-scheduling-agent
$body = Get-Content -Raw -Encoding UTF8 examples/api-request.json
$result = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/schedule" -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$result | ConvertTo-Json -Depth 50
~~~

请求体只包含业务要求：

~~~json
{
  "request_id": "REQ-HTTP-001",
  "material_id": "M01",
  "channel": "wechat",
  "time_requirement": {
    "mode": "fixed",
    "requested_at": "2026-11-02T18:00:00+08:00"
  },
  "search_window": {
    "start": "2026-11-02T18:00:00+08:00",
    "end": "2026-11-02T21:00:00+08:00"
  },
  "save_draft": true
}
~~~

HTTP 使用当前真实时间，要求发布时间在未来；示例日期过期后需修改请求与服务端演示占用日期。
不接受客户端指定 evaluated_at、规则、素材来源、确认标记或故障注入参数。

默认演示数据包含 M01 正常素材、M02 缺失信息、M03 阻断风险、M04 待人工复核。
新数据库中的 wechat 正式占用为 2026-11-02 的 18:00–19:00；请求 M01 的 18:00 时，系统暂停，返回 19:00、20:00 的候选列表，此时不新建草稿。
更换素材、时间或其他输入时使用新的 request_id；同一请求在等待期间重复提交返回同一运行与中断，不重复调用模型。
已完成请求重新提交会重新检查当前占用，允许保存时仍复用同一草稿 ID。

响应包含 mode、status、decision、result、model_calls、model_steps、tool_calls、execution_steps、execution_limits、error、run_id、interrupt、human_actions。
result 中分别给出审核、排期、确认、实际保存及未发布状态；tool_calls 记录实际调用与返回。
model_steps 记录各轮模型观察到的工具结果及模型实际返回的动作，真实调用验收与证据保存见 [ReAct 验收说明](react-acceptance.md)。
execution_steps 另给出步骤、实际工具参数、返回摘要、后续动作及模型提供的简短 reason，不导出内部思维过程。
save_draft=false 时只给建议，不写入草稿。

## 时间冲突：暂停、选择和恢复

如果原固定时间或窗口内优选时间不可用，而存在其他可用时段，返回 HTTP 200，status=waiting_for_human。
这是等待用户选择，不代表流程完成；result.persistence.status=not_attempted，尚未新建草稿。
候选由 SlotSearchService 按正式占用、时间约束、时长与步长计算，模型不能生成或改写候选。

响应中的相关字段示意（ID 以实际返回为准）：

~~~json
{
  "status": "waiting_for_human",
  "decision": "request_confirmation",
  "run_id": "RUN-实际运行ID",
  "interrupt": {
    "interrupt_id": "实际中断ID",
    "kind": "choose_time",
    "original_at": "2026-11-02T18:00:00+08:00",
    "candidates": [
      {"start_at": "2026-11-02T19:00:00+08:00", "end_at": "2026-11-02T20:00:00+08:00"},
      {"start_at": "2026-11-02T20:00:00+08:00", "end_at": "2026-11-02T21:00:00+08:00"}
    ]
  }
}
~~~

在 Swagger 页面展开 POST /runs/{run_id}/resume，点击 Try it out：

1. 路径参数 run_id 填写上一次响应中的 run_id。
2. 请求体的 interrupt_id 填写上一次响应中的 interrupt.interrupt_id。
3. selected_at 从 interrupt.candidates 中复制一个 start_at，例如选择 20:00：

~~~json
{
  "interrupt_id": "复制上一次响应的实际中断ID",
  "selected_at": "2026-11-02T20:00:00+08:00"
}
~~~

点击 Execute 后，系统重新审核素材并在事务内复查所选时段。
仍可用时，status=completed，confirmation.status=confirmed，accepted_at 为用户所选时间。
原 requested_at 仍为 18:00；用户选择和中断 ID 单独记录，原请求内容及 request_id 不被改写。
save_draft=true 时实际保存；false 时完成选择但不写入草稿。无论哪种情况均不执行发布。

也可以继续使用提交请求时获得的 PowerShell $result：

~~~powershell
$choice = @{
    interrupt_id = $result.interrupt.interrupt_id
    selected_at = $result.interrupt.candidates[1].start_at
} | ConvertTo-Json
$runId = $result.run_id
$result = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/runs/$runId/resume" -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($choice))
$result | ConvertTo-Json -Depth 50
~~~

这里 [1] 选择第二个候选，实际使用时确认候选数量，也可用 [0] 选择第一个。

如果所选时间已被占用，系统再次返回 waiting_for_human、新的 interrupt_id 和最新候选，不能自动替用户换时间。
如果已无候选，则返回 request_info/no_slot，要求使用新请求补充时间要求，不扩大原约束。
若素材已出现风险，恢复时返回新的审核结论，不能用时间选择解除风险。

GET /runs/{run_id} 可查看当前运行状态。已经完成的运行再次恢复返回 409；旧中断 ID 返回 409；列表外时间返回 422。
原请求的发布时间或查询起点已过期时，恢复返回 RUN_EXPIRED/409，需使用新请求 ID 和未来时间重新提交。

恢复时若发现新缺失或待复核风险，会转到下面对应的人工中断；明确阻断和工具失败直接返回结果。

## 素材信息缺失：补充后重新审核

以默认演示素材 M02 发起请求：将上面的 material_id 改为 M02，request_id 改为 REQ-HTTP-M02-001。自动补查没有找到信息后，返回 status=waiting_for_human、interrupt.kind=provide_metadata。
interrupt.missing_fields 列出缺失字段，requirements 提供每个字段的用途和补充要求，editable_fields 限定本次可以提交的字段。

在 POST /runs/{run_id}/resume 中选择 metadata 示例，替换实际 run_id 和 interrupt_id：

~~~json
{
  "interrupt_id": "复制本次中断 ID",
  "fields": {
    "authorization_status": "authorized",
    "authorization_evidence": "AUTH-M02-VERIFIED"
  }
}
~~~

如果缺少 channel，则提交 `"fields": {"channel": "wechat"}`。素材渠道必须匹配原请求的 channel，不能通过补资料改变请求渠道。
空值、非法枚举、非本次允许的字段返回 422，原中断保留。可以分次补充，但每次恢复都会重新执行模型和相关工具，仍缺字段时再次中断。
授权状态只提供 authorized、unknown 或 denied；authorized 需要凭证，unknown 转入风险审核，denied 直接阻断。
资料补齐后重新读取、审核并查询排期；如 18:00 被占用，随后返回 choose_time，需继续选择候选，才可保存。

## 风险待复核：通过、驳回或要求修改

以 M04、一个新的 request_id 发起请求，返回 interrupt.kind=review_risk。risks 给出规则编号、级别、字段、相关值和内容摘要；material 展示本次待审素材，options 给出可选操作。
当前待复核规则是 R_AUTH_REVIEW（授权状态 unknown）。配置中的禁用短语、明确 denied、渠道不符属于阻断规则，直接终止，不提供人工通过入口。

人工通过时选择 review 示例，填实际 ID、审核人、说明及授权凭证：

~~~json
{
  "interrupt_id": "复制本次中断 ID",
  "action": "approve",
  "reviewer": "reviewer-demo",
  "note": "已核对素材与授权文件编号",
  "authorization_evidence": "AUTH-M04-VERIFIED"
}
~~~

approve 仅将本次运行的授权资料补齐，然后重新执行审核和排期，不直接把审核状态改为通过；没有新凭证时，可使用此前补充的非空凭证。没有凭证返回 422。
驳回和要求修改的请求如下（二选一）：

~~~json
{
  "interrupt_id": "复制本次中断 ID",
  "action": "reject",
  "reviewer": "reviewer-demo",
  "note": "无法确认授权，驳回本次申请"
}
~~~

~~~json
{
  "interrupt_id": "复制本次中断 ID",
  "action": "request_changes",
  "reviewer": "reviewer-demo",
  "note": "请补充授权文件编号后重新提交审核"
}
~~~

reject 返回 status=rejected、decision=reject，不继续调用模型、不新建草稿；保留原规则发现以便核对。
request_changes 转为 provide_metadata，message 展示审核意见；按 editable_fields 提交修改后，重新审核。授权存疑不能通过把字段直接改成 authorized 绕过人工复核，需补充凭证后再次 approve。

人工输入仅作用于当前 run_id，不写回共享 JSON 文件。素材来源发生变化时旧补充和旧通过结论失效，恢复时重新判断；新的阻断风险仍会被拦截。
human_actions 记录补充资料、审核人/意见/操作和时间选择；保存成功时随草稿记录写入现有 drafts 表。待办和驳回记录目前只在内存中保存。
reviewer 是调用方填写的演示身份，本项目未实现登录鉴权或独立审核权限。

新增两类流程的实际离线运行材料见 [资料补充](../examples/02_missing_metadata/human/summary.md) 和 [人工风险审核](../examples/03_rule_risk/human/summary.md)。可运行 `python examples/record_human_interrupts.py` 复现，不会调用 DeepSeek。

## 查询草稿

~~~powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/drafts/REQ-HTTP-001" | ConvertTo-Json -Depth 30
~~~

该接口按 request_id 返回数据库中的当前 ScheduleResult，找不到已保存草稿时返回 404。
草稿保存不代表发布；替代时间在用户选择前待确认，确认后仅表示排期草稿时间已被接受，接口不提供发布操作。

## 服务端数据与重启

data/api-data.json 保存规则、素材、可信补查信息和初始化正式排期；路径可由 MATERIAL_AGENT_API_DATA_PATH 修改。
服务启动时校验文件；规则修改后重启生效，素材读取与补查每次读取该文件。

启动仅在正式排期表首次创建时导入初始化占用；已存在的正式排期，包括空表，重启时均保留。
草稿始终保留在配置的 SQLite 文件中。启动和每次请求都不会清空原数据库或重新导入占用。
修改 JSON 的初始化占用不会覆盖已有数据库；新的演示可指定新的 MATERIAL_AGENT_DATABASE_PATH。
既有业务代码仍可通过 SQLiteStore.replace_schedule 更新正式占用。

人工中断通过 LangGraph interrupt()、Command(resume=...) 和内存检查点实现。
请使用单进程、单 worker 运行。服务重启、重载或进程退出会清除等待中的运行；旧 run_id 返回 404，需重新提交请求。
已提交的 SQLite 草稿和正式排期保留。未增加检查点表、草稿版本历史或独立审批数据库。

## HTTP 状态

| 状态码 | 含义 |
| --- | --- |
| 200 | 已完成、等待人工或人工驳回，需查看 status、decision、result 和 interrupt |
| 404 | 素材、草稿或运行不存在；服务重启后的内存运行也返回 404 |
| 409 | 请求 ID 内容冲突、中断已失效、重复恢复或原请求时间过期 |
| 422 | 请求或人工输入校验失败、缺少审核凭证，或选择不在当前候选列表中 |
| 500 | 工具、数据库保存或服务内部失败 |
| 502 | 模型接口失败、响应无效或缺少必要工具步骤 |

模型调用达到配置上限时返回 HTTP 409、status=incomplete、error.code=LIMIT_EXCEEDED；实际计数与已有执行记录保留，不返回成功。
默认最多 12 轮模型调用，每轮至多执行一个模型选择的工具。补资料和风险审核后的模型重跑累计计数，不因恢复而重置；人工时间选择的确定性业务复查单独记录。较长的多轮演示可按需提高 MATERIAL_AGENT_MAX_MODEL_CALLS。

实现参考 [FastAPI 官方入门](https://fastapi.tiangolo.com/tutorial/first-steps/) 与 [测试说明](https://fastapi.tiangolo.com/tutorial/testing/)。
