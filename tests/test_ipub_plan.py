"""Offline tests for the phase-3 read-only answer planner."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ipub_adapter import normalize_groups  # noqa: E402
from ipub_plan import (  # noqa: E402
    UNSUPPORTED_REASONS,
    AnswerPlan,
    OpenAICompatClient,
    _parse_suggestions,
    apply_suggestions,
    build_plan,
    build_prompt,
    plan_question,
    save_plan,
)
from ipub_questions import attach_stems, load_stem_index  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def question(**overrides):
    base = {
        "index": 0,
        "qid": "q-1",
        "stem": "Choose the best answer for this question.",
        "stem_source": "quesTextString",
        "api_options": [],
        "options": [],
        "blank_count": 0,
        "blanks": [],
        "has_answers": False,
    }
    base.update(overrides)
    return base


def options(*labels):
    return [{"label": label, "value": "Option %s text" % label, "option_id": "o-" + label}
            for label in labels]


class SlotLayoutTests(unittest.TestCase):
    def test_single_choice_has_one_choice_slot_with_labels(self):
        q = plan_question(question(api_options=options("A", "B", "C", "D")),
                          "single_choice_group", "g1")
        self.assertTrue(q.supported)
        self.assertEqual(q.kind, "single_choice")
        self.assertEqual(len(q.slots), 1)
        self.assertEqual(q.slots[0].kind, "choice")
        self.assertEqual(q.slots[0].accepted, ["A", "B", "C", "D"])
        self.assertIn("Choose the best answer", q.prompt)
        self.assertIn("A, B, C, D", q.prompt)

    def test_dom_options_are_used_when_api_options_absent(self):
        q = plan_question(question(options=[{"label": "A"}, {"label": "B"}]),
                          "single_choice_group")
        self.assertEqual(q.slots[0].accepted, ["A", "B"])

    def test_true_false_uses_config_labels(self):
        q = plan_question(question(api_options=options("T", "F")), "true_false_group")
        self.assertEqual(q.kind, "true_false")
        self.assertEqual(q.slots[0].accepted, ["T", "F"])

    def test_true_false_falls_back_to_t_f(self):
        q = plan_question(question(), "true_false_group")
        self.assertEqual(q.slots[0].accepted, ["T", "F"])

    def test_fill_blanks_uses_blank_count(self):
        q = plan_question(question(blank_count=5), "fill_blanks_group")
        self.assertEqual(len(q.slots), 5)
        self.assertTrue(all(s.kind == "blank" for s in q.slots))
        self.assertEqual([s.index for s in q.slots], list(range(5)))
        self.assertIn("Number of blanks: 5", q.prompt)

    def test_fill_blanks_falls_back_to_dom_blank_nodes(self):
        q = plan_question(question(blanks=[{"tag": "DIV"}, {"tag": "DIV"}]),
                          "fill_blanks_group")
        self.assertEqual(len(q.slots), 2)

    def test_fill_blanks_choice_uses_word_bank(self):
        q = plan_question(question(blank_count=3, api_options=options("w1", "w2", "w3")),
                          "fill_blanks_choice_group")
        self.assertEqual(len(q.slots), 3)
        self.assertEqual(q.slots[0].kind, "bank")
        self.assertEqual(q.slots[0].accepted, ["w1", "w2", "w3"])

    def test_blank_count_is_capped(self):
        q = plan_question(question(blank_count=10000), "fill_blanks_group")
        self.assertEqual(len(q.slots), 40)

    def test_short_answer_has_one_text_slot(self):
        q = plan_question(question(), "short_answer_group")
        self.assertEqual(len(q.slots), 1)
        self.assertEqual(q.slots[0].kind, "text")

    def test_option_labels_are_deduplicated(self):
        q = plan_question(question(api_options=options("A", "A", "B")),
                          "single_choice_group")
        self.assertEqual(q.slots[0].accepted, ["A", "B"])


class UnsupportedTests(unittest.TestCase):
    def test_every_declared_unsupported_type_is_reported(self):
        for group_type, reason in UNSUPPORTED_REASONS.items():
            with self.subTest(group_type=group_type):
                q = plan_question(question(), group_type)
                self.assertFalse(q.supported)
                self.assertEqual(q.slots, [])
                self.assertEqual(q.reason, reason)
                self.assertEqual(q.prompt, "")

    def test_unknown_group_type_is_not_guessed(self):
        q = plan_question(question(), "some_future_group")
        self.assertFalse(q.supported)
        self.assertEqual(q.slots, [])
        self.assertIn("unrecognised", q.reason)

    def test_missing_stem_withholds_prompt_never_guesses(self):
        q = plan_question(question(stem="", api_options=options("A", "B")),
                          "single_choice_group")
        self.assertTrue(q.supported)
        self.assertEqual(len(q.slots), 1)
        self.assertEqual(q.prompt, "")
        self.assertIn("stem missing", q.reason)

    def test_build_prompt_returns_empty_without_stem_or_slots(self):
        q = plan_question(question(stem="", api_options=options("A")),
                          "single_choice_group")
        self.assertEqual(build_prompt(q), "")


class SuggestionTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, replies=None, fail=False):
            self.replies = replies or ['["A"]']
            self.calls = []
            self.fail = fail

        def complete(self, prompt):
            self.calls.append(prompt)
            if self.fail:
                raise RuntimeError("network down")
            return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

    def test_parse_suggestions_tolerates_markdown_and_extra_text(self):
        self.assertEqual(_parse_suggestions('Here you go:\n```json\n["A"]\n```', 1), ["A"])
        self.assertEqual(_parse_suggestions('["w1", "w2"]', 2), ["w1", "w2"])
        self.assertEqual(_parse_suggestions("not json", 1), [])
        self.assertEqual(_parse_suggestions('{"a":1}', 1), [])

    def test_suggestions_are_written_into_slots_only(self):
        plan = AnswerPlan()
        from ipub_plan import PlannedGroup
        q = plan_question(question(api_options=options("A", "B", "C")),
                          "single_choice_group", "g1")
        plan.groups.append(PlannedGroup(index=0, group_id="g1",
                                        group_type="single_choice_group",
                                        supported=True, questions=[q]))
        client = self.FakeClient(['["B"]'])
        apply_suggestions(plan, client)
        self.assertEqual(q.slots[0].suggestion, "B")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(plan.totals()["suggestions"], 1)
        self.assertEqual(q.error, "")

    def test_partial_reply_is_reported_not_padded(self):
        from ipub_plan import PlannedGroup
        q = plan_question(question(blank_count=3), "fill_blanks_group")
        plan = AnswerPlan(groups=[PlannedGroup(
            index=0, group_type="fill_blanks_group", supported=True, questions=[q])])
        apply_suggestions(plan, self.FakeClient(['["only-one"]']))
        self.assertEqual(q.slots[0].suggestion, "only-one")
        self.assertEqual(q.slots[1].suggestion, "")
        self.assertIn("1/3", q.error)

    def test_client_failure_is_recorded_per_question(self):
        from ipub_plan import PlannedGroup
        q = plan_question(question(api_options=options("A", "B")), "single_choice_group")
        plan = AnswerPlan(groups=[PlannedGroup(
            index=0, group_type="single_choice_group", supported=True, questions=[q])])
        apply_suggestions(plan, self.FakeClient(fail=True))
        self.assertIn("RuntimeError", q.error)
        self.assertEqual(q.slots[0].suggestion, "")

    def test_unsupported_and_stemless_questions_are_never_sent(self):
        from ipub_plan import PlannedGroup
        unsupported = plan_question(question(), "discussion_group")
        stemless = plan_question(question(stem=""), "single_choice_group")
        plan = AnswerPlan(groups=[PlannedGroup(
            index=0, group_type="mixed", questions=[unsupported, stemless])])
        client = self.FakeClient()
        apply_suggestions(plan, client)
        self.assertEqual(client.calls, [])

    def test_config_client_requires_key_without_network(self):
        with self.assertRaises(ValueError):
            OpenAICompatClient({"base_url": "https://example.invalid/v1", "model": "m"})
        client = OpenAICompatClient({
            "base_url": "https://example.invalid/v1", "model": "m", "api_key": "dummy"})
        self.assertEqual(client.model, "m")

    def _three_questions(self):
        from ipub_plan import PlannedGroup
        qs = [plan_question(question(index=i, api_options=options("A", "B")),
                            "single_choice_group", "g1") for i in range(3)]
        plan = AnswerPlan(groups=[PlannedGroup(
            index=0, group_id="g1", group_type="single_choice_group",
            supported=True, questions=qs)])
        return plan, qs

    def test_limit_caps_model_calls_and_marks_the_rest(self):
        plan, qs = self._three_questions()
        client = self.FakeClient(['["A"]', '["B"]'])
        apply_suggestions(plan, client, limit=2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([q.slots[0].suggestion for q in qs], ["A", "B", ""])
        self.assertIn("--llm-limit 2 reached", qs[2].error)
        self.assertEqual(plan.totals()["errors"], 1)

    def test_limit_zero_never_touches_the_network(self):
        plan, qs = self._three_questions()
        client = self.FakeClient(fail=True)
        apply_suggestions(plan, client, limit=0)
        self.assertEqual(client.calls, [])
        self.assertTrue(all("--llm-limit 0 reached" in q.error for q in qs))
        self.assertEqual(plan.totals()["suggestions"], 0)

    def test_no_limit_still_sends_every_planned_question(self):
        plan, qs = self._three_questions()
        client = self.FakeClient(['["A"]'])
        apply_suggestions(plan, client)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(plan.totals()["errors"], 0)


class PlanIntegrationTests(unittest.TestCase):
    def _offline_result(self):
        data = json.loads((FIXTURES / "ipub_dryrun_result.json").read_text(encoding="utf-8"))
        result = normalize_groups(
            data["groups"], url=data.get("url", ""),
            load_counts=data.get("load_counts", {}),
            frame_available=data.get("frame_available", True),
        )
        attach_stems(result, load_stem_index(FIXTURES / "question_configs.json"))
        return result

    def test_build_plan_from_fixtures(self):
        plan = build_plan(self._offline_result())
        t = plan.totals()
        self.assertEqual(t["groups"], 16)
        self.assertEqual(t["questions"], 38)
        self.assertGreater(t["slots"], 38)
        self.assertEqual(t["stem_missing"], 1)
        self.assertGreater(t["unsupported_groups"], 0)
        self.assertEqual(t["suggestions"], 0)

    def test_plan_dict_has_no_correct_answers(self):
        plan = build_plan(self._offline_result())
        raw = json.dumps(plan.to_dict(), ensure_ascii=False)
        for forbidden in ('"answers"', "correct_answer", "api_key", "Bearer ",
                          "q-signature", "sk-"):
            self.assertNotIn(forbidden, raw)

    def test_save_plan_roundtrip(self):
        plan = build_plan(self._offline_result())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "plan.json"
            save_plan(plan, path)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["totals"], plan.totals())
        self.assertEqual(len(data["groups"]), 16)

    def test_cli_offline_plan_mode(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ipub_dryrun.py"),
             "--from-json", str(FIXTURES / "ipub_dryrun_result.json"),
             "--stems-from", str(FIXTURES / "question_configs.json"),
             "--plan"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Phase 3 answer plan", proc.stdout)
        self.assertIn("Read-only plan: nothing was answered or submitted.", proc.stdout)

    def test_cli_llm_limit_zero_builds_plan_without_network(self):
        # --llm-limit 0 exercises the LLM wiring while guaranteeing zero calls.
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ipub_dryrun.py"),
             "--from-json", str(FIXTURES / "ipub_dryrun_result.json"),
             "--stems-from", str(FIXTURES / "question_configs.json"),
             "--llm-limit", "0"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Phase 3 answer plan", proc.stdout)
        self.assertIn("LLM errors", proc.stdout)

    def test_cli_rejects_negative_limit(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ipub_dryrun.py"),
             "--from-json", str(FIXTURES / "ipub_dryrun_result.json"),
             "--llm-limit", "-1"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must be >= 0", proc.stderr)

    def test_cli_without_plan_flag_is_unchanged(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ipub_dryrun.py"),
             "--from-json", str(FIXTURES / "ipub_dryrun_result.json"),
             "--stems-from", str(FIXTURES / "question_configs.json")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Phase 3 answer plan", proc.stdout)


class ReadOnlyGuarantees(unittest.TestCase):
    def test_planner_source_has_no_page_writes(self):
        source = (ROOT / "ipub_plan.py").read_text(encoding="utf-8")
        for token in (".click(", ".submit(", "send_keys", "switch_to",
                      "XMLHttpRequest", "innerHTML =", "fetch(",
                      "it.answers", "c.answers", "localStorage"):
            self.assertNotIn(token, source, token)

    def test_plan_does_not_expose_answers_payload(self):
        q = plan_question(question(has_answers=True, api_options=options("A", "B")),
                          "single_choice_group")
        self.assertNotIn("answers", q.__dict__)
        self.assertEqual(build_prompt(q).count("Allowed values"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
