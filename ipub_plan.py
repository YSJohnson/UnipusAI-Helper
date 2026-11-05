"""Phase 3: read-only answer planning for U Campus 5.0 ipub groups.

Phases 1 and 2 tell us *what* is on the page: group types, question stems,
option labels and blank counts (from the React ``questionConfig``).  Phase 3
turns that into a structured, per-question **answer plan**: how many slots each
question needs, what each slot accepts, and what prompt would be sent to an LLM.

Safety boundary
---------------
This module is a *planner*, not an executor.  It never types into the page,
never clicks, never switches frames, never submits a group, never issues a
network request on its own and never reads the ``answers`` payload (correct
answers).  ``apply_suggestions`` is the only function that can reach the
network, and only when the caller explicitly supplies a client; its output is
written into an in-memory plan, never into the courseware.

An ``AnswerPlan`` is therefore safe to serialise, diff and unit-test.  It is
intentionally *not* consumed by any answering/submission code in this repo.

Standard library only; no import from ``ipub_adapter`` or ``ipub_questions``.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

__all__ = [
    "AnswerSlot",
    "PlannedQuestion",
    "PlannedGroup",
    "AnswerPlan",
    "SuggestionClient",
    "OpenAICompatClient",
    "plan_question",
    "build_plan",
    "build_prompt",
    "apply_suggestions",
    "save_plan",
    "UNSUPPORTED_REASONS",
]

# Groups we can describe an answer shape for.  Being "supported" here means
# "we can plan a slot layout", NOT "we can or will submit an answer".
UNSUPPORTED_REASONS = {
    "discussion_group": "discussion post requires posting to the class forum",
    "oral_sentence_scoop_group": "oral recording requires microphone/media upload",
    "oral_personal_state_group": "oral recording requires microphone/media upload",
    "multi_media_upload_group": "answer is an uploaded media file",
}


@dataclass
class AnswerSlot:
    """One value a question expects.  ``suggestion`` is advisory only."""

    index: int
    kind: str                       # choice | true_false | blank | bank | text
    accepted: List[str] = field(default_factory=list)
    hint: str = ""
    suggestion: str = ""


@dataclass
class PlannedQuestion:
    index: int = 0
    qid: str = ""
    group_id: str = ""
    group_type: str = ""
    kind: str = "unknown"
    stem: str = ""
    stem_source: str = "none"
    supported: bool = False
    reason: str = ""
    slots: List[AnswerSlot] = field(default_factory=list)
    prompt: str = ""
    error: str = ""


@dataclass
class PlannedGroup:
    index: int = 0
    group_id: str = ""
    group_type: str = ""
    supported: bool = False
    reason: str = ""
    questions: List[PlannedQuestion] = field(default_factory=list)


@dataclass
class AnswerPlan:
    groups: List[PlannedGroup] = field(default_factory=list)
    source: str = "phase-3-planner"

    @property
    def question_count(self) -> int:
        return sum(len(g.questions) for g in self.groups)

    @property
    def slot_count(self) -> int:
        return sum(len(q.slots) for g in self.groups for q in g.questions)

    def totals(self) -> Dict[str, int]:
        questions = [q for g in self.groups for q in g.questions]
        return {
            "groups": len(self.groups),
            "supported_groups": sum(1 for g in self.groups if g.supported),
            "unsupported_groups": sum(1 for g in self.groups if not g.supported),
            "questions": len(questions),
            "planned_questions": sum(1 for q in questions if q.supported and q.slots),
            "stem_missing": sum(1 for q in questions if not q.stem),
            "slots": self.slot_count,
            "suggestions": sum(
                1 for q in questions for s in q.slots if s.suggestion
            ),
            "errors": sum(1 for q in questions if q.error),
        }

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "totals": self.totals(),
            "groups": [asdict(g) for g in self.groups],
        }


def _options(question: dict) -> List[dict]:
    """React config options are richer; fall back to the phase-1 DOM labels."""
    api = question.get("api_options") or []
    if api:
        return [o for o in api if isinstance(o, dict)]
    return [o for o in (question.get("options") or []) if isinstance(o, dict)]


def _labels(question: dict) -> List[str]:
    out: List[str] = []
    for o in _options(question):
        label = str(o.get("label") or "").strip()
        if label and label not in out:
            out.append(label)
    return out


def _blank_count(question: dict) -> int:
    n = int(question.get("blank_count", 0) or 0)
    if n > 0:
        return min(n, 40)
    dom_blanks = question.get("blanks") or []
    return min(max(len(dom_blanks), 1), 40) if dom_blanks else 1


def plan_question(question: dict, group_type: str, group_id: str = "") -> PlannedQuestion:
    """Pure transform: one phase-1/2 question dict -> a planned question."""
    q = PlannedQuestion(
        index=int(question.get("index", 0) or 0),
        qid=str(question.get("api_qid") or question.get("qid") or ""),
        group_id=group_id,
        group_type=group_type,
        stem=str(question.get("stem") or "").strip(),
        stem_source=str(question.get("stem_source") or "none"),
    )
    labels = _labels(question)

    if group_type in UNSUPPORTED_REASONS:
        q.kind = group_type.replace("_group", "")
        q.reason = UNSUPPORTED_REASONS[group_type]
        return q

    if group_type == "single_choice_group":
        q.kind = "single_choice"
        q.slots = [AnswerSlot(0, "choice", labels[:20],
                              "return exactly one option label, e.g. A")]
    elif group_type == "true_false_group":
        q.kind = "true_false"
        q.slots = [AnswerSlot(0, "true_false", labels[:4] or ["T", "F"],
                              "return T or F")]
    elif group_type == "fill_blanks_group":
        q.kind = "fill_blanks"
        q.slots = [
            AnswerSlot(i, "blank", [], "one word or short phrase for blank %d" % (i + 1))
            for i in range(_blank_count(question))
        ]
    elif group_type == "fill_blanks_choice_group":
        q.kind = "fill_blanks_choice"
        q.slots = [
            AnswerSlot(i, "bank", labels[:40],
                       "pick one word from the word bank for blank %d" % (i + 1))
            for i in range(_blank_count(question))
        ]
    elif group_type == "short_answer_group":
        q.kind = "short_answer"
        q.slots = [AnswerSlot(0, "text", [], "a short written answer")]
    else:
        q.kind = group_type.replace("_group", "") or "unknown"
        q.reason = "unrecognised group type; no slot layout available"
        return q

    q.supported = True
    if not q.stem:
        q.reason = "stem missing: prompt withheld, never guess from options"
    else:
        q.prompt = build_prompt(q)
    return q


def build_plan(result) -> AnswerPlan:
    """Build a plan from a phase-1/2 :class:`~ipub_adapter.DryRunResult`."""
    plan = AnswerPlan()
    for g in result.groups:
        pg = PlannedGroup(
            index=g.index,
            group_id=getattr(g, "group_id", "") or "",
            group_type=g.type,
            supported=g.type not in UNSUPPORTED_REASONS,
            reason=UNSUPPORTED_REASONS.get(g.type, ""),
        )
        for question in g.questions:
            pg.questions.append(plan_question(question, g.type, pg.group_id))
        plan.groups.append(pg)
    return plan


def build_prompt(q: PlannedQuestion) -> str:
    """Prompt for a *suggestion*.  Contains no correct answers or credentials."""
    if not q.stem or not q.slots:
        return ""
    lines = [
        "You are solving one question from a university English course.",
        "Question type: %s" % q.kind,
        "Question: %s" % q.stem[:2000],
    ]
    accepted = q.slots[0].accepted
    if accepted:
        lines.append("Allowed values: %s" % ", ".join(accepted[:30]))
    if len(q.slots) > 1:
        lines.append("Number of blanks: %d (one value per blank, in order)." % len(q.slots))
    lines.append(q.slots[0].hint)
    lines.append(
        'Return ONLY a JSON array of strings with exactly %d element(s), '
        'e.g. ["A"] or ["word1","word2"]. No explanation.' % len(q.slots)
    )
    return "\n".join(lines)


class SuggestionClient(Protocol):
    """Anything that turns a prompt into raw text (injectable for tests)."""

    def complete(self, prompt: str) -> str:
        ...


class OpenAICompatClient:
    """Minimal OpenAI-compatible chat client, configured from ``config.json``.

    Instantiating this class does **not** open a connection; only
    :meth:`complete` does, and callers must invoke it explicitly via
    ``--llm``.  The key is read from disk and never logged or serialised.
    """

    def __init__(self, config: dict, timeout: float = 45.0):
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        self.model = str(config.get("model") or "")
        self.api_key = str(config.get("api_key") or "")
        self.temperature = float(config.get("temperature", 0.2) or 0.2)
        self.max_tokens = int(config.get("max_tokens", 512) or 512)
        self.timeout = timeout
        if not (self.base_url and self.model and self.api_key):
            raise ValueError("config.json needs base_url, model and api_key")

    @classmethod
    def from_config_file(cls, path: Path) -> "OpenAICompatClient":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.load(response)
        return str(body["choices"][0]["message"]["content"] or "")


def _parse_suggestions(raw: str, expected: int) -> List[str]:
    """Tolerant parser for an LLM JSON array; never fabricates missing values."""
    text = (raw or "").strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data[:expected]]


def apply_suggestions(
    plan: AnswerPlan,
    client: SuggestionClient,
    limit: Optional[int] = None,
) -> AnswerPlan:
    """Fill ``slot.suggestion`` in memory.  Nothing is written to the page.

    ``limit`` caps the number of model calls (useful for a cheap smoke test of
    a newly configured key).  Questions beyond the cap are marked as skipped
    rather than silently left blank, so a partial run can never be mistaken
    for a complete one.
    """
    sent = 0
    for group in plan.groups:
        for q in group.questions:
            if not q.supported or not q.prompt:
                continue
            if limit is not None and sent >= limit:
                q.error = "skipped: --llm-limit %d reached" % limit
                continue
            sent += 1
            try:
                values = _parse_suggestions(client.complete(q.prompt), len(q.slots))
            except Exception as exc:  # network/parse errors are reported, not guessed
                q.error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
                continue
            for slot, value in zip(q.slots, values):
                slot.suggestion = value
            if len(values) < len(q.slots):
                q.error = "model returned %d/%d values" % (len(values), len(q.slots))
    return plan


def save_plan(plan: AnswerPlan, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
