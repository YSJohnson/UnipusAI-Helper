"""Read-only question-stem extraction for U Campus 5.0 ipub courseware.

Phase 1 of the iframe adapter found that question stems are **not** rendered
into the question containers: ``.editor-content`` is empty, the DOM only shows
option labels.  Phase 2 closes that gap.

Where the stems really are
--------------------------
The ipub reader is a React application.  Each ``[data-question-id]`` element
belongs to a component whose ``memoizedProps.questionConfig.json`` (also
reachable via ``questionStore``) holds the group definition::

    {"groupId": "...", "groupType": "single_choice_group",
     "list": [{"id": "...", "questionType": "single_choice",
               "quesText": "<lexical JSON>", "quesTextString": "1. What ...",
               "options": [], "children": [{"quesTextString": "...",
                                            "options": [...]}]}]}

``groupId`` equals the ``id`` attribute of the corresponding
``[data-ipublish-question-group-type]`` DOM element, so phase-1 groups and
phase-2 stems can be aligned by id (with an order-based fallback).

Scope and safety
----------------
This module is **read-only**: it walks the React fiber tree and reads props.
It does not click, type, focus, scroll, submit, call the LLM, issue a network
request or mutate page state.  It deliberately does **not** export the
``answers`` payload (correct answers); only a boolean ``has_answers`` is kept
so a caller can tell an already-answered question from a fresh one.

It has no third-party dependencies and no import from ``ipub_adapter``: the
driver is any object implementing ``execute(script, args)``.  All normalisation
is pure Python so it can be unit-tested offline with synthetic fixtures.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

__all__ = [
    "IpubQuestionDriver",
    "Option",
    "Question",
    "QuestionGroup",
    "StemIndex",
    "extract_configs",
    "normalize_configs",
    "build_stem_index",
    "load_stem_index",
    "save_stem_index",
    "attach_stems",
    "DEFAULT_SOURCE",
]

DEFAULT_SOURCE = "react-question-config"

# Executed inside the ipub iframe.  Read-only: no writes, no events, no fetch.
_EXTRACT_CONFIG_JS = r"""
const clean = (s) => (s == null ? '' : String(s).replace(/\s+/g, ' ').trim());

// quesText / optionValue are either plain strings or a serialised Lexical
// editor tree.  Flatten the tree to readable text, keeping blank markers out
// of the text (they are counted separately).
const plain = (v) => {
  if (typeof v !== 'string') return '';
  if (v.charAt(0) !== '{') return clean(v);
  try {
    const obj = JSON.parse(v);
    const parts = [];
    const walk = (n) => {
      if (Array.isArray(n)) { n.forEach(walk); return; }
      if (!n || typeof n !== 'object') return;
      if (n.type === 'text' && typeof n.text === 'string') parts.push(n.text);
      else if (n.type === 'linebreak') parts.push(' ');
      if (n.children) walk(n.children);
    };
    walk(obj.root || obj);
    return clean(parts.join(''));
  } catch (e) { return clean(v); }
};

const blankCount = (s) => {
  if (typeof s !== 'string' || s.charAt(0) !== '{') return 0;
  try {
    const obj = JSON.parse(s);
    let n = 0;
    const walk = (x) => {
      if (Array.isArray(x)) { x.forEach(walk); return; }
      if (!x || typeof x !== 'object') return;
      if (/insert-dig-empty|insert-blank|blank/i.test(x.type || '')) n++;
      if (x.children) walk(x.children);
    };
    walk(obj.root || obj);
    return n;
  } catch (e) { return 0; }
};

// React attaches its internal fiber to host nodes under __reactFiber$<suffix>.
// Walk up to the provider that owns questionConfig / questionStore.
const findCfg = (el) => {
  const fk = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
  let f = fk ? el[fk] : null;
  for (let i = 0; f && i < 25; i++, f = f.return) {
    const p = f.memoizedProps || {};
    const direct = p.questionConfig && p.questionConfig.json;
    if (direct && Array.isArray(direct.list)) return direct;
    const stored = p.value && p.value.questionStore &&
      p.value.questionStore.questionConfig && p.value.questionStore.questionConfig.json;
    if (stored && Array.isArray(stored.list)) return stored;
  }
  return null;
};

const seen = {};
const configs = [];
[...document.querySelectorAll('[data-question-id]')].forEach((el) => {
  const cfg = findCfg(el);
  if (!cfg) return;
  const gid = clean(cfg.groupId);
  if (seen[gid]) return;
  seen[gid] = true;

  const questions = (cfg.list || []).map((it, i) => {
    const children = Array.isArray(it.children) ? it.children : [];
    let stem = clean(it.quesTextString);
    let stemSource = stem ? 'quesTextString' : '';
    if (!stem) {
      stem = clean(children.map(c => c.quesTextString || '').join(' '));
      stemSource = stem ? 'children.quesTextString' : '';
    }
    if (!stem) {
      stem = clean(plain(it.quesText));
      stemSource = stem ? 'quesText' : '';
    }

    const options = [];
    const pushOpt = (o) => {
      if (!o) return;
      const label = clean(o.label);
      const value = clean(plain(o.optionValue));
      if (label || value) options.push({label, value, option_id: clean(o.optionId)});
    };
    (Array.isArray(it.options) ? it.options : []).forEach(pushOpt);
    children.forEach(c => (Array.isArray(c.options) ? c.options : []).forEach(pushOpt));
    const keys = {};
    const uniq = options.filter(o => {
      const k = o.label + '|' + o.value;
      if (keys[k]) return false;
      keys[k] = true;
      return true;
    });

    return {
      index: i,
      qid: clean(it.id),
      question_type: clean(it.questionType),
      stem: stem.slice(0, 2000),
      stem_source: stemSource || 'none',
      options: uniq.slice(0, 30),
      blank_count: blankCount(it.quesText),
      child_count: children.length,
      // Answers (including correct answers) are intentionally NOT exported.
      has_answers:
        children.some(c => Array.isArray(c.answers) && c.answers.length > 0) ||
        (Array.isArray(it.answers) && it.answers.length > 0)
    };
  });

  configs.push({
    group_id: gid,
    group_type: clean(cfg.groupType),
    questions: questions
  });
});
return configs;
"""


class IpubQuestionDriver(Protocol):
    """Minimal driver contract: run JS in the current frame and return a value."""

    def execute(self, script: str, args: Optional[List[Any]] = None) -> Any:
        ...


@dataclass
class Option:
    label: str = ""
    value: str = ""
    option_id: str = ""


@dataclass
class Question:
    index: int = 0
    qid: str = ""
    question_type: str = ""
    stem: str = ""
    stem_source: str = "none"
    options: List[Option] = field(default_factory=list)
    blank_count: int = 0
    child_count: int = 0
    has_answers: bool = False

    @property
    def stem_missing(self) -> bool:
        return not self.stem


@dataclass
class QuestionGroup:
    group_id: str
    group_type: str = ""
    questions: List[Question] = field(default_factory=list)


@dataclass
class StemIndex:
    """Question stems keyed by the DOM group id, with an order fallback."""

    groups: Dict[str, QuestionGroup] = field(default_factory=dict)
    source: str = DEFAULT_SOURCE
    order: List[str] = field(default_factory=list)

    def lookup(self, group_id: str, index: int = 0) -> Optional[QuestionGroup]:
        if group_id and group_id in self.groups:
            return self.groups[group_id]
        if 0 <= index < len(self.order):
            return self.groups.get(self.order[index])
        return None

    def alignment_kind(self, group_id: str) -> str:
        if group_id and group_id in self.groups:
            return "id"
        return "order" if self.order else "none"

    @property
    def question_count(self) -> int:
        return sum(len(g.questions) for g in self.groups.values())

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "groups": [
                {
                    "group_id": g.group_id,
                    "group_type": g.group_type,
                    "questions": [asdict(q) for q in g.questions],
                }
                for g in self.groups.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StemIndex":
        return normalize_configs(data.get("groups", []), source=data.get("source") or DEFAULT_SOURCE)


def extract_configs(driver: IpubQuestionDriver) -> List[dict]:
    """Run the read-only JS extractor in the **current** (iframe) frame."""
    return driver.execute(_EXTRACT_CONFIG_JS) or []


def normalize_configs(raw: List[dict], source: str = DEFAULT_SOURCE) -> StemIndex:
    """Pure transform from JS output to a :class:`StemIndex` (offline testable)."""
    index = StemIndex(source=source)
    for item in raw or []:
        gid = str(item.get("group_id") or "").strip()
        if not gid or gid in index.groups:
            continue
        questions: List[Question] = []
        for q in item.get("questions") or []:
            options = [
                Option(
                    label=str(o.get("label") or "").strip(),
                    value=str(o.get("value") or "").strip(),
                    option_id=str(o.get("option_id") or "").strip(),
                )
                for o in (q.get("options") or [])
            ]
            options = [o for o in options if o.label or o.value]
            # The JS extractor already deduplicates; repeat it here so an
            # offline fixture behaves exactly like a live extraction.
            seen_options = set()
            deduped = []
            for o in options:
                key = (o.label, o.value)
                if key in seen_options:
                    continue
                seen_options.add(key)
                deduped.append(o)
            questions.append(
                Question(
                    index=int(q.get("index", len(questions))),
                    qid=str(q.get("qid") or ""),
                    question_type=str(q.get("question_type") or ""),
                    stem=str(q.get("stem") or "").strip(),
                    stem_source=str(q.get("stem_source") or "none"),
                    options=deduped,
                    blank_count=int(q.get("blank_count", 0) or 0),
                    child_count=int(q.get("child_count", 0) or 0),
                    has_answers=bool(q.get("has_answers")),
                )
            )
        index.groups[gid] = QuestionGroup(
            group_id=gid,
            group_type=str(item.get("group_type") or ""),
            questions=questions,
        )
        index.order.append(gid)
    return index


def build_stem_index(driver: IpubQuestionDriver) -> StemIndex:
    """Extract and normalise in one step.  Must run while inside the iframe."""
    return normalize_configs(extract_configs(driver))


def save_stem_index(index: StemIndex, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_stem_index(path: Path) -> StemIndex:
    return StemIndex.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def attach_stems(result, index: StemIndex):
    """Enrich a phase-1 :class:`~ipub_adapter.DryRunResult` with API stems.

    Matching is by ``groupId`` (the DOM element id), then by question order
    inside the group.  Nothing is guessed: questions without a stem in the
    React config stay explicitly marked as missing.  The result is mutated
    and returned for convenience.
    """
    for g in result.groups:
        api_group = index.lookup(g.group_id, g.index)
        if api_group is None:
            g.alignment = "none"
            continue
        g.alignment = index.alignment_kind(g.group_id)
        g.stem_source = index.source
        g.api_question_count = len(api_group.questions)
        for dom_q, api_q in zip(g.questions, api_group.questions):
            if not api_q.stem:
                continue
            dom_q["stem"] = api_q.stem
            dom_q["stem_source"] = api_q.stem_source
            dom_q["stem_missing"] = False
            dom_q["api_options"] = [asdict(o) for o in api_q.options]
            dom_q["api_qid"] = api_q.qid
            dom_q["blank_count"] = api_q.blank_count
            dom_q["has_answers"] = api_q.has_answers
        g.stem_resolved = sum(
            1 for q in g.questions if q.get("stem") and not q.get("stem_missing")
        )
        g.stem_missing_count = sum(
            1 for q in g.questions if q.get("stem_missing")
        )
    return result
