# 验收记录：当前结论与历史阶段

初期按用户选择仅做离线验收；2026-10-29 先补充两条真实 DeepSeek 路径，之后新增六类各两次真实关键路径运行，见本文后续记录。
当前真实批次为 **12/12 项验收通过、62 次模型逻辑调用、58 次模型选择的工具执行**，见 [实际汇总](../examples/04_time_conflict/live-key-paths/summary.json)。该结果仅对应本批合成样本，不代表生产环境成功率或稳定性。
实际运行 LangGraph、素材工具、审核规则、时段查询和 SQLite 保存；
历史离线记录中的模型决策由明确标注的 OfflineTestModel 提供，不能代替后续真实模型记录。
下方阶段记录按开发顺序保留，旧测试数量及“该阶段未进行真实调用”仅说明对应历史阶段；当前结论统一见 [评估文档](03-evaluation.md)。

## 结果

当前 324 项自动化测试通过，启用 -W error 时无警告；其中 75 项覆盖 HTTP 与人工中断。
当前 324 项已另在远程新克隆的 Python 3.12.4 干净环境中通过，安装及六类离线 HTTP 演示亦通过，见 [最新干净环境记录](../examples/04_time_conflict/clean-environment/summary.md)。早期 295 项安装验证保留为历史阶段记录。
步骤 6 完成时 236 项，HTTP 扩展后 257 项，时间选择中断扩展后新增 20 项。
框架工具注册改造新增 4 项，并将模型与工具协议测试迁移到 LangChain 接口。
依赖兼容检查通过。另通过独立 CLI 进程运行七类场景、九个请求步骤：

| 场景 | 决策 | 保存与草稿数量 |
| --- | --- | --- |
| 正常 | ready | saved，1 份 |
| 缺失信息 | request_info | not_attempted，0 份 |
| 规则风险 | block / request_confirmation | 阻断不存；待复核存草稿，共 1 份 |
| 时间冲突 | request_confirmation | 19:00 待确认，saved，1 份 |
| 重复提交 | ready / ready | saved / already_exists，同一草稿，1 份 |
| 保存失败 | failed | 事务回滚，failed，0 份 |
| 无可用时段 | request_info | 保存无时段草稿，1 份 |

机器可读记录：[validation-manifest.json](../examples/validation-manifest.json)。
记录中的 verification_mode=offline_test、real_model_tested=false 明确表示未测试真实外部模型。
保存失败场景的 CLI 退出码为 1，是预期的失败处理结果，验收本身通过。

## 已验证的边界

- 模型循环确实调用工具，没有把固定业务函数的输出冒充真实模型响应。
- 不允许改写绑定请求、传入确认标记、查询其他素材或调用发布工具。
- 未读取、未审核、未查询就尝试保存，会被拒绝；缺失信息须先尝试补查。
- 模型只输出“已保存”不能产生草稿；最终状态从工具返回组装，模型自由文本不作事实依据。
- 模型循环、工具调用次数受限；异常返回错误，不偷偷切换为离线模式。
- 保存时占用变化，以事务内重查结果为准；重复请求仍只保留一份。
- API Key 不出现在配置导出及模型错误信息中。
- 使用 HTTP Mock 验证 LangChain 模型集成的实际 HTTP 工具协议、工具结果回传及 DeepSeek 推理字段保留。
- 测试禁止外部网络连接，LangGraph 运行关闭外部 tracing。

## 验证范围

真实 DeepSeek 已有首批两条历史记录，以及最新六类各两次的 12 次验收。最新批次已覆盖人工补资料、风险有凭证通过、补查重审、工具错误和冲突及时段失效；人工驳回/要求修改、来源变化后的批准失效、保存故障及上限仍主要由离线测试覆盖。
七类场景及人工中断的确定性工具与服务已通过离线验收；真实模型验收范围以各批实际记录为准，未实测分支不声称已通过。
后续按用户要求增加 HTTP API，以及资料补充、授权存疑复核和时间选择中断；不增加页面、跨重启恢复、通用审批或多 Agent。

## HTTP API 验收（2026-10-29）

- 新增 21 项接口测试，使用 FastAPI TestClient 运行真实工作流和 SQLite，禁止外部模型连接。
- 覆盖七类场景、状态码、幂等冲突、失败回滚、草稿查询、过期时间、配置缺失和请求边界。
- 核对重启后正式排期与草稿不被初始化数据覆盖；模型客户端在退出和启动失败时关闭。
- 实际启动 Uvicorn，通过本机 TCP/HTTP 验证 /health、/schedule、/drafts/{request_id}、/openapi.json。
- 该阶段冲突请求建议 19:00、accepted_at=null、待确认且草稿已保存；现已改为先中断、选择后保存，见下。
- 实际 HTTP 记录：[http-api-acceptance.json](../examples/04_time_conflict/interactive/history/http-api-acceptance.json)。该记录 mode=offline_test，未调用真实供应商。
- 依赖检查通过，新增 FastAPI 0.162.0、Uvicorn 0.54.0，54 个安装包版本兼容。

## 时间选择中断验收（2026-10-29）

- 新增 20 项 HTTP 与中断测试，现有 HTTP 测试同步适配；累计 277 项通过，HTTP 模块 41 项。
- 实际 LangGraph interrupt() 暂停在 choose_time 节点，检查点包含可序列化的请求、规则、候选和工具结果。
- 原时段不可用时返回 waiting_for_human、run_id、interrupt_id 和完整候选列表，不新建草稿。
- 用户选择第二个候选 20:00 后保存，原始 18:00 请求及其幂等内容不变，确认信息随草稿结果保存。
- 所选时间被占用后，返回新中断 ID 和新候选；旧 ID、跨运行 ID、候选外时间及重复恢复均被拒绝。
- 覆盖双请求并发恢复只执行一次、窗口内优选冲突、模型批量调用不能越过中断、查询后保存前新增冲突、恢复时素材出现阻断、原请求过期及恢复保存失败回滚。
- 重启后等待中的内存运行失效，已有 SQLite 数据保留；没有新增数据库表。
- 实际启动 Uvicorn，用本机 HTTP 验证暂停零草稿、选择第二项保存、占用变化后二次中断与再次选择保存。
- 机器记录：[http-interrupt-acceptance.json](../examples/04_time_conflict/interactive/history/http-interrupt-acceptance.json)，mode=offline_test，真实模型未测。
- 运行说明见 http-api.md。需要重启原有服务进程并刷新 /docs 才能看到新增接口。

## 框架工具注册改造（2026-10-29）

- 移除手写 TOOL_SCHEMAS、tool_schema()、AgentTools.invoke() 及手写调用分发循环。
- 五个工具改用 @tool 和 Pydantic 参数模型；模型 bind_tools() 后调用 invoke()，图中的 ToolNode 负责实际执行和 ToolMessage 回传。
- 使用 LangChain 标准 AIMessage、ToolMessage、SystemMessage、HumanMessage 及 add_messages 管理消息。
- DeepSeek 使用 ChatDeepSeek 集成，通用兼容接口使用 ChatOpenAI；DeepSeek 子类只补充 reasoning_content 回传，不处理工具协议或执行。
- 当前 LangChain 对空参数工具会跳过参数解析，已通过 ToolNode 的标准校验钩子保证额外字段被拒绝。
- 单次响应只允许一个工具调用；批量工具调用整体拒绝，避免并行执行越过审核和人工确认边界。
- 新增真实 BaseTool 绑定、无效工具 JSON 和两种模型集成构建测试；原业务、HTTP 和中断用例均通过，累计 281 项。
- HTTP Mock 验证实际框架生成的五工具协议、parallel_tool_calls=false、四轮调用和 DeepSeek 推理字段回传，无真实供应商调用。
- 实际 Uvicorn 端口通过冲突暂停、选择第二个候选、复查保存及草稿查询。记录：[framework-tools-acceptance.json](../examples/04_time_conflict/interactive/history/framework-tools-acceptance.json)。
- 58 个安装包依赖兼容检查通过；无需修改既有 .env，更新代码后重启服务即可。

实现参考 [LangChain 工具文档](https://docs.langchain.com/oss/python/langchain/tools) 和 [ChatDeepSeek 集成](https://docs.langchain.com/oss/python/integrations/chat/deepseek)。

## 实际模型验收记录支持（2026-10-29）

- 响应新增 model_steps，记录各轮模型实际观察到的工具返回、工具调用、结束响应、响应 ID、模型标识和可用 token 用量；不记录内部思维链。
- 增加 examples/verify_react.py：先要求服务 mode=api，再提交默认 M02 缺失资料请求，保存原始请求、响应、逐轮摘要和验收检查。
- 通过关联工具调用 ID 核对“观察缺失后选择补查，观察补查为空后结束”，并要求 DeepSeek 标识、无错误及真实未保存/发布状态。
- 离线、只有工具名称、观察与调用 ID 不对应或模型调用失败，均不能判为真实模型验收通过。
- 新增 5 项相关测试，含实际 LangChain HTTP Mock 协议、HTTP 日志字段和错误证据拒绝；累计 286 项测试通过。
- 修正示例目录枚举，忽略 Python 缓存目录；七类九步示例静态校验及依赖检查通过。
- 该阶段只准备并测试记录功能，没有发起真实模型调用；之后的首批两条及最新 12 次实际验收均在后续章节留证。
- 使用步骤见 react-acceptance.md。

## 两条真实决策路径与上限验收（2026-10-29）

- 供应商 DeepSeek、模型 deepseek-flash、mode=api，使用本地已配置密钥实际调用，无内部思维过程导出。
- 正常路径：M01 审核返回 passed，模型下一步选择 query_slots；看到 available 后结束。4 次模型调用、3 次工具调用。
- 缺失路径：M02 审核返回 needs_info，模型下一步选择 supplement_material；看到 empty 后结束并请求补充。4 次模型调用、3 次工具调用。
- 模型在 reason 参数中提供简短公开依据；execution_steps 将实际参数、返回摘要、后续动作和依据对应保存，模型/程序流转来源分别标注。
- 两条真实记录、原始请求/响应及逐项校验已纳入 [交付记录目录](acceptance-runs/20261029-145149-53d9cebd/summary.md)，两条路径检查均通过。
- 首轮尝试中 M02 返回 MODEL_ERROR（ValueError），未执行工具；失败原始响应保留在 data/react-evidence/20261029-144807-bdd8c9f5，不计为成功记录。后续强化单轮提示及无效响应记录后，两条路径重新运行通过。
- MATERIAL_AGENT_MAX_MODEL_CALLS 默认 12，模型每轮至多一个工具；超限明确返回 incomplete/LIMIT_EXCEEDED/HTTP 409。
- 独立 [离线上限运行记录](acceptance-runs/20261029-145149-53d9cebd/execution-limit-offline/summary.md) 将上限设为 2，实际 2 次模型、2 次工具后停止，零草稿。该记录不冒充真实模型路径。
- 本次新增 9 项测试，累计 295 项通过，含上限 1/2/3 的 HTTP 未完成状态与模型公开依据参数校验。
- 实际模型验收使用临时端口及独立数据库，未中断已有服务，也未修改原有草稿。

## 资料补充与授权人工复核中断（2026-10-29）

- 新增 29 项测试，完整回归 324 项通过，HTTP 与人工中断合计 75 项；实际输出见 examples/test-results.txt 和 test-results.xml。
- 自动补查仍缺信息时使用 provide_metadata 中断，展示字段用途与补充要求；无效输入不会消费中断，有效输入后重新审核与排期，支持继续转入时间选择。
- 授权未知使用 review_risk 中断，支持带凭证通过、驳回、要求修改后重新补充与审核。明确禁止的正文、denied 和渠道冲突直接阻断。
- 人工资料限定当前 run_id，共享来源文件不变；来源变化使旧资料/批准结论失效。已保存草稿包含有效资料及人工动作，未引入额外表或独立审批系统。
- 跨补充和复核恢复累计模型调用预算，不重置上限；测试覆盖 4/5 次预算用尽后的 incomplete/409。
- `python examples/record_validation.py` 实际回放七类九步 CLI、12 个原有时间交互事件，以及新增 4 个资料补充事件和 12 个人工审核事件，全部检查通过。见 examples/02_missing_metadata/human 和 examples/03_rule_risk/human。
- 该阶段先使用离线模型验证人工流程；随后补资料恢复和风险有凭证通过均已各进行两次真实调用，见下一节。待办使用内存检查点，服务重启后需重新提交。

## 扩展真实验收与完整演示（2026-10-29）

- 新增 examples/verify_key_paths.py：默认离线，--live 才读取 .env 调用供应商；实际启动隔离 Uvicorn、使用独立合成数据和 SQLite，所有请求与夹具操作留证，未改已有服务或生产接口。
- 新批次六类各两次，12/12 验收通过，共 62 次模型逻辑调用、58 次模型选择工具执行。记录位于 [examples/04_time_conflict/live-key-paths](../examples/04_time_conflict/live-key-paths/summary.md)，全部尝试保留，无自动场景重跑。
- 冲突时返回候选和中断；所选时间失效后新中断、旧 ID 409、再次选择后保存。复查为程序职责，不伪称模型主动换时间。
- 两次补查成功后，模型观察 ok 选择 check_rules，再观察 passed 选择 query_slots；两次补查工具错误后，模型下一轮观察 error 并返回无工具调用，业务失败且零草稿。
- 人工资料补充及有凭证风险通过均各两次真实恢复成功。驳回、要求修改、保存故障与上限仍主要由离线测试覆盖。
- 本批验收失败为 0；两次预期 TOOL_ERROR/HTTP 500 是故障处理验收通过，不是保存成功；小样本比例不代表生产稳定性。
- 默认离线运行器的六类也已实际跑通，完整 pytest 回归仍为 324 项通过，未增加业务架构或持久化检查点。
- README 调整为简介、架构、快速启动、核心演示、验证结果；[demo.md](demo.md) 提供一条命令和 Swagger 手动演示及模块讲解。

## 当前版本干净环境验证（2026-10-29）

- 从 GitHub main 的 `06fbdf8f399dfb03d397bdad72e083b7a16caa16` 浅克隆，未复制 .env、旧虚拟环境或运行数据库。
- 使用 Python 3.12.4 创建新的 venv，禁用系统和用户级包复用，移除 MATERIAL_AGENT_* 环境变量影响，重新安装 requirements.txt 的 58 个锁定包；pip check 无依赖冲突。
- 全量 `pytest -q -W error` 通过 324 项，其中 HTTP 与人工中断 75 项；实际输出、逐项 XML、依赖安装和 pip freeze 均归档。
- 在该环境运行 examples/verify_key_paths.py，实际启动 Uvicorn，通过六类离线 HTTP 演示，覆盖正常保存、冲突及选择失效、补查重审、补资料恢复、风险人工通过恢复和工具错误停止。
- 本次 mode=offline_test，未调用外部模型；不改变既有真实批次的 12/12、62 次模型调用和 58 次模型工具执行统计。
- [汇总](../examples/04_time_conflict/clean-environment/summary.md)、[机器可读记录](../examples/04_time_conflict/clean-environment/summary.json)、[完整测试](../examples/04_time_conflict/clean-environment/pytest.txt)、[HTTP 实际记录](../examples/04_time_conflict/clean-environment/http-demo/summary.json)。
