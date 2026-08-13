"""GOAI 十角色多 Agent 辩论运行时（LLM 仅产文字判断与证据引用）。

包含：
- llm_client：OpenAI 兼容接口客户端（默认 DeepSeek，stdlib urllib，零新增依赖）；
- cards：十角色 agent card 注册表；
- tools：白名单确定性工具注册表（只读、只返回 JSON）；
- runtime：两轮定向辩论编排与审计链。
"""
