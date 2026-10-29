# 03 评估、实际结果与限制

## 评审顺序

1. 阅读 [需求](01-requirements.md) 和 [方案](02-solution.md)。
2. 查看至少三组输入、实际输出与 Trace：正常、资料不足、工具失败。
3. 对照原有两条及新增六类真实模型记录，核对模型观察不同结果后的动作，以及程序中断与人工恢复。
4. 运行测试与离线回放，检查错误、中断、幂等与执行上限。

expected.json 是事先定义的预期，不是实际输出。actual.json 与 trace.json 由实际执行 CLI 后保存。
七类场景使用夹具时钟回放，mode=offline_test，不代表真实模型效果；[manifest](../examples/validation-manifest.json) 记录时间、命令、退出码、比较结果与代码指纹。

## 全部验收点

| 场景 | 预期 | 实际结果及记录 |
| --- | --- | --- |
| 正常 | passed、原时段 available、保存一份 | [输入](../examples/01_normal/input.json) / [实际输出](../examples/01_normal/actual.json) / [Trace](../examples/01_normal/trace.json) |
| 信息缺失 | 补查仍缺失，request_info，不保存 | [输入](../examples/02_missing_metadata/input.json) / [实际输出](../examples/02_missing_metadata/actual.json) / [Trace](../examples/02_missing_metadata/trace.json) |
| 审核风险 | 阻断不保存；待复核仅存待复核草稿 | [输入](../examples/03_rule_risk/input.json) / [实际输出](../examples/03_rule_risk/actual.json) / [Trace](../examples/03_rule_risk/trace.json) |
| 时间冲突 | 原时间保留，替代建议待确认 | [输入](../examples/04_time_conflict/input.json) / [实际输出](../examples/04_time_conflict/actual.json) / [Trace](../examples/04_time_conflict/trace.json) |
| 重复提交 | saved / already_exists，同 ID、一份草稿 | [输入](../examples/05_duplicate_request/input.json) / [实际输出](../examples/05_duplicate_request/actual.json) / [Trace](../examples/05_duplicate_request/trace.json) |
| 工具/保存失败 | 失败返回、事务回滚、零草稿 | [输入](../examples/06_save_failure/input.json) / [实际输出](../examples/06_save_failure/actual.json) / [Trace](../examples/06_save_failure/trace.json) |
| 无可用时段 | no_slot，不放宽硬性要求 | [输入](../examples/07_no_available_slot/input.json) / [实际输出](../examples/07_no_available_slot/actual.json) / [Trace](../examples/07_no_available_slot/trace.json) |

06 的失败是明确标注的故障注入：SQLite 写入后、提交前抛出异常，实际回滚。CLI 退出码 1 是预期结果，不是验收脚本把失败冒充成功。
文案允许同义表达，状态、缺失字段、风险、原始/建议时间、确认事实、错误码和草稿数量必须符合预期。

HTTP 人工流程另见 [实际输入](../examples/04_time_conflict/interactive/input.json)、[实际输出](../examples/04_time_conflict/interactive/actual.json)、[Trace](../examples/04_time_conflict/interactive/trace.json)。
它覆盖暂停前不新增草稿、用户选择第二个候选、所选时间失效后再次暂停、旧中断拒绝，以及占用变化后重复请求刷新同一草稿。
该记录明确为离线模型下的真实服务/数据库执行，不作为真实模型决策证据。

新增两类人工中断的实际材料：

| 人工流程 | 预期与已验证行为 | 记录 |
| --- | --- | --- |
| 资料补充 | 自动补查后暂停；无效输入不消耗中断；补齐后重审和查询，再选择时间保存 | [输入](../examples/02_missing_metadata/human/input.json) / [输出](../examples/02_missing_metadata/human/actual.json) / [Trace](../examples/02_missing_metadata/human/trace.json) |
| 授权复核 | 有凭证通过后重审；驳回终止；要求修改后补资料并再次审核；明确禁止直接阻断 | [输入](../examples/03_rule_risk/human/input.json) / [输出](../examples/03_rule_risk/human/actual.json) / [Trace](../examples/03_rule_risk/human/trace.json) |

以上执行真实 FastAPI/LangGraph interrupt/Command/SQLite，模型为 offline_test，不作为真实模型效果证据。主场景 03 的旧 CLI 回放仍可保存待复核草稿；当前 HTTP 在待复核时先暂停、不新建草稿。
新增 29 项测试覆盖恢复串联、共享数据隔离、旧结论因素材变化失效、禁止内容不可绕过和跨恢复累计预算；完整回归共 324 项通过，其中 HTTP 与人工中断共 75 项。

## 两条真实 DeepSeek 决策路径

供应商 DeepSeek，模型 deepseek-flash，2026-10-29 通过实际 API 运行；每条 4 轮模型调用、3 次工具执行。

| 路径 | 观察 → 模型下一步 | 原始材料 |
| --- | --- | --- |
| 正常 M01 | check_rules=passed → query_slots；available → 结束 | [输入](../examples/01_normal/live/request.json) / [实际输出](../examples/01_normal/live/response.json) / [Trace](../examples/01_normal/live/trace.json) / [摘要](../examples/01_normal/live/summary.md) |
| 缺失 M02 | needs_info → supplement_material；empty → 结束并请求补充 | [输入](../examples/02_missing_metadata/live/request.json) / [实际输出](../examples/02_missing_metadata/live/response.json) / [Trace](../examples/02_missing_metadata/live/trace.json) / [摘要](../examples/02_missing_metadata/live/summary.md) |

Trace 对应实际工具调用 ID，包含参数、返回摘要、后续动作和模型给出的 reason。workflow 流转单独标注，不冒充模型动作。
这两条真实记录采集于新增资料/风险中断之前；当前 HTTP 的 M02 会在模型请求补充后进入 waiting_for_human，而非以原记录的 completed 结束。当前版本的新增真实验收见下一节。
两条请求均设置 save_draft=false，验证模型反馈循环；保存与回滚由上表单独验证，不将未保存说成保存成功。

## 扩展真实关键路径验收（2026-10-29）

使用本地配置的 DeepSeek / deepseek-flash，真实 Uvicorn 回环 HTTP、真实模型工具调用及独立 SQLite。六类各两次，固定本批 12 次业务尝试，无自动场景重跑筛选成功记录。
[总览](../examples/04_time_conflict/live-key-paths/summary.md)、[机器可读统计](../examples/04_time_conflict/live-key-paths/summary.json) 保存所有样本、调用次数、失败原因、源代码指纹与运行器指纹。

| 场景 | 通过 / 尝试 | 每次模型调用 | 每次模型选择工具 | 实际结果与证据 |
| --- | --- | --- | --- | --- |
| 正常保存 | 2 / 2 | 4 | 4 | [正常请求、查询后保存](../examples/04_time_conflict/live-key-paths/normal-1/response.json) |
| 时间冲突、选择前时段失效 | 2 / 2 | 3 | 3 | [两次人工选择、旧 ID 409、最终保存](../examples/04_time_conflict/live-key-paths/conflict_stale-1/events.json) |
| 自动补查成功后重审 | 2 / 2 | 6 | 6 | [needs_info → supplement_material → check_rules → query_slots → save_draft](../examples/04_time_conflict/live-key-paths/supplement_success-1/trace.json) |
| 人工补充资料恢复 | 2 / 2 | 8 | 7 | [empty 后模型结束、人工补齐、同一运行重审保存](../examples/04_time_conflict/live-key-paths/human_metadata-1/events.json) |
| 授权存疑人工通过 | 2 / 2 | 6 | 6 | [review_risk → 有凭证通过 → 重审保存](../examples/04_time_conflict/live-key-paths/human_review-1/events.json) |
| 补查工具返回错误 | 2 / 2 | 4 | 3 | [模型观察 TOOL_ERROR 后结束，不查询不保存](../examples/04_time_conflict/live-key-paths/tool_failure-1/trace.json) |

本批验收通过率 **12/12（100%）**，共 **62 次模型逻辑调用、58 次模型选择的工具执行**。验收失败次数为 0；其中工具错误场景有 2 次预期业务失败，HTTP 500 / TOOL_ERROR，模型在下一轮观察 error 后返回无工具调用，数据库零草稿。这两次计为“故障处理验收通过”，不能解释为业务保存成功。
底层 SDK 仍可能进行配置中的一次网络重试，统计不将该重试当成新的模型决策。

ReAct 证据是实际模型轮次：补查返回 ok 后模型选择 check_rules；工具返回 error 后模型选择结束；审核 passed 后模型选择 query_slots。各轮使用真实 tool_call_id 关联，不提交内部思维过程。
冲突后强制中断、人工选择、事务中复查和时间失效后再次暂停属于程序职责；这些通过真实模型服务启动的业务路径验证，**不称为模型自主改选了发布时间**。
工具错误为合成补查源 OSError，经现有注册工具转换为真实 error 回执；未模拟供应商错误。占用变化通过服务端夹具直接更新独立数据库，未增加生产故障注入接口。

复现命令为 `python examples/verify_key_paths.py --live --repeats 2`；默认不带 --live 时为离线演示，已经单独跑通全部六类。完整演示和模块讲解见 [demo.md](demo.md)。

## 测试与复现

- [test-results.txt](../examples/test-results.txt)：当前版本实际 pytest 命令与输出。
- [test-results.xml](../examples/test-results.xml)：逐项执行结果。
- [environment.json](../examples/environment.json)：实际 Python、平台、依赖及 SQLite 版本。
- [install-results.txt](../examples/install-results.txt)、[clean-install-test-results.txt](../examples/clean-install-test-results.txt)：新增人工中断前，干净虚拟环境的依赖安装与 295 项测试原始输出。
- [clean-install-smoke.json](../examples/clean-install-smoke.json)：不带 .env 的干净副本执行离线冲突示例的实际输出。
- 在项目根目录运行 `python -m pytest -q -W error`，或 `python examples/record_validation.py` 重新生成离线实际材料。
- 已启动真实模型服务时，运行 `python examples/verify_react.py` 重新生成两条实际 API 记录。
- 运行 `python examples/verify_key_paths.py --live --repeats 2` 可在隔离服务中重新生成六类真实验收；用例结果和失败均保留。

交付前另建只包含准备提交文件的副本，不带本地 .env、虚拟环境或运行数据库，再创建 Python 3.12.4 环境并从 requirements.txt 安装依赖。
该历史副本运行 295 项测试通过，离线冲突命令退出码为 0；原始输出保存在上述文件中。新增人工中断后的 324 项回归在当前虚拟环境执行，依赖未变，未重新做全新环境安装。

执行上限默认 12；[上限输入](../examples/01_normal/execution-limit-offline/request.json)、[输出](../examples/01_normal/execution-limit-offline/response.json)、[摘要](../examples/01_normal/execution-limit-offline/summary.md) 在上限 2 时返回 incomplete/LIMIT_EXCEEDED/HTTP 409，零草稿。该上限记录为离线测试，未计入两条真实路径。

## 失败与已知限制

首轮真实 M02 调用失败：[原始响应](../examples/02_missing_metadata/live-failure/response.json)。旧日志只记录 ValueError，无法精确还原原因；该轮未执行工具，也未计为成功。
之后强化单轮单工具提示并增加无效响应审计，后续两条真实运行通过；这不能证明所有模型失败都已消除。

只在 Windows/Python 3.12.4 下正式验证；新增六类共 12 次真实运行扩大了覆盖，样本仍有限，不代表生产稳定性。人工驳回/要求修改、来源变化后的批准失效、保存故障回滚及调用上限仍主要由离线测试覆盖，未全部追加真实模型调用。
授权证据是本地合成元数据，不代表对外部版权真实性的核验；内容风险规则是明确配置的短语检查，不是通用内容安全审核器。
内存检查点只支持单进程，服务重启会使等待中的 run_id 失效；草稿不预留正式时段，不保证未来仍空闲。
不提供外部发布、用户认证、生产级授权审批或持久化检查点。模型逻辑调用虽受限，网络可用性与供应商延迟仍影响运行时间。
