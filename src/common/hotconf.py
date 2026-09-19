"""配置文件热刷新内核（S23）：pull-on-access 探测 + 统一异步重载。

``HotFile`` 以 pull-on-access 方式探测配置文件变化（无定时器、无 watcher
线程，与 ``safety.policy_store.PolicyStore`` 热重载先例同款）：每次
``get()`` 持锁做至多一次 stat，stamp 相等即零文件 IO 返回缓存值。检测到
变化后在后台 daemon 线程重读解析并原子换值（stale-until-ready）——触发
刷新的请求拿到最近合法值，下一次调用见新值（最终一致语义）。

语义契约（对齐 PolicyStore，零内部依赖）：

- stamp = ``(st_mtime_ns, st_size, st_ino)``（``getattr`` 防 stat 替身缺
  属性；Windows/FAT 上 ino 可能为 0，退化为二元组比较）；
- **内容哈希防抖**：stamp 变化后先比对文件 sha256，内容未变（touch /
  备份工具扫描）仅推进水位，不解析、不触发 on_reload（防联动副作用风暴）；
  哈希与解析共用同一次文件读取，零额外 IO；
- **坏文件容错**：解析失败 / 文件短暂不可读 → 沿用最近合法版本 + WARNING
  （解析失败推进水位防刷屏，文件再次变更时自动重试）；恢复后自动采纳 +
  INFO；
- **构造期急切加载 fail-fast**：缺文件 / JSON 非法 / schema 非法直接抛出
  （与 load_opt_file 启动语义逐字一致）；
- **完成时 stamp 比对**：刷新期间文件再次变化则弃结果、清 pending，由下
  一次 get() 重新触发（后台化特有竞态——否则会「换入旧内容 + 盖上新水位」
  静默丢失一次变更）；
- **单飞守卫**：同一实例至多一个在飞刷新线程，pending 期间 get() 恒返回
  旧值（全进程至多 = 配置面数的并发线程，per-event daemon，非常驻池）。

锁纪律：单把 RLock 串行化探测 / 换值 / 回调；``on_reload`` 在持锁状态执
行——回调内禁止再获取其它锁（与 MemoryStore 的锁序固定为 HotFile →
store 单向，无反向路径）；回调异常被遏制（记 WARNING，不影响换值生效）。
``on_reload`` 为公开可写属性，仅限装配阶段（ToolService 构造期）注入，
有流量后勿变更。

已知边界（继承 PolicyStore）：HFS+/FAT 等长同 tick 编辑漏检一次；NFS 属
性缓存使探测延迟可达 ~60s（无正确性损坏）；多进程部署各进程独立探测。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any, Callable, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

StatFn = Callable[[str], Any]
ParseFn = Callable[[dict], T]
ReloadHook = Callable[[T], None]


def stamp_of(path: str, stat_fn: StatFn = os.stat) -> Any:
    """文件变化探测 stamp：(mtime_ns, size, ino)；getattr 防 stat 替身缺属性。"""
    st = stat_fn(path)
    return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))


def _read(path: str) -> tuple[bytes, str]:
    """读文件字节并计算内容哈希（解析与防抖共用同一次读取）。"""
    with open(path, "rb") as f:
        data = f.read()
    return data, hashlib.sha256(data).hexdigest()


class HotFile(Generic[T]):
    """配置文件热刷新 holder：小接口（唯一方法 ``get()``）+ 深实现。

    调用方只需知道：``get()`` 返回当前合法值（可能比磁盘旧一个刷新周期）；
    其余（探测、防抖、后台重载、降级、联动）全部藏在实现内。
    """

    def __init__(self, path: str, parse: ParseFn[T], *,
                 stat_fn: StatFn = os.stat,
                 on_reload: ReloadHook[T] | None = None) -> None:
        self._path = path
        self._parse = parse
        self._stat_fn = stat_fn
        self.on_reload = on_reload
        self._lock = threading.RLock()  # 序列化探测/换值/回调，杜绝并发撕裂
        self._pending = False
        self._missing = False
        self._thread: threading.Thread | None = None
        data, digest = _read(path)  # 缺文件/JSON 非法 → 透出（构造期 fail-fast）
        self._value: T = parse(json.loads(data))  # schema 非法 → 透出
        self._digest = digest
        self._stamp = stamp_of(path, stat_fn)

    # ---------- 公共接口 ----------

    def get(self) -> T:
        """返回当前合法值；至多一次 stat，stamp 相等零文件 IO。"""
        with self._lock:
            self._maybe_refresh()
            return self._value

    # ---------- 内部实现 ----------

    def _maybe_refresh(self) -> None:
        """持锁调用：探测变化并按需起后台刷新线程（单飞）。"""
        if self._pending:
            return
        try:
            stamp = stamp_of(self._path, self._stat_fn)
        except OSError:
            if not self._missing:
                logger.warning("配置文件暂时不可读 %s，沿用最近合法版本", self._path)
                self._missing = True
            return
        self._missing = False
        if stamp == self._stamp:
            return
        self._pending = True
        self._thread = threading.Thread(target=self._reload, args=(stamp,),
                                        name=f"hotconf:{self._path}", daemon=True)
        self._thread.start()

    def _reload(self, observed: Any) -> None:
        """后台刷新主体（锁外读盘解析，锁内落位）。"""
        try:
            data, digest = _read(self._path)
            if digest == self._digest:
                with self._lock:
                    self._stamp = observed  # touch：内容未变仅推水位
                    self._pending = False
                return
            value = self._parse(json.loads(data))
        except Exception as exc:
            with self._lock:
                logger.warning("配置文件变更后解析失败 %s：%s（沿用最近合法版本）",
                               self._path, exc)
                self._stamp = observed  # 推进水位防刷屏；文件再次变更时自动重试
                self._pending = False
            return
        self._apply(observed, digest, value)

    def _apply(self, observed: Any, digest: str, value: T) -> None:
        """换值落位（持锁）；完成时 stamp 比对，构建期间文件又变则弃结果。"""
        with self._lock:
            self._pending = False
            try:
                current = stamp_of(self._path, self._stat_fn)
            except OSError:
                return
            if current != observed:
                return
            self._value = value
            self._digest = digest
            self._stamp = current
            logger.info("配置文件热重载完成 %s", self._path)
            hook = self.on_reload
            if hook is not None:
                try:
                    hook(value)
                except Exception as exc:
                    logger.warning("on_reload 回调失败 %s：%s（换值已生效）", self._path, exc)

    def _join_pending(self, timeout: float = 5.0) -> None:
        """internal seam（测试同步点）：等待在飞刷新线程结束，不 sleep 轮询。"""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
