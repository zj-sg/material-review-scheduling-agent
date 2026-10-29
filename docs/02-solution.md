# 02 方案与设计取舍

## 调用流程

HTTP 入口为 [src/api.py](../src/api.py)，命令行入口为 [src/main.py](../src/main.py)。
HTTP 请求绑定服务端素材、规则和数据库，然后由 SchedulingRuns 执行单个 Agent，或等待资料补充、风险审核和时间选择。

```mermaid
flowchart TD
    A[业务请求] --> M[模型观察上下文并选择工具]
    M --> T[ToolNode 校验参数并执行]
    T --> O[记录实际工具返回]
    O -->|继续处理| M
    O -->|自动补查后仍缺信息| I[interrupt 展示缺失字段与用途]
    I --> P[用户补充资料并校验]
    P --> M
    O -->|授权存疑| R[interrupt 展示风险与审核选项]
    R -->|通过且有凭证| M
    R -->|要求修改| I
    R -->|驳回| X[终止且不新建草稿]
    O -->|明确禁止| B[直接阻断]
    O -->|原时间不可用且有候选| H[interrupt 暂停并返回候选]
    H --> U[用户选择具体时间]
    U --> C[重新审核并在事务内复查所选时间]
    C -->|已被占用| H
    C -->|仍可用| S[按请求保存草稿]
    O -->|已保存、模型结束或错误| E[输出真实结果与 Trace]
    S --> E
```

模型执行骨架允许条件路由；具体工具调用来自模型响应，而不是预设动作日志。
[workflow.py](../src/agents/workflow.py) 将实际 ToolMessage 送回模型，未完成、错误或调用上限不会被自动补成成功。

## 真实动态决策

两条实际 DeepSeek 运行使用同一工作流、同一套工具，只改变素材 ID：

| 工具观察 | 模型实际选择 | 证据 |
| --- | --- | --- |
| M01 check_rules=passed | query_slots，观察 available 后结束 | [正常 Trace](../examples/01_normal/live/trace.json) |
| M02 check_rules=needs_info | supplement_material，观察 empty 后结束并请求补充 | [缺失 Trace](../examples/02_missing_metadata/live/trace.json) |

每个工具都有 required reason 参数，记录模型给出的简短动作依据。模型的内部 reasoning_content 只由模型集成按协议回传，不导出到交付 Trace。
时段候选由 SlotSearchService 计算，审核由 RuleService 校验；模型选择业务动作，程序约束事实、权限和保存结果。
三类强制人工中断本身不是模型决策证据，因此单独作为业务工作流验收。上表为首批记录；新增六类各两次真实运行，已覆盖补查成功后重审、工具错误后模型结束、资料补充和风险通过后的恢复，见 [评估文档](03-evaluation.md)。

## 模块职责与框架使用

以下为实际安装验证版本，完整传递依赖锁定在 requirements.txt，环境快照见 [environment.json](../examples/environment.json)。

| 组件 | 版本 | 本项目使用的功能与职责 |
| --- | --- | --- |
| Python | 3.12.4 | 程序运行、标准库 SQLite、时间与文件读取 |
| Pydantic / pydantic-settings | 2.14.1 / 2.16.1 | 请求和跨状态校验、严格工具参数、环境变量、密钥不导出 |
| LangGraph | 1.2.18 | StateGraph、ToolNode、add_messages、interrupt/Command、内存检查点 |
| langchain-core | 1.7.0 | @tool、BaseTool、AIMessage/ToolMessage 与 Runnable 接口 |
| langchain-deepseek / langchain-openai | 1.1.1 / 1.7.3 | bind_tools、模型调用、工具协议转换；DeepSeek 集成保留推理协议字段 |
| OpenAI SDK | 2.54.0 | LangChain 集成的底层兼容 API 客户端，不手写工具分发 |
| FastAPI / Uvicorn | 0.162.0 / 0.54.0 | HTTP 校验、Swagger 与本地服务 |
| pytest | 9.2.0 | 可执行回归测试与验证材料生成 |

框架承担工具注册、协议解析、消息管理和暂停恢复。项目自行实现的部分是业务规则、候选计算、工具使用顺序约束、保存事务、请求幂等和验收记录。
DeepSeekChatModel 子类只补齐集成库遗漏的 reasoning_content 回传，不重写工具调用协议。

## 数据、事务与确认

- SQLite 只有 scheduled_slots 和 drafts 两张表，后者按 request_id 唯一。
- 正式占用仅在表首次创建时由合成文件导入，服务重启不会覆盖已经维护的占用。
- BEGIN IMMEDIATE 事务内重查正式占用，再计算、写入和提交；成功状态在提交后才返回。
- 原请求按规范化内容比较，用户接受的时间另存入确认结果，不覆盖原始硬性要求。
- 恢复只接受本次中断 ID 和已提供的候选；所选时间失效时再次中断，不能擅自换一个。
- 待确认草稿不预留正式时段。草稿保存成功和未来仍可用是不同事实。
- 人工资料使用 RunMaterialSource 在当前运行内覆盖缺失字段；风险通过需要审核人、说明和凭证，再运行确定性审核，不直接覆盖审核状态。
- 覆盖值和通过结论绑定素材内容指纹；底层素材变化后失效，重新检查新风险。共享素材文件保持原样，成功保存时将有效资料和人工记录写入现有草稿 JSON。
- 请求修改先进入资料补充，再回到审核；驳回只终止当前运行。当前只有授权存疑可复核，明确禁止的规则没有通过入口。

## 取舍

用单 Agent 和确定性工具代替多 Agent；业务规则可独立测试，模型错误与存储错误更容易定位。
用 SQLite 代替独立数据库服务，保持笔试项目容易运行；明确单进程内存恢复限制。
用合成资料替代外部素材平台，补查为可信本地数据读取，不假称实现了外网检索或授权真实性鉴定。
保留离线模式用于业务回归，但只有 mode=api 的实际调用能证明模型执行路径。
不增加不必要的版本历史、前端和通用审批状态机；实际范围由需求决定。

MATERIAL_AGENT_MAX_MODEL_CALLS 默认 12、范围 1–100。每轮至多一个工具，达到上限时 HTTP 返回 409、status=incomplete、LIMIT_EXCEEDED。
同一运行各次人工恢复后的模型调用累计共用预算；model_steps 和 execution_steps 追加记录并标注 cycle，human_actions 保存人工动作。
该计数是逻辑模型调用；底层 SDK 的有限网络重试不作为新的业务决策，也不作为 ReAct 达标证据。

交付目录参考用户指定的 [agent-campus-submission-example](https://github.com/lwz1357/agent-campus-submission-example)，只借鉴材料组织，不以示例的离线 Mock 作为本项目模型决策的达标依据。
