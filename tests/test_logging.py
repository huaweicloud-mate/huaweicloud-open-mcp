"""日志配置单元测试：root logger 接管全部模块命名空间。"""

import logging
from pathlib import Path

import pytest

from common import logconf


@pytest.fixture(autouse=True)
def _reset_root_logging():
    """恢复 root/third-party logger 状态，避免污染其它测试。"""
    yield
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_configure_returns_file_path(tmp_path):
    log_file = str(tmp_path / "x.log")
    out = logconf.configure_logging(program="test", log_file=log_file)
    assert out == log_file
    assert len(logging.getLogger().handlers) == 2


def test_configure_writes_module_namespaces_to_file(tmp_path):
    log_file = str(tmp_path / "x.log")
    logconf.configure_logging(program="test", level="INFO", log_file=log_file)
    logging.getLogger("main").info("server start: elicit=off")       # 既有断裂命名空间
    logging.getLogger("apie.catalog").info("cache hit")
    text = open(log_file, encoding="utf-8").read()
    assert "server start: elicit=off" in text
    assert "cache hit" in text


def test_configure_debug_filtered_at_info(tmp_path):
    log_file = str(tmp_path / "x.log")
    logconf.configure_logging(program="test", level="INFO", log_file=log_file)
    logging.getLogger("mcp_openapi.service").debug("debug line")
    text = open(log_file, encoding="utf-8").read()
    assert "debug line" not in text


def test_configure_warning_also_stderr(tmp_path, capsys):
    log_file = str(tmp_path / "x.log")
    logconf.configure_logging(program="test", level="INFO", log_file=log_file)
    logging.getLogger("common.elicit").warning("warn line")
    err = capsys.readouterr().err
    assert "warn line" in err


def test_reconfigure_clears_previous_handlers(tmp_path):
    logconf.configure_logging(program="test", log_file=str(tmp_path / "a.log"))
    logconf.configure_logging(program="test", log_file=str(tmp_path / "b.log"))
    root = logging.getLogger()
    assert len(root.handlers) == 2
    logconf.configure_logging(program="test", log_file=str(tmp_path / "b.log"))
    logging.getLogger("safety.policy_store").info("once only")
    text = open(str(tmp_path / "b.log"), encoding="utf-8").read()
    assert text.count("once only") == 1


def test_noisy_third_party_loggers_demoted_to_warning(tmp_path):
    logconf.configure_logging(program="test", level="INFO",
                              log_file=str(tmp_path / "x.log"))
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def _clean_env(monkeypatch):
    monkeypatch.delenv("HUAWEICLOUD_MCP_LOG_FILE", raising=False)
    monkeypatch.delenv("HUAWEICLOUD_MCP_LOG_LEVEL", raising=False)


def test_unconfigured_writes_no_file_and_stderr_carries_info(tmp_path, monkeypatch, capsys):
    """MCP server 缺省档：未配置即不落盘，stderr 成为唯一通道（INFO 可见）。"""
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    out = logconf.configure_logging(program="test", level="INFO")
    assert out is None
    assert len(logging.getLogger().handlers) == 1          # 仅 stderr
    assert not (tmp_path / logconf.LOG_DIR).exists()       # 不创建 logs/
    logging.getLogger("mcp_openapi.service").info("start summary")
    assert "start summary" in capsys.readouterr().err


def test_unconfigured_stderr_still_honours_level(tmp_path, monkeypatch, capsys):
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    logconf.configure_logging(program="test", level="WARNING")
    logging.getLogger("mcp_openapi.service").info("debug-ish")
    assert "debug-ish" not in capsys.readouterr().err


def test_write_default_file_keeps_pipeline_behaviour(tmp_path, monkeypatch):
    """构建期管道：write_default_file=True 时仍落 logs/{program}.log。"""
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    out = logconf.configure_logging(program="test", level="INFO",
                                    write_default_file=True)
    assert out == str(Path(logconf.LOG_DIR) / "test.log")
    logging.getLogger("apie.catalog").info("cache hit")
    assert "cache hit" in (tmp_path / "logs" / "test.log").read_text(encoding="utf-8")


def test_file_present_pins_stderr_to_warning(tmp_path, monkeypatch, capsys):
    """文件承载全量 → stderr 退为 WARNING+ 兜底（既有红线）。"""
    _clean_env(monkeypatch)
    logconf.configure_logging(program="test", level="INFO",
                              log_file=str(tmp_path / "x.log"))
    logging.getLogger("mcp_openapi.service").info("file only")
    err = capsys.readouterr().err
    assert "file only" not in err
    assert "file only" in (tmp_path / "x.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("off_value", ["off", "OFF", "", "  "])
def test_off_sentinel_disables_file_even_with_default(tmp_path, monkeypatch, off_value):
    """off/空串显式禁用：即便 write_default_file=True 也不落盘（显式值优先）。"""
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    out = logconf.configure_logging(program="test", log_file=off_value,
                                    write_default_file=True)
    assert out is None
    assert not (tmp_path / logconf.LOG_DIR).exists()


def test_env_log_file_used_when_arg_absent(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    target = str(tmp_path / "env.log")
    monkeypatch.setenv("HUAWEICLOUD_MCP_LOG_FILE", target)
    assert logconf.configure_logging(program="test") == target
    assert Path(target).exists()


def test_arg_overrides_env_log_file(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HUAWEICLOUD_MCP_LOG_FILE", str(tmp_path / "env.log"))
    target = str(tmp_path / "arg.log")
    assert logconf.configure_logging(program="test", log_file=target) == target
    assert Path(target).exists()


def test_env_off_sentinel_disables_file(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HUAWEICLOUD_MCP_LOG_FILE", "off")
    monkeypatch.chdir(tmp_path)
    assert logconf.configure_logging(program="test", write_default_file=True) is None
    assert not (tmp_path / logconf.LOG_DIR).exists()
