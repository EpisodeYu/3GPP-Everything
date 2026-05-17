"""configure_logging 不应抛错；get_logger 输出可调用。"""

from __future__ import annotations

from app.core.logging import configure_logging, get_logger


def test_configure_dev_mode() -> None:
    configure_logging(level="DEBUG", json_mode=False)
    log = get_logger("test")
    log.info("hello", x=1)


def test_configure_json_mode(capfd) -> None:
    configure_logging(level="INFO", json_mode=True)
    log = get_logger("test.json")
    log.info("ev", a=1)
    out = capfd.readouterr()
    combined = out.out + out.err
    assert '"event": "ev"' in combined or '"event":"ev"' in combined
