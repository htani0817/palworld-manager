# -*- coding: utf-8 -*-
"""設定差分の正規化・秘密除外・障害切り分けを検証する。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import config_diff


def test_compare_settings_normalises_values_and_excludes_secrets():
    file_values = {
        "BoolValue": "True",
        "Rate": "1.000000",
        "Names": '("Alice", "Bob")',
        "ServerName": "  My Server  ",
        "Pending": "old",
        "FileOnly": "file",
        "AdminPassword": "file-secret",
        "ServerPassword": "file-password",
    }
    runtime_values = {
        "BoolValue": True,
        "Rate": 1,
        "Names": ["Alice", "Bob"],
        "ServerName": "My Server",
        "Pending": "new",
        "RuntimeOnly": "runtime",
        "AdminPassword": "runtime-secret",
        "ServerPassword": "runtime-password",
    }

    result = config_diff.compare_settings(file_values, runtime_values)
    by_key = {item["key"]: item for item in result["items"]}

    assert by_key["BoolValue"]["status"] == "same"
    assert by_key["Rate"]["status"] == "same"
    assert by_key["Names"]["status"] == "same"
    assert by_key["ServerName"]["status"] == "same"
    assert by_key["Pending"]["status"] == "pending"
    assert by_key["FileOnly"]["status"] == "file_only"
    assert by_key["FileOnly"]["runtime_value"] is None
    assert by_key["RuntimeOnly"]["status"] == "runtime_only"
    assert by_key["RuntimeOnly"]["file_value"] is None
    assert "AdminPassword" not in by_key
    assert "ServerPassword" not in by_key
    assert result["counts"] == {
        "same": 4,
        "pending": 1,
        "file_only": 1,
        "runtime_only": 1,
    }


def test_parenthesised_struct_list_is_compared_semantically():
    result = config_diff.compare_settings(
        {
            "Structs": '((Name="A",Rate=1.000),(Name="B",Enabled=True))',
        },
        {
            "Structs": '((Name="A",Rate=1),(Name="B",Enabled=true))',
        },
        secret_keys=[],
    )
    assert result["items"][0]["status"] == "same"


def test_non_finite_values_are_safe_for_json_response():
    result = config_diff.compare_settings(
        {"Rate": float("nan")},
        {"Rate": float("inf")},
        secret_keys=[],
    )
    assert result["items"][0]["file_value"] is None
    assert result["items"][0]["runtime_value"] is None
    json.dumps(result, allow_nan=False)


def test_get_config_diff_reports_missing_ini():
    with tempfile.TemporaryDirectory() as directory:
        original = config_diff.settings.pal_settings_ini
        config_diff.settings.pal_settings_ini = str(Path(directory) / "missing.ini")
        try:
            async def run():
                try:
                    await config_diff.get_config_diff()
                except config_diff.IniNotFoundError as exc:
                    return str(exc)
                raise AssertionError("INI 不在が検出されませんでした")

            message = asyncio.run(run())
            assert "見つかりません" in message
        finally:
            config_diff.settings.pal_settings_ini = original


def test_get_config_diff_distinguishes_rest_failure():
    with tempfile.TemporaryDirectory() as directory:
        ini_path = Path(directory) / "PalWorldSettings.ini"
        ini_path.write_text("dummy", encoding="utf-8")
        original_path = config_diff.settings.pal_settings_ini
        original_read = config_diff.ini_editor.read_ini
        original_get = config_diff.pal.get_settings_data

        def fake_read_ini():
            return {"ExpRate": "1.0"}

        async def fail_runtime():
            raise ConnectionError("connection refused")

        config_diff.settings.pal_settings_ini = str(ini_path)
        config_diff.ini_editor.read_ini = fake_read_ini
        config_diff.pal.get_settings_data = fail_runtime
        try:
            async def run():
                try:
                    await config_diff.get_config_diff()
                except config_diff.RuntimeSettingsError as exc:
                    return str(exc)
                raise AssertionError("REST 障害が検出されませんでした")

            message = asyncio.run(run())
            assert "/settings" in message
            assert "connection refused" in message
        finally:
            config_diff.settings.pal_settings_ini = original_path
            config_diff.ini_editor.read_ini = original_read
            config_diff.pal.get_settings_data = original_get


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
