# 关键路径实际运行记录

模式：api；模型：deepseek-flash。
本批验收通过 12/12（100.0%），实际模型调用 62 次，模型选择工具 58 次。

工具失败用例以明确失败、模型观察错误后停止、无保存作为验收通过标准。
时间选择、占用复查和强制人工中断由业务流程执行，不作为模型观察后调整动作的证据。
每个样本均保留，没有自动重跑筛选成功记录；供应商 SDK 仍可能进行配置中的一次网络重试。

| 场景 | 次数 | 通过 | 模型调用 | 模型工具调用 | 失败原因 | 记录 |
| --- | --- | --- | --- | --- | --- | --- |
| normal | 1 | True | 4 | 4 | 无 | [normal-1](normal-1/inspection.json) |
| normal | 2 | True | 4 | 4 | 无 | [normal-2](normal-2/inspection.json) |
| conflict_stale | 1 | True | 3 | 3 | 无 | [conflict_stale-1](conflict_stale-1/inspection.json) |
| conflict_stale | 2 | True | 3 | 3 | 无 | [conflict_stale-2](conflict_stale-2/inspection.json) |
| supplement_success | 1 | True | 6 | 6 | 无 | [supplement_success-1](supplement_success-1/inspection.json) |
| supplement_success | 2 | True | 6 | 6 | 无 | [supplement_success-2](supplement_success-2/inspection.json) |
| human_metadata | 1 | True | 8 | 7 | 无 | [human_metadata-1](human_metadata-1/inspection.json) |
| human_metadata | 2 | True | 8 | 7 | 无 | [human_metadata-2](human_metadata-2/inspection.json) |
| human_review | 1 | True | 6 | 6 | 无 | [human_review-1](human_review-1/inspection.json) |
| human_review | 2 | True | 6 | 6 | 无 | [human_review-2](human_review-2/inspection.json) |
| tool_failure | 1 | True | 4 | 3 | 无 | [tool_failure-1](tool_failure-1/inspection.json) |
| tool_failure | 2 | True | 4 | 3 | 无 | [tool_failure-2](tool_failure-2/inspection.json) |

每个目录包含 request.json、catalog.json、events.json、response.json、trace.json 和 inspection.json。
events 包括实际 HTTP 输入/输出，以及明确标为 fixture 的合成故障或占用变更；trace 包括模型观察、实际参数、公开依据和后续动作。
这是有限合成样本的验收通过率，不是生产环境成功率或稳定性保证。未导出模型内部思维过程。
