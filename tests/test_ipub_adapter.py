"""Offline tests for the phase-1 ipub adapter (stdlib unittest only).

Run with::

    .\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ipub_adapter import (  # noqa: E402
    IpubFrame,
    SUPPORTED_TYPES,
    normalize_groups,
    dry_run,
)

FIXTURE = ROOT / "tests" / "fixtures" / "ipub_dryrun_result.json"


class FakeDriver:
    """Minimal IpubDriver stand-in that records the call sequence."""

    def __init__(self, has_iframe=True, groups=None):
        self.has_iframe = has_iframe
        self.groups = groups if groups is not None else []
        self.calls = []
        self.frame = "top"

    def execute(self, script, args=None):
        self.calls.append(("execute", script[:40]))
        if "document.querySelector(arguments[0])" in script:
            return self.has_iframe
        if "data-ipublish-question-group-type" in script:
            return self.groups
        return 0

    def set_frame(self, frame_id):
        self.calls.append(("frame", frame_id))
        self.frame = "ipub" if frame_id is not None else "top"


class FrameTests(unittest.TestCase):
    def test_switches_into_iframe_and_back(self):
        d = FakeDriver(has_iframe=True)
        with IpubFrame(d) as frame:
            self.assertTrue(frame.available)
            self.assertEqual(d.frame, "ipub")
        self.assertEqual(d.frame, "top")
        self.assertIn(("frame", 0), d.calls)
        self.assertIn(("frame", None), d.calls)

    def test_legacy_page_is_left_untouched(self):
        d = FakeDriver(has_iframe=False)
        with IpubFrame(d) as frame:
            self.assertFalse(frame.available)
        self.assertEqual(d.frame, "top")
        self.assertNotIn(("frame", 0), d.calls)

    def test_dry_run_on_legacy_page_reports_no_iframe(self):
        result = dry_run(FakeDriver(has_iframe=False), wait=False, url="legacy")
        self.assertFalse(result.frame_available)
        self.assertEqual(result.totals["groups"], 0)


class NormalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.result = normalize_groups(
            data["groups"],
            url=data.get("url", ""),
            load_counts=data.get("load_counts", {}),
        )

    def test_group_and_question_counts(self):
        t = self.result.totals
        self.assertEqual(t["groups"], 16)
        self.assertEqual(t["questions"], 38)
        self.assertEqual(t["supported_groups"], 10)
        self.assertEqual(t["unsupported_groups"], 6)
        self.assertEqual(t["groups_done"], 1)
        self.assertEqual(t["supported_questions"], 24)

    def test_every_type_is_seen(self):
        types = {g.type for g in self.result.groups}
        self.assertEqual(len(types), 9)
        self.assertIn("multi_media_upload_group", types)
        self.assertTrue(all(t in types for t in
                            ("single_choice_group", "true_false_group",
                             "fill_blanks_group")))

    def test_unsupported_groups_have_a_reason(self):
        for g in self.result.groups:
            if not g.supported:
                self.assertTrue(g.unsupported_reason)
                self.assertNotIn(g.type, SUPPORTED_TYPES)

    def test_stem_gap_is_surfaced_not_hidden(self):
        # The point of phase 1: a missing stem is reported, never guessed.
        self.assertEqual(self.result.totals["stem_missing"], 38)
        self.assertTrue(all(g.stem_missing_count == g.question_count
                            for g in self.result.groups))

    def test_already_answered_group_is_excluded_from_answerable(self):
        done = [g for g in self.result.groups if g.done]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].question_count, 3)
        self.assertEqual(self.result.totals["supported_questions"],
                         sum(g.question_count for g in self.result.groups
                             if g.supported and not g.done))

    def test_empty_input(self):
        r = normalize_groups([])
        self.assertEqual(r.totals["groups"], 0)
        self.assertEqual(r.totals["questions"], 0)

    def test_unknown_group_type_is_unsupported(self):
        r = normalize_groups([{"index": 0, "type": "new_future_group",
                               "questions": [{"stem_missing": True}]}])
        g = r.groups[0]
        self.assertFalse(g.supported)
        self.assertEqual(g.unsupported_reason, "unrecognised group type")
        self.assertEqual(r.totals["unsupported_groups"], 1)


if __name__ == "__main__":
    unittest.main()
