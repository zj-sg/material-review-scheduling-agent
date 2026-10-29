# 素材审核与发布排期 Agent

按题目实现素材审核、冲突处理与草稿保存，一个 Agent 调用必要工具，只保存草稿。

## 评审入口

已完成素材审核、冲突处理、SQLite 保存、单个 LangGraph Agent、HTTP API，以及冲突时的人工时间选择。
命令行和 HTTP 接口通过离线验收；两条真实 DeepSeek 决策路径也已运行通过，记录见下方验收文档。
工具使用 LangChain @tool 定义、bind_tools() 绑定，由 LangGraph ToolNode 执行；DeepSeek 使用 LangChain 模型集成。

| 内容 | 入口 |
| --- | --- |
| 用户、范围与成功标准 | [01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策与取舍 | [02-solution.md](docs/02-solution.md) |
| 测试预期、实际结果、失败与限制 | [03-evaluation.md](docs/03-evaluation.md) |
| AI 辅助、个人参与与验证 | [04-ai-usage.md](docs/04-ai-usage.md) |
| 最难问题、收获与下一步 | [05-retrospective.md](docs/05-retrospective.md) |

至少三组完整材料：[正常](examples/01_normal/trace.json)、[资料不足](examples/02_missing_metadata/trace.json)、[工具失败](examples/06_save_failure/trace.json)。每组同目录提供 input.json、expected.json、actual.json、trace.json，分别区分预期与实际。
两条真实模型记录：[M01 通过后查询](examples/01_normal/live/summary.md)、[M02 缺失后补查](examples/02_missing_metadata/live/summary.md)。其余六类业务要求及人工时间选择见评估文档。

## 环境与安装

已在 Windows、Python 3.12.4 验证，完整依赖锁定在 requirements.txt。新环境在项目根目录执行：

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
~~~

安装后，无需 API Key 即可运行测试和离线回放：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -W error
.\.venv\Scripts\python.exe -X utf8 examples/record_validation.py
~~~

record_validation.py 实际运行七类九步场景及 HTTP 人工选择，刷新 examples 中的 actual.json、trace.json 和 validation-manifest.json；该模式明确标记为 offline_test，不能代替真实模型验收。

Linux/macOS 使用 `python3 -m venv .venv`、`.venv/bin/python -m pip install -r requirements.txt` 及 `.venv/bin/python -m pytest -q -W error`。本次正式验证平台为 Windows，其他平台结果未单独留证。

## 运行

首次配置可复制 .env.example 为本地 .env，已有配置不要覆盖。真实模型填写：

~~~dotenv
MATERIAL_AGENT_BASE_URL=https://api.deepseek.com
MATERIAL_AGENT_MODEL=deepseek-flash
MATERIAL_AGENT_API_KEY=在本地填写
MATERIAL_AGENT_API_DATA_PATH=data/api-data.json
MATERIAL_AGENT_API_OFFLINE=false
~~~

API Key 不提交。素材、规则与初始化占用均是合成数据；默认读取 data/api-data.json。
在项目根目录的 PowerShell 启动 HTTP 服务：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 -m uvicorn src.api:app --host 127.0.0.1 --port 8000
~~~

填写 .env 中的 DeepSeek 密钥后即可使用真实模型；默认 MATERIAL_AGENT_API_OFFLINE=false。
打开 http://127.0.0.1:8000/docs，在 POST /schedule 中点击 Try it out，使用示例请求并执行。
接口说明及 PowerShell 调用见 [HTTP API 使用说明](docs/http-api.md)。
原时间不可用时 POST /schedule 返回 waiting_for_human 和候选列表；再通过 POST /runs/{run_id}/resume 选择时间，复查后保存。
等待期间不新建草稿。运行检查点保存在内存中，请单 worker 运行；服务重启后需重新提交等待中的请求。
若只演示离线流程，启动前设置 $env:MATERIAL_AGENT_API_OFFLINE="true"，退出后用 Remove-Item Env:MATERIAL_AGENT_API_OFFLINE 清除。

原命令行仍可使用：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 -m src.main validate-examples
.\.venv\Scripts\python.exe -X utf8 -m src.main plan-example examples/04_time_conflict/input.json
.\.venv\Scripts\python.exe -X utf8 -m src.main save-example examples/05_duplicate_request/input.json --database data/duplicate-demo.sqlite3
.\.venv\Scripts\python.exe -X utf8 -m src.main agent-example examples/04_time_conflict/input.json --offline --database data/agent-demo.sqlite3
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -W error
~~~

plan-example 只生成建议；save-example 会实际保存 SQLite 草稿，重复请求复用同一草稿 ID。
M01 冲突后建议的 19:00 仍待确认，不能自动接受或声称发布。

--offline 使用明确标注的可控测试模型，实际执行 LangGraph 和业务工具，不调用外部模型。
真实模型需配置 .env 并移除该参数；未配置时直接报错，不会自动转为离线模式。

当前 295 项测试通过，包含 46 项 HTTP 接口与中断测试；七类场景、九个步骤的独立 CLI 离线验收通过。
实际 Uvicorn 端口验证了暂停、用户选择后保存、占用变化后再次暂停，以及草稿查询等流程。
数据库仍只有正式排期和当前草稿两张表，没有版本历史。
模型调用默认最多 12 次，每轮至多一个工具；MATERIAL_AGENT_MAX_MODEL_CALLS 可调整上限。超限返回 status=incomplete、error.code=LIMIT_EXCEEDED（HTTP 409），保留已有记录。

已保存的测试输出见 [examples/test-results.txt](examples/test-results.txt)，逐项报告见 [test-results.xml](examples/test-results.xml)，环境版本见 [environment.json](examples/environment.json)。
运行数据库和临时文件写入 data/，Git 仅提交其中的合成 JSON 与说明；.env、.venv、数据库和缓存均排除。

## 文档

- [精简计划](docs/implementation-plan.md)
- [业务约定](docs/business-spec.md)
- [安装与使用](docs/usage.md)
- [HTTP API 使用说明](docs/http-api.md)
- [DeepSeek 实际 ReAct 验收](docs/react-acceptance.md)
- [两条真实 DeepSeek 运行记录](docs/acceptance-runs/20261029-145149-53d9cebd/summary.md)
- [示例说明](examples/README.md)
- [离线验收记录与验证边界](docs/verification.md)
