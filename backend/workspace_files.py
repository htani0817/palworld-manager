# -*- coding: utf-8 -*-
"""サーバーの設定・セーブ領域だけを扱うファイル操作。"""

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath

from config import settings

TEXT_LIMIT = 1024 * 1024
CONFIG_NAMES = frozenset({"Engine.ini", "Game.ini", "GameUserSettings.ini"})


class WorkspaceError(ValueError):
    pass


def roots() -> dict[str, Path]:
    config = Path(settings.pal_settings_ini).absolute().parent
    return {"config": config, "saves": config.parent.parent / "SaveGames"}


def safe_path(key: str, *, must_exist: bool = True) -> Path:
    """相対キーを検証し、リンクや親ディレクトリへの脱出を拒否する。"""
    if not isinstance(key, str) or not key or "\\" in key or ":" in key or "\x00" in key:
        raise WorkspaceError("ファイルパスが不正です")
    parts = key.split("/")
    if any(p in {"", ".", ".."} or p.startswith(".") for p in parts):
        raise WorkspaceError("ファイルパスが不正です")
    base = roots().get(parts[0])
    if base is None:
        raise WorkspaceError("許可されていない領域です")
    # ルート自身とその祖先も確認する（設定ディレクトリの差し替え対策）。
    for ancestor in (base, *base.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise WorkspaceError("リンクされたディレクトリは扱えません")
    path = base
    for part in parts[1:]:
        path = path / part
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise WorkspaceError("リンクされたファイルは扱えません")
    if not path.resolve().is_relative_to(base.resolve()):
        raise WorkspaceError("許可領域の外にはアクセスできません")
    if must_exist and not path.exists():
        raise FileNotFoundError("ファイルが見つかりません")
    return path


def allowed_file(key: str) -> bool:
    p = PurePosixPath(key)
    if p.parts[0] == "config":
        return len(p.parts) == 2 and p.name in (CONFIG_NAMES | {Path(settings.pal_settings_ini).name})
    return p.parts[0] == "saves" and p.suffix.lower() == ".sav"


def listing(key: str) -> dict:
    path = safe_path(key)
    if not path.is_dir():
        raise WorkspaceError("フォルダを指定してください")
    result = []
    scanned = 0
    for child in path.iterdir():
        scanned += 1
        if scanned > 5000:
            raise WorkspaceError("項目が多すぎます。より小さいフォルダを指定してください")
        child_key = key + "/" + child.name
        try:
            safe_path(child_key)
        except (WorkspaceError, FileNotFoundError):
            continue
        directory = child.is_dir()
        if key.startswith("config") and directory:
            continue
        if not directory and not allowed_file(child_key):
            continue
        info = child.stat()
        if not directory and not stat.S_ISREG(info.st_mode):
            continue
        result.append({"name": child.name, "path": child_key, "directory": directory,
                       "size": None if directory else info.st_size,
                       "modified_at": info.st_mtime,
                       "editable": key == "config" and child.name in CONFIG_NAMES,
                       "settings": key == "config" and child.name == Path(settings.pal_settings_ini).name})
    result.sort(key=lambda row: (not row["directory"], row["name"].casefold()))
    return {"path": key, "entries": result}


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(key: str) -> dict:
    path = safe_path(key)
    if key.split("/")[0] != "config" or path.name not in CONFIG_NAMES or len(key.split("/")) != 2:
        raise WorkspaceError("このファイルはテキストエディターで開けません")
    if not path.is_file() or path.stat().st_size > TEXT_LIMIT:
        raise WorkspaceError("編集可能なファイルは1 MiB以下です")
    with path.open("rb") as stream:
        raw = stream.read(TEXT_LIMIT + 1)
    if len(raw) > TEXT_LIMIT:
        raise WorkspaceError("編集可能なファイルは1 MiB以下です")
    text = raw.decode("utf-8-sig")
    # 生の認証設定をエディターに返さず、専用設定画面を利用する。
    if re.search(r"(?im)^\s*[^;#\r\n]*(password|token|secret|webhook|api.?key)\s*=", text):
        raise WorkspaceError("認証情報を含むファイルはこのエディターでは開けません")
    return {"path": key, "content": text, "revision": revision(raw), "size": len(raw)}


def preserve_metadata(incoming: Path, target: Path) -> None:
    """root実行でも、置換後のファイルをPalworldユーザーが扱えるようにする。"""
    exists = target.exists()
    info = (target if exists else target.parent).stat()
    if hasattr(os, "chown"):
        os.chown(incoming, info.st_uid, info.st_gid)
    os.chmod(incoming, stat.S_IMODE(info.st_mode) if exists else 0o600)


def ensure_directory(path: Path) -> None:
    """新しいワールド用の親フォルダにも既存領域の所有者を引き継ぐ。"""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        info = directory.parent.stat()
        directory.mkdir(mode=stat.S_IMODE(info.st_mode))
        if hasattr(os, "chown"):
            os.chown(directory, info.st_uid, info.st_gid)
        os.chmod(directory, stat.S_IMODE(info.st_mode))


def atomic_write(path: Path, data: bytes) -> None:
    ensure_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".manager-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        preserve_metadata(Path(name), path)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_text(key: str, content: str, expected_revision: str) -> dict:
    current = read_text(key)
    if current["revision"] != expected_revision:
        raise WorkspaceError("別の操作でファイルが更新されています。再読込して変更を確認してください")
    if "\x00" in content or len(content.encode("utf-8")) > TEXT_LIMIT:
        raise WorkspaceError("ファイル内容が不正、または1 MiBを超えています")
    path = safe_path(key)
    raw = path.read_bytes()
    # 再読込直前の変更も検出し、元ファイルを失わない。
    if revision(raw) != expected_revision:
        raise WorkspaceError("ファイルが更新されています。再読込してください")
    backup = path.with_name(path.name + ".bak_" + expected_revision[:16])
    if not backup.exists():
        atomic_write(backup, raw)
    newline = "\r\n" if b"\r\n" in raw else "\n"
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    encoded = normalized.encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    atomic_write(path, encoded)
    return {"path": key, "revision": revision(encoded), "backup": backup.name, "restart_required": True}
