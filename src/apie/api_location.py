"""ApiLocation：一个操作在转换后 OpenAPI doc 中的命名位置（CONTEXT.md C 接缝）。

冻结命名值 ``(doc, path, method, op)`` + operationId 查找单点（精确 + 大小写
不敏感，无子串兜底——远端按 exact name 查询，无跨文件歧义）。此前该 4 元组
以裸 tuple 在 memory_store/live_fallback/catalog/service/api_docs/format_
api_detail 间流动、查找逻辑住在 live_fallback 私有函数——按命名值传递后，
消费方拿命名属性，查找语义只有一份。NamedTuple 形态保持既有元组解包/
索引兼容（缓存条目与执行接缝的通行值）。
"""

from __future__ import annotations

from typing import Any, NamedTuple


class ApiLocation(NamedTuple):
    """一个操作在转换后 OpenAPI doc 中的位置（缓存条目与执行接缝的通行值）。"""

    doc: dict[str, Any]
    path: str
    method: str
    op: dict[str, Any]

    @classmethod
    def find(cls, doc: dict[str, Any], api_name: str) -> ApiLocation | None:
        """按 operationId 在 doc 中定位操作（exact + 大小写不敏感），首个命中。"""
        if not doc:
            return None
        target = (api_name or "").lower()
        for path, path_item in (doc.get("paths") or {}).items():
            for method, op in path_item.items():
                if not isinstance(op, dict):
                    continue
                opid = op.get("operationId")
                if opid == api_name or (opid and opid.lower() == target):
                    return cls(doc=doc, path=path, method=method, op=op)
        return None
