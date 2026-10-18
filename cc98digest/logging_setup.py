"""日志配置：文件（UTF-8，按大小滚动）+ 控制台。

Windows 控制台默认 GBK，中文/表情会炸；这里统一把流重配为 UTF-8（无法重配时退回
errors="replace"，绝不因为日志编码让整轮运行失败）。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "cc98digest"
_CONFIGURED = False


def _safe_utf8(stream):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - 某些被包装的流不支持
        pass
    return stream


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    to_console: bool = True,
    filename: str = "cc98-digest.log",
) -> logging.Logger:
    """初始化日志器；同一进程里重复调用只更新级别，不重复添加 handler。

    例外：如果显式指定了**另一个**文件名（例如开机自启入口要写 autostart.log），
    就把现有文件 handler 换掉——否则入口跑起来后一个日志文件都不会生成。
    """
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    target = (Path(log_dir) / filename).resolve()
    if _CONFIGURED:
        if not _has_file_handler(logger, target):
            _remove_file_handlers(logger)
            _add_file_handler(logger, target)
        return logger

    log_dir.mkdir(parents=True, exist_ok=True)
    _add_file_handler(logger, target)

    if to_console:
        console = logging.StreamHandler(_safe_utf8(sys.stdout))
        console.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
        logger.addHandler(console)

    _CONFIGURED = True
    return logger


def _has_file_handler(logger: logging.Logger, target: Path) -> bool:
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == target:
            return True
    return False


def _remove_file_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()


def _add_file_handler(logger: logging.Logger, target: Path) -> None:
    """挂上滚动文件 handler；**打不开就跳过**，绝不能因为日志不可用把主功能打死。

    踩过：批处理把 stdout 重定向到 autostart.log，入口自己也去开同一个文件 →
    PermissionError 直接让自启服务崩掉（表现就是"服务没起来"）。
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[warn] 日志文件不可用（{target}）：{exc}；本次不写文件日志。", file=sys.stderr)
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME if not name else f"{_LOGGER_NAME}.{name}")
