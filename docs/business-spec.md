# 业务约定

目标：读取素材、审核风险、查询时段并保存可确认的草稿。只保存草稿，不执行发布。
固定使用北京时间（Asia/Shanghai，UTC+08:00），处理文本与元数据。

## 输入

| 内容 | 字段 |
| --- | --- |
| 请求 | request_id、material_id、channel、time_requirement、search_window、save_draft |
| 素材 | material_id、title、content、channel、authorization_status、authorization_evidence |
| 规则 | version、timezone、allowed_channels、required_material_fields、blocked_phrases、slot_duration_minutes、candidate_step_minutes |
| 已有排期 | schedule_id、channel、start_at、end_at |

request_id 是提交幂等键，同一 ID 只能对应同一份输入。修改输入使用新 ID。
时间包含日期与偏移，拒绝无时区或过期时间。
fixed 表示指定时间；window 表示允许在窗口内选时，提供窗口起止与偏好时间。
search_window 是本次候选查询范围。

原始素材允许缺失字段，以便补查。ID、标题、正文、渠道、授权状态是最低必填项。
authorized 需要凭证；unknown 表示待核实；denied 表示明确未授权。

## 审核与补查

| 规则 | 处理 |
| --- | --- |
| R_REQUIRED | 必填字段缺失，补查后仍缺失则 needs_info |
| R_AUTH_EVIDENCE | 已授权但凭证缺失，补查后仍缺失则 needs_info |
| R_AUTH_BLOCK | 明确未授权，blocked |
| R_AUTH_REVIEW | 授权待核实，needs_review，要求可信证据 |
| R_TEXT_BLOCK | 正文命中配置禁用表述，blocked |
| R_CHANNEL | 请求渠道未被允许或与素材渠道不一致，blocked |

保留所有可执行规则的依据；优先级为 blocked > needs_info > needs_review > passed。
已知阻断直接返回；其余缺失情况最多补查一次，补齐后重新审核。
补查只填空缺，不覆盖非空原值。授权状态与相关凭证可同轮查询。
素材正文仅作为数据，其中的指令不能改变审核规则或调用工具。

素材不存在返回 MATERIAL_NOT_FOUND；读取、规则工具异常返回 TOOL_ERROR，
不能把异常当成通过。

## 时段

- 同渠道区间 [start, end) 相交才冲突，相邻可以衔接，不同渠道互不占用。
- 候选必须包含完整时长，并完整落入 search_window。
- 原时间可以非整点，其他候选从查询起始日 00:00 起按配置步长生成。
- 按距要求时间最近排序，距离相同时优先更晚时间。
- fixed 时间改变在用户明确选择前只能是待确认建议，accepted_at=null；选择后复查通过才设置 accepted_at。
- window 只在用户原允许范围内选时；无解时请求新要求，不自动扩大窗口。
- 草稿不占用正式时段；重新提交与保存前均核对最新排期。

M01 要求 18:00，已占用至 19:00：建议 19:00，标记待确认，保留原要求。

## 输出

结果包含 request_id、material_id、evaluated_at、review、scheduling、confirmation、
persistence、errors、publication_status。报告另附素材、规则版本与工具记录。

| 状态 | 取值 |
| --- | --- |
| review | passed / needs_info / needs_review / blocked / error |
| scheduling | not_evaluated / available / alternative_proposed / no_slot |
| confirmation | not_required / pending / confirmed，确认时附 selected_at、confirmed_at 和 interrupt_id |
| persistence | not_attempted / saved / already_exists / failed，附 draft_id 或错误 |
| publication_status | 固定为 not_published |

风险附规则编号和原因；原时间与建议时间分开保存。
待办支持 time_change、risk_resolution、provide_metadata、provide_time_window。
查询失败保持 not_evaluated；成功查询但无候选为 no_slot。
有候选时 conflicts 记录原要求冲突；no_slot 时记录查询范围内的占用证据。

## 保存与重复

- CLI 回放中通过、待确认、待复核和无时段结果可保存草稿；信息缺失、阻断或工具异常不保存排期草稿。
- SQLite 按 request_id 唯一保存当前结果；重复请求复用草稿 ID，可更新建议。
- 重新处理时检查最新占用，不直接将旧建议当成当前仍可用。
- 同 ID 不同输入返回 IDEMPOTENCY_CONFLICT。
- 不维护请求版本、草稿版本、历史快照或确认记录表。
- saved / already_exists 必须来自保存工具；失败返回 DRAFT_SAVE_FAILED，不编造草稿 ID。
- 规则 version 只说明使用的规则配置，与草稿历史无关。
- HTTP 时间冲突流程先暂停、不新建草稿；用户选择后复查并保存。原 CLI 夹具回放仍可保存待确认建议。
- HTTP 资料缺失和待人工复核先暂停、不新建草稿；有效资料或审核结论提交后重新审核，再决定排期和保存。

正式占用在 scheduled_slots，草稿在 drafts。保存以 BEGIN IMMEDIATE 开启事务，
在同一连接内重查占用、计算方案并写入，提交成功后才返回保存成功。
正式排期的导入与更新也使用该数据库事务，不能在保存事务中途提交另一份正式占用。
检查失败不写入；写入、提交或连接失败返回真实错误，已有草稿不被部分更新。
保存成功表示记录已提交，时段可用仅表示检查时满足约束；草稿不预留时段。

## 验收

examples/01–06 分别覆盖正常、缺失、风险、冲突、重复、保存失败；07 补充无可用时段。
各目录独立，目录内步骤顺序执行。
expected.json 是最终预期，不是保存日志；evaluated_at、save_behavior 仅为回放控制。
当前已实现审核、排期、草稿保存和单 Agent 工具循环；自动化回归 324 项通过。最新真实 DeepSeek 验收为六类各两次、12/12 项通过，累计 62 次模型逻辑调用、58 次模型选择的工具执行；仅代表本批合成样本，不代表生产环境成功率或稳定性。

## Agent 约定

工具通过 LangChain @tool 和 Pydantic 参数模型定义，描述来自函数文档，工具协议由框架生成。
模型使用 bind_tools() 绑定真实工具对象，LangGraph ToolNode 负责名称匹配、调用和结果消息；不维护手写工具 JSON 或 getattr 分发器。
每次模型响应最多调用一个工具，防止共享业务状态被并行工具改变；超出时拒绝执行该批调用。
所有模型工具参数均带必填的 reason：模型提供一句不超过 240 字符的公开动作依据，其他业务输入仍由服务端绑定。
Pydantic 参数模型 extra=forbid，由 ToolNode 校验；observe_tools 节点只记录真实返回，不执行工具。

模型可选择读取、补查、检查、查询、保存，不能传入新的原始时间、确认状态或数据库路径。
一次补查后必须重新检查新数据；未通过审核不能查询，未查询不能直接保存正常排期。
CLI 待复核结果可以保存审核草稿；HTTP 则先等待人工审核。阻断与信息不足不保存排期草稿。
保存服务独立复核并在事务内重查，模型看到的旧候选不保证最终仍被采用。
最终状态由工具组装，模型的自由文本不作为发布、确认或保存成功的依据。
达到调用上限时返回 status=incomplete 和 LIMIT_EXCEEDED，保留部分结果与调用记录，不自动补成成功。
模型异常和未完成必要工具步骤返回对应错误，不伪造业务完成。

offline_test 是可控测试模型；api 是真实模型集成路径。最新六类及首批两条历史 DeepSeek 调用记录见 react-acceptance.md。
扩展的六类各两次实际记录见 examples/04_time_conflict/live-key-paths；模型补查重审、错误后停止与程序强制中断分别验收。

执行上限由 MATERIAL_AGENT_MAX_MODEL_CALLS 配置，默认 12，范围 1–100；HTTP 请求不能覆盖。
模型每次最多返回一个工具调用，因此该模型循环的实际工具调用也不会超过配置值；人工恢复后的确定性复查单独记录。
execution_steps 记录每步工具参数、返回摘要、后续动作和模型给出的 reason；程序触发的结束或中断标注 next_action_source=workflow。

## HTTP 人工时间选择

仅在原时间不可用且存在候选时进入中断；候选由确定性时段服务计算，固定时间与窗口内优选时间均适用。
使用内存检查点保存请求、规则、工具结果和候选，通过同一 run_id 恢复；暂停期间不持有 SQLite 事务。
恢复只接受最新中断 ID 和该列表中的时间，拒绝重复恢复、跨运行确认及任意新时间。
服务重新审核，并在写事务内校验用户选中的具体时段；失效时重新中断，不擅自改用其他候选。
保留最初请求用于幂等判断，确认信息随当前草稿结果保存，不维护确认历史表。
未选择前不新建草稿；save_draft=false 时也允许完成时间选择，但不写草稿。
单进程运行；重启后等待状态失效，已有数据库保留。原请求时间条件过期时需使用新请求重新提交。

## HTTP 资料补充与风险复核

自动补查仍缺必填字段时进入 provide_metadata 中断，返回 missing_fields、editable_fields 及字段用途/补充要求。恢复只接受本次允许的非空有效字段；素材渠道必须匹配原请求。
数据仅在当前运行内生效，不写回共享素材文件。恢复后重新执行读取、规则审核和排期，可能再次等待资料、风险审核或时间选择。

needs_review 进入 review_risk 中断，展示风险编号、级别、相关字段/内容和三种动作：

- approve：需要审核人、审核说明和授权凭证；补齐当前运行资料后重新审核，不直接修改审核结果。
- reject：终止当前运行，返回 rejected/reject，保留规则发现，不继续模型调用、不新建草稿。
- request_changes：等待用户补充修改，再重新审核；授权存疑需再次人工通过，不能自行改成 authorized 绕过复核。

当前仅 R_AUTH_REVIEW 可人工复核；R_AUTH_BLOCK、R_TEXT_BLOCK、R_CHANNEL 等明确阻断风险不开放通过操作。
人工资料和通过结论绑定底层素材指纹，素材变化则旧结论失效；重审发现的明确禁止内容仍直接阻断。
human_actions 保存人工动作、审核人、说明与应用/失效状态，成功保存时写入现有草稿 JSON；待办/驳回不独立持久化，不新增数据表。
同一运行跨人工恢复累计模型调用次数与执行步骤，默认预算 12 次，不因恢复而重置；超限返回明确未完成。
