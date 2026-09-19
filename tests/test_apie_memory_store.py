"""apie.memory_store 单元测试：内存缓存语义（不联网）。"""

from apie.memory_store import MemoryStore


def test_products_cache_and_negative_cache():
    store = MemoryStore()
    assert store.products() is None
    store.set_products([{"name": "计算"}])
    assert store.products() == [{"name": "计算"}]
    store.clear()
    assert store.products() is None


def test_apis_per_product():
    store = MemoryStore()
    assert store.apis("ECS") is None
    store.set_apis("ECS", [{"name": "ListServers", "product_short": "ECS"}])
    assert store.apis("ecs")[0]["name"] == "ListServers"
    store.clear()
    assert store.apis("ECS") is None


def test_find_api_hit():
    store = MemoryStore()
    key = ("ecs", "ListServers", "cn-north-4")
    hit = ({"swagger": "2.0"}, "/p", "get", {"operationId": "ListServers"})
    store.set_api_cache(key, hit)
    assert store.find_api("ECS", "ListServers", "cn-north-4") == hit


def test_find_api_miss():
    store = MemoryStore()
    assert store.find_api("ECS", "Nope", "cn-north-4") is None


def test_lru_eviction():
    store = MemoryStore(max_details=3)
    for i in range(5):
        key = ("ecs", f"Api{i}", "cn-north-4")
        hit = ({"swagger": "2.0"}, f"/p{i}", "get", {"operationId": f"Api{i}"})
        store.set_api_cache(key, hit)
    assert len(store._api_details) == 3
    assert store.find_api("ECS", "Api0", "cn-north-4") is None
    assert store.find_api("ECS", "Api1", "cn-north-4") is None
    assert store.find_api("ECS", "Api4", "cn-north-4") is not None


def test_lru_access_refreshes():
    store = MemoryStore(max_details=3)
    for i in range(3):
        key = ("ecs", f"Api{i}", "cn-north-4")
        hit = ({"swagger": "2.0"}, f"/p{i}", "get", {"operationId": f"Api{i}"})
        store.set_api_cache(key, hit)
    store.find_api("ECS", "Api0", "cn-north-4")
    store.set_api_cache(("ecs", "Api3", "cn-north-4"),
                        ({"swagger": "2.0"}, "/p3", "get", {"operationId": "Api3"}))
    store.set_api_cache(("ecs", "Api4", "cn-north-4"),
                        ({"swagger": "2.0"}, "/p4", "get", {"operationId": "Api4"}))
    assert store.find_api("ECS", "Api0", "cn-north-4") is not None
    assert store.find_api("ECS", "Api1", "cn-north-4") is None
    assert store.find_api("ECS", "Api2", "cn-north-4") is None


# ---------- S23：定向失效 + 并发安全（补锁） ----------


def test_clear_api_details_targets_details_only():
    store = MemoryStore()
    store.set_products([{"name": "计算"}])
    store.set_apis("ECS", [{"name": "ListServers", "product_short": "ECS"}])
    store.set_api_cache(("ecs", "Api0", "cn-north-4"),
                        ({"swagger": "2.0"}, "/p", "get", {"operationId": "Api0"}))
    store.clear_api_details()  # corrections 热刷新联动口
    assert store.find_api("ECS", "Api0", "cn-north-4") is None
    assert store.products() == [{"name": "计算"}]      # 发现链缓存不受牵连
    assert store.apis("ECS") == [{"name": "ListServers", "product_short": "ECS"}]


def test_concurrent_find_set_clear_never_corrupts():
    """sync 工具并发派发 + reload 失效写源交错：补锁后无 LRU 损坏/KeyError。"""
    import threading

    store = MemoryStore(max_details=64)
    errors: list[Exception] = []

    def worker(offset: int) -> None:
        try:
            for n in range(200):
                i = (n + offset) % 32
                store.find_api("ECS", f"Api{i}", "cn-north-4")
                store.set_api_cache(("ecs", f"Api{i}", "cn-north-4"),
                                    ({"swagger": "2.0"}, "/p", "get", {"n": n}))
                if n % 50 == 0:
                    store.clear_api_details()
        except Exception as exc:  # LRU 紊乱 / KeyError / size 越界等竞态
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store._api_details) <= 64
