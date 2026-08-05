# -*- coding: utf-8 -*-
"""ランキング CSV 出力 API のテスト。"""

import copy
import csv
import io
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from routers import ranking as ranking_router


app = FastAPI()
app.include_router(ranking_router.router)
client = TestClient(app)


def _player(
    user_id,
    name,
    *,
    account="account",
    total_seconds=0,
    max_level=0,
    login_days=0,
    last_seen="2026-07-29T12:34:56+09:00",
    online=False,
):
    return {
        "userId": user_id,
        "name": name,
        "accountName": account,
        "total_seconds": total_seconds,
        "max_level": max_level,
        "login_days": login_days,
        "last_seen": last_seen,
        "online": online,
    }


def _snapshot(players):
    return {
        "started_at": "2026-07-01T00:00:00+09:00",
        "updated": "2026-07-29T12:34:56+09:00",
        "sample_interval": 10,
        "players": players,
    }


@contextmanager
def _serve_snapshot(snapshot):
    original = ranking_router.ranking_tracker.get_ranking_view
    calls = {"count": 0}

    def get_ranking_view():
        calls["count"] += 1
        return snapshot

    ranking_router.ranking_tracker.get_ranking_view = get_ranking_view
    try:
        yield calls
    finally:
        ranking_router.ranking_tracker.get_ranking_view = original


def _csv_rows(response):
    text = response.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


def test_response_headers_bom_crlf_and_all_display_columns():
    snapshot = _snapshot([
        _player(
            "user-1",
            "テスト太郎",
            account="steam-1",
            total_seconds=3661.5,
            max_level=42,
            login_days=7,
            online=True,
        )
    ])

    with _serve_snapshot(snapshot):
        response = client.get("/api/ranking/export.csv?metric=total_seconds")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="ranking-total_seconds.csv"'
    )
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\xef\xbb\xbf")

    body_without_bom = response.content[3:]
    assert b"\n" in body_without_bom
    assert b"\n" not in body_without_bom.replace(b"\r\n", b"")
    assert b"\r" not in body_without_bom.replace(b"\r\n", b"")

    rows = _csv_rows(response)
    assert rows[0] == list(ranking_router.CSV_HEADERS)
    assert rows[1] == [
        "1",
        "テスト太郎",
        "steam-1",
        "3661.5",
        "42",
        "7",
        "2026-07-29T12:34:56+09:00",
        "オンライン",
    ]


def test_selected_metric_sort_and_deterministic_tie_order():
    snapshot = _snapshot([
        _player("user-z", "Z", total_seconds=100, max_level=8, login_days=1),
        _player("user-a", "A", total_seconds=50, max_level=8, login_days=4),
        _player("user-b", "B", total_seconds=100, max_level=5, login_days=3),
    ])
    expected_names = {
        "total_seconds": ["B", "Z", "A"],
        "max_level": ["Z", "A", "B"],
        "login_days": ["A", "B", "Z"],
    }

    with _serve_snapshot(snapshot):
        for metric, expected in expected_names.items():
            response = client.get(f"/api/ranking/export.csv?metric={metric}")
            assert response.status_code == 200
            rows = _csv_rows(response)
            assert [row[0] for row in rows[1:]] == ["1", "2", "3"]
            assert [row[1] for row in rows[1:]] == expected


def test_export_does_not_modify_accumulated_data_or_files():
    tracker = ranking_router.ranking_tracker
    originals = {
        "_data": tracker._data,
        "_prev_ids": tracker._prev_ids,
        "RANKING_FILE": tracker.RANKING_FILE,
        "BACKUP_FILE": tracker.BACKUP_FILE,
        "_save": tracker._save,
        "_backup_if_due": tracker._backup_if_due,
        "get_ranking_view": tracker.get_ranking_view,
    }

    with tempfile.TemporaryDirectory(prefix="ranking_csv_test_") as tmp:
        tmp_path = Path(tmp)
        ranking_file = tmp_path / "ranking.json"
        backup_file = tmp_path / "ranking.json.bak"
        ranking_file.write_bytes(b'{"existing":"ranking data"}')
        backup_file.write_bytes(b'{"existing":"backup data"}')
        tracker.RANKING_FILE = ranking_file
        tracker.BACKUP_FILE = backup_file
        tracker._data = {
            "version": 1,
            "started_at": "2026-07-01T00:00:00+09:00",
            "last_updated": "2026-07-29T12:34:56+09:00",
            "players": {
                "user-b": {
                    "name": "B",
                    "accountName": "account-b",
                    "total_seconds": 10,
                    "max_level": 2,
                    "login_days": 1,
                    "last_seen": "2026-07-29T12:00:00+09:00",
                },
                "user-a": {
                    "name": "A",
                    "accountName": "account-a",
                    "total_seconds": 20,
                    "max_level": 3,
                    "login_days": 2,
                    "last_seen": "2026-07-29T12:30:00+09:00",
                },
            },
        }
        tracker._prev_ids = {"user-a"}

        def must_not_write():
            raise AssertionError("CSV出力から永続化処理が呼ばれました")

        calls = {"count": 0}

        def get_ranking_view():
            calls["count"] += 1
            return originals["get_ranking_view"]()

        tracker._save = must_not_write
        tracker._backup_if_due = must_not_write
        tracker.get_ranking_view = get_ranking_view
        data_before = copy.deepcopy(tracker._data)
        prev_ids_before = set(tracker._prev_ids)
        ranking_before = (ranking_file.read_bytes(), ranking_file.stat().st_mtime_ns)
        backup_before = (backup_file.read_bytes(), backup_file.stat().st_mtime_ns)
        files_before = sorted(path.name for path in tmp_path.iterdir())

        try:
            response = client.get("/api/ranking/export.csv?metric=total_seconds")
            assert response.status_code == 200
            assert calls["count"] == 1
            assert tracker._data == data_before
            assert tracker._prev_ids == prev_ids_before
            assert (ranking_file.read_bytes(), ranking_file.stat().st_mtime_ns) == ranking_before
            assert (backup_file.read_bytes(), backup_file.stat().st_mtime_ns) == backup_before
            assert sorted(path.name for path in tmp_path.iterdir()) == files_before
        finally:
            tracker._data = originals["_data"]
            tracker._prev_ids = originals["_prev_ids"]
            tracker.RANKING_FILE = originals["RANKING_FILE"]
            tracker.BACKUP_FILE = originals["BACKUP_FILE"]
            tracker._save = originals["_save"]
            tracker._backup_if_due = originals["_backup_if_due"]
            tracker.get_ranking_view = originals["get_ranking_view"]


def test_dangerous_cells_are_neutralized_for_excel():
    snapshot = _snapshot([
        _player(
            "user-1",
            '=HYPERLINK("https://example.invalid")',
            account="  +SUM(1,1)",
            last_seen="\t@SUM(1,1)",
        ),
        _player(
            "user-2",
            "-2+3",
            account="@malicious",
            last_seen="\r=NOW()",
        ),
        _player(
            "user-3",
            "　＝HYPERLINK()",
            account="＋SUM(1,1)",
            last_seen="＠NOW()",
        ),
    ])

    with _serve_snapshot(snapshot):
        response = client.get("/api/ranking/export.csv?metric=login_days")

    rows = _csv_rows(response)
    assert rows[1][1] == '\'=HYPERLINK("https://example.invalid")'
    assert rows[1][2] == "'  +SUM(1,1)"
    assert rows[1][6] == "'\t@SUM(1,1)"
    assert rows[2][1] == "'-2+3"
    assert rows[2][2] == "'@malicious"
    assert rows[2][6] == "'\r=NOW()"
    assert rows[3][1] == "'　＝HYPERLINK()"
    assert rows[3][2] == "'＋SUM(1,1)"
    assert rows[3][6] == "'＠NOW()"


def test_metric_is_strictly_validated_before_snapshot_read():
    original = ranking_router.ranking_tracker.get_ranking_view

    def must_not_be_called():
        raise AssertionError("入力エラー時にランキングを読み取っています")

    ranking_router.ranking_tracker.get_ranking_view = must_not_be_called
    try:
        invalid = client.get("/api/ranking/export.csv?metric=TOTAL_SECONDS")
        unknown = client.get("/api/ranking/export.csv?metric=score")
        missing = client.get("/api/ranking/export.csv")
    finally:
        ranking_router.ranking_tracker.get_ranking_view = original

    assert invalid.status_code == 422
    assert unknown.status_code == 422
    assert missing.status_code == 422


def _run_all():
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
        except Exception as error:
            print(f" FAIL {test.__name__}: {error}")
            failed.append(test.__name__)
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        return 1
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
