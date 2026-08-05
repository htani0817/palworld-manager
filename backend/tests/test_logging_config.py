# -*- coding: utf-8 -*-
"""Palworld Manager のログファイル管理・秘匿化・アクセスログ抑止のテスト。

実行:
    cd backend
    python -m pytest tests/test_logging_config.py -v
または pytest 無しでも実行できる:
    python tests/test_logging_config.py
"""

import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# backend をインポートパスに追加
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from logging_config import (  # noqa: E402
    PollingAccessFilter,
    RedactingFormatter,
    build_log_filename,
    prepare_log_file,
)


def _run_logging_script(script: str, *args: Path) -> subprocess.CompletedProcess[str]:
    """ログ設定をテストプロセスから分離して実行する。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-c", script, *(str(arg) for arg in args)],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_build_log_filename_uses_start_timestamp():
    """起動時刻が YYYYMMDD-HHMMSS 形式のファイル名になること"""
    started_at = datetime(2026, 7, 22, 12, 34, 56)
    assert build_log_filename(started_at) == "20260722-123456-palworld-manager.log"


def test_prepare_log_file_creates_directory_and_file():
    """未作成のログディレクトリと今回のログファイルを自動作成すること"""
    with tempfile.TemporaryDirectory(prefix="logging_config_test_") as tmp:
        log_dir = Path(tmp) / "nested" / "log"
        started_at = datetime(2026, 7, 22, 12, 34, 56)

        log_file = prepare_log_file(log_dir, started_at)

        assert log_dir.is_dir()
        assert log_file == log_dir / "20260722-123456-palworld-manager.log"
        assert log_file.is_file()


def test_prepare_log_file_keeps_latest_seven_generations():
    """更新日時ではなくファイル名の起動時刻で最新7世代を残すこと"""
    with tempfile.TemporaryDirectory(prefix="logging_config_test_") as tmp:
        log_dir = Path(tmp) / "log"
        first_started_at = datetime(2026, 7, 22, 12, 0, 0)
        started_times = [first_started_at + timedelta(seconds=i) for i in range(8)]

        for started_at in started_times[:7]:
            prepare_log_file(log_dir, started_at, retention_count=7)

        # 古い世代ほどmtimeを新しくしても、世代の判定が逆転しないことを確認する。
        for index, started_at in enumerate(started_times[:7]):
            path = log_dir / build_log_filename(started_at)
            reversed_mtime = 2_000_000_000 - index
            os.utime(path, (reversed_mtime, reversed_mtime))

        prepare_log_file(log_dir, started_times[7], retention_count=7)

        actual_names = sorted(path.name for path in log_dir.iterdir())
        expected_names = sorted(build_log_filename(value) for value in started_times[-7:])
        assert actual_names == expected_names


def test_prepare_log_file_does_not_delete_unrelated_files():
    """世代管理の命名規則に一致しないファイルを削除しないこと"""
    with tempfile.TemporaryDirectory(prefix="logging_config_test_") as tmp:
        log_dir = Path(tmp) / "log"
        log_dir.mkdir(parents=True)
        unrelated_files = [
            log_dir / "README.txt",
            log_dir / "20260722-120000-palworld-server.log",
            log_dir / "palworld-manager.log.bak",
        ]
        for path in unrelated_files:
            path.write_text("削除しない", encoding="utf-8")

        first_started_at = datetime(2026, 7, 22, 12, 0, 0)
        for i in range(8):
            prepare_log_file(
                log_dir,
                first_started_at + timedelta(seconds=i),
                retention_count=7,
            )

        for path in unrelated_files:
            assert path.read_text(encoding="utf-8") == "削除しない"


def test_redacting_formatter_hides_discord_webhook_url_and_token():
    """Discord Webhook の完全URL・ID・トークンが整形結果に残らないこと"""
    webhook_id = "123456789012345678"
    webhook_token = "very-secret_webhook-token.ABC123"
    webhook_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}"
    record = logging.LogRecord(
        name="palworld_manager.discord",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Discord 送信失敗: %s",
        args=(webhook_url,),
        exc_info=None,
    )

    formatted = RedactingFormatter("%(levelname)s %(message)s").format(record)

    assert webhook_url not in formatted
    assert webhook_id not in formatted
    assert webhook_token not in formatted
    assert "[REDACTED]" in formatted


def test_configure_logging_writes_utf8_redacted_logs_without_dependency_noise():
    """実際のファイル・stderrでUTF-8、秘匿、依存ライブラリ抑止を確認すること"""
    script = r'''
import logging
import sys
from datetime import datetime
from pathlib import Path

from logging_config import configure_logging

log_dir = Path(sys.argv[1])
log_file = configure_logging(log_dir, datetime(2026, 7, 22, 12, 34, 56))
webhook_id = "123456789012345678"
webhook_token = "very-secret_webhook-token.ABC123"
webhook_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}"
logger = logging.getLogger("palworld_manager.test")
logger.info("UTF-8確認: 日本語・📝 SAFE_FILE_MARKER")
try:
    raise RuntimeError(webhook_url)
except RuntimeError:
    logger.exception("Webhook送信失敗: %s", webhook_url)
logging.getLogger("httpx").info("HTTPX_INFO_MUST_NOT_APPEAR %s", webhook_url)
logging.getLogger("httpcore.connection").info("HTTPCORE_INFO_MUST_NOT_APPEAR")
logging.getLogger("apscheduler.executors.default").info("SCHEDULER_INFO_MUST_NOT_APPEAR")
logging.getLogger("apscheduler.executors.default").warning("SCHEDULER_WARNING_MARKER")
logging.shutdown()
print(log_file.name)
'''
    with tempfile.TemporaryDirectory(prefix="logging_config_test_") as tmp:
        log_dir = Path(tmp) / "nested" / "log"
        result = _run_logging_script(script, log_dir)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "20260722-123456-palworld-manager.log"
        log_text = (log_dir / result.stdout.strip()).read_text(encoding="utf-8")
        combined = log_text + result.stderr

        assert "UTF-8確認: 日本語・📝 SAFE_FILE_MARKER" in log_text
        assert "SAFE_FILE_MARKER" in result.stderr
        assert "SCHEDULER_WARNING_MARKER" in log_text
        assert "SCHEDULER_WARNING_MARKER" in result.stderr
        assert "HTTPX_INFO_MUST_NOT_APPEAR" not in combined
        assert "HTTPCORE_INFO_MUST_NOT_APPEAR" not in combined
        assert "SCHEDULER_INFO_MUST_NOT_APPEAR" not in combined
        assert "https://discord.com/api/webhooks/" in combined
        assert "[REDACTED]" in combined
        assert "123456789012345678" not in combined
        assert "very-secret_webhook-token.ABC123" not in combined


def test_configure_logging_falls_back_to_stderr_when_file_setup_fails():
    """ログフォルダを作れなくてもstderrへ警告し、処理を継続すること"""
    script = r'''
import logging
import sys
from datetime import datetime
from pathlib import Path

from logging_config import configure_logging

result = configure_logging(
    Path(sys.argv[1]) / "log",
    datetime(2026, 7, 22, 12, 34, 56),
)
logging.getLogger("palworld_manager.test").info("FALLBACK_CONTINUED_MARKER")
logging.shutdown()
print(result is None)
'''
    with tempfile.TemporaryDirectory(prefix="logging_config_test_") as tmp:
        blocker = Path(tmp) / "not-a-directory"
        blocker.write_text("block", encoding="utf-8")
        result = _run_logging_script(script, blocker)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True"
        assert result.stderr.count("journal のみで続行します") == 1
        assert result.stderr.count("FALLBACK_CONTINUED_MARKER") == 1


def test_reconfigure_does_not_duplicate_file_or_console_handlers():
    """再設定しても同じログが各出力先へ重複しないこと"""
    script = r'''
import logging
import sys
from datetime import datetime
from pathlib import Path

from logging_config import configure_logging

started_at = datetime(2026, 7, 22, 12, 34, 56)
log_file = configure_logging(Path(sys.argv[1]), started_at)
configure_logging(Path(sys.argv[1]), started_at)
logging.getLogger("palworld_manager.test").info("NO_DUPLICATE_MARKER")
logging.shutdown()
print(log_file.name)
'''
    with tempfile.TemporaryDirectory(prefix="logging_config_test_") as tmp:
        log_dir = Path(tmp) / "log"
        result = _run_logging_script(script, log_dir)

        assert result.returncode == 0, result.stderr
        log_text = (log_dir / result.stdout.strip()).read_text(encoding="utf-8")
        assert log_text.count("NO_DUPLICATE_MARKER") == 1
        assert result.stderr.count("NO_DUPLICATE_MARKER") == 1


def test_systemd_service_keeps_root_and_journal_fallback():
    """systemdサービスがroot実行とjournal出力を維持すること"""
    service_text = (BACKEND.parent / "palworld-manager.service").read_text(encoding="utf-8")
    assert "User=root" in service_text
    assert "StandardOutput=journal" in service_text
    assert "StandardError=journal" in service_text
    assert "KillMode=mixed" in service_text
    assert "TimeoutStopSec=2400" in service_text


def _access_record(method: str, path: str, status: int) -> logging.LogRecord:
    """Uvicorn の標準アクセスログと同じ引数構造のレコードを作る。"""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", method, path, "1.1", status),
        exc_info=None,
    )


def test_polling_access_filter_suppresses_only_known_get_2xx():
    """既知の定期GETの2xxだけを抑止し、POST・非2xx・未知パスは残すこと"""
    access_filter = PollingAccessFilter()
    polling_paths = (
        "/api/server/info",
        "/api/server/metrics",
        "/api/server/players",
        "/api/system/metrics",
        "/api/system/service-status",
        "/api/ranking/",
    )

    for path in polling_paths:
        assert access_filter.filter(_access_record("GET", path, 200)) is False, path

    assert access_filter.filter(
        _access_record("GET", "/api/server/metrics?window=latest", 204)
    ) is False
    assert access_filter.filter(_access_record("POST", "/api/server/metrics", 200)) is True
    assert access_filter.filter(_access_record("GET", "/api/server/metrics", 302)) is True
    assert access_filter.filter(_access_record("GET", "/api/server/metrics", 500)) is True
    assert access_filter.filter(_access_record("GET", "/api/config/env", 200)) is True


# ── 簡易ランナー（pytest 無しでも実行可能）────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  OK  {test.__name__}")
        except Exception as exc:
            print(f" FAIL {test.__name__}: {exc}")
            failed.append(test.__name__)
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        return 1
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
