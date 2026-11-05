"""Offline tests for the GUI executor compatibility report."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ipub_adapter import normalize_groups  # noqa: E402
from ipub_support import (  # noqa: E402
    AUTO_KINDS,
    capability,
    extract_group_kinds,
    build_support_report,
    format_support_report,
)

FIXTURES = ROOT / "tests" / "fixtures"

SYNTHETIC_MAIN = '''
class PublishQuestionParser:
    GROUP_KINDS = {
        'single_choice_group': 'choice',
        'fill_blanks_group': 'fill',
        'short_answer_group': 'text',
        'oral_sentence_scoop_group': 'record',
        'discussion_group': 'skip',
        'multi_media_upload_group': 'upload',
    }
'''


def dry_result():
    data = json.loads((FIXTURES / "ipub_dryrun_result.json").read_text(encoding="utf-8"))
    return normalize_groups(
        data["groups"], url=data.get("url", ""),
        load_counts=data.get("load_counts", {}),
        frame_available=data.get("frame_available", True),
    )


class ParserTests(unittest.TestCase):
    def test_extracts_group_kinds_from_synthetic_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake_main.py"
            path.write_text(SYNTHETIC_MAIN, encoding="utf-8")
            kinds = extract_group_kinds(path)
        self.assertEqual(kinds["single_choice_group"], "choice")
        self.assertEqual(kinds["multi_media_upload_group"], "upload")
        self.assertEqual(len(kinds), 6)

    def test_missing_file_returns_empty(self):
        self.assertEqual(extract_group_kinds(ROOT / "no_such_file.py"), {})

    def test_source_without_parser_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old_main.py"
            path.write_text("class OldParser:\n    pass\n", encoding="utf-8")
            self.assertEqual(extract_group_kinds(path), {})

    def test_capability_mapping(self):
        self.assertTrue(AUTO_KINDS)
        for kind in AUTO_KINDS:
            self.assertEqual(capability(kind), "auto")
        self.assertEqual(capability("skip"), "skip")
        self.assertEqual(capability("upload"), "manual")
        self.assertEqual(capability("something_new"), "unknown")


class RealMainTests(unittest.TestCase):
    def test_upstream_main_registers_the_expected_types(self):
        kinds = extract_group_kinds(ROOT / "UnipusAI_Helper.py")
        if not kinds:
            self.skipTest("course-v3 PublishQuestionParser not present in this checkout")
        for group_type in (
            "single_choice_group", "true_false_group", "fill_blanks_group",
            "fill_blanks_choice_group", "short_answer_group",
            "oral_sentence_scoop_group", "oral_personal_state_group",
            "discussion_group", "multi_media_upload_group",
        ):
            with self.subTest(group_type=group_type):
                self.assertIn(group_type, kinds)

    def test_real_page_groups_are_mostly_auto_answerable(self):
        report = build_support_report(dry_result())
        if not report["main_found"]:
            self.skipTest("course-v3 PublishQuestionParser not present in this checkout")
        t = report["totals"]
        self.assertEqual(t["groups"], 16)
        self.assertGreater(t["auto"], t["manual"] + t["unknown"])
        self.assertGreaterEqual(t["manual"], 1)  # multi_media_upload_group
        self.assertGreaterEqual(t["skip"], 1)   # discussion_group

    def test_report_text_is_read_only_and_lists_labels(self):
        # Use a synthetic main file so the assertion does not depend on which
        # GUI version happens to be checked out (old branches have no parser).
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake_main.py"
            path.write_text(SYNTHETIC_MAIN, encoding="utf-8")
            report = build_support_report(dry_result(), path)
        text = format_support_report(report)
        self.assertIn("GUI course-v3 executor compatibility", text)
        self.assertIn("nothing was answered or submitted", text)
        self.assertIn("GUI 自动作答", text)
        self.assertIn("GUI 主动跳过", text)
        self.assertIn("需手动处理", text)


class CliTests(unittest.TestCase):
    def test_cli_gui_support_offline(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ipub_dryrun.py"),
             "--from-json", str(FIXTURES / "ipub_dryrun_result.json"),
             "--gui-support"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GUI course-v3 executor compatibility", proc.stdout)
        self.assertIn("Read-only run: nothing was answered or submitted.", proc.stdout)

    def test_cli_without_flag_is_unchanged(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ipub_dryrun.py"),
             "--from-json", str(FIXTURES / "ipub_dryrun_result.json")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("GUI course-v3 executor compatibility", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
