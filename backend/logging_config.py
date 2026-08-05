# -*- coding: utf-8 -*-
"""Palworld Manager のログ初期化と世代管理。"""

import logging
import logging.config
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "log"
LOG_RETENTION_COUNT = 7

_LOG_FILE_RE = re.compile(r"^\d{8}-\d{6}-palworld-manager\.log$")
_DISCORD_WEBHOOK_RE = re.compile(
    r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/[^\s\"'<>]+",
    re.IGNORECASE,
)

_POLLING_PATHS = frozenset({
    "/api/server/info",
    "/api/server/metrics",
    "/api/server/players",
    "/api/system/metrics",
    "/api/system/service-status",
    "/api/ranking/",
})

_active_log_file: Optional[Path] = None


def build_log_filename(started_at: datetime) -> str:
    """起動時刻から1世代分のログファイル名を作る。"""
    return f"{started_at:%Y%m%d-%H%M%S}-palworld-manager.log"


def _manager_log_files(log_dir: Path) -> list[Path]:
    """世代管理対象の通常ファイルだけを新しい順で返す。"""
    files = [
        path
        for path in log_dir.iterdir()
        if _LOG_FILE_RE.fullmatch(path.name)
        and path.is_file()
        and not path.is_symlink()
    ]
    return sorted(files, key=lambda path: path.name, reverse=True)


def _prune_log_files(log_dir: Path, retention_count: int) -> None:
    if retention_count < 1:
        raise ValueError("retention_count は1以上で指定してください")

    for old_log in _manager_log_files(log_dir)[retention_count:]:
        try:
            old_log.unlink()
        except OSError as exc:
            # ログ削除失敗だけで Manager を止めない。stderr は systemd journal に残る。
            print(
                f"Palworld Manager: 古いログを削除できませんでした "
                f"({old_log.name}, {type(exc).__name__})",
                file=sys.stderr,
                flush=True,
            )


def prepare_log_file(
    log_dir: Path,
    started_at: Optional[datetime] = None,
    retention_count: int = LOG_RETENTION_COUNT,
) -> Path:
    """ログフォルダと今回のファイルを作り、最新世代だけを残す。"""
    if retention_count < 1:
        raise ValueError("retention_count は1以上で指定してください")

    started_at = started_at or datetime.now().astimezone()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / build_log_filename(started_at)
    if log_file.is_symlink():
        raise OSError("ログファイルとしてシンボリックリンクは使用できません")
    if log_file.exists() and not log_file.is_file():
        raise OSError("ログファイルと同名の通常ファイル以外が存在します")

    # FileHandler の設定前に書き込み可能性を確認する。同秒再起動時は追記する。
    with log_file.open("a", encoding="utf-8"):
        pass

    _prune_log_files(log_dir, retention_count)
    return log_file


def redact_sensitive_text(text: str) -> str:
    """Discord Webhook の完全URLをログへ残さない。"""
    return _DISCORD_WEBHOOK_RE.sub(
        "https://discord.com/api/webhooks/[REDACTED]",
        text,
    )


class RedactingFormatter(logging.Formatter):
    """例外文字列を含む整形後のログ全体から秘密URLを除去する。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


class PollingAccessFilter(logging.Filter):
    """正常な定期取得だけをアクセスログから除外する。"""

    @staticmethod
    def _request_fields(record: logging.LogRecord) -> tuple[Optional[str], Optional[str], Optional[int]]:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            try:
                method = str(args[1]).upper()
                path = str(args[2]).partition("?")[0]
                status = int(args[4])
                return method, path, status
            except (TypeError, ValueError):
                pass

        match = re.search(
            r'"([A-Z]+)\s+([^\s]+)\s+HTTP/[^\"]+"\s+(\d{3})',
            record.getMessage(),
        )
        if not match:
            return None, None, None
        return match.group(1), match.group(2).partition("?")[0], int(match.group(3))

    def filter(self, record: logging.LogRecord) -> bool:
        method, path, status = self._request_fields(record)
        return not (
            method == "GET"
            and path in _POLLING_PATHS
            and status is not None
            and 200 <= status < 300
        )


def _logging_dict(log_file: Optional[Path]) -> dict:
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "level": "INFO",
            "stream": "ext://sys.stderr",
        },
    }
    handler_names = ["console"]

    if log_file is not None:
        handlers["file"] = {
            "class": "logging.FileHandler",
            "formatter": "standard",
            "level": "INFO",
            "filename": str(log_file),
            "mode": "a",
            "encoding": "utf-8",
        }
        handler_names.append("file")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "()": "logging_config.RedactingFormatter",
                "format": "%(asctime)s %(levelname)s %(name)s [%(process)d]: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "filters": {
            "polling_access": {
                "()": "logging_config.PollingAccessFilter",
            },
        },
        "handlers": handlers,
        "root": {
            "handlers": handler_names,
            "level": "WARNING",
        },
        "loggers": {
            "palworld_manager": {
                "handlers": handler_names,
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn": {
                "handlers": handler_names,
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": [],
                "level": "INFO",
                "propagate": True,
            },
            "uvicorn.access": {
                "handlers": handler_names,
                "filters": ["polling_access"],
                "level": "INFO",
                "propagate": False,
            },
            "httpx": {
                "handlers": [],
                "level": "WARNING",
                "propagate": True,
            },
            "httpcore": {
                "handlers": [],
                "level": "WARNING",
                "propagate": True,
            },
            "apscheduler": {
                "handlers": [],
                "level": "WARNING",
                "propagate": True,
            },
        },
    }


def configure_logging(
    log_dir: Path = DEFAULT_LOG_DIR,
    started_at: Optional[datetime] = None,
    retention_count: int = LOG_RETENTION_COUNT,
) -> Optional[Path]:
    """journalとファイルへのログ出力を設定する。

    ファイル準備に失敗した場合も、journalへ警告を残して起動を継続する。
    """
    global _active_log_file

    try:
        log_file = prepare_log_file(log_dir, started_at, retention_count)
        logging.config.dictConfig(_logging_dict(log_file))
    except Exception as exc:
        _active_log_file = None
        logging.config.dictConfig(_logging_dict(None))
        logging.getLogger("palworld_manager").warning(
            "ファイルログを初期化できないため journal のみで続行します（%s）",
            type(exc).__name__,
        )
        return None

    _active_log_file = log_file
    logging.getLogger("palworld_manager").info("Manager ログの出力先: %s", log_file)
    return log_file


def get_active_log_file() -> Optional[Path]:
    return _active_log_file
