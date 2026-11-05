"""Read-only adapter for U Campus 5.0 ``ipub`` courseware iframes.

U Campus 5.0 renders task content inside
``iframe#ipublish-pc-book-easy-iframe`` (``ipub.unipus.cn``).  The legacy
parser in ``UnipusAI_Helper.py`` only reads the top-level document, so it
reports zero question containers on these pages.

This module locates the iframe, waits for lazy content, extracts the question
structure and reports it.  It never clicks a submit button, never writes an
answer and never calls the LLM.  Answer execution and submission belong to a
later phase.

Phase 2 (``ipub_questions.py``) resolves the question stems from the React
``questionConfig`` held in page memory and attaches them to the result via
:func:`ipub_questions.attach_stems`.  The ``Group`` fields ``alignment``,
``stem_source``, ``stem_resolved`` and ``api_question_count`` are populated by
that step and keep their defaults when phase 2 is not used.

The module depends only on the standard library.  Selenium is optional: pass
any object implementing the small ``IpubDriver`` protocol documented below
(``execute`` / ``set_frame``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Protocol

__all__ = [
    "IpubDriver",
    "SeleniumDriver",
    "WebDriverHttpClient",
    "IpubFrame",
    "ensure_loaded",
    "extract_groups",
    "normalize_groups",
    "dry_run",
    "SUPPORTED_TYPES",
    "FRAME_SELECTOR",
]

FRAME_SELECTOR = "iframe#ipublish-pc-book-easy-iframe"

# Phase 1 recognises these groups.  Everything else is reported as
# unsupported instead of being silently mis-parsed.
SUPPORTED_TYPES = frozenset(
    {"single_choice_group", "true_false_group", "fill_blanks_group"}
)

_UNSUPPORTED_HINT = {
    "discussion_group": "nested bbs iframe",
    "fill_blanks_choice_group": "drag-and-drop word bank",
    "short_answer_group": "Lexical long-text editor",
    "oral_sentence_scoop_group": "browser recording / speech evaluation",
    "oral_personal_state_group": "browser recording / speech evaluation",
    "multi_media_upload_group": "file upload",
}

# Executed *inside* the ipub iframe.  Kept free of side effects: no clicks,
# no focus changes, no writes.  Class names are matched by prefix because
# CSS Modules hashes change between deployments.
_EXTRACT_JS = r"""
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const groups = [...document.querySelectorAll('[data-ipublish-question-group-type]')];

// A word-bank option also carries data-question-id; keep only top-level
// question nodes so we do not count those as separate questions.
const isQuestion = (el, root) => {
  let p = el.parentElement;
  while (p && p !== root) {
    if (p.hasAttribute && p.hasAttribute('data-question-id')) return false;
    p = p.parentElement;
  }
  return true;
};

return groups.map((g, gi) => {
  const type = g.getAttribute('data-ipublish-question-group-type');
  const groupText = clean(g.innerText);
  const done = groupText.includes('重新作答') || groupText.includes('得分：');
  const submit = g.querySelector('[data-ipublish-event-type="question-submit"]');

  // Shared material usually lives just before the group in the textbook
  // body, not inside the question container.
  let material = '';
  let prev = g.previousElementSibling;
  for (let i = 0; i < 3 && prev; i++, prev = prev.previousElementSibling) {
    const t = clean([...prev.querySelectorAll('[data-lexical-text="true"]')]
      .map(e => e.textContent).join(' '));
    if (t) { material = t.slice(0, 800); break; }
  }

  const questions = [...g.querySelectorAll('[data-question-id]')]
    .filter(q => isQuestion(q, g))
    .map((q, qi) => {
      const editors = [...q.querySelectorAll('.editor-content')];
      const stem = clean(editors.map(e => clean(e.innerText) || clean(e.textContent)).join(' '));
      const options = [...q.querySelectorAll(
        '[class*="chioceView_strongItem"], [class*="answer_option__"], [data-option-value]'
      )].map(o => ({
        label: clean(o.innerText) || clean(o.getAttribute('data-option-value')),
        value: o.getAttribute('data-option-value') || null,
        draggable: o.getAttribute('draggable') === 'true',
      })).filter(o => o.label).slice(0, 20);
      const blanks = [...q.querySelectorAll('[contenteditable="true"], [data-ipublish-blank]')]
        .map(b => ({ tag: b.tagName, editable: b.getAttribute('contenteditable') }));
      return {
        index: qi,
        qid: q.getAttribute('data-question-id'),
        stem: stem.slice(0, 1200),
        stem_missing: !stem,
        options,
        blanks,
      };
    });

  return {
    index: gi,
    group_id: g.id || '',
    type,
    done,
    has_submit: !!submit,
    submit_label: submit ? clean(submit.innerText) : null,
    group_text: groupText.slice(0, 400),
    material,
    question_count: questions.length,
    questions,
  };
});
"""

_SCROLL_JS = r"""
const els = [...document.querySelectorAll(
  '[data-ipublish-question-group-type], [class*="editor-question-block"]'
)];
for (const e of els) e.scrollIntoView({block: 'center'});
window.scrollTo(0, 0);
return els.length;
"""

_COUNT_JS = r"""
return {
  groups: document.querySelectorAll('[data-ipublish-question-group-type]').length,
  questions: document.querySelectorAll('[data-question-id]').length,
  stems: [...document.querySelectorAll('.editor-content')]
    .filter(e => (e.innerText || '').trim()).length,
};
"""


class IpubDriver(Protocol):
    """Minimal driver contract used by this module."""

    def execute(self, script: str, args: Optional[List[Any]] = None) -> Any:
        """Run JS in the current frame and return its value."""
        ...

    def set_frame(self, frame_id: Optional[Any]) -> None:
        """Switch to a frame, or back to the top document when ``None``."""
        ...


class SeleniumDriver:
    """Adapter around a Selenium ``WebDriver``."""

    def __init__(self, driver: Any):
        self._driver = driver

    def execute(self, script: str, args=None):
        return self._driver.execute_script(script, *(args or []))

    def set_frame(self, frame_id):
        if frame_id is None:
            self._driver.switch_to.default_content()
        elif isinstance(frame_id, int):
            # Selenium 4 no longer accepts a bare frame index reliably;
            # resolve the iframe element instead.
            frames = self._driver.find_elements("css selector", "iframe")
            if frame_id >= len(frames):
                raise IndexError(f"frame index {frame_id} out of range")
            self._driver.switch_to.frame(frames[frame_id])
        else:
            self._driver.switch_to.frame(frame_id)


class WebDriverHttpClient:
    """Talk to an **existing** WebDriver session over its HTTP endpoint.

    Lets a dry-run attach to the Edge window the GUI already opened, without
    creating a second browser session.  ``base_url`` is the msedgedriver
    address (e.g. ``http://127.0.0.1:59736``).
    """

    def __init__(self, base_url: str, session_id: Optional[str] = None, timeout: int = 30):
        import requests  # optional dependency, only needed for this adapter

        self._requests = requests
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if session_id is None:
            r = requests.get(f"{self.base_url}/sessions", timeout=timeout).json()
            sessions = r.get("value") or []
            if not sessions:
                raise RuntimeError("no active WebDriver session found")
            session_id = sessions[0]["id"]
        self.session_id = session_id
        self._prefix = f"{self.base_url}/session/{session_id}"

    def _post(self, path: str, payload: Optional[dict] = None) -> dict:
        r = self._requests.post(
            f"{self._prefix}/{path}", json=payload or {}, timeout=self.timeout
        )
        data = r.json()
        if data.get("value") is not None and isinstance(data.get("value"), dict):
            if "error" in data["value"]:
                raise RuntimeError(f"WebDriver error: {data['value']}")
        return data

    def execute(self, script: str, args=None):
        return self._post(
            "execute/sync", {"script": script, "args": args or []}
        ).get("value")

    def set_frame(self, frame_id):
        self._post("frame", {"id": frame_id})

    @property
    def current_url(self) -> str:
        return self._requests.get(
            f"{self._prefix}/url", timeout=self.timeout
        ).json().get("value", "")


class IpubFrame:
    """Context manager that switches into the ipub iframe and back.

    ``available`` is False on legacy pages without the iframe; in that case
    the caller should fall back to the existing top-level parser.
    """

    def __init__(self, driver: IpubDriver):
        self.driver = driver
        self.available = False

    def __enter__(self) -> "IpubFrame":
        try:
            found = self.driver.execute(
                "return !!document.querySelector(arguments[0]);", [FRAME_SELECTOR]
            )
        except Exception:
            found = False
        if not found:
            return self
        self.driver.set_frame(0)
        self.available = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.available:
            try:
                self.driver.set_frame(None)
            except Exception:
                pass
        return None


def ensure_loaded(
    driver: IpubDriver, settle_rounds: int = 3, pause: float = 0.6
) -> Dict[str, int]:
    """Scroll lazy blocks and wait until group/question counts stabilise.

    Must be called while inside the iframe.  Read-only apart from scrolling.
    """
    last = {"groups": -1, "questions": -1, "stems": -1}
    stable = 0
    for _ in range(settle_rounds * 3):
        try:
            driver.execute(_SCROLL_JS)
        except Exception:
            break
        time.sleep(pause)
        try:
            counts = driver.execute(_COUNT_JS) or {}
        except Exception:
            break
        counts = {k: int(counts.get(k, 0)) for k in last}
        if counts == last:
            stable += 1
            if stable >= settle_rounds:
                return counts
        else:
            stable = 0
            last = counts
    return last if last["groups"] >= 0 else {"groups": 0, "questions": 0, "stems": 0}


def extract_groups(driver: IpubDriver) -> List[dict]:
    """Return raw group dictionaries from the current (iframe) document."""
    return driver.execute(_EXTRACT_JS) or []


@dataclass
class Group:
    index: int
    type: str
    done: bool
    supported: bool
    question_count: int
    stem_missing_count: int
    unsupported_reason: Optional[str] = None
    material: str = ""
    questions: List[dict] = field(default_factory=list)
    # Populated by ipub_questions.attach_stems (phase 2).
    group_id: str = ""
    alignment: str = "none"
    stem_source: Optional[str] = None
    stem_resolved: int = 0
    api_question_count: int = 0


@dataclass
class DryRunResult:
    url: str
    frame_available: bool
    load_counts: Dict[str, int]
    groups: List[Group] = field(default_factory=list)

    @property
    def totals(self) -> Dict[str, int]:
        return {
            "groups": len(self.groups),
            "groups_done": sum(1 for g in self.groups if g.done),
            "questions": sum(g.question_count for g in self.groups),
            "supported_questions": sum(
                g.question_count for g in self.groups if g.supported and not g.done
            ),
            "stem_missing": sum(g.stem_missing_count for g in self.groups),
            "stem_resolved": sum(g.stem_resolved for g in self.groups),
            "groups_with_stems": sum(1 for g in self.groups if g.stem_resolved),
            "unsupported_groups": sum(1 for g in self.groups if not g.supported),
            "supported_groups": sum(1 for g in self.groups if g.supported),
        }

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "frame_available": self.frame_available,
            "load_counts": self.load_counts,
            "totals": self.totals,
            "groups": [asdict(g) for g in self.groups],
        }


def normalize_groups(raw: List[dict], url: str = "", load_counts=None,
                     frame_available: bool = True) -> DryRunResult:
    """Pure data transform, independent of Selenium.  Unit-testable offline."""
    result = DryRunResult(
        url=url,
        frame_available=frame_available,
        load_counts=dict(load_counts or {}),
    )
    for item in raw or []:
        gtype = item.get("type") or "unknown"
        questions = item.get("questions") or []
        result.groups.append(
            Group(
                index=int(item.get("index", 0)),
                type=gtype,
                done=bool(item.get("done")),
                supported=gtype in SUPPORTED_TYPES,
                question_count=len(questions),
                stem_missing_count=sum(1 for q in questions if q.get("stem_missing")),
                unsupported_reason=None
                if gtype in SUPPORTED_TYPES
                else _UNSUPPORTED_HINT.get(gtype, "unrecognised group type"),
                material=item.get("material") or "",
                questions=questions,
                group_id=item.get("group_id") or "",
            )
        )
    return result


def dry_run(
    driver: IpubDriver, wait: bool = True, url: str = ""
) -> DryRunResult:
    """Inspect the current task page without answering or submitting.

    Falls back to a legacy-page result when the ipub iframe is absent, so the
    caller can keep using the existing parser.
    """
    with IpubFrame(driver) as frame:
        if not frame.available:
            return DryRunResult(url=url, frame_available=False, load_counts={})
        counts = ensure_loaded(driver) if wait else {}
        raw = extract_groups(driver)
    return normalize_groups(raw, url=url, load_counts=counts, frame_available=True)
