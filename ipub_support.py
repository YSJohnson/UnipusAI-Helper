"""Read-only compatibility report: ipub groups vs. the GUI answer executor.

The GUI's course-v3 support lives in ``UnipusAI_Helper.py`` as
``PublishQuestionParser.GROUP_KINDS``.  This module parses that table with
``ast`` (no import, no browser, no GUI startup) and combines it with a phase-1
dry-run result so you can tell, before logging in, which groups the GUI will
answer, skip or leave for manual work.

Standard library only.  Nothing here clicks, fills or submits.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path
from typing import Dict, List

__all__ = [
    "AUTO_KINDS",
    "SKIP_KINDS",
    "MANUAL_KINDS",
    "DEFAULT_MAIN",
    "extract_group_kinds",
    "capability",
    "build_support_report",
    "format_support_report",
]

DEFAULT_MAIN = "UnipusAI_Helper.py"

# PublishQuestionParser.parse_group() has explicit branches for these kinds.
AUTO_KINDS = frozenset(
    {"choice", "true_false", "fill", "dropdown", "text", "link_match", "record"}
)
# 'skip' means intentionally non-answerable (reading material / discussion).
SKIP_KINDS = frozenset({"skip"})
# 'upload' is registered but has no parse_group() branch -> reported unsupported.
MANUAL_KINDS = frozenset({"upload"})


def extract_group_kinds(path: str | Path = DEFAULT_MAIN) -> Dict[str, str]:
    """Return ``{group_type: kind}`` from the GUI source, or ``{}`` if absent."""
    source = Path(path)
    if not source.exists():
        return {}
    # Upstream contains regex literals with `\s` in plain strings; parsing them
    # raises DeprecationWarning on 3.12+. It is unrelated to what we extract.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "PublishQuestionParser"):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "GROUP_KINDS"
                for t in stmt.targets
            ):
                continue
            value = stmt.value
            if not isinstance(value, ast.Dict):
                continue
            out: Dict[str, str] = {}
            for key, val in zip(value.keys, value.values):
                if isinstance(key, ast.Constant) and isinstance(val, ast.Constant):
                    if isinstance(key.value, str) and isinstance(val.value, str):
                        out[key.value] = val.value
            return out
    return {}


def capability(kind: str) -> str:
    """auto | skip | manual | unknown."""
    if kind in AUTO_KINDS:
        return "auto"
    if kind in SKIP_KINDS:
        return "skip"
    if kind in MANUAL_KINDS:
        return "manual"
    return "unknown"


_LABELS = {
    "auto": "GUI 自动作答",
    "skip": "GUI 主动跳过",
    "manual": "需手动处理",
    "unknown": "GUI 未登记",
}


def build_support_report(result, path: str | Path = DEFAULT_MAIN) -> dict:
    """Combine a phase-1/2 DryRunResult with the GUI's group-kind table."""
    kinds = extract_group_kinds(path)
    rows: List[dict] = []
    for group in result.groups:
        kind = kinds.get(group.type, "")
        cap = capability(kind) if kind else "unknown"
        rows.append(
            {
                "index": group.index,
                "group_id": getattr(group, "group_id", "") or "",
                "group_type": group.type,
                "questions": group.question_count,
                "gui_kind": kind or "(not registered)",
                "capability": cap,
                "label": _LABELS[cap],
            }
        )
    counts = {name: sum(1 for r in rows if r["capability"] == name)
              for name in ("auto", "skip", "manual", "unknown")}
    return {
        "source": "gui-publish-question-parser",
        "main_file": str(path),
        "main_found": bool(kinds),
        "registered_group_types": len(kinds),
        "groups": rows,
        "totals": {
            "groups": len(rows),
            "questions": sum(r["questions"] for r in rows),
            **counts,
        },
    }


def format_support_report(report: dict) -> str:
    lines = [
        "=" * 74,
        "GUI course-v3 executor compatibility (read-only)",
        "=" * 74,
        "main file           : %s" % report["main_file"],
        "registered groups   : %d" % report["registered_group_types"],
    ]
    if not report["main_found"]:
        lines.append("WARNING: PublishQuestionParser.GROUP_KINDS not found; "
                     "is this the upstream course-v3 build?")
    t = report["totals"]
    lines.append(
        "groups              : %d (%d auto, %d skip, %d manual, %d unknown)"
        % (t["groups"], t["auto"], t["skip"], t["manual"], t["unknown"])
    )
    lines.append("-" * 74)
    for row in report["groups"]:
        lines.append(
            "%-4s [%02d] %-30s q=%-3d %-18s %s"
            % (
                {"auto": "ok  ", "skip": "--  ", "manual": "!!  ", "unknown": "??  "}[
                    row["capability"]
                ],
                row["index"],
                row["group_type"],
                row["questions"],
                row["gui_kind"],
                row["label"],
            )
        )
    lines.append("-" * 74)
    lines.append("Read-only report: nothing was answered or submitted.")
    return "\n".join(lines)
