# HTTP 人工时间选择运行记录（离线模型）

本记录实际执行 FastAPI、LangGraph 中断和 SQLite；不是外部模型验收。

- paused_without_new_draft: True
- selected_second_candidate: True
- duplicate_reuses_draft: True
- occupied_choice_reinterrupts: True
- old_interrupt_rejected: True
- new_choice_saved: True
- changed_occupancy_rechecked_on_resubmit: True
- not_published: True

实际输入见 input.json，完整输出见 actual.json，逐步 HTTP 与正式占用变更见 trace.json。
