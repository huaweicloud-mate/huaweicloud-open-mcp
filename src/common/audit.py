"""审计 NDJSON 写入器（第1层，零外部依赖）。

AuditSink 是审计持久化的接缝：生产用 NdjsonAuditSink（每事件一行 JSON、
best-effort 永不抛出——Exception 级口径，KI/SE 停机信号按全局重抛策略
传播例外），未配置审计用 NullAuditSink，测试注入内存替身。
sink 只拥有信封（ts）与持久化语义；事件 payload 的 schema 由
build_audit_event 定义（对 verifier 的已发布契约：tool/input/ok）。
audited 装饰器供各模式 service 复用（openapi/discover/data 同构），
契约：self.config.audit_sink 提供 sink（None=零开销跳过）。

会话归因（ADR-0003，HTTP 多会话）：_audit_write 在写事件单点从 ambient
contextvar（common.sessions）读会话键——键非 None 时事件顶层附加 ``session``
字段（多会话部署的「谁授予/谁执行」归因；input 快照纯净性不动）；stdio /
InMemoryTransport 恒 None 恒不加键（NDJSON 逐字节回归红线）。
"""

import functools
import inspect
import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, overload

from common.sessions import current_session_key

logger = logging.getLogger("common.audit")

_F = TypeVar("_F", bound=Callable[..., Any])


def build_audit_event(tool: str, input_args: Mapping[str, Any],
                      result: Any) -> dict[str, Any]:
    """审计事件 payload（对 verifier 的已发布契约）：tool/input/ok；ts 由 sink 注入。

    ok 取 result 的 ok 字段（缺失视为成功）；input 为调用方显式入参快照
    （不含默认值，对齐 agent 侧 trace 口径）。
    """
    ok = bool(result.get("ok", True)) if isinstance(result, Mapping) else True
    return {"tool": tool, "input": dict(input_args), "ok": ok}


@overload
def audited(fn: _F) -> _F: ...
@overload
def audited(*, name: Literal["connect_mcp_server", "call_server_tool",
                             "disconnect_mcp_server"]) -> "Callable[[_F], _F]": ...
def audited(fn: _F | None = None, *,
            name: Literal["connect_mcp_server", "call_server_tool",
                          "disconnect_mcp_server"] | None = None
            ) -> Any:
    """工具方法审计装饰器：每次调用经 self.config.audit_sink 记一条事件。

    input 为绑定后的显式入参（不含 self 与默认值）；方法抛异常时记 ok=False
    并抛出（异常路径捕获 BaseException——asyncio.CancelledError 等逃逸
    Exception 的取消类同样入账；_audit_write 本体 best-effort（捕获
    BaseException 记 **ERROR+栈**——违反契约的 sink 不可劫持原异常、
    成功路径的已完成结果不被审计失败摧毁（第七轮：此前仅异常路径有
    护栏、成功路径裸奔且 except Exception 挡不住 BaseException 级
    违约——三方检视实证后统一收进 _audit_write 单 owner；第八轮
    ERROR 升格+KI/SE 重抛）。**KeyboardInterrupt/SystemExit 窗口例外**
    （第八轮 carve-out，第九轮补锚点）：sink 抛 KI/SE 时重抛——异常
    路径下原异常降级 ``__context__``（可恢复）而 KI/SE 成为顶层异常、
    成功路径下已完成结果被 KI/SE 替换——重抛前经守卫记 ERROR 锚点，
    该病态路径的事件丢失可 grep 定位）。
    2026-10 第五轮：discover connect/call_tool 直挂，与 openapi service
    层同层同语义）。
    async 方法按目标分流装饰（装饰期 iscoroutinefunction 判定（经
    ``getattr(fn, "__func__", fn)`` 解包 staticmethod/classmethod 形态），
    双 wrapper）：async 目标得到真正的 ``async def`` wrapper——
    ``iscoroutinefunction`` 保持 True，签名经 functools.wraps 保持。修复的
    可达机制为**审计时序**：同步 wrapper 在协程创建期即写 ok=True（操作
    未完成）、异常/取消路径无事件；SDK is_async_callable 误判
    （run_sync 产出未 await 协程）为本仓库接线（工具面注册的是闭包、
    service 方法从不直接注册）不可达的次级防御注记。异常/取消路径
    同样入账后重抛。
    name 覆盖事件 tool 字段（缺省=方法名，``Literal`` 收紧到已注册工具面
    名——拼写错误 mypy 捕获，防 NDJSON tool 字段静默错标；空白串装饰期
    拒绝）：service 方法名与 MCP 工具面名不一致时用工具面名（discover
    connect→connect_mcp_server、call_tool→call_server_tool、
    disconnect→disconnect_mcp_server——NDJSON tool 字段恒为工具面名，
    trace 消费者（harbor verifier）按工具面名匹配）。
    契约：宿主 service 须暴露 config.audit_sink（None=零开销跳过）。

    用法：@audited / @audited(name="tool_face_name")
    """
    def decorator(fn: _F) -> _F:
        if name is not None and not name.strip():
            raise ValueError("@audited(name=...) 不接受空白串（tool 字段不可空）")
        sig = inspect.signature(fn)
        tool = name or fn.__name__

        if inspect.iscoroutinefunction(getattr(fn, "__func__", fn)):
            @functools.wraps(fn)
            async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    bound = sig.bind(self, *args, **kwargs)
                    input_args = {k: v for k, v in bound.arguments.items() if k != "self"}
                except TypeError:
                    input_args = {}
                try:
                    result = await fn(self, *args, **kwargs)
                except BaseException:
                    _audit_write(self, tool, input_args, {"ok": False})
                    raise
                _audit_write(self, tool, input_args, result)
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                bound = sig.bind(self, *args, **kwargs)
                input_args = {k: v for k, v in bound.arguments.items() if k != "self"}
            except TypeError:
                input_args = {}
            try:
                result = fn(self, *args, **kwargs)
            except BaseException:
                _audit_write(self, tool, input_args, {"ok": False})
                raise
            _audit_write(self, tool, input_args, result)
            return result

        return wrapper  # type: ignore[return-value]

    if fn is None:
        return decorator
    return decorator(fn)


def _audit_write(host: Any, tool: str, input_args: Mapping[str, Any],
                 result: Any) -> None:
    """经宿主 config.audit_sink 记一条事件（**本体 best-effort**，未配置
    sink 零开销跳过；host 违反 config 契约在 try 外 AttributeError——
    fail-fast，与 sink 违约分型不混入 best-effort）。

    捕获 BaseException 记 **ERROR（含栈）**——违反「永不抛出」契约的 sink
    不可：① 替换被装饰方法的原始异常（含 CancelledError 传播——重抛
    无条件）；② 摧毁已完成操作的成功结果（副作用已发生，agent 不可因
    审计失败误判失败而重试重复执行）。成功/异常全路径由此单点承担
    （第七轮收口，覆盖装饰器四个写入点与 server 层工具面拒绝补记两个
    直调点）。**KeyboardInterrupt/SystemExit 除外重抛**（第八轮：解释器
    停机信号不是 sink 违约——吞掉会把 SIGINT 转成「操作成功」且需二次
    信号；**第九轮：重抛前经守卫记 ERROR 锚点**；**第十轮修格式缺陷**
    ——绑定异常传入（KI/SE 可区分 + exc_info 栈，此前占位符悬空打出
    字面 %s 且零异常身份）；sink 抛 KI/SE 的病态路径原异常降级
    ``__context__`` 可恢复，日志留痕是唯一诊断面；CancelledError 为
    协程取消语义仍属吞没集合——同步 sink 写入窗口不可能收到协程取消，
    异步路径的取消来自被装饰方法本身而非本缝）。
    ERROR 级 + exc_info：审计是本安全模型声明的唯一硬保障，事件永久
    丢失必须在日志可 grep 定位（NDJSON 流中的静默空洞诊断锚点）；
    WARNING 与运维噪音同级不可区分（第八轮升格；**第九轮补齐生产通道**
    ——NdjsonAuditSink 内部 catch 同步升 ERROR，磁盘满/权限等真实丢失
    原被内部 WARNING 吞没、_audit_write 的 ERROR 分支对生产 sink 不可
    达）。logger 调用自身经 ``_guarded_log`` 嵌套守卫（logging Filter
    抛错不可重新打开本护栏刚关闭的洞；守卫收窄 Exception——logging
    窗口的 KI/SE 按外层重抛策略传播，第九轮：第八轮 BaseException
    守卫会吞掉 logging 窗口停机信号、与 sink 窗口策略相反）。
    会话归因在此单点：键非 None 时事件顶层加 ``session``（写侧 provenance，
    不进 input 快照、不进 build_audit_event 契约形状）；None 恒省略。
    """
    sink = getattr(host.config, "audit_sink", None)
    if sink is None:
        return
    try:
        event = build_audit_event(tool, input_args, result)
        session = current_session_key()
        if session is not None:
            event["session"] = session
        sink.record(event)
    except (KeyboardInterrupt, SystemExit) as exc:
        # 停机信号重抛（第九轮起；第十二轮签名收口）：重抛前经守卫记
        # ERROR 锚点（f-string 预格式化——_safe_str(exc) 入消息、栈走
        # exc_info），KI/SE 窗口的事件丢失同样可 grep 定位（sink 抛 KI/SE
        # 的病态路径原异常降级 __context__ 可恢复，日志留痕是唯一诊断面）
        _guarded_log(f"audit write aborted by shutdown-signal exception "
                     f"(event lost): {_safe_str(exc)}", exc=exc)
        raise
    except BaseException as exc:
        _guarded_log(f"audit write failed (best-effort suppressed): "
                     f"{_safe_str(exc)}", exc=exc)


def _safe_str(value: BaseException | object) -> str:
    """``str()`` 的防御形态（对齐 ``traceback._safe_string``——stdlib
    3.11+ 对格式化边界的现行防御，bare-except 全量吞）：病态 ``__str__``
    抛**任何**异常（含 KeyboardInterrupt 等 BaseException）时返回占位文本。

    第十二轮定向检视三方会合的收尾：f-string 预格式化把 ``str(exc)``
    求值从守卫窗口（r11 延迟插值在 ``Handler.emit`` 内被 handleError
    吞）移到 except 块内的急切插值——病态 ``__str__`` 会从 ``_audit_write``
    逃出（成功路径摧毁已完成结果/失败路径替换原异常/KI/SE 不再重抛，
    即 r7/r8 声明不变量的破口）。第 13 轮闭包检视把本函数的防御边界
    收全：except BaseException（``str()`` 能抛的一切在此全捕——
    ``traceback._some_str`` 已于 3.11 被 ``_safe_string`` 取代（3.10 仍
    在但 fallback 带类型名），原 r12 引用失实；``_safe_string`` 为
    bare-except 口径，exc_info 通道的 stdlib 防御同为全量）；fallback
    为静态文本不触碰 value 派生数据（类名等诊断由 exc_info 栈通道
    承载——3.10 的 ``_some_str`` 带类型名形态是**有意的**偏离，避免
    派生数据缺口），故本函数不可再抛——插值点不可成为逃逸口。
    """
    try:
        return str(value)
    except BaseException:
        return "<unprintable>"


def _guarded_log(message: str, *, exc: BaseException | None = None) -> None:
    """守卫的 ERROR 日志（三个 ERROR 站点单点；**第十二轮签名收口**——
    f-string 预格式化：message 恒为已格式化纯文本，exc 只走 ``exc_info``
    通道不作插值参数，参数-格式失配缺陷族（第九轮悬空 %s / 第十轮参数序
    转置 / 第十一轮占位符计数算术）在本函数**不可表示**——连续三轮
    同族修复后按「不变量由签名承载而非纪律」收口，三方实证同族形态）。
    急切插值的对偶风险（``str(exc)`` 在调用点求值、守卫窗口外）由
    ``_safe_str`` 防御——message 的构造同样不可成为逃逸口（定向检视
    三方会合收尾）。

    logger 抛 Exception 级错误不可重新打开护栏、不可违反 record 的
    Exception 级永不抛出语义；KI/SE 停机信号在 logger 窗口按外层重抛
    策略传播。守卫收窄到 Exception——KI/SE 在两窗口一致传播、Exception
    级一致吞没；其余 BaseException（GeneratorExit/CancelledError 等）
    在 logger 窗口传播而在 sink 窗口吞没——非严格对称，logger 调用为
    同步窗口收不到协程取消。
    """
    try:
        logger.error(message, exc_info=exc)
    except Exception:  # logging 守卫：Filter/Handler 违约不外溢
        pass


class AuditSink(Protocol):
    def record(self, event: Mapping[str, Any]) -> None:
        """追加一条审计事件。实现永不抛出（**Exception 级口径**——KI/SE
        停机信号按全局重抛策略传播例外；默认 sink 经 _guarded_log 守卫，
        logging Handler/Filter 抛出的其余非 Exception BaseException 可
        逃出 record 但由 _audit_write 外层网吞没；best-effort，失败仅告警）。"""
        ...


class NdjsonAuditSink:
    """NDJSON 文件 sink：每事件一行，追加写，逐行落盘保证读方可见。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: Mapping[str, Any]) -> None:
        try:
            line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event},
                              ensure_ascii=False)
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            # 生产通道 ERROR（第九轮升格）经 _guarded_log（第十轮收编、
            # 第十二轮 f-string 预格式化——参数-格式失配不可表示）；裸
            # logger.error 遇 raising Filter 会逃出 record 违反 Exception 级
            # 永不抛出语义，且经 _audit_write 二次守卫打同一坏 logger
            # 被吞 → 磁盘满的真实丢失产出零日志
            _guarded_log(f"audit write failed path={self._path}: {_safe_str(e)}",
                         exc=e)


class NullAuditSink:
    """未配置审计时的空实现。"""

    def record(self, event: Mapping[str, Any]) -> None:
        return None


def sink_from_path(path: str | Path | None) -> AuditSink | None:
    """路径为空返回 None（不审计）；否则返回文件 sink。"""
    if not path:
        return None
    return NdjsonAuditSink(path)
