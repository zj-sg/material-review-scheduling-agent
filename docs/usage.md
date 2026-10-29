# 安装与使用

已在 Windows、Python 3.12.4 下验证，依赖锁定在 requirements.txt。
当前使用 Pydantic、SQLite、LangGraph、LangChain 模型集成和 pytest。
HTTP 入口使用 FastAPI 与 Uvicorn，启动和请求示例见 [HTTP API 使用说明](http-api.md)。
已验证 LangGraph 1.2.18、langchain-openai 1.7.3、langchain-deepseek 1.1.1；底层 SDK 及完整版本见 requirements.txt。

当前工作区已有 .venv，新环境可执行：

~~~powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
~~~

确定性命令和显式离线模式无需模型密钥。默认读取根目录 .env，可从 .env.example 复制。
本期只用北京时间；数据库路径默认 data/drafts.sqlite3，加载配置不创建数据库。
保存时才打开数据库；正式排期和草稿分别存在 scheduled_slots、drafts 两张表。

在项目根目录的 PowerShell 执行：

~~~powershell
# 静态校验与配置
.\.venv\Scripts\python.exe -X utf8 -m src.main show-config
.\.venv\Scripts\python.exe -X utf8 -m src.main validate-examples

# 正常、冲突、无可用时段
.\.venv\Scripts\python.exe -X utf8 -m src.main plan-example examples/01_normal/input.json
.\.venv\Scripts\python.exe -X utf8 -m src.main plan-example examples/04_time_conflict/input.json
.\.venv\Scripts\python.exe -X utf8 -m src.main plan-example examples/07_no_available_slot/input.json

# 待复核和可信补查
.\.venv\Scripts\python.exe -X utf8 -m src.main review-example examples/03_rule_risk/input.json --step 2
.\.venv\Scripts\python.exe -X utf8 -m src.main review-example examples/02_missing_metadata/input.json --material-source examples/materials-with-supplement.json

# 回归测试
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -W error
~~~

实际保存草稿（每个演示使用独立数据库，便于核对数量）：

~~~powershell
# 正常保存
.\.venv\Scripts\python.exe -X utf8 -m src.main save-example examples/01_normal/input.json --database data/normal-demo.sqlite3

# 相同请求执行两步，返回同一 draft_id，只有一份草稿
.\.venv\Scripts\python.exe -X utf8 -m src.main save-example examples/05_duplicate_request/input.json --database data/duplicate-demo.sqlite3

# 故障注入：退出码为 1，事务回滚，无新草稿
.\.venv\Scripts\python.exe -X utf8 -m src.main save-example examples/06_save_failure/input.json --database data/failure-demo.sqlite3
~~~

save-example 默认执行该场景所有步骤，--step 可只运行一步。
每次调用会从指定 JSON 导入正式排期，替换 scheduled_slots 内容，保留 drafts；
--schedule-source 可选择其他正式排期文件。导入完成后，保存服务以 SQLite 正式排期表为依据。
服务的 replace_schedule 可以更新正式占用；重新提交同一请求会检查新占用并更新原草稿，不留历史版本。
独立的 save_draft 工具只接收原请求和规则，由服务审核并在事务内重查，不接受模型提供的“保存成功”或确认标记。

命令按夹具记录时间回放。--step 从 1 开始；plan-example 可用 --schedule-source 指定正式排期文件。
工具失败退出码为 1，审核阻断、信息不足或无时段是正常业务结论，退出码为 0。
review-example、plan-example 的 actions 记录来源、规则、占用及候选，保存状态为 not_attempted。
save-example 返回每步实际结果与数据库草稿数；保存后审核、时段快照及来源保存在 report_json 中。
saved 表示新建并提交，already_exists 表示已提交对同一草稿的刷新；failed 表示本次保存未提交。
查询错误返回 TOOL_ERROR 并停止写入；存储失败返回 DRAFT_SAVE_FAILED；同 ID 不同输入返回 IDEMPOTENCY_CONFLICT。

save_behavior 仅控制演示故障注入，不是保存工具的业务参数。

## 单 Agent：离线验收

~~~powershell
.\.venv\Scripts\python.exe -X utf8 -m src.main agent-example examples/04_time_conflict/input.json --offline --database data/agent-conflict.sqlite3
.\.venv\Scripts\python.exe -X utf8 -m src.main agent-example examples/05_duplicate_request/input.json --offline --database data/agent-duplicate.sqlite3
.\.venv\Scripts\python.exe -X utf8 -m src.main agent-example examples/06_save_failure/input.json --offline --database data/agent-failure.sqlite3
~~~

该模式运行真实 LangGraph 与业务工具，但模型是 OfflineTestModel，不是真实 LLM。
输出 mode=offline_test；runs 中含决策、真实结果、调用次数、工具参数与返回。
五个工具由程序绑定当前请求；save_draft 返回后即结束，最终交付数据来自实际保存结果。
Agent 工具补查最多一次；保存服务仍会独立复核可信素材及正式占用。

## 兼容接口配置

在本地 .env 取消注释并填写以下三项；不要把 Key 放入聊天或提交到仓库：

~~~dotenv
MATERIAL_AGENT_BASE_URL=https://your-provider.example/v1
MATERIAL_AGENT_MODEL=your-tool-capable-model
MATERIAL_AGENT_API_KEY=your-api-key
~~~

模型须支持 Chat Completions 工具调用。供应商与模型不写死，三个配置缺一则报错。
配置后移除 --offline 运行 agent-example，输出 mode=api。离线测试不会自动验证真实模型质量。
show-config 不导出 Key；模型异常只报告类型与 HTTP 状态，不回显密钥。
循环参考 [LangGraph 官方 quickstart](https://docs.langchain.com/oss/python/langgraph/quickstart)，
工具协议参考 [OpenAI 官方 Function calling](https://developers.openai.com/api/docs/guides/function-calling)。

当前 295 项测试通过（含 46 项 HTTP 接口与时间选择测试），CLI、HTTP 与两条真实 DeepSeek 运行结果见 verification.md。
真实 DeepSeek ReAct 验收及证据保存命令见 [ReAct 验收说明](react-acceptance.md)。
HTTP 原时间不可用时支持内存检查点暂停及人工选择恢复，操作见 http-api.md；原 CLI 保留夹具回放模式。
不启用外部 tracing、跨服务重启的检查点恢复、发布或其他人工审批流程。
