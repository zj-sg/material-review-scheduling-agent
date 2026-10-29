# DeepSeek 实际 ReAct 验收

两条真实 DeepSeek 路径已于 2026-10-29 运行通过。记录包括实际请求、响应、模型动作、参数、返回摘要、后续动作及公开决策依据，不用程序中断或预设日志代替模型决策。

- [两条路径总览](acceptance-runs/20261029-145149-53d9cebd/summary.md)
- [正常路径：通过后查询时段](acceptance-runs/20261029-145149-53d9cebd/normal/summary.md)
- [缺失路径：补查为空后请求补充](acceptance-runs/20261029-145149-53d9cebd/missing/summary.md)
- [上限测试记录（明确为离线）](acceptance-runs/20261029-145149-53d9cebd/execution-limit-offline/summary.md)

## 场景

默认 data/api-data.json 提供两个不同的工具结果（examples/api-data.json 保留为兼容副本）：

| 路径 | 工具返回 | 模型实际下一步 | 最终结果 |
| --- | --- | --- | --- |
| normal / M01 | check_rules 返回 passed | 选择 query_slots | 观察 available 后结束，ready |
| missing / M02 | check_rules 返回 needs_info | 选择 supplement_material | 观察 empty 后结束，request_info |

M02 缺少 authorization_status，supplemental_materials 为空。
模型可以在补查后再复核一次；验收以实际响应为准，不预先编造调用日志。

## 运行

保持默认 M01、M02 数据，在原服务窗口按 Ctrl+C 停止服务。
确认本地 .env 的 DeepSeek 地址、模型与密钥已经填写；在项目根目录启动：

~~~powershell
$env:MATERIAL_AGENT_API_OFFLINE="false"
.\.venv\Scripts\python.exe -X utf8 -m uvicorn src.api:app --host 127.0.0.1 --port 8000
~~~

需要重启，以加载 reason 工具参数、execution_steps 和执行上限配置。

另开一个 PowerShell 窗口：

~~~powershell
cd D:\ProgramData\material-review-scheduling-agent
.\.venv\Scripts\python.exe -X utf8 examples/verify_react.py
~~~

服务使用其他端口时，追加 --base-url http://127.0.0.1:你的端口。
脚本先读取 /health，要求 mode=api，然后依次提交 M01、M02 两个请求。每次使用新的 request_id、北京时间明天的时段和 save_draft=false，不修改业务文件，不新建草稿。
默认 --path all 验收两条路径，也可以用 --path normal 或 --path missing 单独复测。单路径通过不能替代两条不同路径的要求。
脚本不读取 .env、不记录密钥；实际模型调用由服务完成。

## 保存的证据

终端会打印独立目录：data/react-evidence/日期时间-随机编号/。

| 文件 | 内容 |
| --- | --- |
| health.json | 运行模式检查结果 |
| inspection.json、summary.md | 两条路径总览及 two_path_requirement_met 检查 |
| normal/request.json、missing/request.json | 两条路径实际提交的请求 |
| normal/response.json、missing/response.json | 原始响应，含模型观察、动作和执行限制 |
| 各路径 http.json、inspection.json | HTTP 状态码、时间与逐项校验 |
| 各路径 execution-steps.json、summary.md | 步骤、工具参数、返回摘要、后续动作及简要决策依据 |

model_steps 每一轮记录模型调用前最后一个工具结果，以及模型实际返回的工具调用或结束响应。
另记录工具调用 ID、模型响应 ID、供应商/模型标识和可用的 token 用量，便于核对；不导出内部思维链。
tool_calls 是实际执行记录。execution_steps 对应生成便于阅读的运行记录；决策依据直接来自模型填写的 reason 参数，不由程序预先生成。
next_action_source=model 表示后续动作来自真实模型响应；workflow 表示程序结束、强制中断或达到上限，二者明确区分。

验收检查需要：api 模式、DeepSeek 模型标识、实际观察与调用 ID 对应、完整执行记录和公开依据、未超过执行限制；正常路径须观察 passed 后选择查询，缺失路径须观察 needs_info 后选择补查，并观察 empty 后结束。
只存在工具名称列表、离线模式、无模型轮次、调用失败或未完成上述分支，均不能判为通过。

只有实际运行成功后，才能将本次 evidence 目录与项目一起提交，并声明“本场景已通过 DeepSeek 实际调用验收”。
脚本的测试和 HTTP Mock 不代表真实模型验收；复测结果以各次实际生成的 inspection.json 为准。此次已交付的两条记录均通过，并保留原始响应。

## 执行上限

MATERIAL_AGENT_MAX_MODEL_CALLS 默认 12，可在 .env 配为 1–100。每轮只允许一个工具，所以模型工具循环的工具执行数也有同样上限。
人工恢复后的确定性审核与查询另外记录，不冒充新的模型动作。

达到上限时返回 HTTP 409：

~~~json
{
  "status": "incomplete",
  "error": {
    "code": "LIMIT_EXCEEDED",
    "message": "达到 2 次模型调用上限，流程未完成；已执行 2 次工具调用。"
  },
  "model_calls": 2,
  "execution_limits": {"max_model_calls": 2, "max_tool_calls": 2}
}
~~~

这是字段节选，实际响应保留已有工具与模型记录，不返回成功。上限为 2 的实际离线 HTTP 记录已附在本页开头，区别于两条真实 DeepSeek 路径。

## 在 Swagger 中查看

也可用 POST /schedule 提交 material_id=M02、save_draft=false 的请求，使用新的 request_id 和未来时间。
在 Response body 中查看 model_steps：

- 某轮 observation.name=check_rules，output.data.status=needs_info，模型 tool_calls 中选择 supplement_material。
- 后续某轮 observation.name=supplement_material，output.status=empty，随后模型返回空 tool_calls 并结束。
- 最终 decision=request_info，review.status=needs_info，persistence.status=not_attempted。

自动脚本更适合保留原始响应和验收文件。接口仍只保存草稿，不执行发布。
