# -*- coding: utf-8 -*-
"""一時サーバー領域で編集・バックアップ・復元の失敗経路を検証する。"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_store as backups
import workspace_files as files


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "Saved" / "Config" / "LinuxServer"
        self.saves = self.root / "Saved" / "SaveGames"
        self.config.mkdir(parents=True)
        self.saves.mkdir()
        self.settings_patch = patch.object(files.settings, "pal_settings_ini", str(self.config / "PalWorldSettings.ini"))
        self.settings_patch.start()
        self.env_patch = patch.dict(os.environ, {"PAL_BACKUP_DIR": str(self.root / "backups")})
        self.env_patch.start()
        (self.config / "Engine.ini").write_bytes(b"\xef\xbb\xbf[Engine]\r\nFPS=60\r\n")
        (self.config / "PalWorldSettings.ini").write_text('OptionSettings=(ServerName="Sample",AdminPassword="test-only")', encoding="utf-8")
        (self.saves / "Level.sav").write_bytes(b"world-original")

    def tearDown(self):
        self.env_patch.stop()
        self.settings_patch.stop()
        self.temp.cleanup()

    def test_paths_reject_escape_absolute_hidden_and_alternate_separator(self):
        for key in ["../outside", "config/../Game.ini", "/config/Engine.ini", "config\\Engine.ini", "config/C:/x", "config/.secret", "config//Engine.ini", "config/./Engine.ini", "other/test"]:
            with self.subTest(key=key), self.assertRaises(files.WorkspaceError):
                files.safe_path(key, must_exist=False)

    def test_listing_excludes_credentials_and_non_save_files(self):
        (self.config / ".env").write_text("test", encoding="utf-8")
        (self.config / "credentials.json").write_text("test", encoding="utf-8")
        (self.saves / "arbitrary.py").write_text("test", encoding="utf-8")
        self.assertEqual({item["name"] for item in files.listing("config")["entries"]}, {"Engine.ini", "PalWorldSettings.ini"})
        self.assertEqual([item["name"] for item in files.listing("saves")["entries"]], ["Level.sav"])

    def test_palworld_secrets_cannot_be_read_as_raw_text(self):
        with self.assertRaises(files.WorkspaceError):
            files.read_text("config/PalWorldSettings.ini")

    def test_other_config_with_password_is_not_returned(self):
        (self.config / "Game.ini").write_text("[Engine]\nPassword=sample", encoding="utf-8")
        with self.assertRaises(files.WorkspaceError):
            files.read_text("config/Game.ini")

    def test_edit_preserves_bom_newline_and_original_backup(self):
        raw = (self.config / "Engine.ini").read_bytes()
        current = files.read_text("config/Engine.ini")
        result = files.write_text(current["path"], "[Engine]\nFPS=90\n", current["revision"])
        self.assertEqual((self.config / "Engine.ini").read_bytes(), b"\xef\xbb\xbf[Engine]\r\nFPS=90\r\n")
        self.assertEqual((self.config / result["backup"]).read_bytes(), raw)

    def test_edit_rejects_conflict_without_losing_external_change(self):
        current = files.read_text("config/Engine.ini")
        (self.config / "Engine.ini").write_text("FPS=120", encoding="utf-8")
        with self.assertRaises(files.WorkspaceError):
            files.write_text(current["path"], "FPS=90", current["revision"])
        self.assertEqual((self.config / "Engine.ini").read_text(), "FPS=120")

    def test_save_binary_cannot_be_edited(self):
        with self.assertRaises(files.WorkspaceError):
            files.write_text("saves/Level.sav", "destroy", "0" * 64)

    def test_size_limit_does_not_overwrite_file(self):
        current = files.read_text("config/Engine.ini")
        with self.assertRaises(files.WorkspaceError):
            files.write_text(current["path"], "あ" * files.TEXT_LIMIT, current["revision"])
        self.assertEqual(files.read_text(current["path"])["revision"], current["revision"])

    def test_backup_roundtrip_and_recovery_contains_previous_data(self):
        backup = backups.create("before-update", "test", ["config", "saves"])
        (self.saves / "Level.sav").write_bytes(b"world-changed")
        result = backups.restore(backup["id"])
        self.assertEqual((self.saves / "Level.sav").read_bytes(), b"world-original")
        with zipfile.ZipFile(backups.archive_path(result["recovery_backup_id"])) as archive:
            self.assertEqual(archive.read("saves/Level.sav"), b"world-changed")

    def test_selected_backup_does_not_include_unselected_files(self):
        backup = backups.create("config", "", ["config/Engine.ini"])
        self.assertEqual([item["path"] for item in backup["files"]], ["config/Engine.ini"])

    def test_restore_does_not_delete_files_absent_from_archive(self):
        backup = backups.create("world", "", ["saves"])
        (self.saves / "NewPlayer.sav").write_bytes(b"new-player")
        backups.restore(backup["id"])
        self.assertEqual((self.saves / "NewPlayer.sav").read_bytes(), b"new-player")

    def rewrite_archive(self, backup_id, mutate):
        path = backups.archive_path(backup_id)
        with zipfile.ZipFile(path) as archive:
            content = {name: archive.read(name) for name in archive.namelist()}
        mutate(content)
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in content.items():
                archive.writestr(name, value)

    def test_corrupt_archive_is_rejected_before_any_write(self):
        backup = backups.create("world", "", ["config", "saves"])
        self.rewrite_archive(backup["id"], lambda content: content.update({"saves/Level.sav": b"world-corrupt!"}))
        before = (self.config / "Engine.ini").read_bytes()
        with self.assertRaises(files.WorkspaceError):
            backups.restore(backup["id"])
        self.assertEqual((self.config / "Engine.ini").read_bytes(), before)
        self.assertEqual((self.saves / "Level.sav").read_bytes(), b"world-original")

    def test_zip_traversal_is_rejected(self):
        backup = backups.create("world", "", ["saves"])
        def change(content):
            manifest = json.loads(content["manifest.json"])
            manifest["files"][0]["path"] = "saves/../../outside.sav"
            content["manifest.json"] = json.dumps(manifest).encode()
            content["saves/../../outside.sav"] = content.pop("saves/Level.sav")
        self.rewrite_archive(backup["id"], change)
        with self.assertRaises(files.WorkspaceError):
            backups.restore(backup["id"])
        self.assertFalse((self.root / "outside.sav").exists())

    def test_failed_restore_rolls_back_written_files(self):
        backup = backups.create("all", "", ["config", "saves"])
        (self.config / "Engine.ini").write_bytes(b"new-engine")
        (self.saves / "Level.sav").write_bytes(b"new-world")
        replace = os.replace
        def fail_once(source, target):
            if Path(source).name.startswith(".restore-") and Path(target).name == "Level.sav":
                raise OSError("simulated disk error")
            return replace(source, target)
        with patch.object(backups.os, "replace", side_effect=fail_once), self.assertRaises(OSError):
            backups.restore(backup["id"])
        self.assertEqual((self.config / "Engine.ini").read_bytes(), b"new-engine")
        self.assertEqual((self.saves / "Level.sav").read_bytes(), b"new-world")

    def test_deleted_backup_moves_to_recoverable_trash(self):
        backup = backups.create("world", "", ["saves"])
        backups.move_to_trash(backup["id"])
        self.assertEqual(backups.summaries(), [])
        self.assertTrue((backups.storage() / "trash" / (backup["id"] + ".zip")).is_file())

    def test_archive_id_cannot_escape(self):
        with self.assertRaises(files.WorkspaceError):
            backups.archive_path("../outside")

    def test_backup_directory_cannot_overlap_source(self):
        with patch.dict(os.environ, {"PAL_BACKUP_DIR": str(self.saves / "backups")}), self.assertRaises(files.WorkspaceError):
            backups.storage()

    def test_duplicate_selections_are_deduplicated(self):
        backup = backups.create("world", "", ["saves", "saves/Level.sav"])
        self.assertEqual(len(backup["files"]), 1)

    def test_empty_selection_does_not_create_archive(self):
        with self.assertRaises(files.WorkspaceError):
            backups.create("empty", "", [])
        self.assertEqual(backups.summaries(), [])

    def test_symlink_source_is_rejected(self):
        outside = self.root / "outside.sav"
        outside.write_bytes(b"private")
        link = self.saves / "Linked.sav"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("このOSではシンボリックリンクの作成権限がありません")
        with self.assertRaises(files.WorkspaceError):
            files.safe_path("saves/Linked.sav")

    def test_atomic_write_preserves_existing_owner_when_chown_is_available(self):
        path = self.config / "Engine.ini"
        info = path.stat()
        with patch.object(files.os, "chown", create=True) as chown:
            files.atomic_write(path, b"FPS=90")
        chown.assert_called_once()
        self.assertEqual(chown.call_args.args[1:], (info.st_uid, info.st_gid))

    def test_new_restore_file_inherits_parent_owner(self):
        backup = backups.create("world", "", ["saves"])
        (self.saves / "Level.sav").unlink()
        info = self.saves.stat()
        with patch.object(files.os, "chown", create=True) as chown:
            backups.restore(backup["id"])
        self.assertTrue(any(call.args[1:] == (info.st_uid, info.st_gid) for call in chown.call_args_list))
        self.assertEqual((self.saves / "Level.sav").read_bytes(), b"world-original")

    def test_invalid_manifest_does_not_break_other_backup_entries(self):
        good = backups.create("good", "", ["saves"])
        broken = backups.create("broken", "", ["saves"])
        self.rewrite_archive(broken["id"], lambda content: content.update({"manifest.json": b"[]"}))
        items = backups.summaries()
        self.assertEqual(len(items), 2)
        self.assertTrue(next(item for item in items if item["id"] == broken["id"])["invalid"])
        self.assertEqual(next(item for item in items if item["id"] == good["id"])["label"], "good")

    def test_restore_recreated_world_directories_inherit_owner(self):
        world = self.saves / "0" / "world"
        world.mkdir(parents=True)
        (world / "Level.sav").write_bytes(b"world")
        backup = backups.create("world", "", ["saves/0/world"])
        (world / "Level.sav").unlink()
        world.rmdir()
        world.parent.rmdir()
        with patch.object(files.os, "chown", create=True) as chown:
            backups.restore(backup["id"])
        changed = {Path(call.args[0]) for call in chown.call_args_list}
        self.assertIn(world.parent, changed)
        self.assertIn(world, changed)
        self.assertEqual((world / "Level.sav").read_bytes(), b"world")


if __name__ == "__main__":
    unittest.main()
