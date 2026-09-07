# -*- coding: utf-8 -*-
"""FastAPIの実ルーターでバックアップ排他・停止確認・要求制限を検証する。"""

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

if any(importlib.util.find_spec(name) is None for name in ("fastapi", "httpx", "apscheduler", "psutil")):
    raise unittest.SkipTest("backend/requirements.txt の依存パッケージが必要です")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
import scheduler
from routers import workspace
from test_workspace_files import WorkspaceTests


class WorkspaceApiTests(unittest.TestCase):
    # HTTPテストでもファイル層と同じ隔離領域を使う。
    def setUp(self):
        WorkspaceTests.setUp(self)
        self.status = patch.object(workspace, "service_status", AsyncMock(return_value={"query_ok": True, "status": "inactive"}))
        self.status_mock = self.status.start()
        self.update = patch.object(scheduler, "is_update_in_progress", AsyncMock(return_value=False))
        self.update.start()
        self.save = patch.object(workspace.palworld_client, "post_save", AsyncMock())
        self.save_mock = self.save.start()
        app = FastAPI()
        app.include_router(workspace.router)
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.save.stop()
        self.update.stop()
        self.status.stop()
        WorkspaceTests.tearDown(self)

    def wait_job(self):
        for _ in range(100):
            job = self.client.get("/api/backups/jobs/current").json()["job"]
            if job["state"] in {"failed", "completed"}:
                return job
            time.sleep(.01)
        self.fail("ジョブが完了しませんでした")

    def test_api_cross_origin_request_rejected(self):
        result = self.client.get("/api/files", headers={"Origin": "https://untrusted.example"})
        self.assertEqual(result.status_code, 403)

    def test_api_response_is_not_cached(self):
        result = self.client.get("/api/files")
        self.assertEqual(result.status_code, 200)
        self.assertIn("no-store", result.headers["cache-control"])

    def test_api_file_conflict_returns_409(self):
        data = self.client.get("/api/files/content", params={"path": "config/Engine.ini"}).json()
        (self.config / "Engine.ini").write_text("external", encoding="utf-8")
        result = self.client.put("/api/files/content", json={"path": data["path"], "content": "changed", "revision": data["revision"]})
        self.assertEqual(result.status_code, 409)

    def test_api_create_backup_completes_and_lists_archive(self):
        result = self.client.post("/api/backups", json={"label": "API", "paths": ["config"]})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(self.wait_job()["state"], "completed")
        self.assertEqual(len(self.client.get("/api/backups").json()["backups"]), 1)

    def test_api_active_backup_requests_save_first(self):
        self.status_mock.return_value = {"query_ok": True, "status": "active"}
        self.client.post("/api/backups", json={"label": "API", "paths": ["saves"]})
        self.assertEqual(self.wait_job()["state"], "completed")
        self.save_mock.assert_awaited_once()

    def test_api_restore_refuses_running_server(self):
        self.status_mock.return_value = {"query_ok": True, "status": "active"}
        self.client.post("/api/backups/" + "a" * 32 + "/restore", json={"confirm": "RESTORE"})
        job = self.wait_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("停止", job["message"])

    def test_api_unknown_service_state_refuses_backup(self):
        self.status_mock.return_value = {"query_ok": False, "status": "unknown"}
        self.client.post("/api/backups", json={"label": "API", "paths": ["saves"]})
        self.assertEqual(self.wait_job()["state"], "failed")
        self.assertEqual(self.client.get("/api/backups").json()["backups"], [])

    def test_api_invalid_cron_refused(self):
        result = self.client.put("/api/backups/schedule", json={"enabled": True, "cron": "invalid"})
        self.assertEqual(result.status_code, 422)

    def test_api_manual_maintenance_reservation_refuses_backup(self):
        token = object()
        self.assertTrue(scheduler.reserve_manual_maintenance(token))
        try:
            result = self.client.post("/api/backups", json={"label": "API", "paths": ["saves"]})
            self.assertEqual(result.status_code, 409)
        finally:
            scheduler.release_manual_maintenance(token)


if __name__ == "__main__":
    unittest.main()
