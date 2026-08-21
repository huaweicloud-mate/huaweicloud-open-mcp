"""日志配置单元测试。"""

import logging

from common import logconf


def test_configure_returns_file_path(tmp_path):
    log_file = str(tmp_path / "x.log")
    out = logconf.configure_logging(program="test", log_file=log_file)
    assert out == log_file
    assert len(logging.getLogger("openmcp").handlers) == 2


def test_configure_writes_info_to_file(tmp_path):
    log_file = str(tmp_path / "x.log")
    logconf.configure_logging(program="test", level="INFO", log_file=log_file)
    logging.getLogger("openmcp.test").info("hello %s", "world")
    text = open(log_file, encoding="utf-8").read()
    assert "hello world" in text


def test_configure_debug_filtered_at_info(tmp_path):
    log_file = str(tmp_path / "x.log")
    logconf.configure_logging(program="test", level="INFO", log_file=log_file)
    logging.getLogger("openmcp.test").debug("debug line")
    text = open(log_file, encoding="utf-8").read()
    assert "debug line" not in text


def test_configure_warning_also_stderr(tmp_path, capsys):
    log_file = str(tmp_path / "x.log")
    logconf.configure_logging(program="test", level="INFO", log_file=log_file)
    logging.getLogger("openmcp.test").warning("warn line")
    err = capsys.readouterr().err
    assert "warn line" in err


def test_reconfigure_clears_previous_handlers(tmp_path):
    logconf.configure_logging(program="test", log_file=str(tmp_path / "a.log"))
    logconf.configure_logging(program="test", log_file=str(tmp_path / "b.log"))
    logger = logging.getLogger("openmcp")
    assert len(logger.handlers) == 2
