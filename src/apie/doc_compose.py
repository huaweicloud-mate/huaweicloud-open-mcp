"""转换后文档组合根（doc_compose）：convert + correct 同一核心。

「转换后的 doc 必已纠偏」不变量的生产者单点（ADR-0001）：live_fallback
（运行时，缓存写入前 in-place）与离线管道 convert 阶段共同委托。service
不再持有纠偏入口——correct_doc_cow（运行时 COW）与 correct_api_result
（信封级）对已纠偏 doc 恒 no-op，属假想 seam，已删除；「缓存 doc 恒不改写」
由构造保证（纠偏发生在缓存写入之前，非消费方机制）。
"""

from typing import Any

from .convert_openapi2 import AuthDemotePolicy, convert_api
from .metadata_corrections import MetadataCorrections, correct_doc

__all__ = ["compose_doc"]


def compose_doc(raw: dict[str, Any], *, product: str, api: str,
                auth_demote: AuthDemotePolicy | None = None,
                corrections: MetadataCorrections | None = None) -> dict[str, Any]:
    """raw 元数据 → 转换 → 纠偏 → doc（唯一转换组合点）。

    auth_demote 透传 convert_api（认证头 required 归一）；corrections 透传
    correct_doc（op 级 + doc 级指针一次落位，幂等）。未命中纠偏条目时与
    convert_api 输出逐字节一致。
    """
    return correct_doc(convert_api(raw, auth_demote=auth_demote),
                       product, api, corrections)
