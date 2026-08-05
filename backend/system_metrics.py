# -*- coding: utf-8 -*-
"""
システム・Palworld プロセスのリソース監視。

CPU 使用率は psutil の仕様上「前回計測からの差分」で求める必要があり、
executor スレッドを跨ぐと基準点が失われて 0.0 が返る問題がある。
そこで専用のバックグラウンドサンプラー（単一 asyncio タスク）で一定間隔に
計測してキャッシュし、API と Discord はキャッシュ値を読む方式にする。
"""
import asyncio
import platform
import threading
import time
from typing import Optional

import psutil

# 直近のスナップショットを保持するキャッシュ（サンプラーが更新、API が読む）
_cache: Optional[dict] = None
_cache_lock = threading.Lock()

# CPU 差分計測のため Palworld プロセスハンドルをキャッシュする
_pal_proc: Optional[psutil.Process] = None

SAMPLE_INTERVAL = 3.0  # サンプリング間隔（秒）
HOST_INFO_MAX_LENGTH = 128
UNKNOWN_HOST_VALUE = "Unknown"


def _sanitize_host_value(value: object) -> str:
    """OS 情報の制御文字を除去し、画面表示用の長さに制限する。"""
    if not isinstance(value, str):
        return UNKNOWN_HOST_VALUE
    printable = "".join(
        " " if char.isspace() else char
        for char in value
        if char.isprintable() or char.isspace()
    )
    cleaned = " ".join(printable.split()).strip()
    return cleaned[:HOST_INFO_MAX_LENGTH] or UNKNOWN_HOST_VALUE


def _read_host_os_info() -> dict[str, str]:
    """ホスト OS の公開可能な項目だけを取得する。"""
    try:
        release = platform.freedesktop_os_release()
    except (AttributeError, OSError, ValueError):
        release = {}

    if not hasattr(release, "get"):
        release = {}

    return {
        "os_name": _sanitize_host_value(release.get("NAME")),
        "os_version": _sanitize_host_value(release.get("VERSION_ID")),
        "os_pretty_name": _sanitize_host_value(release.get("PRETTY_NAME")),
    }


# OS 情報は起動中に変わらないため、3 秒ごとのサンプリングでは読み直さない。
_HOST_INFO = _read_host_os_info()


def _find_pal_process() -> Optional[psutil.Process]:
    """Palworld サーバー本体プロセスを探す

    "PalServer" に一致するプロセスには起動用シェル（PalServer.sh）も含まれるため、
    本体バイナリ（PalServer-Linux-*）を優先し、なければ RSS が最大のものを選ぶ。
    """
    candidates: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info["name"] or ""
            if "PalServer" in name:
                candidates.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not candidates:
        return None

    for proc in candidates:
        if "PalServer-Linux" in (proc.info["name"] or ""):
            return proc

    def _rss(p: psutil.Process) -> int:
        try:
            return p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    return max(candidates, key=_rss)


def _get_pal_process() -> Optional[psutil.Process]:
    """キャッシュ済みハンドルが生きていれば再利用し、CPU 差分計測を維持する"""
    global _pal_proc
    if _pal_proc is not None:
        try:
            if _pal_proc.is_running() and "PalServer" in _pal_proc.name():
                return _pal_proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        _pal_proc = None

    proc = _find_pal_process()
    if proc is not None:
        try:
            proc.cpu_percent(interval=None)  # 基準点を作る
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        _pal_proc = proc
    return _pal_proc


def _sample_once() -> dict:
    """1回分のスナップショットを同期計測する（サンプラースレッドから呼ぶ）

    interval=None の cpu_percent は「前回この関数を呼んでからの差分」を返す。
    サンプラーが同じスレッドで一定間隔に呼び続けるため、2回目以降は有効な値になる。
    """
    cpu_percent = psutil.cpu_percent(interval=None)
    cpu_count = psutil.cpu_count() or 1
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    pal_proc = _get_pal_process()
    pal_cpu = None
    pal_mem_mb = None
    pal_pid = None
    if pal_proc:
        try:
            pal_cpu = round(pal_proc.cpu_percent(interval=None) / cpu_count, 1)
            pal_mem_mb = round(pal_proc.memory_info().rss / 1024 / 1024, 1)
            pal_pid = pal_proc.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pal_proc = None

    return {
        "host": dict(_HOST_INFO),
        "system": {
            "cpu_percent": round(cpu_percent, 1),
            "cpu_count": cpu_count,
            "mem_total_gb": round(mem.total / 1024**3, 1),
            "mem_used_gb": round(mem.used / 1024**3, 1),
            "mem_percent": mem.percent,
            "disk_total_gb": round(disk.total / 1024**3, 1),
            "disk_used_gb": round(disk.used / 1024**3, 1),
            "disk_percent": disk.percent,
        },
        "palworld": {
            "pid": pal_pid,
            "cpu_percent": pal_cpu,
            "mem_mb": pal_mem_mb,
            "running": pal_proc is not None,
        },
    }


# キャッシュが何秒古くなったら stale とみなすか（サンプル間隔の数倍）
STALE_AFTER = SAMPLE_INTERVAL * 3 + 5


def _sampler_thread() -> None:
    """専用スレッドで一定間隔にサンプリングしてキャッシュを更新する。

    最初のサンプル（CPU 差分の基準点直後）は 0.0 になりやすいため公開せず、
    2周目以降の有効な値だけをキャッシュに載せる。
    """
    global _cache
    # 基準点作成（system CPU の初回 0.0 を捨てる）
    psutil.cpu_percent(interval=None)
    first = True
    while not _stop_event.is_set():
        try:
            snap = _sample_once()
            snap["sampled_at"] = time.time()
            if first:
                # 1周目は CPU の差分基準がまだ甘いので破棄（warm-up）
                first = False
            else:
                with _cache_lock:
                    _cache = snap
        except Exception:
            pass
        _stop_event.wait(SAMPLE_INTERVAL)


_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def start_sampler() -> None:
    """バックグラウンドサンプラーを起動する（main.py の startup から呼ぶ）"""
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop_event.clear()
        _thread = threading.Thread(target=_sampler_thread, name="metrics-sampler", daemon=True)
        _thread.start()


def stop_sampler() -> None:
    _stop_event.set()


async def get_system_metrics() -> dict:
    """キャッシュされた最新スナップショットを返す。

    - warm-up 中（有効サンプルなし）は cpu_percent を null にして warming フラグを立てる
    - キャッシュが古い（STALE_AFTER 超過）場合は stale フラグを立てる
    """
    with _cache_lock:
        cached = _cache
    if cached is not None:
        result = dict(cached)
        age = time.time() - cached.get("sampled_at", 0)
        result["stale"] = age > STALE_AFTER
        result["warming"] = False
        return result

    # まだ有効サンプルがない（warm-up 中）。メモリ・ディスクは即値で返し、CPU は null にする。
    loop = asyncio.get_event_loop()
    snap = await loop.run_in_executor(None, _sample_once)
    snap["system"]["cpu_percent"] = None
    snap["palworld"]["cpu_percent"] = None
    snap["warming"] = True
    snap["stale"] = False
    snap["sampled_at"] = time.time()
    return snap
