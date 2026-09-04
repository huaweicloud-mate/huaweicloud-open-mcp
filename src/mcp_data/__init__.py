"""mcp_data：data 模式（DataFusion 只读 SQL 分析）。

外部接缝：engine.run_query（表注册/只读守卫/执行/规范化/截断，单函数深接口）
         + service.query_data（audit 信封与错误翻译）。
query_data 不受 safety policy 约束（本地计算工具，口径见 AGENTS.md「校验规则」）。
"""
