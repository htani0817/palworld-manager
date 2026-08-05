# -*- coding: utf-8 -*-
"""安全な再起動・更新を直列化する単一ワーカー用ジョブ管理。"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Mapping, Optional

import palworld_client as pal
import scheduler


MaintenanceAction = Literal["restart", "update"]
MaintenanceStatus = Literal[
    "idle",
    "checking",
    "countdown",
    "saving",
    "restarting",
    "updating",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "skipped",
]

ACTIVE_STATUSES = frozenset(
    {"checking", "countdown", "saving", "restarting", "updating", "verifying"}
)

UPDATE_SCRIPT = "/home/palworld-user/scripts/update.sh"
UPDATE_RUN_AS_USER = "palworld-user"
UPDATE_WORKING_DIRECTORY = "/home/palworld-user"
UPDATE_AUTH_MARKER = "__PALWORLD_MANAGER_SUDO_AUTH_OK_4F8B2D__"
UPDATE_AUTH_MARKER_BYTES = (UPDATE_AUTH_MARKER + "\n").encode("ascii")
UPDATE_SHELL_COMMAND = (
    "sudo -S -k -p '' -- sh -c "
    f"""'printf "%s\\n" "{UPDATE_AUTH_MARKER}" >&2; """
    "exec /home/palworld-user/scripts/update.sh </dev/null'; "
    'status=$?; sudo -k; exit "$status"'
)
UPDATE_COMMAND = (
    "runuser",
    "-u",
    UPDATE_RUN_AS_USER,
    "--",
    "sh",
    "-c",
    UPDATE_SHELL_COMMAND,
)
SUDO_INVALIDATE_COMMAND = (
    "runuser",
    "-u",
    UPDATE_RUN_AS_USER,
    "--",
    "sudo",
    "-k",
)
SUDO_PASSWORD_MAX_LENGTH = 1024
SUDO_AUTH_TIMEOUT_SECONDS = 30.0
SUDO_INVALIDATE_TIMEOUT_SECONDS = 10.0
UPDATE_TIMEOUT_SECONDS = 1800.0
PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
UPDATE_ERROR_OUTPUT_MAX_BYTES = 65536
VERIFY_TIMEOUT_SECONDS = 180.0
VERIFY_POLL_SECONDS = 2.0
VERIFY_INITIAL_DELAY_SECONDS = 2.0
SHUTDOWN_GRACE_SECONDS = 15.0
SHUTDOWN_POLL_SECONDS = 0.5

logger = logging.getLogger("palworld_manager.maintenance")


class MaintenanceError(RuntimeError):
    """メンテナンスジョブの基底例外。"""


class JobAlreadyRunningError(MaintenanceError):
    """別のメンテナンスジョブが実行中。"""


class PlayersConnectedError(MaintenanceError):
    """接続者がいるため、安全側に拒否した。"""

    def __init__(self, player_count: int):
        self.player_count = player_count
        super().__init__(
            f"接続中のプレイヤーが {player_count} 人いるため実行しません。"
            "allow_players=true を明示すると実行できます"
        )


class PlayerCheckError(MaintenanceError):
    """接続者数を安全に確認できない。"""


class JobNotCancellableError(MaintenanceError):
    """現在のジョブはカウントダウン中ではない。"""


@dataclass
class JobState:
    status: MaintenanceStatus = "idle"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    player_count: Optional[int] = None
    error: Optional[str] = None
    action: Optional[MaintenanceAction] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "player_count": self.player_count,
            "error": self.error,
            "action": self.action,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exception_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _wipe_secret(secret: Optional[bytearray]) -> None:
    """可能な範囲で一時認証情報を上書きする。"""
    if secret is None:
        return
    for index in range(len(secret)):
        secret[index] = 0


def _kill_posix_process_group(proc: asyncio.subprocess.Process) -> bool:
    """親が終了済みでも、同じPGIDに残った更新子プロセスを終了する。"""
    if os.name != "posix":
        return False
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        logger.warning("更新プロセスグループを終了できませんでした", exc_info=True)
        return False


async def _terminate_process(
    proc: asyncio.subprocess.Process,
    *,
    process_group: bool = False,
) -> None:
    """タイムアウトまたは取消時に、子プロセスを有限時間で終了させる。"""
    if proc.returncode is not None:
        if process_group:
            _kill_posix_process_group(proc)
        return

    terminated = False
    if process_group:
        terminated = _kill_posix_process_group(proc)

    if not terminated:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            logger.warning("子プロセスを終了できませんでした", exc_info=True)
            return

    try:
        wait_method = getattr(proc, "wait", None)
        wait_for_exit = (
            wait_method()
            if callable(wait_method)
            else proc.communicate()
        )
        await asyncio.wait_for(
            wait_for_exit,
            timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("終了させた子プロセスの回収に失敗しました", exc_info=True)


def _non_negative_count(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def count_connected_players(payload: Any) -> int:
    """REST /players の応答から、安全側で接続者数を求める。"""
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, Mapping):
        raise ValueError("/players の応答形式が不正です")

    counts: list[int] = []
    if "players" in payload:
        players = payload["players"]
        if isinstance(players, list):
            counts.append(len(players))
        elif players is not None:
            raise ValueError("/players の players が配列ではありません")

    for key in ("player_count", "count"):
        if key in payload:
            count = _non_negative_count(payload[key])
            if count is None:
                raise ValueError(f"/players の {key} が0以上の整数ではありません")
            counts.append(count)

    if not counts:
        raise ValueError("/players の応答に接続者情報がありません")
    # 配列長と count が矛盾する場合も、過小評価しない。
    return max(counts)


async def _default_get_players() -> Any:
    return await pal.get_players()


async def _default_announce(message: str) -> None:
    await pal.post_announce(message)


async def _default_save() -> None:
    await pal.post_save()


async def _restart_service() -> tuple[bool, str]:
    """保存済みのワールドを再度保存せず、既存の安全な systemctl 実行部を使う。"""
    return await scheduler.restart_service_after_save()


async def _write_sudo_password(
    proc: asyncio.subprocess.Process,
    sudo_password: bytearray,
) -> None:
    """sudo の標準入力へ1行だけ書き、更新スクリプト開始前に閉じる。"""
    stdin = proc.stdin
    if stdin is None:
        raise RuntimeError("sudo の標準入力を開けませんでした")

    sudo_password.append(0x0A)
    try:
        # StreamWriter.write() は渡された bytearray をコピーせず参照のまま
        # 内部バッファへ保持することがある。さらに drain() は「バッファに空きが
        # できるまで待つ」だけで、実際にパイプへ書き出されたことは保証しない。
        # そのため bytearray のまま渡すと、finally の _wipe_secret() が
        # 送信前のバッファをゼロ埋めし、sudo へ NUL 列が渡って認証に失敗する。
        # bytes() でコピーを作り、ワイプの影響を受けないようにする。
        stdin.write(bytes(sudo_password))
        await asyncio.wait_for(
            stdin.drain(),
            timeout=SUDO_AUTH_TIMEOUT_SECONDS,
        )
    finally:
        _wipe_secret(sudo_password)
        stdin.close()
        try:
            wait_closed = getattr(stdin, "wait_closed", None)
            if callable(wait_closed):
                await asyncio.wait_for(
                    wait_closed(),
                    timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS,
                )
        except Exception:
            # close() は完了しているため、後始末の待機失敗は更新処理を妨げない。
            logger.debug("sudo 標準入力のclose待機に失敗しました", exc_info=True)


async def _read_limited_tail(
    stream: Optional[asyncio.StreamReader],
    *,
    limit: int = UPDATE_ERROR_OUTPUT_MAX_BYTES,
    marker: Optional[bytes] = None,
    marker_event: Optional[asyncio.Event] = None,
) -> bytes:
    """パイプを詰まらせずに読み、認証印を除いた末尾だけを保持する。"""
    if stream is None:
        return b""

    tail = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(tail)
        tail.extend(chunk)
        if marker is not None:
            marker_index = tail.find(marker)
            if marker_index >= 0:
                del tail[marker_index : marker_index + len(marker)]
                marker = None
                if marker_event is not None:
                    marker_event.set()
        if len(tail) > limit:
            del tail[: len(tail) - limit]


async def _settle_pipe_reader(task: Optional[asyncio.Task[Any]]) -> None:
    """終了させたプロセスのパイプ読取タスクを残さない。"""
    if task is None:
        return
    try:
        await asyncio.wait_for(
            task,
            timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _wait_for_sudo_authentication(
    auth_event: asyncio.Event,
    process_wait_task: asyncio.Task[int],
) -> Literal["authenticated", "exited", "timeout"]:
    """root側shellの認証完了印か、プロセス終了を最大30秒待つ。"""
    auth_wait_task = asyncio.create_task(
        auth_event.wait(),
        name="maintenance-sudo-auth",
    )
    try:
        done, _ = await asyncio.wait(
            {auth_wait_task, process_wait_task},
            timeout=SUDO_AUTH_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if auth_event.is_set():
            return "authenticated"
        if process_wait_task in done:
            return "exited"
        return "timeout"
    finally:
        if not auth_wait_task.done():
            auth_wait_task.cancel()
        await asyncio.gather(auth_wait_task, return_exceptions=True)


async def _finish_update_stderr(
    proc: asyncio.subprocess.Process,
    task: asyncio.Task[bytes],
) -> tuple[bytes, bool]:
    """親終了後も有限時間でstderrを閉じ、残った背景子を許可しない。"""
    try:
        return (
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS,
            ),
            True,
        )
    except asyncio.TimeoutError:
        _kill_posix_process_group(proc)
        await _settle_pipe_reader(task)
        return b"", False
    except asyncio.CancelledError:
        _kill_posix_process_group(proc)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


def _posix_process_group_is_alive(proc: asyncio.subprocess.Process) -> bool:
    """親終了後も同じPGIDの背景子が残っているか確認する。"""
    if os.name != "posix":
        return False
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return False
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        logger.warning("更新プロセスグループの残存確認に失敗しました", exc_info=True)
        return True


def _update_failure_detail(stderr: bytes, returncode: Optional[int]) -> str:
    error = stderr.decode(errors="replace").strip()
    detail = error or f"exit {returncode}"
    return f"sudoパスワードの確認または更新に失敗しました: {detail}"


async def _launch_update_script(
    sudo_password: bytearray,
) -> tuple[bool, str]:
    """palworld-user の sudo で固定パスの更新スクリプトを直接実行する。"""
    proc: Optional[asyncio.subprocess.Process] = None
    stderr_task: Optional[asyncio.Task[bytes]] = None
    process_wait_task: Optional[asyncio.Task[int]] = None
    try:
        process_options = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": UPDATE_WORKING_DIRECTORY,
        }
        if os.name == "posix":
            # TTYのないsystemdサービスでは、runuser・sudo・スクリプトを
            # 同じプロセスグループにし、期限超過時にまとめて終了する。
            process_options["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(*UPDATE_COMMAND, **process_options)
        auth_event = asyncio.Event()
        stderr_task = asyncio.create_task(
            _read_limited_tail(
                proc.stderr,
                marker=UPDATE_AUTH_MARKER_BYTES,
                marker_event=auth_event,
            ),
            name="maintenance-update-stderr",
        )
        try:
            await _write_sudo_password(proc, sudo_password)
        except asyncio.TimeoutError:
            await _terminate_process(proc, process_group=True)
            await _settle_pipe_reader(stderr_task)
            return False, "sudoパスワードの送信がタイムアウトしました"

        process_wait_task = asyncio.create_task(
            proc.wait(),
            name="maintenance-update-process",
        )
        auth_result = await _wait_for_sudo_authentication(
            auth_event,
            process_wait_task,
        )
        if auth_result == "timeout":
            await _terminate_process(proc, process_group=True)
            await _settle_pipe_reader(process_wait_task)
            await _settle_pipe_reader(stderr_task)
            return False, (
                f"sudoパスワードを{SUDO_AUTH_TIMEOUT_SECONDS:g}秒以内に"
                "確認できませんでした"
            )
        if auth_result == "exited":
            await _settle_pipe_reader(process_wait_task)
            stderr, stderr_closed = await _finish_update_stderr(proc, stderr_task)
            if not stderr_closed:
                return False, "更新スクリプトの子プロセスが終了しませんでした"
            if _posix_process_group_is_alive(proc):
                _kill_posix_process_group(proc)
                return False, "更新スクリプトが背景子プロセスを残しました"
            if not auth_event.is_set():
                if proc.returncode != 0:
                    return False, _update_failure_detail(stderr, proc.returncode)
                return False, "sudo認証の完了を確認できませんでした"
            if proc.returncode != 0:
                return False, _update_failure_detail(stderr, proc.returncode)
            return True, ""

        try:
            await asyncio.wait_for(
                asyncio.shield(process_wait_task),
                timeout=UPDATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await _terminate_process(proc, process_group=True)
            await _settle_pipe_reader(process_wait_task)
            await _settle_pipe_reader(stderr_task)
            return False, (
                f"更新スクリプトが{UPDATE_TIMEOUT_SECONDS:g}秒以内に"
                "完了しませんでした"
            )
        except asyncio.CancelledError:
            await _terminate_process(proc, process_group=True)
            await _settle_pipe_reader(process_wait_task)
            await _settle_pipe_reader(stderr_task)
            raise
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process(proc, process_group=True)
        await _settle_pipe_reader(process_wait_task)
        await _settle_pipe_reader(stderr_task)
        raise
    except Exception as exc:
        if proc is not None:
            await _terminate_process(proc, process_group=True)
        await _settle_pipe_reader(process_wait_task)
        await _settle_pipe_reader(stderr_task)
        return False, f"更新スクリプトを開始できませんでした: {_exception_text(exc)}"
    finally:
        _wipe_secret(sudo_password)

    stderr, stderr_closed = (
        await _finish_update_stderr(proc, stderr_task)
        if stderr_task is not None
        else (b"", True)
    )
    if not stderr_closed:
        return False, "更新スクリプトの子プロセスが終了しませんでした"
    if _posix_process_group_is_alive(proc):
        _kill_posix_process_group(proc)
        return False, "更新スクリプトが背景子プロセスを残しました"

    if proc.returncode != 0:
        return False, _update_failure_detail(stderr, proc.returncode)
    return True, ""


async def _invalidate_sudo_timestamp() -> None:
    """認証用に作成された sudo のタイムスタンプを更新終了時に破棄する。"""
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *SUDO_INVALIDATE_COMMAND,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(
            proc.communicate(),
            timeout=SUDO_INVALIDATE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            logger.warning("sudo の認証キャッシュを破棄できませんでした")
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process(proc)
        raise
    except Exception:
        if proc is not None and proc.returncode is None:
            await _terminate_process(proc)
        logger.warning("sudo の認証キャッシュ破棄に失敗しました", exc_info=True)


async def _run_update_command(sudo_password: bytearray) -> tuple[bool, str]:
    """sudoへパスワードを渡し、固定スクリプトの終了コードを返す。"""
    try:
        return await _launch_update_script(sudo_password)
    finally:
        _wipe_secret(sudo_password)
        await _invalidate_sudo_timestamp()


async def _verify_restored() -> None:
    """REST /info が復旧するまで、有限時間でポーリングする。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + VERIFY_TIMEOUT_SECONDS
    if VERIFY_INITIAL_DELAY_SECONDS > 0:
        await asyncio.sleep(min(VERIFY_INITIAL_DELAY_SECONDS, VERIFY_TIMEOUT_SECONDS))

    last_error = "応答がありません"
    while True:
        try:
            await pal.get_info()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _exception_text(exc)

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(
                f"Palworld REST API /info が {VERIFY_TIMEOUT_SECONDS:g} 秒以内に復旧しませんでした"
                f"（最終エラー: {last_error}）"
            )
        await asyncio.sleep(min(VERIFY_POLL_SECONDS, remaining))


async def _interruptible_countdown(seconds: float, cancel_event: asyncio.Event) -> bool:
    """キャンセル可能な待機。キャンセル時は True を返す。"""
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def _operation_failure(result: Any, operation_name: str) -> Optional[str]:
    """差し替え可能な外部操作の戻り値を共通判定する。"""
    if result is None or result is True:
        return None
    if result is False:
        return f"{operation_name} が失敗しました"
    if isinstance(result, tuple) and result:
        ok = bool(result[0])
        if ok:
            return None
        detail = str(result[1]).strip() if len(result) > 1 else ""
        return detail or f"{operation_name} が失敗しました"
    if isinstance(result, Mapping):
        outcome = result.get("result")
        if outcome == "ok":
            return None
        detail = str(result.get("error") or result.get("reason") or "").strip()
        return detail or f"{operation_name} が失敗しました"
    return None


class MaintenanceCoordinator:
    """同時に1件だけメンテナンスジョブを実行する。"""

    def __init__(
        self,
        *,
        get_players: Optional[Callable[[], Awaitable[Any]]] = None,
        announce: Optional[Callable[[str], Awaitable[Any]]] = None,
        save: Optional[Callable[[], Awaitable[Any]]] = None,
        restart: Optional[Callable[[], Awaitable[Any]]] = None,
        update: Optional[Callable[[], Awaitable[Any]]] = None,
        verify: Optional[Callable[[], Awaitable[Any]]] = None,
        countdown_wait: Optional[
            Callable[[float, asyncio.Event], Awaitable[bool]]
        ] = None,
        update_in_progress: Optional[Callable[[], Awaitable[bool]]] = None,
        restart_was_recent: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._get_players = get_players or _default_get_players
        self._announce = announce or _default_announce
        self._save = save or _default_save
        self._restart = restart or _restart_service
        self._update_override = update
        self._verify = verify or _verify_restored
        self._countdown_wait = countdown_wait or _interruptible_countdown
        self._update_in_progress = update_in_progress or scheduler.is_update_in_progress
        self._restart_was_recent = restart_was_recent or scheduler.restart_was_recent
        self._lock = asyncio.Lock()
        self._state = JobState()
        self._task: Optional[asyncio.Task[None]] = None
        self._cancel_event: Optional[asyncio.Event] = None
        self._admission_done: Optional[asyncio.Event] = None

    def _is_running_locked(self) -> bool:
        if self._task is not None and not self._task.done():
            return True
        return self._state.status in ACTIVE_STATUSES

    async def _set_status(self, status: MaintenanceStatus) -> None:
        async with self._lock:
            self._state.status = status

    async def _finish(
        self,
        status: Literal["completed", "failed", "cancelled", "skipped"],
        error: Optional[str] = None,
    ) -> None:
        async with self._lock:
            self._state.status = status
            self._state.finished_at = _now_iso()
            self._state.error = error
            self._cancel_event = None

    async def _begin_saving(self, cancel_event: asyncio.Event) -> bool:
        """カウントダウン終了と DELETE の競合をロック下で解決する。"""
        async with self._lock:
            if cancel_event.is_set() or self._state.status == "cancelled":
                self._state.status = "cancelled"
                self._state.finished_at = self._state.finished_at or _now_iso()
                self._state.error = None
                self._cancel_event = None
                return False
            self._state.status = "saving"
            return True

    async def start_job(
        self,
        *,
        action: MaintenanceAction,
        allow_players: bool = False,
        countdown_seconds: int = 0,
        message: Optional[str] = None,
        sudo_password: Optional[str] = None,
    ) -> dict[str, Any]:
        """接続者を確認し、許可できたジョブだけを即時にタスク化する。"""
        if action not in ("restart", "update"):
            raise ValueError("action は restart または update で指定してください")
        if type(allow_players) is not bool:
            raise ValueError("allow_players は真偽値で指定してください")
        if type(countdown_seconds) is not int or not 0 <= countdown_seconds <= 300:
            raise ValueError("countdown_seconds は 0 から 300 の整数で指定してください")
        if message is not None and not isinstance(message, str):
            raise ValueError("message は文字列または null で指定してください")
        if message is not None and len(message) > 500:
            raise ValueError("message は500文字以内で指定してください")
        if action == "update":
            if not isinstance(sudo_password, str) or not sudo_password:
                raise ValueError("アップデートにはsudoパスワードが必要です")
            if len(sudo_password) > SUDO_PASSWORD_MAX_LENGTH:
                raise ValueError(
                    f"sudoパスワードは{SUDO_PASSWORD_MAX_LENGTH}文字以内で指定してください"
                )
            if any(character in sudo_password for character in ("\r", "\n", "\x00")):
                raise ValueError("sudoパスワードに改行またはNUL文字は使用できません")
            if any(0xD800 <= ord(character) <= 0xDFFF for character in sudo_password):
                raise ValueError("sudoパスワードに不正なUnicode文字は使用できません")
        elif sudo_password is not None:
            raise ValueError("sudoパスワードはアップデート時だけ指定してください")

        async with self._lock:
            if self._is_running_locked():
                raise JobAlreadyRunningError("別のメンテナンスジョブが実行中です")
            reservation_token = object()
            if not scheduler.reserve_manual_maintenance(reservation_token):
                raise JobAlreadyRunningError(
                    "別のサーバー操作またはメンテナンスが実行中です"
                )
            self._state = JobState(
                status="checking",
                started_at=_now_iso(),
                action=action,
            )
            self._cancel_event = None
            admission_done = asyncio.Event()
            self._admission_done = admission_done

        try:
            try:
                update_in_progress = await self._update_in_progress()
            except BaseException:
                async with self._lock:
                    self._state = JobState()
                scheduler.release_manual_maintenance(reservation_token)
                raise
            if update_in_progress:
                async with self._lock:
                    self._state = JobState()
                scheduler.release_manual_maintenance(reservation_token)
                raise JobAlreadyRunningError(
                    "palworld-update.service が実行中か、状態を確認できません"
                )

            try:
                payload = await self._get_players()
                player_count = count_connected_players(payload)
            except asyncio.CancelledError:
                async with self._lock:
                    self._state = JobState()
                scheduler.release_manual_maintenance(reservation_token)
                raise
            except Exception as exc:
                error = f"接続中プレイヤーの確認に失敗しました: {_exception_text(exc)}"
                async with self._lock:
                    self._state.status = "failed"
                    self._state.finished_at = _now_iso()
                    self._state.error = error
                scheduler.release_manual_maintenance(reservation_token)
                raise PlayerCheckError(error) from exc

            async with self._lock:
                self._state.player_count = player_count
                if player_count > 0 and not allow_players:
                    # 拒否はジョブとして受付けず、外部への変更も一切行わない。
                    self._state = JobState()
                    scheduler.release_manual_maintenance(reservation_token)
                    raise PlayersConnectedError(player_count)

                cancel_event = asyncio.Event()
                self._cancel_event = cancel_event
                password_buffer: Optional[bytearray] = None
                try:
                    password_buffer = (
                        bytearray(sudo_password, "utf-8")
                        if action == "update" and sudo_password is not None
                        else None
                    )
                    self._task = asyncio.create_task(
                        self._execute_job(
                            action=action,
                            allow_players=allow_players,
                            countdown_seconds=countdown_seconds,
                            message=message,
                            sudo_password=password_buffer,
                            cancel_event=cancel_event,
                            reservation_token=reservation_token,
                        ),
                        name=f"maintenance-{action}",
                    )
                except BaseException:
                    _wipe_secret(password_buffer)
                    self._state = JobState()
                    self._cancel_event = None
                    scheduler.release_manual_maintenance(reservation_token)
                    raise
                return self._state.as_dict()
        finally:
            admission_done.set()
            async with self._lock:
                if self._admission_done is admission_done:
                    self._admission_done = None

    async def _execute_job(
        self,
        *,
        action: MaintenanceAction,
        allow_players: bool,
        countdown_seconds: int,
        message: Optional[str],
        sudo_password: Optional[bytearray],
        cancel_event: asyncio.Event,
        reservation_token: object,
    ) -> None:
        try:
            if message or countdown_seconds > 0:
                # 予告中も予約再起動にロックを渡さず、二重停止を防ぐ。
                async with scheduler.maintenance_operation_lock:
                    await self._set_status("countdown")

                    if message:
                        try:
                            await self._announce(message)
                        except Exception as exc:
                            raise RuntimeError(
                                f"アナウンスに失敗しました: {_exception_text(exc)}"
                            ) from exc

                    cancelled = cancel_event.is_set()
                    if countdown_seconds > 0 and not cancelled:
                        cancelled = await self._countdown_wait(countdown_seconds, cancel_event)
                    if cancelled or cancel_event.is_set():
                        await self._finish("cancelled")
                        return

            # 予約再起動や旧API経由の操作とも共通ロックを使い、保存から
            # 復旧確認までを直列化する。ロック待ちの間も countdown 状態なら
            # DELETE で安全にキャンセルできる。
            async with scheduler.maintenance_operation_lock:
                if cancel_event.is_set():
                    await self._finish("cancelled")
                    return
                if await self._update_in_progress():
                    raise RuntimeError(
                        "palworld-update.service が実行中か、状態を確認できません"
                    )
                if action == "restart" and self._restart_was_recent():
                    await self._finish(
                        "skipped", "直近に再起動済みのため再起動を省略しました"
                    )
                    return

                try:
                    final_payload = await self._get_players()
                    final_player_count = count_connected_players(final_payload)
                except Exception as exc:
                    raise RuntimeError(
                        f"保存直前の接続者確認に失敗しました: {_exception_text(exc)}"
                    ) from exc

                async with self._lock:
                    self._state.player_count = final_player_count
                if final_player_count > 0 and not allow_players:
                    raise RuntimeError(
                        f"保存直前にプレイヤー接続を確認したため実行しませんでした"
                        f"（接続中: {final_player_count}人）"
                    )

                if not await self._begin_saving(cancel_event):
                    return

                try:
                    await self._save()
                except Exception as exc:
                    raise RuntimeError(
                        f"ワールド保存に失敗しました: {_exception_text(exc)}"
                    ) from exc

                if action == "restart":
                    await self._set_status("restarting")
                    try:
                        result = await self._restart()
                    except Exception as exc:
                        raise RuntimeError(
                            f"再起動に失敗しました: {_exception_text(exc)}"
                        ) from exc
                    failure = _operation_failure(result, "再起動")
                else:
                    await self._set_status("updating")
                    try:
                        if sudo_password is None:
                            raise RuntimeError("sudoパスワードがありません")
                        if self._update_override is None:
                            result = await _run_update_command(sudo_password)
                        else:
                            result = await self._update_override()
                    except Exception as exc:
                        raise RuntimeError(
                            f"更新に失敗しました: {_exception_text(exc)}"
                        ) from exc
                    finally:
                        _wipe_secret(sudo_password)
                    failure = _operation_failure(result, "更新")

                if failure is not None:
                    raise RuntimeError(failure)

                await self._set_status("verifying")
                try:
                    verified = await self._verify()
                except Exception as exc:
                    raise RuntimeError(
                        f"復旧確認に失敗しました: {_exception_text(exc)}"
                    ) from exc
                if verified is False:
                    raise RuntimeError("復旧確認に失敗しました")

                scheduler.mark_restart_completed()

            await self._finish("completed")
        except asyncio.CancelledError:
            await self._finish("failed", "メンテナンスジョブが中断されました")
            raise
        except Exception as exc:
            await self._finish("failed", _exception_text(exc))
        finally:
            _wipe_secret(sudo_password)
            scheduler.release_manual_maintenance(reservation_token)

    async def get_current(self) -> dict[str, Any]:
        async with self._lock:
            return self._state.as_dict()

    async def cancel_current(self) -> dict[str, Any]:
        """カウントダウン中のジョブだけをキャンセルする。"""
        async with self._lock:
            if (
                self._state.status != "countdown"
                or self._cancel_event is None
                or self._task is None
                or self._task.done()
            ):
                raise JobNotCancellableError(
                    "キャンセルできるのはカウントダウン中のジョブだけです"
                )
            self._cancel_event.set()
            self._state.status = "cancelled"
            self._state.finished_at = _now_iso()
            self._state.error = None
            return self._state.as_dict()

    async def wait_until_finished(self) -> dict[str, Any]:
        """テストや終了処理用に、現在のタスク完了を待つ。"""
        async with self._lock:
            task = self._task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
        return await self.get_current()

    async def shutdown(self) -> None:
        """予告は中止し、保存・再起動・更新・復旧確認は完了まで待つ。"""
        async with self._lock:
            admission_done = self._admission_done
            task = self._task
        if admission_done is not None and not admission_done.is_set():
            await admission_done.wait()
            async with self._lock:
                task = self._task

        async with self._lock:
            if (
                self._state.status == "countdown"
                and self._cancel_event is not None
                and task is not None
                and not task.done()
            ):
                self._cancel_event.set()
        if task is None or task.done():
            return

        loop = asyncio.get_running_loop()
        reversible_deadline: Optional[float] = None

        while not task.done():
            async with self._lock:
                phase = self._state.status
                cancel_event = self._cancel_event

            now = loop.time()
            if phase in {"checking", "countdown"}:
                if reversible_deadline is None:
                    reversible_deadline = now + SHUTDOWN_GRACE_SECONDS
                if phase == "countdown" and cancel_event is not None:
                    cancel_event.set()
                if now >= reversible_deadline:
                    logger.warning("安全に中断できるメンテナンス待機を打ち切ります")
                    task.cancel()
                    break
            else:
                reversible_deadline = None

            await asyncio.wait({task}, timeout=SHUTDOWN_POLL_SECONDS)

        try:
            await task
        except asyncio.CancelledError:
            pass


coordinator = MaintenanceCoordinator()
