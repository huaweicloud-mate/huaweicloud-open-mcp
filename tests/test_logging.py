"""日志配置单元测试：root logger 接管全部模块命名空间。"""

import logging

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
