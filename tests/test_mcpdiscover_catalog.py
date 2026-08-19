"""mcpdiscover/catalog.py 纯函数单测（S7a）。"""

import json

from openmcp.mcpdiscover import catalog

SAMPLE_ENTRIES = [
    {"id": "@huaweicloud/ecs", "name": "ECS", "display_name": "弹性云服务器",
     "category": "计算", "description": "管理ECS实例", "auth": "none",
     "version": "1.0", "endpoint": "https://ecs/mcp"},
    {"id": "@huaweicloud/vpc", "name": "VPC", "display_name": "虚拟私有云",
     "category": "网络", "description": "管理VPC", "auth": "none",
     "version": "1.0", "endpoint": "https://vpc/mcp"},
    {"id": "@huaweicloud/obs", "name": "OBS", "display_name": "对象存储",
     "category": "存储", "description": "管理OBS", "auth": "none",
     "version": "1.0", "endpoint": "https://obs/mcp"},
]


class _StubSource:
    """注入测试用的固定目录源。"""

    def __init__(self, entries):
        self._entries = entries
        self._fetch_count = 0

    def fetch(self):
        self._fetch_count += 1
        return self._entries


class TestListServers:
    def test_fetch_all(self):
        src = _StubSource(SAMPLE_ENTRIES)
        servers = catalog.list_servers(src)
        assert len(servers) == 3

    def test_filter_by_category(self):
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, category="计算")
        assert len(result) == 1
        assert result[0]["id"] == "@huaweicloud/ecs"

    def test_filter_by_category_case_insensitive(self):
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, category="存储")
        assert len(result) == 1
        assert result[0]["id"] == "@huaweicloud/obs"

    def test_filter_by_keyword_id(self):
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, keyword="ecs")
        assert len(result) == 1
        assert result[0]["id"] == "@huaweicloud/ecs"

    def test_filter_by_keyword_display_name(self):
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, keyword="云服务")
        assert len(result) == 1
        assert result[0]["id"] == "@huaweicloud/ecs"

    def test_filter_by_keyword_description(self):
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, keyword="OBS")
        assert len(result) == 1
        assert result[0]["id"] == "@huaweicloud/obs"

    def test_filter_no_match(self):
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, keyword="nonexistent")
        assert result == []

    def test_filter_both_category_and_keyword(self):
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, category="计算", keyword="ecs")
        assert len(result) == 1

    def test_filter_category_mismatch_keyword_hit(self):
        """category 先过滤，keyword 再过滤。若 category 不匹配则返回空。"""
        src = _StubSource(SAMPLE_ENTRIES)
        result = catalog.list_servers(src, category="计算", keyword="vpc")
        assert result == []


class TestGetServer:
    def test_get_existing(self):
        src = _StubSource(SAMPLE_ENTRIES)
        entry = catalog.get_server(src, "@huaweicloud/ecs")
        assert entry is not None
        assert entry["name"] == "ECS"

    def test_get_case_insensitive(self):
        src = _StubSource(SAMPLE_ENTRIES)
        entry = catalog.get_server(src, "@HUAWEICLOUD/ECS")
        assert entry is not None

    def test_get_not_found(self):
        src = _StubSource(SAMPLE_ENTRIES)
        entry = catalog.get_server(src, "@huaweicloud/nonexistent")
        assert entry is None


class TestLocalCatalogSource:
    def test_load_file(self, tmp_path):
        f = tmp_path / "catalog.json"
        f.write_text(json.dumps(SAMPLE_ENTRIES, ensure_ascii=False), encoding="utf-8")
        src = catalog.LocalCatalogSource(str(f))
        entries = src.fetch()
        assert len(entries) == 3

    def test_cache_hit_no_reread(self, tmp_path):
        f = tmp_path / "catalog.json"
        f.write_text(json.dumps(SAMPLE_ENTRIES, ensure_ascii=False), encoding="utf-8")
        src = catalog.LocalCatalogSource(str(f))
        src.fetch()
        f.write_text("[]", encoding="utf-8")
        entries = src.fetch()
        assert len(entries) == 3  # cached, not re-read from empty file

    def test_clear_flushes_cache(self, tmp_path):
        f = tmp_path / "catalog.json"
        f.write_text(json.dumps(SAMPLE_ENTRIES, ensure_ascii=False), encoding="utf-8")
        src = catalog.LocalCatalogSource(str(f))
        src.fetch()
        f.write_text("[]", encoding="utf-8")
        src.clear()
        entries = src.fetch()
        assert entries == []

    def test_file_not_found_returns_empty(self, tmp_path):
        src = catalog.LocalCatalogSource(str(tmp_path / "nonexistent.json"))
        entries = src.fetch()
        assert entries == []

    def test_stale_cache_on_file_not_found(self, tmp_path):
        f = tmp_path / "catalog.json"
        f.write_text(json.dumps(SAMPLE_ENTRIES[:1], ensure_ascii=False), encoding="utf-8")
        src = catalog.LocalCatalogSource(str(f))
        entries = src.fetch()
        assert len(entries) == 1
        f.unlink()
        entries = src.fetch()
        assert len(entries) == 1  # stale cache returned

    def test_single_object_auto_wraps_to_list(self, tmp_path):
        f = tmp_path / "catalog.json"
        f.write_text(json.dumps(SAMPLE_ENTRIES[0], ensure_ascii=False), encoding="utf-8")
        src = catalog.LocalCatalogSource(str(f))
        entries = src.fetch()
        assert len(entries) == 1
        assert entries[0]["id"] == "@huaweicloud/ecs"

    def test_clear_on_empty(self):
        src = catalog.LocalCatalogSource()
        src.clear()  # no-op, no crash
        assert src.fetch() is not None  # actual read from default path
