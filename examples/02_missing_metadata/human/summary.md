# 人工中断运行记录（离线模型）

实际执行 HTTP 接口、LangGraph interrupt/Command 和 SQLite，未调用真实模型。

- automatic_lookup_before_human: True
- invalid_input_preserves_interrupt: True
- rechecks_then_waits_for_time: True
- same_run_saved_after_both_inputs: True
- metadata_and_time_audited: True
- source_json_unchanged: True
- nothing_published: True

input.json 保存各步请求，actual.json 保存 HTTP 返回，trace.json 保存累计工具步骤和人工输入。
