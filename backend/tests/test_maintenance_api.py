# -*- coding: utf-8 -*-
"""メンテナンスAPIの認証情報取扱いを外部操作なしで検証する。"""

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from routers import maintenance as maintenance_router
from routers import system as system_router
from sensitive_requests import sensitive_request_validation_exception_handler


app = FastAPI()
app.add_exception_handler(
    RequestValidationError,
    sensitive_request_validation_exception_handler,
)
app.include_router(maintenance_router.router)
app.include_router(system_router.router)
client = TestClient(app)


class _FakeCoordinator:
    def __init__(self) -> None:
        self.received = None

    async def start_job(self, **kwargs):
        password = kwargs.get("sudo_password")
        if kwargs["action"] == "update" and not password:
            raise ValueError("アップデートにはsudoパスワードが必要です")
        if kwargs["action"] == "restart" and password is not None:
            raise ValueError("sudoパスワードはアップデート時だけ指定してください")
        self.received = kwargs
        return {
            "status": "checking",
            "started_at": "2026-07-29T00:00:00+00:00",
            "finished_at": None,
            "player_count": 0,
            "error": None,
            "action": kwargs["action"],
        }

    async def get_current(self):
        return {
            "status": "checking",
            "started_at": None,
            "finished_at": None,
            "player_count": None,
            "error": None,
            "action": None,
        }

    async def cancel_current(self):
        return await self.get_current()


@contextmanager
def _use_fake_coordinator():
    original = maintenance_router.coordinator
    fake = _FakeCoordinator()
    maintenance_router.coordinator = fake
    try:
        yield fake
    finally:
        maintenance_router.coordinator = original


@contextmanager
def _use_fake_system_coordinator():
    original = system_router.maintenance_coordinator
    fake = _FakeCoordinator()
    system_router.maintenance_coordinator = fake
    try:
        yield fake
    finally:
        system_router.maintenance_coordinator = original


def _assert_no_store(response) -> None:
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_update_password_is_forwarded_once_but_never_returned():
    sentinel = "SUDO_SENTINEL_7f4f"
    model = maintenance_router.MaintenanceJobRequest(
        action="update",
        sudo_password=sentinel,
    )
    assert sentinel not in repr(model)

    with _use_fake_coordinator() as fake:
        response = client.post(
            "/api/maintenance/jobs",
            json={
                "action": "update",
                "allow_players": False,
                "countdown_seconds": 30,
                "message": "更新します",
                "sudo_password": sentinel,
            },
        )

    assert response.status_code == 202
    _assert_no_store(response)
    assert fake.received["sudo_password"] == sentinel
    assert sentinel not in response.text
    assert "sudo_password" not in response.text
    fake.received = None


def test_password_requirement_and_action_scope_return_fixed_422():
    with _use_fake_coordinator():
        missing = client.post(
            "/api/maintenance/jobs",
            json={"action": "update"},
        )
        unexpected = client.post(
            "/api/maintenance/jobs",
            json={"action": "restart", "sudo_password": "secret"},
        )

    assert missing.status_code == 422
    assert missing.json()["detail"] == "アップデートにはsudoパスワードが必要です"
    _assert_no_store(missing)
    assert unexpected.status_code == 422
    assert unexpected.json()["detail"] == (
        "sudoパスワードはアップデート時だけ指定してください"
    )
    _assert_no_store(unexpected)
    assert "secret" not in unexpected.text


def test_model_validation_error_never_reflects_request_password():
    sentinel = "VALIDATION_SENTINEL_83b1"
    response = client.post(
        "/api/maintenance/jobs",
        json={"sudo_password": sentinel},
    )
    legacy = client.post(
        "/api/system/update",
        json={"sudo_password": {"nested": sentinel}},
    )

    for result in (response, legacy):
        assert result.status_code == 422
        _assert_no_store(result)
        assert result.json() == {"detail": "リクエストの形式が不正です"}
        assert sentinel not in result.text
        assert "sudo_password" not in result.text


def test_current_status_is_not_cached_and_has_no_secret_field():
    with _use_fake_coordinator():
        response = client.get("/api/maintenance/jobs/current")

    assert response.status_code == 200
    _assert_no_store(response)
    assert "sudo_password" not in response.text


def test_legacy_update_route_cannot_bypass_password_or_cache_protection():
    sentinel = "LEGACY_SUDO_SENTINEL"
    with _use_fake_system_coordinator() as fake:
        response = client.post(
            "/api/system/update",
            json={"sudo_password": sentinel},
        )

    assert response.status_code == 202
    _assert_no_store(response)
    assert fake.received["sudo_password"] == sentinel
    assert sentinel not in response.text
    assert "sudo_password" not in response.text
    fake.received = None


def test_system_command_timeout_kills_and_reaps_child_process():
    async def run():
        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.killed = False
                self.communicate_calls = 0

            async def communicate(self):
                self.communicate_calls += 1
                if self.killed:
                    return b"", b""
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True
                self.returncode = -9

        process = HangingProcess()

        async def create_process(*_args, **_kwargs):
            return process

        original = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = create_process
        try:
            result = await system_router._run("fake-command", timeout=0.01)
        finally:
            asyncio.create_subprocess_exec = original

        assert result == (1, "", "タイムアウトしました")
        assert process.killed
        assert process.communicate_calls == 2

    asyncio.run(run())


def test_system_command_cancellation_kills_and_reaps_child_process():
    async def run():
        started = asyncio.Event()

        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.killed = False
                self.communicate_calls = 0

            async def communicate(self):
                self.communicate_calls += 1
                if self.killed:
                    return b"", b""
                started.set()
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True
                self.returncode = -9

        process = HangingProcess()

        async def create_process(*_args, **_kwargs):
            return process

        original = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = create_process
        try:
            task = asyncio.create_task(system_router._run("fake-command"))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("取消が呼び出し元へ伝播しませんでした")
        finally:
            asyncio.create_subprocess_exec = original

        assert process.killed
        assert process.communicate_calls == 2

    asyncio.run(run())


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
