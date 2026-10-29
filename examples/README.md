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

当前已实现审核、排期、保存和单 Agent；两条真实 DeepSeek 决策路径已运行通过，记录见 ../docs/react-acceptance.md。
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
test-results.txt、test-results.xml 为实际测试输出；environment.json 记录验证环境。评审映射见 ../docs/03-evaluation.md。
