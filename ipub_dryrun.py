"""Read-only dry-run for U Campus 5.0 iframe courseware.

Phase 1 inspects the question groups in the DOM.  Phase 2 (enabled by default
for live runs) additionally reads the question stems from the React
``questionConfig`` held in page memory and aligns them with those groups by
``groupId``.

Examples
--------
Attach to the Edge window the GUI already opened::

    .\\.venv\\Scripts\\python.exe ipub_dryrun.py --driver-url http://127.0.0.1:59736

Re-check a previously saved extraction without a browser::

    .\\.venv\\Scripts\\python.exe ipub_dryrun.py \\
        --from-json tests/fixtures/ipub_dryrun_result.json \\
        --stems-from tests/fixtures/question_configs.json

By default this command NEVER clicks submit, fills an answer or calls the LLM.
``--llm`` is an explicit opt-in that asks the configured model for *advisory*
answers and writes them only into an in-memory/JSON plan -- it still never
touches the page.  The ``answers`` (correct-answer) payload is never exported.

Phase 3 (``--plan`` / ``--llm``) turns the phase-2 stems into a per-question
answer-slot plan.  It is a planner, not an executor: nothing is typed, clicked
or submitted.

Build a plan offline (no browser, no model calls)::

    .\\.venv\\Scripts\\python.exe ipub_dryrun.py \\
        --from-json tests/fixtures/ipub_dryrun_result.json \\
        --stems-from tests/fixtures/question_configs.json \\
        --plan --plan-out phase3_plan.json

Smoke-test a newly configured key with at most N model calls::

    .\\.venv\\Scripts\\python.exe ipub_dryrun.py \\
        --from-json tests/fixtures/ipub_dryrun_result.json \\
        --stems-from tests/fixtures/question_configs.json \\
        --llm-limit 3 --plan-out phase3_llm_smoke.json

``--llm-limit 0`` builds the plan without any network access or config file.

Check which groups the GUI's course-v3 executor can actually answer::

    .\\.venv\\Scripts\\python.exe ipub_dryrun.py \\
        --from-json tests/fixtures/ipub_dryrun_result.json --gui-support

This parses ``PublishQuestionParser.GROUP_KINDS`` in ``UnipusAI_Helper.py``
with ``ast``; no browser, GUI or network is involved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ipub_adapter import (
    DryRunResult,
    IpubFrame,
    WebDriverHttpClient,
    ensure_loaded,
    extract_groups,
    normalize_groups,
)
from ipub_questions import (
    attach_stems,
    build_stem_index,
    load_stem_index,
    save_stem_index,
)
from ipub_plan import (
    OpenAICompatClient,
    apply_suggestions,
    build_plan,
    save_plan,
)
from ipub_support import build_support_report, format_support_report

DEFAULT_DRIVER_URL = "http://127.0.0.1:59736"


class _NoNetworkClient:
    """Guards ``--llm-limit 0``: even a configured key cannot reach the net."""

    def complete(self, prompt: str) -> str:
        raise RuntimeError("no network: --llm-limit 0")


def _print_summary(result) -> None:
    t = result.totals
    print(f"URL               : {result.url or '(offline)'}")
    print(f"ipub iframe       : {'found' if result.frame_available else 'NOT found (legacy page)'}")
    if result.load_counts:
        print(f"loaded            : {result.load_counts}")
    print(
        f"groups            : {t['groups']} "
        f"({t['groups_done']} already answered, {t['unsupported_groups']} unsupported)"
    )
    print(f"questions         : {t['questions']}")
    print(f"phase-1 answerable: {t['supported_questions']}")
    print(
        f"stems             : {t['stem_resolved']} resolved / "
        f"{t['stem_missing']} missing "
        f"({t['groups_with_stems']} groups with stems)"
    )
    print("-" * 72)
    for g in result.groups:
        flag = "ok " if g.supported else "-- "
        state = "done" if g.done else "todo"
        extra = ""
        if g.stem_resolved:
            extra += f"  stems={g.stem_resolved}"
        if g.stem_missing_count:
            extra += f"  stem-missing={g.stem_missing_count}"
        if g.alignment != "none":
            extra += f"  align={g.alignment}"
        if g.unsupported_reason:
            extra += f"  reason={g.unsupported_reason}"
        print(
            f"{flag}[{g.index:02d}] {g.type:<28} {state:<5} "
            f"q={g.question_count:<3}{extra}"
        )
    print("-" * 72)

    samples = [
        q["stem"]
        for g in result.groups
        for q in g.questions
        if q.get("stem") and not q.get("stem_missing")
    ][:3]
    if samples:
        print("Stem samples (read-only, truncated):")
        for s in samples:
            print(f"  - {s[:100]}")
        print("-" * 72)

    if not result.frame_available:
        print("No ipub iframe: keep using the existing top-level parser.")
    elif t["stem_missing"] and not t["stem_resolved"]:
        print(
            "NOTE: no stems found in the React questionConfig. "
            "Falling back to phase-1 DOM-only inspection."
        )
    print("Read-only run: nothing was answered or submitted.")


def _print_plan_summary(plan) -> None:
    t = plan.totals()
    print("=" * 72)
    print("Phase 3 answer plan (read-only, no submission)")
    print("=" * 72)
    print(
        f"groups            : {t['groups']} "
        f"({t['supported_groups']} plannable, {t['unsupported_groups']} unsupported)"
    )
    print(f"questions         : {t['questions']} ({t['planned_questions']} with slot layouts)")
    print(f"answer slots      : {t['slots']}")
    print(f"stem missing      : {t['stem_missing']}")
    print(f"LLM suggestions   : {t['suggestions']} / {t['slots']}")
    if t["errors"]:
        print(f"LLM errors        : {t['errors']}")
    print("-" * 72)
    for g in plan.groups:
        slots = sum(len(q.slots) for q in g.questions)
        print(
            f"{'ok ' if g.supported else '-- '}[{g.index:02d}] {g.group_type:<28} "
            f"q={len(g.questions):<3} slots={slots:<3} {g.reason}"
        )
    print("-" * 72)
    samples = [
        (q.kind, [s.suggestion for s in q.slots])
        for g in plan.groups
        for q in g.questions
        if any(s.suggestion for s in q.slots)
    ][:3]
    if samples:
        print("Suggestion samples (advisory only, never written to the page):")
        for kind, values in samples:
            print(f"  - {kind}: {values}")
        print("-" * 72)
    print("Read-only plan: nothing was answered or submitted.")


def _offline_result(path: Path, stems_from: Path | None = None) -> DryRunResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("groups", []) if isinstance(data, dict) else data
    result = (
        normalize_groups(
            raw,
            url=data.get("url", ""),
            load_counts=data.get("load_counts", {}),
            frame_available=data.get("frame_available", True),
        )
        if isinstance(data, dict)
        else normalize_groups(raw)
    )
    if stems_from:
        attach_stems(result, load_stem_index(stems_from))
    return result


def _live_result(base_url: str, wait: bool, with_stems: bool, stems_out: Path | None):
    client = WebDriverHttpClient(base_url)
    url = client.current_url
    index = None
    with IpubFrame(client) as frame:
        if not frame.available:
            return DryRunResult(url=url, frame_available=False, load_counts={}), None
        counts = ensure_loaded(client) if wait else {}
        raw = extract_groups(client)
        if with_stems:
            # Must run while still inside the iframe: the React app lives there.
            index = build_stem_index(client)
    result = normalize_groups(raw, url=url, load_counts=counts, frame_available=True)
    if index is not None:
        attach_stems(result, index)
        if stems_out:
            save_stem_index(index, stems_out)
    return result, index


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--driver-url", default=DEFAULT_DRIVER_URL,
                   help="msedgedriver HTTP base URL of the running GUI session")
    p.add_argument("--from-json", type=Path,
                   help="offline mode: replay a saved result instead of using a browser")
    p.add_argument("--stems-from", type=Path,
                   help="offline mode: load a saved questionConfig extraction")
    p.add_argument("--stems-out", type=Path,
                   help="live mode: save the extracted questionConfig to this path")
    p.add_argument("--no-stems", action="store_true",
                   help="phase 1 only: skip React questionConfig extraction")
    p.add_argument("--json-out", type=Path,
                   help="also write the full structured result to this path")
    p.add_argument("--no-wait", action="store_true",
                   help="skip lazy-load scrolling/waiting")
    p.add_argument("--plan", action="store_true",
                   help="phase 3: build a per-question answer-slot plan (still read-only)")
    p.add_argument("--llm", action="store_true",
                   help="phase 3: ask the configured model for advisory answers; "
                        "implies --plan, never writes to the page")
    p.add_argument("--llm-limit", type=int, metavar="N",
                   help="phase 3: cap the number of model calls (implies --plan); "
                        "use a small N to smoke-test a new API key")
    p.add_argument("--plan-out", type=Path,
                   help="phase 3: write the full answer plan JSON to this path")
    p.add_argument("--gui-support", action="store_true",
                   help="report which groups the GUI course-v3 executor can answer, "
                        "by parsing PublishQuestionParser.GROUP_KINDS (no browser)")
    p.add_argument("--support-out", type=Path,
                   help="write the GUI compatibility report JSON to this path")
    args = p.parse_args(argv)
    if args.llm_limit is not None and args.llm_limit < 0:
        p.error("--llm-limit must be >= 0")

    try:
        if args.from_json:
            result = _offline_result(args.from_json, args.stems_from)
        else:
            result, _ = _live_result(
                args.driver_url,
                wait=not args.no_wait,
                with_stems=not args.no_stems,
                stems_out=args.stems_out,
            )
    except Exception as exc:
        print(f"Cannot attach to WebDriver session: {exc}", file=sys.stderr)
        print(
            "Start the GUI first, then pass --driver-url with the "
            "msedgedriver port (look for it in the GUI log).",
            file=sys.stderr,
        )
        return 2

    plan = None
    if args.plan or args.llm or args.llm_limit is not None or args.plan_out:
        plan = build_plan(result)
        if args.llm or args.llm_limit is not None:
            try:
                client = (
                    _NoNetworkClient()
                    if args.llm_limit == 0 and not args.llm
                    else OpenAICompatClient.from_config_file(Path("config.json"))
                )
                apply_suggestions(plan, client, limit=args.llm_limit)
            except Exception as exc:
                print(
                    f"WARNING: LLM suggestions unavailable: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    _print_summary(result)
    if args.gui_support or args.support_out:
        report = build_support_report(result)
        print(format_support_report(report))
        if args.support_out:
            args.support_out.parent.mkdir(parents=True, exist_ok=True)
            args.support_out.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"GUI compatibility report written to {args.support_out}")
    if plan is not None:
        _print_plan_summary(plan)
        if args.plan_out:
            save_plan(plan, args.plan_out)
            print(f"Full answer plan written to {args.plan_out}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Full result written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
