from __future__ import annotations

from typing import Any, Optional

SKIP_RECORD_MODES = ("off", "follow_aloud", "all")

SKIP_RECORD_LABELS = {
    "off": "不跳过（正常作答）",
    "follow_aloud": "仅跳过跟读与朗读",
    "all": "跳过全部录音题（含自由口语）",
}


def normalize_skip_record_mode(raw: Any) -> str:
    if raw is None:
        return "off"
    if isinstance(raw, bool):
        return "all" if raw else "off"
    value = str(raw).strip().lower()
    if not value:
        return "off"
    if value in SKIP_RECORD_MODES:
        return value
    raise ValueError("skip_record_mode 必须是 off、follow_aloud 或 all")


def _q_type_name(question: Any) -> str:
    q_type = getattr(question, "q_type", None)
    return getattr(q_type, "name", "") if q_type is not None else ""


def is_record_question(question: Any) -> bool:
    return _q_type_name(question) in ("RECORD", "ORAL_ALOUD")


def is_follow_aloud_question(question: Any) -> bool:
    q_name = _q_type_name(question)
    if q_name == "ORAL_ALOUD":
        return True
    if q_name != "RECORD":
        return False

    items = list(getattr(question, "record_items", None) or [])
    if not items:
        return False

    first = items[0] if isinstance(items[0], dict) else {}
    if first.get("vocab") or first.get("role_play"):
        return True

    group_type = str(first.get("group_type") or "").strip()
    if group_type == "vocabulary_group":
        return True
    if group_type == "oral_personal_state_group":
        return any(bool(str((item or {}).get("url") or "").strip()) for item in items)

    needs_script = getattr(question, "needs_speech_script", None)
    if isinstance(needs_script, bool):
        return not needs_script

    return not any(
        not str((item or {}).get("url") or "").strip()
        and not str((item or {}).get("text") or "").strip()
        for item in items
    )


def should_skip_record_question(question: Any, mode: Any) -> bool:
    normalized = normalize_skip_record_mode(mode)
    if normalized == "off" or not is_record_question(question):
        return False
    if normalized == "all":
        return True
    return is_follow_aloud_question(question)


def should_skip_publish_group_fast(group_type: str, mode: Any) -> Optional[bool]:
    normalized = normalize_skip_record_mode(mode)
    if normalized == "off":
        return False
    if normalized == "all":
        return True
    if (group_type or "").strip() == "vocabulary_group":
        return True
    return None
