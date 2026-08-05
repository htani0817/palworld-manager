# -*- coding: utf-8 -*-
"""メンテナンスジョブの安全性と状態遷移を外部操作なしで検証する。"""

import asyncio
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import maintenance


async def _no_external_update():
    return False


def _restart_not_recent():
    return False


def _coordinator(**kwargs):
    kwargs.setdefault("update_in_progress", _no_external_update)
    kwargs.setdefault("restart_was_recent", _restart_not_recent)
    return maintenance.MaintenanceCoordinator(**kwargs)


def test_players_present_are_rejected_without_any_change():
    async def run():
        calls = []

        async def get_players():
            calls.append("players")
            return {"players": [{"name": "Alice"}]}

        async def forbidden_operation(*_args):
            calls.append("external-change")
            raise AssertionError("拒否後に外部操作が呼ばれました")

        coordinator = _coordinator(
            get_players=get_players,
            announce=forbidden_operation,
            save=forbidden_operation,
            restart=forbidden_operation,
            update=forbidden_operation,
            verify=forbidden_operation,
        )
        try:
            await coordinator.start_job(action="restart")
        except maintenance.PlayersConnectedError as exc:
            assert exc.player_count == 1
        else:
            raise AssertionError("接続者がいるのにジョブが受理されました")

        assert calls == ["players"]
        assert (await coordinator.get_current())["status"] == "idle"

    asyncio.run(run())


def test_restart_runs_announce_countdown_save_restart_verify_in_order():
    async def run():
        calls = []

        async def get_players():
            calls.append("players")
            return {"players": []}

        async def announce(message):
            calls.append(("announce", message))

        async def countdown(seconds, _cancel_event):
            assert maintenance.scheduler.maintenance_operation_lock.locked()
            calls.append(("countdown", seconds))
            return False

        async def save():
            calls.append("save")

        async def restart():
            calls.append("restart")
            return True, ""

        async def verify():
            calls.append("verify")

        coordinator = _coordinator(
            get_players=get_players,
            announce=announce,
            save=save,
            restart=restart,
            verify=verify,
            countdown_wait=countdown,
        )
        accepted = await coordinator.start_job(
            action="restart",
            countdown_seconds=30,
            message="30秒後に再起動します",
        )
        assert accepted["status"] == "checking"
        state = await coordinator.wait_until_finished()

        assert state["status"] == "completed"
        assert state["action"] == "restart"
        assert state["player_count"] == 0
        assert state["started_at"] is not None
        assert state["finished_at"] is not None
        assert state["error"] is None
        assert calls == [
            "players",
            ("announce", "30秒後に再起動します"),
            ("countdown", 30),
            "players",
            "save",
            "restart",
            "verify",
        ]

    asyncio.run(run())


def test_save_failure_never_calls_restart():
    async def run():
        calls = []

        async def get_players():
            return {"players": []}

        async def fail_save():
            calls.append("save")
            raise RuntimeError("disk full")

        async def restart():
            calls.append("restart")
            return True, ""

        async def verify():
            calls.append("verify")

        coordinator = _coordinator(
            get_players=get_players,
            save=fail_save,
            restart=restart,
            verify=verify,
        )
        await coordinator.start_job(action="restart")
        state = await coordinator.wait_until_finished()

        assert state["status"] == "failed"
        assert "ワールド保存" in state["error"]
        assert "disk full" in state["error"]
        assert calls == ["save"]

    asyncio.run(run())


def test_countdown_can_be_cancelled_before_save():
    async def run():
        calls = []
        countdown_started = asyncio.Event()

        async def get_players():
            return {"players": []}

        async def countdown(_seconds, cancel_event):
            countdown_started.set()
            await cancel_event.wait()
            return True

        async def save():
            calls.append("save")

        async def restart():
            calls.append("restart")
            return True, ""

        coordinator = _coordinator(
            get_players=get_players,
            save=save,
            restart=restart,
            countdown_wait=countdown,
        )
        await coordinator.start_job(action="restart", countdown_seconds=300)
        await asyncio.wait_for(countdown_started.wait(), timeout=1)
        cancelled = await coordinator.cancel_current()
        state = await coordinator.wait_until_finished()

        assert cancelled["status"] == "cancelled"
        assert state["status"] == "cancelled"
        assert state["finished_at"] is not None
        assert calls == []

    asyncio.run(run())


def test_player_joining_during_countdown_stops_before_save():
    async def run():
        calls = []
        player_checks = 0

        async def get_players():
            nonlocal player_checks
            player_checks += 1
            calls.append("players")
            if player_checks == 1:
                return {"players": []}
            return {"players": [{"name": "Late joiner"}]}

        async def countdown(_seconds, _cancel_event):
            calls.append("countdown")
            return False

        async def save():
            calls.append("save")

        async def restart():
            calls.append("restart")
            return True, ""

        coordinator = _coordinator(
            get_players=get_players,
            save=save,
            restart=restart,
            countdown_wait=countdown,
        )
        await coordinator.start_job(action="restart", countdown_seconds=30)
        state = await coordinator.wait_until_finished()

        assert state["status"] == "failed"
        assert state["player_count"] == 1
        assert "保存直前にプレイヤー接続" in state["error"]
        assert calls == ["players", "countdown", "players"]

    asyncio.run(run())


def test_update_command_authenticates_then_runs_fixed_script_directly():
    assert maintenance.UPDATE_SCRIPT == "/home/palworld-user/scripts/update.sh"
    assert maintenance.UPDATE_RUN_AS_USER == "palworld-user"
    assert maintenance.UPDATE_WORKING_DIRECTORY == "/home/palworld-user"
    assert "sudo -S -k -p '' -- sh -c " in maintenance.UPDATE_SHELL_COMMAND
    assert maintenance.UPDATE_AUTH_MARKER in maintenance.UPDATE_SHELL_COMMAND
    assert (
        "exec /home/palworld-user/scripts/update.sh </dev/null"
        in maintenance.UPDATE_SHELL_COMMAND
    )
    assert maintenance.UPDATE_COMMAND == (
        "runuser",
        "-u",
        "palworld-user",
        "--",
        "sh",
        "-c",
        maintenance.UPDATE_SHELL_COMMAND,
    )

    async def run():
        process_calls = []
        password = bytearray(b"palworld-user-secret")

        class FakeStdin:
            def __init__(self, call):
                self.call = call

            def write(self, value):
                self.call["input"] = bytes(value)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class FakeReader:
            def __init__(self, value=b""):
                self.value = value

            async def read(self, _size=-1):
                value, self.value = self.value, b""
                return value

        class FakeProcess:
            def __init__(self, call, *, update=False):
                self.call = call
                self.returncode = 0
                self.stdin = FakeStdin(call) if update else None
                self.stderr = FakeReader(
                    maintenance.UPDATE_AUTH_MARKER_BYTES if update else b""
                )

            async def wait(self):
                if self.call["command"] == maintenance.UPDATE_COMMAND:
                    assert password
                    assert all(value == 0 for value in password)
                return self.returncode

            async def communicate(self, input=None):
                self.call["input"] = None if input is None else bytes(input)
                return b"", b""

            def kill(self):
                raise AssertionError("成功するプロセスを kill しようとしました")

        async def create_process(*command, **kwargs):
            call = {
                "command": command,
                "kwargs": kwargs,
                "input": "not-called",
            }
            process_calls.append(call)
            return FakeProcess(call, update=command == maintenance.UPDATE_COMMAND)

        original_create_process = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = create_process
        try:
            result = await maintenance._run_update_command(password)
        finally:
            maintenance._wipe_secret(password)
            asyncio.create_subprocess_exec = original_create_process

        assert result == (True, "")
        assert len(process_calls) == 2

        update = process_calls[0]
        assert update["command"] == maintenance.UPDATE_COMMAND
        expected_update_options = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": maintenance.UPDATE_WORKING_DIRECTORY,
        }
        if maintenance.os.name == "posix":
            expected_update_options["start_new_session"] = True
        assert update["kwargs"] == expected_update_options
        assert update["input"] == b"palworld-user-secret\n"

        invalidation = process_calls[1]
        assert invalidation["command"] == maintenance.SUDO_INVALIDATE_COMMAND
        assert invalidation["kwargs"] == {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
        }
        assert invalidation["input"] is None
        assert password
        assert all(value == 0 for value in password)

    asyncio.run(run())


def test_sudo_password_write_timeout_terminates_process_and_wipes_buffer():
    async def run():
        killed = asyncio.Event()

        class HangingStdin:
            def write(self, _value):
                return None

            async def drain(self):
                await asyncio.Event().wait()

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class HangingReader:
            async def read(self, _size=-1):
                await killed.wait()
                return b""

        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.stdin = HangingStdin()
                self.stderr = HangingReader()

            async def wait(self):
                await killed.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                killed.set()

        process = HangingProcess()

        async def create_process(*_command, **_kwargs):
            return process

        original_create_process = asyncio.create_subprocess_exec
        original_timeout = maintenance.SUDO_AUTH_TIMEOUT_SECONDS
        asyncio.create_subprocess_exec = create_process
        maintenance.SUDO_AUTH_TIMEOUT_SECONDS = 0.01
        password = bytearray(b"timeout-secret")
        try:
            result = await maintenance._launch_update_script(password)
        finally:
            maintenance.SUDO_AUTH_TIMEOUT_SECONDS = original_timeout
            asyncio.create_subprocess_exec = original_create_process

        assert result == (False, "sudoパスワードの送信がタイムアウトしました")
        assert killed.is_set()
        assert password
        assert all(value == 0 for value in password)

    asyncio.run(run())


def test_cancellation_while_writing_password_terminates_process_and_reader():
    async def run():
        writing = asyncio.Event()
        killed = asyncio.Event()

        class HangingStdin:
            def write(self, _value):
                return None

            async def drain(self):
                writing.set()
                await asyncio.Event().wait()

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class HangingReader:
            async def read(self, _size=-1):
                await killed.wait()
                return b""

        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.stdin = HangingStdin()
                self.stderr = HangingReader()

            async def wait(self):
                await killed.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                killed.set()

        process = HangingProcess()

        async def create_process(*_command, **_kwargs):
            return process

        original_create_process = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = create_process
        password = bytearray(b"cancel-secret")
        try:
            task = asyncio.create_task(
                maintenance._launch_update_script(password)
            )
            await asyncio.wait_for(writing.wait(), timeout=1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("更新タスクの取消が伝播しませんでした")
        finally:
            asyncio.create_subprocess_exec = original_create_process

        assert killed.is_set()
        assert password
        assert all(value == 0 for value in password)

    asyncio.run(run())


def test_sudo_authentication_timeout_terminates_process_after_password_write():
    async def run():
        killed = asyncio.Event()

        class FakeStdin:
            def write(self, _value):
                return None

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class HangingReader:
            async def read(self, _size=-1):
                await killed.wait()
                return b""

        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.stdin = FakeStdin()
                self.stderr = HangingReader()

            async def wait(self):
                await killed.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                killed.set()

        process = HangingProcess()

        async def create_process(*_command, **_kwargs):
            return process

        original_create_process = asyncio.create_subprocess_exec
        original_timeout = maintenance.SUDO_AUTH_TIMEOUT_SECONDS
        original_os_name = maintenance.os.name
        asyncio.create_subprocess_exec = create_process
        maintenance.SUDO_AUTH_TIMEOUT_SECONDS = 0.01
        maintenance.os.name = "nt"
        password = bytearray(b"pam-timeout-secret")
        try:
            result = await maintenance._launch_update_script(password)
        finally:
            maintenance.os.name = original_os_name
            maintenance.SUDO_AUTH_TIMEOUT_SECONDS = original_timeout
            asyncio.create_subprocess_exec = original_create_process

        assert result == (False, "sudoパスワードを0.01秒以内に確認できませんでした")
        assert killed.is_set()
        assert password
        assert all(value == 0 for value in password)

    asyncio.run(run())


def test_update_script_timeout_terminates_process_group_and_wipes_buffer():
    async def run():
        killed = asyncio.Event()

        class FakeStdin:
            def write(self, _value):
                return None

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class HangingReader:
            def __init__(self):
                self.marker_sent = False

            async def read(self, _size=-1):
                if not self.marker_sent:
                    self.marker_sent = True
                    return maintenance.UPDATE_AUTH_MARKER_BYTES
                await killed.wait()
                return b""

        class HangingProcess:
            def __init__(self):
                self.returncode = None
                self.stdin = FakeStdin()
                self.stderr = HangingReader()

            async def wait(self):
                await killed.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                killed.set()

        process = HangingProcess()

        async def create_process(*command, **_kwargs):
            assert command == maintenance.UPDATE_COMMAND
            return process

        original_create_process = asyncio.create_subprocess_exec
        original_timeout = maintenance.UPDATE_TIMEOUT_SECONDS
        original_os_name = maintenance.os.name
        asyncio.create_subprocess_exec = create_process
        maintenance.UPDATE_TIMEOUT_SECONDS = 0.01
        # Windows のテストでも kill() 経路を決定的に検証する。
        maintenance.os.name = "nt"
        password = bytearray(b"timeout-secret")
        try:
            result = await maintenance._launch_update_script(password)
        finally:
            maintenance.os.name = original_os_name
            maintenance.UPDATE_TIMEOUT_SECONDS = original_timeout
            asyncio.create_subprocess_exec = original_create_process

        assert result == (False, "更新スクリプトが0.01秒以内に完了しませんでした")
        assert killed.is_set()
        assert password
        assert all(value == 0 for value in password)

    asyncio.run(run())


def test_update_error_reader_drains_stream_but_keeps_only_limited_tail():
    async def run():
        class ChunkedReader:
            def __init__(self):
                self.chunks = [b"abcdef", b"ghijkl", b""]

            async def read(self, _size=-1):
                return self.chunks.pop(0)

        result = await maintenance._read_limited_tail(
            ChunkedReader(),
            limit=8,
        )
        assert result == b"efghijkl"

        class MarkerReader:
            def __init__(self):
                marker = maintenance.UPDATE_AUTH_MARKER_BYTES
                self.chunks = [b"before" + marker[:7], marker[7:] + b"after", b""]

            async def read(self, _size=-1):
                return self.chunks.pop(0)

        auth_event = asyncio.Event()
        filtered = await maintenance._read_limited_tail(
            MarkerReader(),
            marker=maintenance.UPDATE_AUTH_MARKER_BYTES,
            marker_event=auth_event,
        )
        assert filtered == b"beforeafter"
        assert auth_event.is_set()

    asyncio.run(run())


def test_completed_update_rejects_background_child_holding_stderr_open():
    async def run():
        reader_cancelled = asyncio.Event()

        class FakeStdin:
            def write(self, _value):
                return None

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class HangingReader:
            def __init__(self):
                self.marker_sent = False

            async def read(self, _size=-1):
                if not self.marker_sent:
                    self.marker_sent = True
                    return maintenance.UPDATE_AUTH_MARKER_BYTES
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    reader_cancelled.set()
                    raise

        class CompletedProcess:
            def __init__(self):
                self.returncode = 0
                self.stdin = FakeStdin()
                self.stderr = HangingReader()

            async def wait(self):
                return 0

            def kill(self):
                raise AssertionError("終了済みの親プロセスをkillしようとしました")

        process = CompletedProcess()

        async def create_process(*_command, **_kwargs):
            return process

        original_create_process = asyncio.create_subprocess_exec
        original_timeout = maintenance.PROCESS_CLEANUP_TIMEOUT_SECONDS
        asyncio.create_subprocess_exec = create_process
        maintenance.PROCESS_CLEANUP_TIMEOUT_SECONDS = 0.01
        password = bytearray(b"background-secret")
        try:
            result = await maintenance._launch_update_script(password)
        finally:
            maintenance.PROCESS_CLEANUP_TIMEOUT_SECONDS = original_timeout
            asyncio.create_subprocess_exec = original_create_process

        assert result == (False, "更新スクリプトの子プロセスが終了しませんでした")
        assert reader_cancelled.is_set()
        assert password
        assert all(value == 0 for value in password)

    asyncio.run(run())


def test_completed_update_kills_background_group_with_closed_stdio():
    async def run():
        kill_signals = []

        class FakeStdin:
            def write(self, _value):
                return None

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class CompletedReader:
            def __init__(self):
                self.chunks = [maintenance.UPDATE_AUTH_MARKER_BYTES, b""]

            async def read(self, _size=-1):
                return self.chunks.pop(0)

        class CompletedProcess:
            pid = 24680

            def __init__(self):
                self.returncode = 0
                self.stdin = FakeStdin()
                self.stderr = CompletedReader()

            async def wait(self):
                return 0

            def kill(self):
                raise AssertionError("終了済みの親プロセスをkillしようとしました")

        process = CompletedProcess()

        async def create_process(*_command, **_kwargs):
            return process

        def killpg(pid, sig):
            assert pid == process.pid
            kill_signals.append(sig)

        original_create_process = asyncio.create_subprocess_exec
        original_os_name = maintenance.os.name
        had_killpg = hasattr(maintenance.os, "killpg")
        original_killpg = getattr(maintenance.os, "killpg", None)
        had_sigkill = hasattr(maintenance.signal, "SIGKILL")
        original_sigkill = getattr(maintenance.signal, "SIGKILL", None)
        asyncio.create_subprocess_exec = create_process
        maintenance.os.name = "posix"
        maintenance.os.killpg = killpg
        maintenance.signal.SIGKILL = 9
        password = bytearray(b"background-secret")
        try:
            result = await maintenance._launch_update_script(password)
        finally:
            if had_killpg:
                maintenance.os.killpg = original_killpg
            else:
                delattr(maintenance.os, "killpg")
            if had_sigkill:
                maintenance.signal.SIGKILL = original_sigkill
            else:
                delattr(maintenance.signal, "SIGKILL")
            maintenance.os.name = original_os_name
            asyncio.create_subprocess_exec = original_create_process

        assert result == (False, "更新スクリプトが背景子プロセスを残しました")
        assert kill_signals == [0, 9]
        assert password
        assert all(value == 0 for value in password)

    asyncio.run(run())


def test_update_requires_password_and_restart_rejects_password():
    async def run():
        external_calls = []

        async def forbidden_get_players():
            external_calls.append("players")
            raise AssertionError("入力拒否後に接続者確認が呼ばれました")

        coordinator = _coordinator(get_players=forbidden_get_players)

        try:
            await coordinator.start_job(action="update")
        except ValueError as exc:
            assert "sudoパスワードが必要" in str(exc)
        else:
            raise AssertionError("sudoパスワードなしの更新が受理されました")

        try:
            await coordinator.start_job(
                action="restart",
                sudo_password="root-secret",
            )
        except ValueError as exc:
            assert "アップデート時だけ" in str(exc)
        else:
            raise AssertionError("再起動にsudoパスワードを指定できました")

        assert external_calls == []
        assert (await coordinator.get_current())["status"] == "idle"

    asyncio.run(run())


def test_invalid_update_passwords_are_rejected_before_external_calls():
    async def run():
        external_calls = []

        async def forbidden_get_players():
            external_calls.append("players")
            raise AssertionError("入力拒否後に接続者確認が呼ばれました")

        coordinator = _coordinator(get_players=forbidden_get_players)
        invalid_passwords = [
            ("", "sudoパスワードが必要"),
            ("line\nbreak", "改行またはNUL"),
            ("line\rbreak", "改行またはNUL"),
            ("nul\x00byte", "改行またはNUL"),
            ("\ud800", "不正なUnicode文字"),
            (
                "x" * (maintenance.SUDO_PASSWORD_MAX_LENGTH + 1),
                f"{maintenance.SUDO_PASSWORD_MAX_LENGTH}文字以内",
            ),
        ]

        for password, expected_error in invalid_passwords:
            try:
                await coordinator.start_job(
                    action="update",
                    sudo_password=password,
                )
            except ValueError as exc:
                assert expected_error in str(exc)
            else:
                raise AssertionError(f"不正なsudoパスワードが受理されました: {password!r}")

        assert external_calls == []
        assert (await coordinator.get_current())["status"] == "idle"

    asyncio.run(run())


def test_job_state_never_contains_sudo_password():
    async def run():
        update_started = asyncio.Event()
        allow_update_to_finish = asyncio.Event()

        async def get_players():
            return {"players": []}

        async def save():
            return None

        async def update():
            update_started.set()
            await allow_update_to_finish.wait()
            return True, ""

        async def verify():
            return None

        secret = "state-secret-value"
        coordinator = _coordinator(
            get_players=get_players,
            save=save,
            update=update,
            verify=verify,
        )
        accepted = await coordinator.start_job(
            action="update",
            sudo_password=secret,
        )
        await asyncio.wait_for(update_started.wait(), timeout=1)
        running = await coordinator.get_current()

        allow_update_to_finish.set()
        completed = await coordinator.wait_until_finished()

        expected_keys = {
            "status",
            "started_at",
            "finished_at",
            "player_count",
            "error",
            "action",
        }
        for state in (accepted, running, completed):
            assert set(state) == expected_keys
            assert secret not in repr(state)

    asyncio.run(run())


def test_sudo_authentication_failure_never_launches_script_or_verifies():
    async def run():
        process_calls = []
        operation_calls = []

        class FakeStdin:
            def __init__(self, call):
                self.call = call

            def write(self, value):
                self.call["input"] = bytes(value)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class FakeReader:
            def __init__(self, value):
                self.value = value

            async def read(self, _size=-1):
                value, self.value = self.value, b""
                return value

        class FailedAuthenticationProcess:
            def __init__(self, call, *, update=False):
                self.call = call
                self.returncode = 1 if update else 0
                self.stdin = FakeStdin(call) if update else None
                self.stderr = FakeReader(b"authentication failed" if update else b"")

            async def wait(self):
                return self.returncode

            async def communicate(self, input=None):
                self.call["input"] = None if input is None else bytes(input)
                return b"", b""

            def kill(self):
                raise AssertionError("終了済みの認証プロセスを kill しようとしました")

        async def create_process(*command, **kwargs):
            call = {
                "command": command,
                "kwargs": kwargs,
                "input": "not-called",
            }
            process_calls.append(call)
            return FailedAuthenticationProcess(
                call,
                update=command == maintenance.UPDATE_COMMAND,
            )

        async def get_players():
            return {"players": []}

        async def save():
            operation_calls.append("save")

        async def verify():
            operation_calls.append("verify")

        original_create_process = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = create_process
        try:
            coordinator = _coordinator(
                get_players=get_players,
                save=save,
                verify=verify,
            )
            await coordinator.start_job(
                action="update",
                sudo_password="wrong-secret",
            )
            state = await coordinator.wait_until_finished()
        finally:
            asyncio.create_subprocess_exec = original_create_process

        assert state["status"] == "failed"
        assert "sudoパスワードの確認または更新に失敗しました" in state["error"]
        assert operation_calls == ["save"]
        assert len(process_calls) == 2
        assert process_calls[0]["command"] == maintenance.UPDATE_COMMAND
        assert process_calls[0]["input"] == b"wrong-secret\n"
        assert process_calls[1]["command"] == maintenance.SUDO_INVALIDATE_COMMAND

    asyncio.run(run())


def test_update_password_buffer_is_wiped_after_completion():
    async def run():
        captured_buffers = []

        async def get_players():
            return {"players": []}

        async def save():
            return None

        async def run_update(password):
            captured_buffers.append(password)
            assert bytes(password) == b"complete-secret"
            return True, ""

        async def verify():
            return None

        original_run_update = maintenance._run_update_command
        maintenance._run_update_command = run_update
        try:
            coordinator = _coordinator(
                get_players=get_players,
                save=save,
                verify=verify,
            )
            await coordinator.start_job(
                action="update",
                sudo_password="complete-secret",
            )
            state = await coordinator.wait_until_finished()
        finally:
            maintenance._run_update_command = original_run_update

        assert state["status"] == "completed"
        assert len(captured_buffers) == 1
        assert captured_buffers[0]
        assert all(value == 0 for value in captured_buffers[0])

    asyncio.run(run())


def test_update_password_buffer_is_wiped_after_cancellation():
    async def run():
        countdown_started = asyncio.Event()
        captured_buffers = []
        saw_unwiped_password = []

        async def get_players():
            return {"players": []}

        async def countdown(_seconds, cancel_event):
            countdown_started.set()
            await cancel_event.wait()
            return True

        async def forbidden_update():
            raise AssertionError("キャンセル後に更新が呼ばれました")

        original_wipe = maintenance._wipe_secret

        def track_wipe(secret):
            if secret is not None:
                captured_buffers.append(secret)
                saw_unwiped_password.append(any(secret))
            original_wipe(secret)

        maintenance._wipe_secret = track_wipe
        try:
            coordinator = _coordinator(
                get_players=get_players,
                update=forbidden_update,
                countdown_wait=countdown,
            )
            await coordinator.start_job(
                action="update",
                countdown_seconds=300,
                sudo_password="cancel-secret",
            )
            await asyncio.wait_for(countdown_started.wait(), timeout=1)
            await coordinator.cancel_current()
            state = await coordinator.wait_until_finished()
        finally:
            maintenance._wipe_secret = original_wipe

        assert state["status"] == "cancelled"
        assert saw_unwiped_password == [True]
        assert len(captured_buffers) == 1
        assert captured_buffers[0]
        assert all(value == 0 for value in captured_buffers[0])

    asyncio.run(run())


def test_update_password_buffer_is_wiped_after_update_exception():
    async def run():
        captured_buffers = []
        operation_calls = []

        async def get_players():
            return {"players": []}

        async def save():
            return None

        async def run_update(password):
            captured_buffers.append(password)
            assert bytes(password) == b"exception-secret"
            raise RuntimeError("update exploded")

        async def verify():
            operation_calls.append("verify")

        original_run_update = maintenance._run_update_command
        maintenance._run_update_command = run_update
        try:
            coordinator = _coordinator(
                get_players=get_players,
                save=save,
                verify=verify,
            )
            await coordinator.start_job(
                action="update",
                sudo_password="exception-secret",
            )
            state = await coordinator.wait_until_finished()
        finally:
            maintenance._run_update_command = original_run_update

        assert state["status"] == "failed"
        assert "update exploded" in state["error"]
        assert operation_calls == []
        assert len(captured_buffers) == 1
        assert captured_buffers[0]
        assert all(value == 0 for value in captured_buffers[0])

    asyncio.run(run())


def test_external_update_blocks_before_player_check():
    async def run():
        calls = []

        async def update_in_progress():
            calls.append("update-check")
            return True

        async def get_players():
            calls.append("players")
            return {"players": []}

        coordinator = _coordinator(
            get_players=get_players,
            update_in_progress=update_in_progress,
        )
        try:
            await coordinator.start_job(action="restart")
        except maintenance.JobAlreadyRunningError:
            pass
        else:
            raise AssertionError("外部更新中なのにジョブが受理されました")

        assert calls == ["update-check"]
        assert (await coordinator.get_current())["status"] == "idle"

    asyncio.run(run())


def test_recent_restart_is_skipped_before_save():
    async def run():
        calls = []

        async def get_players():
            calls.append("players")
            return {"players": []}

        async def save():
            calls.append("save")

        async def restart():
            calls.append("restart")
            return True, ""

        coordinator = _coordinator(
            get_players=get_players,
            save=save,
            restart=restart,
            restart_was_recent=lambda: True,
        )
        await coordinator.start_job(action="restart")
        state = await coordinator.wait_until_finished()

        assert state["status"] == "skipped"
        assert "直近に再起動済み" in state["error"]
        assert calls == ["players"]

    asyncio.run(run())


def test_allow_players_update_runs_save_update_verify_and_marks_debounce():
    async def run():
        calls = []

        async def get_players():
            calls.append("players")
            return {"players": [{"name": "Alice"}]}

        async def save():
            calls.append("save")

        async def update():
            calls.append("update")
            return True, ""

        async def verify():
            calls.append("verify")

        mark_calls = []
        original_mark = maintenance.scheduler.mark_restart_completed
        maintenance.scheduler.mark_restart_completed = lambda: mark_calls.append("mark")
        try:
            coordinator = _coordinator(
                get_players=get_players,
                save=save,
                update=update,
                verify=verify,
                restart_was_recent=lambda: True,
            )
            await coordinator.start_job(
                action="update",
                allow_players=True,
                sudo_password="root-secret",
            )
            state = await coordinator.wait_until_finished()
        finally:
            maintenance.scheduler.mark_restart_completed = original_mark

        assert state["status"] == "completed"
        assert state["player_count"] == 1
        assert calls == ["players", "players", "save", "update", "verify"]
        assert mark_calls == ["mark"]

    asyncio.run(run())


def test_existing_operation_lock_rejects_new_manual_job():
    async def run():
        calls = []
        operation_lock = asyncio.Lock()
        original_lock = maintenance.scheduler.maintenance_operation_lock
        maintenance.scheduler.maintenance_operation_lock = operation_lock

        async def get_players():
            return {"players": []}

        async def save():
            calls.append("save")

        async def restart():
            calls.append("restart")
            return True, ""

        async def verify():
            calls.append("verify")

        await operation_lock.acquire()
        try:
            coordinator = _coordinator(
                get_players=get_players,
                save=save,
                restart=restart,
                verify=verify,
            )
            try:
                await coordinator.start_job(action="restart")
            except maintenance.JobAlreadyRunningError:
                pass
            else:
                raise AssertionError("既存操作中に手動ジョブが受理されました")
            assert calls == []
            assert (await coordinator.get_current())["status"] == "idle"
        finally:
            if operation_lock.locked():
                operation_lock.release()
            maintenance.scheduler.maintenance_operation_lock = original_lock

    asyncio.run(run())


def test_shutdown_cancels_a_reversible_countdown():
    async def run():
        countdown_started = asyncio.Event()

        async def get_players():
            return {"players": []}

        async def countdown(_seconds, cancel_event):
            countdown_started.set()
            await cancel_event.wait()
            return True

        coordinator = _coordinator(
            get_players=get_players,
            countdown_wait=countdown,
        )
        await coordinator.start_job(action="restart", countdown_seconds=300)
        await asyncio.wait_for(countdown_started.wait(), timeout=1)
        await coordinator.shutdown()
        assert (await coordinator.get_current())["status"] == "cancelled"

    asyncio.run(run())


def test_shutdown_waits_for_saving_instead_of_cancelling_it():
    async def run():
        save_started = asyncio.Event()
        allow_save_to_finish = asyncio.Event()

        async def get_players():
            return {"players": []}

        async def save():
            save_started.set()
            await allow_save_to_finish.wait()

        async def restart():
            return True, ""

        async def verify():
            return None

        coordinator = _coordinator(
            get_players=get_players,
            save=save,
            restart=restart,
            verify=verify,
        )
        await coordinator.start_job(action="restart")
        await asyncio.wait_for(save_started.wait(), timeout=1)
        shutdown_task = asyncio.create_task(coordinator.shutdown())
        await asyncio.sleep(0.03)
        assert not shutdown_task.done(), "saving 中のタスクが打ち切られました"
        allow_save_to_finish.set()
        await asyncio.wait_for(shutdown_task, timeout=1)
        assert (await coordinator.get_current())["status"] == "completed"

    asyncio.run(run())


def test_shutdown_waits_for_update_admission_and_then_for_the_job():
    async def run():
        admission_started = asyncio.Event()
        allow_admission = asyncio.Event()
        update_started = asyncio.Event()
        allow_update_to_finish = asyncio.Event()
        player_checks = 0

        async def get_players():
            nonlocal player_checks
            player_checks += 1
            if player_checks == 1:
                admission_started.set()
                await allow_admission.wait()
            return {"players": []}

        async def save():
            return None

        async def update():
            update_started.set()
            await allow_update_to_finish.wait()
            return True, ""

        async def verify():
            return None

        coordinator = _coordinator(
            get_players=get_players,
            save=save,
            update=update,
            verify=verify,
        )
        start_task = asyncio.create_task(
            coordinator.start_job(
                action="update",
                sudo_password="admission-secret",
            )
        )
        await asyncio.wait_for(admission_started.wait(), timeout=1)

        shutdown_task = asyncio.create_task(coordinator.shutdown())
        await asyncio.sleep(0.03)
        assert not shutdown_task.done(), "受付確認中の終了処理が先に完了しました"

        allow_admission.set()
        await asyncio.wait_for(start_task, timeout=1)
        await asyncio.wait_for(update_started.wait(), timeout=1)
        assert not shutdown_task.done(), "受付後の更新ジョブを待ちませんでした"

        allow_update_to_finish.set()
        await asyncio.wait_for(shutdown_task, timeout=1)
        assert (await coordinator.get_current())["status"] == "completed"

    asyncio.run(run())


def test_shutdown_waits_for_direct_update_instead_of_cancelling_it():
    async def run():
        update_started = asyncio.Event()
        allow_update_to_finish = asyncio.Event()
        update_cancelled = asyncio.Event()

        async def get_players():
            return {"players": []}

        async def save():
            return None

        async def update():
            update_started.set()
            try:
                await allow_update_to_finish.wait()
            except asyncio.CancelledError:
                update_cancelled.set()
                raise
            return True, ""

        async def verify():
            return None

        coordinator = _coordinator(
            get_players=get_players,
            save=save,
            update=update,
            verify=verify,
        )
        await coordinator.start_job(
            action="update",
            sudo_password="root-secret",
        )
        await asyncio.wait_for(update_started.wait(), timeout=1)

        shutdown_task = asyncio.create_task(coordinator.shutdown())
        await asyncio.sleep(0.03)
        assert not shutdown_task.done(), "updating 中のタスクが打ち切られました"
        assert not update_cancelled.is_set()

        allow_update_to_finish.set()
        await asyncio.wait_for(shutdown_task, timeout=1)

        assert not update_cancelled.is_set()
        state = await coordinator.get_current()
        assert state["status"] == "completed"
        assert state["error"] is None

    asyncio.run(run())


def test_duplicate_job_is_rejected_while_first_is_running():
    async def run():
        player_checks = 0
        countdown_started = asyncio.Event()

        async def get_players():
            nonlocal player_checks
            player_checks += 1
            return {"players": []}

        async def countdown(_seconds, cancel_event):
            countdown_started.set()
            await cancel_event.wait()
            return True

        coordinator = _coordinator(
            get_players=get_players,
            countdown_wait=countdown,
        )
        await coordinator.start_job(action="restart", countdown_seconds=300)
        await asyncio.wait_for(countdown_started.wait(), timeout=1)

        try:
            await coordinator.start_job(
                action="update",
                allow_players=True,
                sudo_password="root-secret",
            )
        except maintenance.JobAlreadyRunningError:
            pass
        else:
            raise AssertionError("重複ジョブが受理されました")

        assert player_checks == 1
        await coordinator.cancel_current()
        await coordinator.wait_until_finished()

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
