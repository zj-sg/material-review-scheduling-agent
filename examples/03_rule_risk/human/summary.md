# 人工中断运行记录（离线模型）

实际执行 HTTP 接口、LangGraph interrupt/Command 和 SQLite，未调用真实模型。

- review_paused_without_draft: True
- approval_without_evidence_rejected: True
- approval_rechecked_and_saved: True
- reject_ends_without_saving: True
- request_changes_waits_for_metadata: True
- modified_data_requires_new_review: True
- modified_then_approved_and_saved: True
- explicit_block_cannot_enter_review: True
- source_json_unchanged: True
- nothing_published: True

input.json 保存各步请求，actual.json 保存 HTTP 返回，trace.json 保存累计工具步骤和人工输入。
