# 验收示例

input.json 给出数据与顺序请求，expected.json 给出事先定义的预期；actual.json 是实际运行输出，trace.json 是实际模型/工具执行和检查结果。
各目录独立，同一目录内步骤共享存储状态。

| 目录 | 重点 |
| --- | --- |
| 01_normal | 正常审核、排期、保存 |
| 02_missing_metadata | 补查后仍缺失，列出待补充项 |
| 03_rule_risk | 阻断风险与待复核结果 |
| 04_time_conflict | M01 要求 18:00，建议 19:00 并待确认 |
| 05_duplicate_request | 重新检查时段，复用同一草稿 ID |
| 06_save_failure | 保存失败，明确返回失败原因 |
| 07_no_available_slot | 无候选时请求新要求，保持原约束 |

七个主场景目录共 9 个请求步骤；实际输出与 Trace 均显式标注离线模式及夹具时钟。
materials-with-supplement.json 是 M02 的可信补查数据。
回放日期为 2026-11-02，时区为 +08:00。

当前已实现审核、排期、保存和单 Agent；首批两条及新增六类各两次真实 DeepSeek 运行均已留证，记录见 ../docs/react-acceptance.md。
save-example 会先将指定文件的正式排期导入 SQLite，再按顺序执行步骤，保留已有草稿。
每个场景验收建议使用独立数据库文件，以便核对预期草稿数量；命令不会自动清空已有草稿。
06 的 save_behavior=fail 仅为示例故障注入：执行写入后主动报错，验证真实事务回滚。
说明文字允许同义表达，状态、规则、时间、草稿数量和重复 ID 关系必须符合预期。
运行命令见 ../docs/usage.md。

agent-example --offline 使用显式可控模型运行 LangGraph 和业务工具；该结果不能作为真实 LLM 效果证据。
完整离线索引见 [validation-manifest.json](validation-manifest.json)。运行 `python examples/record_validation.py` 可重新取得实际结果，不会调用外部模型。

真实 DeepSeek 的两条不同路径已保存在 [01_normal/live](01_normal/live/summary.md) 和 [02_missing_metadata/live](02_missing_metadata/live/summary.md)，包含 request.json、response.json、trace.json、execution-steps.json、inspection.json 和摘要。
首次失败的真实模型记录在 02_missing_metadata/live-failure；它没有计为成功。
人工时间选择与占用变化后重提的完整 HTTP 记录在 [04_time_conflict/interactive](04_time_conflict/interactive/summary.md)，该记录使用离线模型、真实服务和数据库。
新增 [02_missing_metadata/human](02_missing_metadata/human/summary.md) 覆盖补查无果→资料输入校验→重审→时间选择→保存；[03_rule_risk/human](03_rule_risk/human/summary.md) 覆盖通过、驳回、要求修改后重审及明确阻断。这些为离线模型下实际执行的 HTTP/图/数据库记录。
原有两条 live 记录采集于新增资料/风险人工中断前；新增真实验收位于 [04_time_conflict/live-key-paths](04_time_conflict/live-key-paths/summary.md)，包含正常、冲突与时间失效、补查成功重审、人工补资料、风险通过和工具错误各两次。summary.json 汇总 12/12 验收通过、62 次模型调用和 58 次模型工具执行，保留所有事件与失败检查。
各目录的 events.json 包括真实 HTTP 及明确标注的合成占用/故障事件，trace.json 保存模型观察与后续动作，inspection.json 区分验收通过与业务错误；runner-source.py.txt 保存本批运行器源文件。脚本 `python examples/verify_key_paths.py --live --repeats 2` 可复测，不带 --live 则为离线演示。
test-results.txt、test-results.xml 为实际测试输出；environment.json 记录验证环境。评审映射见 ../docs/03-evaluation.md。
当前版本的最新干净安装、324 项测试及六类离线 HTTP 演示记录见 [04_time_conflict/clean-environment](04_time_conflict/clean-environment/summary.md)。该验证来自远程新克隆和全新 Python 3.12.4 环境，保留来源提交、依赖指纹、安装日志和原始响应；不增加真实模型统计。
