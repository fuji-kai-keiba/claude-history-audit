"""Filesystem probes must never follow untrusted transcript paths to a share."""
import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import claude_history_audit.config_audit as ca
from claude_history_audit.audit import audit


class ConfigSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "cfg"
        self.config.mkdir()

    def test_network_device_and_relative_paths_are_rejected_before_filesystem_access(self):
        paths = ["//blocked.invalid/share/project", "///blocked.invalid/share/project",
                 r"\\blocked.invalid\share\project", r"/\blocked.invalid\share",
                 r"\\?\UNC\blocked.invalid\share", r"\\?\C:\project", r"\\.\C:\project",
                 r"\??\UNC\blocked.invalid\share", "smb://blocked.invalid/share",
                 "file://blocked.invalid/share", "relative/project", "C:relative", "~/project",
                 "\x00/private", "/tmp/line\nbreak"]
        with patch.object(Path, "lstat", side_effect=AssertionError("filesystem probe")), \
                patch.object(Path, "stat", side_effect=AssertionError("filesystem probe")), \
                patch.object(ca, "local_drive", side_effect=AssertionError("drive probe")):
            for value in paths:
                with self.subTest(value=value):
                    self.assertIsNone(ca.local_stat(value))
                    self.assertEqual(ca.load(value), {})

    def test_remote_drive_is_rejected_before_stat(self):
        with patch.object(ca, "local_drive", return_value=False), \
                patch.object(Path, "lstat", side_effect=AssertionError("remote drive probe")):
            self.assertIsNone(ca.local_stat(self.root))

    def test_reparse_parent_is_rejected_before_probing_child(self):
        parent = self.root / "junction"
        child = parent / "CLAUDE.md"
        original = Path.lstat
        def guarded(path, *args, **kwargs):
            if path == parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            if parent in path.parents:
                self.fail("junction descendant was probed")
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", guarded):
            self.assertIsNone(ca.file_size(child))
            self.assertEqual(ca.load(child), {})

    def test_linked_cwd_and_import_do_not_read_target(self):
        target = self.root / "outside"
        target.mkdir()
        (target / "CLAUDE.md").write_text("PRIVATE_TARGET", encoding="utf-8")
        link = self.config / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks")
        (self.config / "CLAUDE.md").write_text("@linked/CLAUDE.md", encoding="utf-8")
        original = Path.lstat
        def guarded(path, *args, **kwargs):
            if link in path.parents:
                self.fail("symlink descendant was probed")
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", guarded):
            summary, private = ca.audit_config(self.config, {"p": str(link)}, {})
        project = summary["projects"][0]
        self.assertFalse(project["cwd_found"])
        self.assertEqual(project["cwd_status"], "not_inspected")
        self.assertNotIn("import", project["by_kind"])
        self.assertGreater(project["unresolved_imports"], 0)
        self.assertNotIn(str(target), json.dumps(private))

    def test_local_files_and_parent_imports_still_work(self):
        project = self.root / "project"
        project.mkdir()
        (self.root / "note.md").write_text("synthetic import", encoding="utf-8")
        (project / "CLAUDE.md").write_text("@../note.md", encoding="utf-8")
        with patch.object(Path, "home", return_value=self.root):
            summary, private = ca.audit_config(self.config, {"p": str(project)}, {})
        self.assertTrue(summary["projects"][0]["cwd_found"])
        self.assertEqual(summary["projects"][0]["by_kind"]["import"], len("synthetic import"))
        self.assertIn(str(self.root / "note.md"), private["p"])

    def test_transcript_cwd_is_not_probed_or_disclosed(self):
        source = self.config / "projects"
        source.mkdir()
        remote = "//blocked.invalid/share/project"
        rows = [dict(type="user", sessionId="synthetic", cwd=remote,
                     timestamp="2026-10-01T00:00:00Z", message={"role": "user", "content": "synthetic task"}),
                dict(type="assistant", sessionId="synthetic", timestamp="2026-10-01T00:00:01Z",
                     message={"id": "synthetic-response", "role": "assistant", "model": "claude-sonnet-4-6",
                              "content": [{"type": "text", "text": "synthetic answer"}],
                              "usage": {"input_tokens": 100, "output_tokens": 20,
                                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}})]
        (source / "synthetic.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        originals = {name: getattr(Path, name) for name in ("stat", "lstat")}
        def guarded(name):
            def check(path, *args, **kwargs):
                if "blocked.invalid" in str(path):
                    self.fail("network path reached the filesystem")
                return originals[name](path, *args, **kwargs)
            return check
        with patch.object(Path, "stat", guarded("stat")), patch.object(Path, "lstat", guarded("lstat")), \
                patch.object(Path, "home", return_value=self.root / "home"):
            report, local_map = audit([source], deep=True, config_dir=self.config,
                                      now=datetime(2026, 10, 9, tzinfo=timezone.utc))
        self.assertEqual(report["coverage"]["unique_requests"], 1)
        self.assertNotIn("blocked.invalid", json.dumps(report))
        self.assertIn(remote, local_map["projects"].values())
        summary, _ = ca.audit_config(self.config, {"p": remote}, {})
        self.assertEqual(summary["projects"][0]["cwd_status"], "not_inspected")


if __name__ == "__main__":
    unittest.main()
