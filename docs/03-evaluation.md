# 03 评估、实际结果与限制

## 评审顺序

1. 阅读 [需求](01-requirements.md) 和 [方案](02-solution.md)。
2. 查看至少三组输入、实际输出与 Trace：正常、资料不足、工具失败。
3. 对照两条真实模型记录，核对模型观察不同结果后选择了不同工具。
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

## 两条真实 DeepSeek 决策路径

供应商 DeepSeek，模型 deepseek-flash，2026-10-29 通过实际 API 运行；每条 4 轮模型调用、3 次工具执行。

| 路径 | 观察 → 模型下一步 | 原始材料 |
| --- | --- | --- |
| 正常 M01 | check_rules=passed → query_slots；available → 结束 | [输入](../examples/01_normal/live/request.json) / [实际输出](../examples/01_normal/live/response.json) / [Trace](../examples/01_normal/live/trace.json) / [摘要](../examples/01_normal/live/summary.md) |
| 缺失 M02 | needs_info → supplement_material；empty → 结束并请求补充 | [输入](../examples/02_missing_metadata/live/request.json) / [实际输出](../examples/02_missing_metadata/live/response.json) / [Trace](../examples/02_missing_metadata/live/trace.json) / [摘要](../examples/02_missing_metadata/live/summary.md) |

Trace 对应实际工具调用 ID，包含参数、返回摘要、后续动作和模型给出的 reason。workflow 流转单独标注，不冒充模型动作。
两条请求均设置 save_draft=false，验证模型反馈循环；保存与回滚由上表单独验证，不将未保存说成保存成功。

## 测试与复现

- [test-results.txt](../examples/test-results.txt)：实际 pytest 命令与输出。
- [test-results.xml](../examples/test-results.xml)：逐项执行结果。
- [environment.json](../examples/environment.json)：实际 Python、平台、依赖及 SQLite 版本。
- [install-results.txt](../examples/install-results.txt)：干净虚拟环境的依赖安装原始输出。
- [clean-install-smoke.json](../examples/clean-install-smoke.json)：不带 .env 的干净副本执行离线冲突示例的实际输出。
- 在项目根目录运行 `python -m pytest -q -W error`，或 `python examples/record_validation.py` 重新生成离线实际材料。
- 已启动真实模型服务时，运行 `python examples/verify_react.py` 重新生成两条实际 API 记录。

交付前另建只包含准备提交文件的副本，不带本地 .env、虚拟环境或运行数据库，再创建 Python 3.12.4 环境并从 requirements.txt 安装依赖。
该副本运行 295 项测试通过，离线冲突命令退出码为 0；原始输出保存在上述文件中，不依赖开发机已有 .venv 的安装状态。

执行上限默认 12；[上限输入](../examples/01_normal/execution-limit-offline/request.json)、[输出](../examples/01_normal/execution-limit-offline/response.json)、[摘要](../examples/01_normal/execution-limit-offline/summary.md) 在上限 2 时返回 incomplete/LIMIT_EXCEEDED/HTTP 409，零草稿。该上限记录为离线测试，未计入两条真实路径。

## 失败与已知限制

首轮真实 M02 调用失败：[原始响应](../examples/02_missing_metadata/live-failure/response.json)。旧日志只记录 ValueError，无法精确还原原因；该轮未执行工具，也未计为成功。
之后强化单轮单工具提示并增加无效响应审计，后续两条真实运行通过；这不能证明所有模型失败都已消除。

只在 Windows/Python 3.12.4 下正式验证；真实模型只有上述两条路径逐项留证，其他风险、失败和人工流程的业务逻辑由离线与测试覆盖。
授权证据是本地合成元数据，不代表对外部版权真实性的核验；内容风险规则是明确配置的短语检查，不是通用内容安全审核器。
内存检查点只支持单进程，服务重启会使等待中的 run_id 失效；草稿不预留正式时段，不保证未来仍空闲。
不提供外部发布、用户认证、生产级授权审批或持久化检查点。模型逻辑调用虽受限，网络可用性与供应商延迟仍影响运行时间。
