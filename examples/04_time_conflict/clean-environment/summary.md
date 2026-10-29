# 当前版本干净环境验证

2026-10-29 从 GitHub main 提交 `06fbdf8f399dfb03d397bdad72e083b7a16caa16` 重新克隆，使用 Python 3.12.4 创建独立虚拟环境；副本没有 .env、旧虚拟环境和运行数据库。禁用系统和用户级包复用，移除 MATERIAL_AGENT_* 环境变量影响。

| 检查 | 实际结果 | 原始记录 |
| --- | --- | --- |
| 远程新克隆 | 成功，提交与来源树记录在汇总中 | [clone.txt](clone.txt)、[summary.json](summary.json) |
| Python 3.12.4 新建环境 | 成功，不使用系统/用户级依赖 | [create-venv.txt](create-venv.txt)、[python.txt](python.txt) |
| 安装 requirements.txt 锁定的 58 个包 | 成功，退出码 0 | [install.txt](install.txt)、[freeze.txt](freeze.txt) |
| pip check | No broken requirements found | [pip-check.txt](pip-check.txt) |
| pytest -q -W error | **324 项通过**，含 75 项 HTTP 与人工中断测试 | [pytest.txt](pytest.txt)、[pytest.xml](pytest.xml) |
| 实际 Uvicorn / HTTP 演示 | **六类全部通过** | [http-demo.txt](http-demo.txt)、[场景汇总](http-demo/summary.md) |

HTTP 演示使用 offline_test，实际执行 LangGraph、注册工具、Uvicorn、HTTP 接口与 SQLite，验证正常保存、冲突及时间失效后再中断、补查成功重审、人工补资料恢复、风险人工通过后恢复和工具错误停止。每个场景保存实际请求、原始响应、事件和 Trace。

这是干净安装与业务流程复现证据，没有发起真实模型调用，不增加既有真实 DeepSeek 的 12 次样本。离线演示中的 model_calls 指测试模型的逻辑轮数。工具错误场景以明确失败并正确停止为通过标准，不表示该业务请求保存成功。

既有真实 DeepSeek 的 12/12 仅代表对应合成样本验收结果，不代表生产环境成功率或稳定性；原有真实记录保持不变。

来源提交中的业务代码和 requirements.txt 与本次验证后的工作区一致；本次仅追加文档和验证材料。完整命令、源代码/依赖指纹、Python 信息、隔离条件和退出码见 summary.json。复现步骤见 [评估文档](../../../docs/03-evaluation.md)。
