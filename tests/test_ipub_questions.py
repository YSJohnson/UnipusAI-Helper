"""Offline tests for the phase-2 question-stem adapter (stdlib unittest only).

The fixtures are synthetic: no real textbook text, ids, tokens or answers are
stored in the repository.  Run with::

    .\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ipub_adapter import normalize_groups  # noqa: E402
from ipub_dryrun import main as dryrun_main  # noqa: E402
from ipub_questions import (  # noqa: E402
    StemIndex,
    _EXTRACT_CONFIG_JS,
    attach_stems,
    normalize_configs,
)

DOM_FIXTURE = ROOT / "tests" / "fixtures" / "ipub_dryrun_result.json"
STEM_FIXTURE = ROOT / "tests" / "fixtures" / "question_configs.json"


def load_dom():
    return json.loads(DOM_FIXTURE.read_text(encoding="utf-8"))


def load_configs():
    return json.loads(STEM_FIXTURE.read_text(encoding="utf-8"))


def build_result(with_group_ids=True):
    data = load_dom()
    groups = data["groups"]
    if not with_group_ids:
        groups = [{k: v for k, v in g.items() if k != "group_id"} for g in groups]
    return normalize_groups(groups, url=data.get("url", ""),
                            load_counts=data.get("load_counts", {}))


class ExtractorSafetyTests(unittest.TestCase):
    def test_extractor_script_is_read_only(self):
        forbidden = ("fetch(", "XMLHttpRequest", ".click(", ".submit(",
                     "innerHTML =", "localStorage")
        for token in forbidden:
            self.assertNotIn(token, _EXTRACT_CONFIG_JS)
        self.assertIn("return configs", _EXTRACT_CONFIG_JS)
        # No field named `answers` is ever returned (has_answers is only a bool).
        self.assertIsNone(re.search(r"\banswers\s*:", _EXTRACT_CONFIG_JS))

    def test_exporter_never_serialises_answers(self):
        # has_answers is a boolean marker; the answers payload must not exist.
        raw = json.dumps(load_configs())
        self.assertIn("has_answers", raw)
        self.assertNotIn('"answers"', raw)


class NormalizeConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = load_configs()
        cls.index = StemIndex.from_dict(data)

    def test_group_and_question_counts(self):
        self.assertEqual(len(self.index.groups), 16)
        self.assertEqual(self.index.question_count, 38)
        self.assertEqual(self.index.source, "synthetic-fixture")
        self.assertEqual(len(self.index.order), 16)

    def test_stem_sources_are_preserved(self):
        sources = {q.stem_source for g in self.index.groups.values() for q in g.questions}
        self.assertIn("quesTextString", sources)
        self.assertIn("children.quesTextString", sources)
        self.assertIn("none", sources)

    def test_missing_stem_is_not_guessed(self):
        q = self.index.groups["fixture-g00"].questions[2]
        self.assertTrue(q.stem_missing)
        self.assertEqual(q.stem, "")
        self.assertEqual(q.stem_source, "none")

    def test_options_and_blanks(self):
        choice = self.index.groups["fixture-g01"].questions[0]
        self.assertEqual([o.label for o in choice.options], ["A", "B", "C", "D"])
        tf = self.index.groups["fixture-g03"].questions[0]
        self.assertEqual([o.value for o in tf.options], ["T", "F"])
        blanks = self.index.groups["fixture-g02"].questions[0]
        self.assertEqual(blanks.blank_count, 5)
        bank = self.index.groups["fixture-g09"].questions[0]
        self.assertEqual(len(bank.options), 10)
        self.assertEqual(bank.blank_count, 10)

    def test_options_are_deduplicated_and_blank_ones_dropped(self):
        idx = normalize_configs([{
            "group_id": "g", "group_type": "single_choice_group",
            "questions": [{"index": 0, "stem": "Q", "options": [
                {"label": "A", "value": "same"},
                {"label": "A", "value": "same"},
                {"label": "", "value": ""},
            ]}],
        }])
        self.assertEqual(len(idx.groups["g"].questions[0].options), 1)

    def test_invalid_and_duplicate_groups_are_skipped(self):
        idx = normalize_configs([
            {"group_id": "", "questions": [{"stem": "x"}]},
            {"group_id": "g", "questions": [{"stem": "first"}]},
            {"group_id": "g", "questions": [{"stem": "duplicate"}]},
        ])
        self.assertEqual(list(idx.groups), ["g"])
        self.assertEqual(idx.groups["g"].questions[0].stem, "first")

    def test_roundtrip(self):
        data = load_configs()
        again = StemIndex.from_dict(self.index.to_dict())
        self.assertEqual(again.question_count, 38)
        self.assertEqual(again.groups["fixture-g01"].questions[0].stem,
                         self.index.groups["fixture-g01"].questions[0].stem)


class LookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = StemIndex.from_dict(load_configs())

    def test_lookup_by_id(self):
        g = self.index.lookup("fixture-g03")
        self.assertIsNotNone(g)
        self.assertEqual(g.group_type, "true_false_group")
        self.assertEqual(self.index.alignment_kind("fixture-g03"), "id")

    def test_order_fallback_when_id_is_unknown(self):
        g = self.index.lookup("not-in-page", 3)
        self.assertIsNotNone(g)
        self.assertEqual(g.group_id, "fixture-g03")
        self.assertEqual(self.index.alignment_kind("not-in-page"), "order")

    def test_no_match_returns_none(self):
        self.assertIsNone(self.index.lookup("missing", 999))


class AttachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = attach_stems(build_result(), StemIndex.from_dict(load_configs()))

    def test_all_but_one_stem_are_resolved(self):
        t = self.result.totals
        self.assertEqual(t["questions"], 38)
        self.assertEqual(t["stem_resolved"], 37)
        self.assertEqual(t["stem_missing"], 1)
        self.assertEqual(t["groups_with_stems"], 16)
        # Phase 1 answerable count is unchanged: phase 2 adds data, not actions.
        self.assertEqual(t["supported_questions"], 24)

    def test_groups_are_aligned_by_id(self):
        self.assertTrue(all(g.alignment == "id" for g in self.result.groups))
        self.assertEqual(self.result.groups[0].stem_source, "synthetic-fixture")
        self.assertEqual(self.result.groups[1].api_question_count, 3)

    def test_questions_carry_api_details(self):
        q = self.result.groups[1].questions[0]
        self.assertIn("Fixture", q["stem"])
        self.assertFalse(q["stem_missing"])
        self.assertEqual(q["stem_source"], "quesTextString")
        self.assertEqual(len(q["api_options"]), 4)
        self.assertEqual(q["api_qid"], "fixture-q-01-00")
        self.assertIn("has_answers", q)

    def test_missing_stem_stays_missing(self):
        q = self.result.groups[0].questions[2]
        self.assertTrue(q["stem_missing"])
        self.assertEqual(q["stem"], "")

    def test_order_fallback_without_group_ids(self):
        result = attach_stems(build_result(with_group_ids=False),
                              StemIndex.from_dict(load_configs()))
        self.assertTrue(all(g.alignment == "order" for g in result.groups))
        self.assertEqual(result.totals["stem_resolved"], 37)

    def test_mismatched_question_counts_are_safe(self):
        raw = load_dom()
        raw["groups"] = raw["groups"][:1]
        result = normalize_groups(raw["groups"])
        short = normalize_configs([{
            "group_id": "fixture-g00", "group_type": "discussion_group",
            "questions": [{"index": 0, "stem": "only one"}],
        }])
        attach_stems(result, short)
        self.assertEqual(result.groups[0].stem_resolved, 1)
        self.assertEqual(result.groups[0].stem_missing_count, 2)

    def test_without_index_phase1_behaviour_is_unchanged(self):
        result = build_result()
        self.assertEqual(result.totals["stem_missing"], 38)
        self.assertEqual(result.totals["stem_resolved"], 0)
        self.assertTrue(all(g.alignment == "none" for g in result.groups))


class CliOfflineTests(unittest.TestCase):
    def test_offline_replay_with_stems(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = dryrun_main([
                "--from-json", str(DOM_FIXTURE),
                "--stems-from", str(STEM_FIXTURE),
            ])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("37 resolved / 1 missing", out)
        self.assertIn("Read-only run", out)


if __name__ == "__main__":
    unittest.main()
