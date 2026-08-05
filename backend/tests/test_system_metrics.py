# -*- coding: utf-8 -*-
"""ホスト OS 情報とシステムメトリクスの回帰テスト。

実行:
    cd backend
    python tests/test_system_metrics.py
または:
    python -m pytest tests/test_system_metrics.py -v
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import system_metrics as metrics


def test_reads_only_allowed_ubuntu_os_release_fields():
    """Ubuntu の表示名を保持し、未許可の情報を応答へ混ぜないこと"""
    release = {
        "NAME": "Ubuntu",
        "VERSION_ID": "26.04",
        "PRETTY_NAME": "Ubuntu 26.04 LTS",
        "HOME_URL": "https://ubuntu.com/",
        "VERSION_CODENAME": "resolute",
    }
    with patch.object(metrics.platform, "freedesktop_os_release", return_value=release):
        info = metrics._read_host_os_info()

    assert info == {
        "os_name": "Ubuntu",
        "os_version": "26.04",
        "os_pretty_name": "Ubuntu 26.04 LTS",
    }
    assert set(info) == {"os_name", "os_version", "os_pretty_name"}


def test_os_release_failure_returns_fixed_unknown_shape():
    """OS 情報を読めない環境でもメトリクス取得を止めないこと"""
    for error in (OSError("missing"), ValueError("invalid"), AttributeError("unsupported")):
        with patch.object(metrics.platform, "freedesktop_os_release", side_effect=error):
            info = metrics._read_host_os_info()
        assert info == {
            "os_name": "Unknown",
            "os_version": "Unknown",
            "os_pretty_name": "Unknown",
        }


def test_host_values_remove_controls_and_limit_length():
    """制御文字と過長な OS 情報をそのまま公開しないこと"""
    release = {
        "NAME": "Ubuntu\x00\nServer",
        "VERSION_ID": None,
        "PRETTY_NAME": "Ubuntu\t26.04 LTS " + ("x" * 200),
    }
    with patch.object(metrics.platform, "freedesktop_os_release", return_value=release):
        info = metrics._read_host_os_info()

    assert info["os_name"] == "Ubuntu Server"
    assert info["os_version"] == "Unknown"
    assert "\x00" not in info["os_name"]
    assert "\n" not in info["os_name"]
    assert "\t" not in info["os_pretty_name"]
    assert len(info["os_pretty_name"]) == metrics.HOST_INFO_MAX_LENGTH


def test_sample_contains_copied_host_info():
    """サンプルにホスト情報を含め、共有キャッシュを応答側から変更できないこと"""
    host = {
        "os_name": "Ubuntu",
        "os_version": "26.04",
        "os_pretty_name": "Ubuntu 26.04 LTS",
    }
    memory = SimpleNamespace(total=16 * 1024**3, used=8 * 1024**3, percent=50.0)
    disk = SimpleNamespace(total=500 * 1024**3, used=125 * 1024**3, percent=25.0)

    with (
        patch.object(metrics, "_HOST_INFO", host),
        patch.object(metrics, "_get_pal_process", return_value=None),
        patch.object(metrics.psutil, "cpu_percent", return_value=12.5),
        patch.object(metrics.psutil, "cpu_count", return_value=8),
        patch.object(metrics.psutil, "virtual_memory", return_value=memory),
        patch.object(metrics.psutil, "disk_usage", return_value=disk),
    ):
        sample = metrics._sample_once()
        sample["host"]["os_name"] = "changed"
        assert metrics._HOST_INFO["os_name"] == "Ubuntu"

    assert set(sample["host"]) == {"os_name", "os_version", "os_pretty_name"}


def test_warmup_and_cached_metrics_keep_host_shape():
    """warm-up とキャッシュ済みの両経路で同じホスト情報を返すこと"""
    host = {
        "os_name": "Ubuntu",
        "os_version": "26.04",
        "os_pretty_name": "Ubuntu 26.04 LTS",
    }
    snapshot = {
        "host": dict(host),
        "system": {"cpu_percent": 10.0},
        "palworld": {"cpu_percent": 5.0},
    }

    async def get_warmup():
        with patch.object(metrics, "_cache", None), patch.object(
            metrics, "_sample_once", return_value=dict(snapshot)
        ):
            return await metrics.get_system_metrics()

    warmup = asyncio.run(get_warmup())
    assert warmup["host"] == host
    assert warmup["warming"] is True

    cached_snapshot = dict(snapshot)
    cached_snapshot["sampled_at"] = time.time()

    async def get_cached():
        with patch.object(metrics, "_cache", cached_snapshot):
            return await metrics.get_system_metrics()

    cached = asyncio.run(get_cached())
    assert cached["host"] == host
    assert cached["warming"] is False


def _run_all() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
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
