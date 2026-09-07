# -*- coding: utf-8 -*-
"""選択ファイルのZIPバックアップ。展開先・サイズ・チェックサムを検証する。"""

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import workspace_files as files

MAX_FILES = 50000
MAX_BYTES = 20 * 1024**3


def storage() -> Path:
    path = Path(os.getenv("PAL_BACKUP_DIR", str(Path(__file__).parent / "backups"))).absolute()
    if any(path.resolve().is_relative_to(root.resolve()) for root in files.roots().values()):
        raise files.WorkspaceError("バックアップ保存先は設定・セーブ領域の外に指定してください")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise files.WorkspaceError("バックアップ保存先にリンクは利用できません")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def archive_path(backup_id: str) -> Path:
    if len(backup_id) != 32 or any(c not in "0123456789abcdef" for c in backup_id):
        raise files.WorkspaceError("バックアップIDが不正です")
    path = storage() / (backup_id + ".zip")
    if path.is_symlink():
        raise files.WorkspaceError("バックアップにリンクは利用できません")
    return path


def collect(paths: list[str]) -> list[str]:
    if not paths or len(paths) > MAX_FILES:
        raise files.WorkspaceError("バックアップ対象の件数が不正です")
    selected = set()
    total = 0
    visited = 0
    for key in paths:
        path = files.safe_path(key)
        stack = [(key, path)]
        while stack:
            child_key, child = stack.pop()
            visited += 1
            if visited > MAX_FILES * 2:
                raise files.WorkspaceError("バックアップ対象が多すぎます")
            files.safe_path(child_key)
            if child.is_dir():
                for p in child.iterdir():
                    if not p.name.startswith("."):
                        stack.append((child_key + "/" + p.name, p))
            elif files.allowed_file(child_key) and child.is_file() and child_key not in selected:
                total += child.stat().st_size
                selected.add(child_key)
                if total > MAX_BYTES or len(selected) > MAX_FILES:
                    raise files.WorkspaceError("バックアップ上限（20 GiB / 50,000ファイル）を超えています")
    if not selected:
        raise files.WorkspaceError("対象の設定ファイルまたは .sav ファイルが見つかりません")
    if shutil.disk_usage(storage()).free < total + 100 * 1024**2:
        raise files.WorkspaceError("バックアップ先の空き容量が不足しています")
    return sorted(selected)


def create(label: str, description: str, paths: list[str], consistency: str = "stopped") -> dict:
    selected = collect(paths)
    backup_id = uuid.uuid4().hex
    target = archive_path(backup_id)
    pending = target.with_suffix(".partial")
    metadata = {"id": backup_id, "label": label, "description": description,
                "created_at": datetime.now(timezone.utc).isoformat(), "paths": paths,
                "consistency": consistency, "files": [], "total_bytes": 0, "version": 1}
    try:
        with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            os.chmod(pending, 0o600)
            for key in selected:
                path = files.safe_path(key)
                before = path.stat()
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as source, archive.open(key, "w", force_zip64=True) as dest:
                    while block := source.read(1024 * 1024):
                        size += len(block)
                        if metadata["total_bytes"] + size > MAX_BYTES:
                            raise files.WorkspaceError("バックアップ容量の上限を超えました")
                        digest.update(block)
                        dest.write(block)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise files.WorkspaceError("コピー中にファイルが変わりました。再実行またはサーバーを停止してください")
                metadata["files"].append({"path": key, "size": size, "sha256": digest.hexdigest()})
                metadata["total_bytes"] += size
            archive.writestr("manifest.json", json.dumps(metadata, ensure_ascii=False))
        os.replace(pending, target)
        return {**metadata, "archive_bytes": target.stat().st_size}
    finally:
        pending.unlink(missing_ok=True)


def detail(backup_id: str) -> dict:
    path = archive_path(backup_id)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("manifest.json")
        if info.file_size > 16 * 1024**2:
            raise files.WorkspaceError("バックアップの索引が大きすぎます")
        data = json.loads(archive.read(info))
        if not isinstance(data, dict) or data.get("id") != backup_id or data.get("version") != 1 or not isinstance(data.get("files"), list):
            raise files.WorkspaceError("バックアップ形式が不正です")
        if len(data["files"]) > MAX_FILES:
            raise files.WorkspaceError("ファイル数の上限を超えています")
        if not isinstance(data.get("label"), str) or not isinstance(data.get("description"), str):
            raise files.WorkspaceError("バックアップの説明が不正です")
        for entry in data["files"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise files.WorkspaceError("バックアップのファイル情報が不正です")
            if type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_BYTES:
                raise files.WorkspaceError("バックアップのファイルサイズが不正です")
            checksum = entry.get("sha256")
            if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
                raise files.WorkspaceError("バックアップのチェックサム形式が不正です")
        return {**data, "archive_bytes": path.stat().st_size}


def summaries() -> list[dict]:
    result = []
    for path in sorted(storage().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:500]:
        try:
            data = detail(path.stem)
            result.append({k: v for k, v in data.items() if k != "files"} | {"file_count": len(data["files"])})
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            result.append({"id": path.stem, "label": "読み取れないバックアップ", "invalid": True})
    return result


def restore(backup_id: str) -> dict:
    """全ファイル検証→復元前退避→置換。失敗時は置換済み分を元に戻す。"""
    data = detail(backup_id)
    keys = []
    seen = set()
    total = 0
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=storage()) as temp:
        stage = Path(temp)
        with zipfile.ZipFile(archive_path(backup_id)) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise files.WorkspaceError("バックアップ内に重複ファイルがあります")
            for index, entry in enumerate(data["files"]):
                key = entry["path"]
                files.safe_path(key, must_exist=False)
                if not files.allowed_file(key) or key in seen:
                    raise files.WorkspaceError("復元対象のパスが不正です")
                info = archive.getinfo(key)
                total += info.file_size
                if info.file_size != entry["size"] or total > MAX_BYTES:
                    raise files.WorkspaceError("復元ファイルのサイズが不正です")
                if shutil.disk_usage(stage).free < info.file_size + 100 * 1024**2:
                    raise files.WorkspaceError("復元用の空き容量が不足しています")
                digest = hashlib.sha256()
                with archive.open(key) as source, (stage / str(index)).open("wb") as dest:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                        dest.write(block)
                if digest.hexdigest() != entry["sha256"]:
                    raise files.WorkspaceError("バックアップのチェックサムが一致しません")
                keys.append(key)
                seen.add(key)
        if not keys:
            raise files.WorkspaceError("復元対象が空です")
        existing = [key for key in keys if files.safe_path(key, must_exist=False).exists()]
        recovery = create("復元前の自動退避", data["label"], existing) if existing else None
        written = []
        # ロールバックに使う元ファイルも、保存先と同じボリュームに退避する。
        rollback = {}
        try:
            for index, key in enumerate(keys):
                target = files.safe_path(key, must_exist=False)
                files.ensure_directory(target.parent)
                if target.exists():
                    fd, old = tempfile.mkstemp(prefix=".rollback-", dir=target.parent)
                    os.close(fd)
                    rollback[key] = Path(old)
                    shutil.copy2(target, old)
                    files.preserve_metadata(Path(old), target)
                else:
                    rollback[key] = None
                fd, incoming = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
                os.close(fd)
                try:
                    shutil.copyfile(stage / str(index), incoming)
                    files.preserve_metadata(Path(incoming), target)
                    os.replace(incoming, target)
                    written.append(key)
                finally:
                    Path(incoming).unlink(missing_ok=True)
        except BaseException:
            for key in reversed(written):
                target = files.safe_path(key, must_exist=False)
                old = rollback[key]
                if old is not None:
                    os.replace(old, target)
                else:
                    target.unlink(missing_ok=True)
            raise
        finally:
            for old in rollback.values():
                if old is not None:
                    old.unlink(missing_ok=True)
    return {"restored_files": len(keys), "recovery_backup_id": recovery["id"] if recovery else None}


def move_to_trash(backup_id: str) -> dict:
    path = archive_path(backup_id)
    trash = storage() / "trash"
    if trash.is_symlink():
        raise files.WorkspaceError("ごみ箱にリンクは利用できません")
    trash.mkdir(exist_ok=True, mode=0o700)
    os.replace(path, trash / path.name)
    return {"result": "trashed", "id": backup_id}
