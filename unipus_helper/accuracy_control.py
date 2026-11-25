from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

OBJECTIVE_TYPE_NAMES = {
    "SINGLE_CHOICE",
    "LISTENING_CHOICE",
    "VIDEO_CHOICE",
    "VOCABULARY_TEST",
    "MULTIPLE_CHOICE",
    "TRUE_FALSE",
    "SORTING",
    "MATCHING",
    "PARAGRAPH_MATCH",
    "LINK_MATCH",
    "DROPDOWN_SELECT",
    "BANKED_CLOZE",
    "FILL_IN",
    "LISTENING_FILL_IN",
}

_RANGE_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*%?\s*(?:[-~–—]\s*(\d+(?:\.\d+)?)\s*%?)?\s*$"
)
_ANSWER_LABEL_PATTERN = r"(?:My voice文字作答|简答题|选词/选择填空|填空题|答案|选词填空|翻译|Answer)"
_NUMBER_PREFIX_PATTERN = r"(?:空\s*)?\d+\s*[.、:：\)\]]|Blank\s*\d+\s*[.、:：\)\]]"


def _fmt_number(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:g}"


def parse_target_accuracy(raw: Any) -> Tuple[float, float, str]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return 100.0, 100.0, "100"
    if isinstance(raw, bool):
        raise ValueError("目标正确率必须是 0–100 的数值或区间（如 85 或 80-90）")
    if isinstance(raw, (int, float)):
        val = float(raw)
        if not math.isfinite(val) or not (0.0 <= val <= 100.0):
            raise ValueError("目标正确率必须是 0–100 之间的数值")
        val = round(val, 2)
        return val, val, _fmt_number(val)

    text = str(raw).strip()
    match = _RANGE_PATTERN.match(text)
    if not match:
        raise ValueError("目标正确率必须是 0–100 的数值或区间（如 85 或 80-90）")
    low = float(match.group(1))
    high = float(match.group(2)) if match.group(2) is not None else low
    if not (math.isfinite(low) and math.isfinite(high)):
        raise ValueError("目标正确率必须是 0–100 之间的数值")
    if not (0.0 <= low <= high <= 100.0):
        raise ValueError("目标正确率区间需满足 0 ≤ 下限 ≤ 上限 ≤ 100")
    low = round(low, 2)
    high = round(high, 2)
    canonical = _fmt_number(low) if low == high else f"{_fmt_number(low)}-{_fmt_number(high)}"
    return low, high, canonical


def effective_score_threshold(
    score_threshold: float,
    target_range: Tuple[float, float],
    *,
    is_record_or_subjective: bool,
    record_score_threshold: float,
    tolerance: float = 10.0,
) -> float:
    if is_record_or_subjective:
        return float(record_score_threshold)
    low, high = target_range
    if low >= 100.0 and high >= 100.0:
        return float(score_threshold)
    return min(float(score_threshold), max(0.0, float(low) - float(tolerance)))


def _q_type_name(question: Any) -> str:
    q_type = getattr(question, "q_type", None)
    return getattr(q_type, "name", "") if q_type is not None else ""


def _clean_extracted(answer: str) -> str:
    text = (answer or "").strip()
    text = re.sub(rf"^{_ANSWER_LABEL_PATTERN}\s*[：:]\s*", "", text, flags=re.I)
    text = re.sub(rf"^(?:{_NUMBER_PREFIX_PATTERN})\s*", "", text.strip(), flags=re.I)
    text = re.sub(rf"\s*{_ANSWER_LABEL_PATTERN}\s*[：:]\s*$", "", text.strip(), flags=re.I)
    return text.strip()


def _parse_choice_letters(answer: str, valid_letters: Sequence[str]) -> List[str]:
    text = re.sub(r"^\s*(?:单选题|多选题)\s*[：:]\s*", "", answer or "")
    text = _clean_extracted(text).rstrip(".。").upper()
    if not re.fullmatch(r"[A-Z][A-Z\s,，、/;&+]*", text):
        return []
    letters = list(dict.fromkeys(re.findall(r"[A-Z]", text)))
    valid = {letter.upper() for letter in valid_letters}
    return letters if letters and set(letters) <= valid else []


def _normalize_tf(answer: str) -> str:
    raw = (answer or "").strip()
    if "正确" in raw or raw in ("对", "是", "√"):
        return "T"
    if "错误" in raw or raw in ("错", "否", "×", "不对"):
        return "F"
    text = re.sub(r"^[\d\s.、):：\]]+", "", raw.upper())
    match = re.search(r"(?<![A-Z])(TRUE|FALSE|T|F)(?![A-Z])", text)
    if not match:
        return ""
    return "T" if match.group(1) in ("TRUE", "T") else "F"


def _parse_sorting_order(answer: str, valid_letters: Sequence[str]) -> List[str]:
    valid = [letter.upper() for letter in valid_letters if letter]
    valid_set = set(valid)
    if not valid:
        return []
    upper = (answer or "").upper()
    single_answer = re.fullmatch(r"\s*\d+\s*[.、:：)\]]\s*([A-Z][A-Z\s,，、;；>→-]*)\s*", upper)
    if single_answer:
        upper = single_answer.group(1)
    numbered = re.findall(r"\d+\s*[.、)\]]\s*([A-Z])\b", upper)
    candidates = numbered if numbered else re.findall(r"\b([A-Z])\b", upper)
    if len(candidates) < len(valid):
        for chunk in re.findall(r"[A-Z]+", upper):
            letters = [ch for ch in chunk if ch in valid_set]
            if len(letters) >= len(valid):
                candidates = letters
                break
    order: List[str] = []
    for letter in candidates:
        if letter in valid_set and letter not in order:
            order.append(letter)
    return order if len(order) == len(valid) else []


def _parse_numbered_letters(answer: str, total: int, pool: Sequence[str]) -> List[str]:
    text = (answer or "").upper()
    valid = {str(x).upper() for x in pool if str(x).strip()}
    if not valid or total <= 0:
        return []
    found: Dict[int, str] = {}
    for num, letter in re.findall(
        r"(?:^|[\s,;，；])(\d{1,2})\s*[.、:：)）=\-]?\s*([A-Z])(?![A-Z])", text
    ):
        idx = int(num) - 1
        if 0 <= idx < total and letter in valid:
            found.setdefault(idx, letter)
    if found:
        return [found.get(i, "") for i in range(total)]
    seq = [ch for ch in re.findall(r"(?<![A-Z])([A-Z])(?![A-Z])", text) if ch in valid]
    if len(seq) >= total:
        return seq[:total]
    return (seq + [""] * total)[:total]


def _parse_link_pairs(answer: str, prompt_count: int, option_count: int) -> List[Tuple[int, str]]:
    pairs: List[Tuple[int, str]] = []
    seen: Set[int] = set()
    for number, letter in re.findall(r"(\d+)\s*[.\-:：)）]?\s*([A-Za-z])", answer or ""):
        index = int(number) - 1
        pos = ord(letter.upper()) - 65
        if not (0 <= index < prompt_count and 0 <= pos < option_count):
            continue
        if index in seen:
            continue
        seen.add(index)
        pairs.append((index, letter.upper()))
    return pairs


def _parse_banked_values(answer: str, expected_count: int) -> List[str]:
    if expected_count <= 0:
        return []
    text = re.sub(
        rf"(?<!^)(?<![$\w])\s+((?:{_ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{_NUMBER_PREFIX_PATTERN}))",
        r"\n\1",
        (answer or "").strip(),
        flags=re.I,
    )
    text = re.sub(rf"^{_ANSWER_LABEL_PATTERN}\s*[：:]\s*", "", text.strip(), flags=re.I)

    token_re = re.compile(
        r"(?<![\d.])(?:(?:空|Blank)\s*)?(\d{1,2})\s*(?:\.(?!\d)|\.(?=\d+\.\d)|[、:：\)\]])",
        re.I,
    )
    tokens = [(int(m.group(1)), m.start(), m.end()) for m in token_re.finditer(text)]
    if tokens:
        chosen: Dict[int, Tuple[int, int, int]] = {}
        cursor = 0
        ok = True
        for number in range(1, expected_count + 1):
            cands = [t for t in tokens if t[0] == number and t[1] >= cursor]
            if not cands:
                ok = False
                break
            chosen[number] = cands[0]
            cursor = cands[0][2]
        if ok:
            values: List[str] = []
            for number in range(1, expected_count + 1):
                start = chosen[number][2]
                end = chosen[number + 1][1] if number < expected_count else len(text)
                val = re.sub(r"[\s,，;；]+$", "", text[start:end].strip())
                if not val:
                    ok = False
                    break
                values.append(val)
            if ok:
                return values

    result = [""] * expected_count
    matched_any = False
    label_prefix = rf"(?:{_ANSWER_LABEL_PATTERN}\s*[：:]\s*)?"
    next_prefix = rf"(?<![$\w])(?:{_ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{_NUMBER_PREFIX_PATTERN})"
    for i in range(1, expected_count + 1):
        num_prefix = rf"(?:(?:空\s*)?{i}\s*[.、:：\)\]]|Blank\s*{i}\s*[.、:：\)\]])"
        m = re.search(
            rf"{label_prefix}{num_prefix}\s*(.*?)(?=\s*{next_prefix}\s*|$)",
            text,
            re.DOTALL | re.I,
        )
        if m:
            result[i - 1] = _clean_extracted(m.group(1)).replace("\n", " ")
            matched_any = True
    if matched_any:
        return result

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    cleaned_lines = []
    for line in lines:
        cleaned = re.sub(
            rf"^(?:{_ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{_NUMBER_PREFIX_PATTERN})\s*",
            "",
            line,
            flags=re.I,
        ).strip()
        if cleaned and not re.match(r"^\d+$", cleaned):
            cleaned_lines.append(_clean_extracted(cleaned))
    for i, val in enumerate(cleaned_lines[:expected_count]):
        result[i] = val
    return result


def _format_numbered(values: Sequence[str]) -> str:
    return " ".join(f"{idx + 1}.{val}" for idx, val in enumerate(values) if val)


def _perturb_free_text(value: str, hint: str, rng: random.Random) -> str:
    raw = (value or "").strip()
    if not raw:
        return raw

    prefix = (hint or "").strip()
    keep_prefix = ""
    mutable = raw
    if prefix and len(raw) > len(prefix) and raw[: len(prefix)].lower() == prefix.lower():
        keep_prefix = raw[: len(prefix)]
        mutable = raw[len(prefix) :]

    # Numeric answer perturbation (e.g. "1998", "25%")
    num_match = re.search(r"\d+", mutable)
    if num_match and not re.search(r"[A-Za-z]", mutable):
        digits = num_match.group(0)
        shifted = str(int(digits) + 1)
        return keep_prefix + mutable[: num_match.start()] + shifted + mutable[num_match.end() :]

    # Alphabetic word perturbation
    words = list(re.finditer(r"[A-Za-z]+", mutable))
    if words:
        target = max(words, key=lambda m: len(m.group(0)))
        word = target.group(0)
        mutated = _mutate_word(word, rng)
        if mutated.lower() != word.lower():
            return keep_prefix + mutable[: target.start()] + mutated + mutable[target.end() :]

    # Fallback: append a trailing character so value differs while remaining valid text
    suffix = "s" if not mutable.lower().endswith("s") else "e"
    return keep_prefix + mutable + suffix


def _mutate_word(word: str, rng: random.Random) -> str:
    if len(word) >= 4:
        # Prefer swapping two adjacent distinct interior letters for natural-looking typo
        swap_positions = [
            i for i in range(1, len(word) - 2) if word[i].lower() != word[i + 1].lower()
        ]
        if swap_positions:
            pos = rng.choice(swap_positions)
            return word[:pos] + word[pos + 1] + word[pos] + word[pos + 2 :]
    if len(word) >= 2:
        vowel_map = {"a": "e", "e": "a", "i": "o", "o": "i", "u": "a"}
        for idx in range(len(word) - 1, -1, -1):
            low = word[idx].lower()
            if low in vowel_map:
                rep = vowel_map[low]
                if word[idx].isupper():
                    rep = rep.upper()
                return word[:idx] + rep + word[idx + 1 :]
        if word[-1].lower() != "s":
            return word + ("S" if word.isupper() else "s")
        return word[:-1]
    return "e" if word.lower() != "e" else "a"


@dataclass
class _AtomicEntry:
    item_index: int
    sub_index: int
    kind: str


class AccuracyController:
    def __init__(
        self,
        low: float = 100.0,
        high: float = 100.0,
        rng: Optional[random.Random] = None,
    ):
        self.low = float(low)
        self.high = float(high)
        self.rng = rng if rng is not None else random.Random()
        self._debt: float = 0.0

    @classmethod
    def from_config_value(
        cls, raw: Any, rng: Optional[random.Random] = None
    ) -> "AccuracyController":
        low, high, _ = parse_target_accuracy(raw)
        return cls(low, high, rng=rng)

    @property
    def enabled(self) -> bool:
        return self.low < 100.0 or self.high < 100.0

    @property
    def target_range(self) -> Tuple[float, float]:
        return (self.low, self.high)

    @property
    def label(self) -> str:
        if self.low == self.high:
            return f"{_fmt_number(self.low)}%"
        return f"{_fmt_number(self.low)}–{_fmt_number(self.high)}%"

    def reset(self) -> None:
        self._debt = 0.0

    def plan_wrong_indices(
        self, total_items: int, *, enforce_submit_floor: bool = False
    ) -> Tuple[Set[int], float]:
        if not self.enabled or total_items <= 0:
            return set(), 1.0
        rate = self.rng.uniform(self.low, self.high) / 100.0
        if enforce_submit_floor:
            desired = int(round(total_items * (1.0 - rate)))
            if self.low >= 60.0:
                max_wrong = (total_items * 2) // 5
                wrong = max(0, min(desired, max_wrong))
            else:
                wrong = max(0, min(total_items, desired))
        else:
            raw_wrong = total_items * (1.0 - rate) + self._debt
            wrong = max(0, min(total_items, int(round(raw_wrong))))
            self._debt = max(-1.0, min(1.0, raw_wrong - wrong))
        chosen = set(self.rng.sample(range(total_items), wrong)) if wrong > 0 else set()
        return chosen, rate

    def apply_to_blank_list(
        self,
        answers: Sequence[str],
        *,
        hints: Optional[Sequence[str]] = None,
        banked_options: Optional[Sequence[str]] = None,
        enforce_submit_floor: bool = False,
    ) -> Tuple[List[str], int]:
        values = list(answers)
        if not self.enabled or not values:
            return values, 0
        valid_indices = [i for i, val in enumerate(values) if (val or "").strip()]
        if not valid_indices:
            return values, 0
        wrong_slots, rate = self.plan_wrong_indices(
            len(valid_indices), enforce_submit_floor=enforce_submit_floor
        )
        if not wrong_slots:
            return values, 0
        target_indices = {valid_indices[slot] for slot in wrong_slots}
        changed = 0
        pool = [str(opt).strip() for opt in (banked_options or []) if str(opt).strip()]
        original_values = list(values)
        for idx in sorted(target_indices):
            hint = hints[idx] if hints and idx < len(hints) else ""
            new_val = self._pick_wrong_blank_value(original_values, idx, hint, pool)
            if new_val and new_val.lower() != values[idx].strip().lower():
                values[idx] = new_val
                changed += 1
        if changed:
            print(
                f"    正确率控制：目标 {self.label}（抽样 {rate * 100:.0f}%），"
                f"本批 {len(valid_indices)} 空，刻意错答 {changed} 空"
            )
        return values, changed

    def apply_to_items(
        self,
        items: Sequence[Tuple[Any, str]],
        *,
        enforce_submit_floor: bool = False,
    ) -> Tuple[List[str], int]:
        outputs = [ans for _, ans in items]
        if not self.enabled or not items:
            return outputs, 0

        parsed_states: List[Optional[Dict[str, Any]]] = []
        atomic_entries: List[_AtomicEntry] = []

        for item_idx, (q, ans) in enumerate(items):
            state = self._inspect_item(q, ans)
            parsed_states.append(state)
            if state is None:
                continue
            for sub_idx in state["perturbable"]:
                atomic_entries.append(_AtomicEntry(item_idx, sub_idx, state["kind"]))

        total_units = len(atomic_entries)
        if total_units == 0:
            return outputs, 0

        wrong_slots, rate = self.plan_wrong_indices(
            total_units, enforce_submit_floor=enforce_submit_floor
        )
        if not wrong_slots:
            return outputs, 0

        selected_by_item: Dict[int, List[int]] = {}
        for slot in sorted(wrong_slots):
            entry = atomic_entries[slot]
            selected_by_item.setdefault(entry.item_index, []).append(entry.sub_index)

        changed_units = 0
        for item_idx, sub_indices in selected_by_item.items():
            state = parsed_states[item_idx]
            if state is None:
                continue
            new_answer, delta = self._apply_item_perturbation(
                state,
                sub_indices,
                enforce_submit_floor=enforce_submit_floor,
                total_units=total_units,
                already_changed=changed_units,
            )
            if delta > 0 and new_answer:
                outputs[item_idx] = new_answer
                changed_units += delta

        if changed_units:
            print(
                f"    正确率控制：目标 {self.label}（抽样 {rate * 100:.0f}%），"
                f"共 {total_units} 项客观小题，刻意错答 {changed_units} 项"
            )
        return outputs, changed_units

    def _inspect_item(self, q: Any, answer: str) -> Optional[Dict[str, Any]]:
        if not answer or not str(answer).strip():
            return None
        q_name = _q_type_name(q)
        if q_name not in OBJECTIVE_TYPE_NAMES:
            return None
        if getattr(q, "is_evaluation", False):
            return None

        if q_name in ("SINGLE_CHOICE", "LISTENING_CHOICE", "VIDEO_CHOICE", "VOCABULARY_TEST"):
            options = list(getattr(q, "options", None) or [])
            valid = [
                (getattr(opt, "letter", "") or chr(65 + idx)).upper()
                for idx, opt in enumerate(options)
            ]
            if len(valid) < 2:
                return None
            letters = _parse_choice_letters(answer, valid)
            if len(letters) != 1:
                return None
            return {
                "kind": "single_choice",
                "valid": valid,
                "current": letters[0],
                "perturbable": [0],
            }

        if q_name == "TRUE_FALSE":
            letter = _normalize_tf(answer)
            if letter not in ("T", "F"):
                return None
            return {
                "kind": "true_false",
                "current": letter,
                "perturbable": [0],
            }

        if q_name == "MULTIPLE_CHOICE":
            options = list(getattr(q, "options", None) or [])
            valid = [
                (getattr(opt, "letter", "") or chr(65 + idx)).upper()
                for idx, opt in enumerate(options)
            ]
            if len(valid) < 2:
                return None
            letters = _parse_choice_letters(answer, valid)
            if not letters:
                return None
            return {
                "kind": "multiple_choice",
                "valid": valid,
                "current": letters,
                "perturbable": [0],
            }

        if q_name == "SORTING":
            options = list(getattr(q, "options", None) or [])
            valid = [
                (getattr(opt, "letter", "") or chr(65 + idx)).upper()
                for idx, opt in enumerate(options)
            ]
            order = _parse_sorting_order(answer, valid)
            if len(order) < 2:
                return None
            return {
                "kind": "sorting",
                "order": order,
                "perturbable": [0],
            }

        if q_name in ("MATCHING", "PARAGRAPH_MATCH"):
            inputs = list(getattr(q, "inputs", None) or [])
            blanks = list(getattr(q, "banked_blanks", None) or [])
            total = len(inputs) or len(blanks)
            if total <= 0:
                return None
            if q_name == "MATCHING":
                pool = [
                    str(getattr(opt, "letter", "")).upper()
                    for opt in (getattr(q, "options", None) or [])
                    if getattr(opt, "letter", "")
                ] or [str(x).upper() for x in (getattr(q, "banked_options", None) or [])] or list("ABCDEF")
            else:
                pool = [str(x).upper() for x in (getattr(q, "banked_options", None) or [])] or list("ABCDEFGHIJKLMNO")
            if len(pool) < 2:
                return None
            letters = _parse_numbered_letters(answer, total, pool)
            if not any(letters):
                return None
            perturbable = [i for i, val in enumerate(letters) if val]
            return {
                "kind": "letter_blanks",
                "pool": pool,
                "values": letters,
                "perturbable": perturbable,
            }

        if q_name == "LINK_MATCH":
            link_items = list(getattr(q, "link_items", None) or [])
            prompts = [item for item in link_items if item.get("side") == "right"]
            options = [item for item in link_items if item.get("side") == "left"]
            if len(prompts) < 1 or len(options) < 2:
                return None
            pairs = _parse_link_pairs(answer, len(prompts), len(options))
            if not pairs:
                return None
            return {
                "kind": "link_match",
                "pairs": pairs,
                "option_count": len(options),
                "perturbable": list(range(len(pairs))),
            }

        if q_name == "DROPDOWN_SELECT":
            blanks = list(getattr(q, "banked_blanks", None) or [])
            count = len(blanks) or int(getattr(q, "answer_count", 0) or 0)
            if count <= 0:
                return None
            values = _parse_banked_values(answer, count)
            if len(values) != count or not any(values):
                return None
            global_pool = [
                str(opt).strip()
                for opt in (getattr(q, "banked_options", None) or [])
                if str(opt).strip()
            ]
            pools: List[List[str]] = []
            perturbable: List[int] = []
            for idx in range(count):
                b_opts = (
                    [str(o).strip() for o in (blanks[idx].get("options") or []) if str(o).strip()]
                    if idx < len(blanks) and isinstance(blanks[idx], dict)
                    else []
                )
                pool = b_opts or global_pool
                pools.append(pool)
                val = values[idx].strip()
                if val and any(opt.lower() != val.lower() for opt in pool):
                    perturbable.append(idx)
            if not perturbable:
                return None
            return {
                "kind": "dropdown",
                "values": values,
                "pools": pools,
                "perturbable": perturbable,
            }

        if q_name in ("BANKED_CLOZE", "FILL_IN", "LISTENING_FILL_IN"):
            blanks = list(getattr(q, "banked_blanks", None) or [])
            count = (
                len(blanks)
                if q_name == "BANKED_CLOZE" and blanks
                else int(getattr(q, "answer_count", 0) or 0) or len(blanks)
            )
            if count <= 0:
                return None
            values = _parse_banked_values(answer, count)
            if len(values) != count or not any(values):
                return None
            hints = [
                str(blanks[i].get("hint") or "").strip()
                if i < len(blanks) and isinstance(blanks[i], dict)
                else ""
                for i in range(count)
            ]
            pool = [
                str(opt).strip()
                for opt in (getattr(q, "banked_options", None) or [])
                if str(opt).strip()
            ]
            perturbable = [i for i, val in enumerate(values) if val.strip()]
            if not perturbable:
                return None
            return {
                "kind": "text_blanks",
                "values": values,
                "hints": hints,
                "pool": pool,
                "perturbable": perturbable,
            }

        return None

    def _apply_item_perturbation(
        self,
        state: Dict[str, Any],
        sub_indices: Sequence[int],
        *,
        enforce_submit_floor: bool,
        total_units: int,
        already_changed: int,
    ) -> Tuple[str, int]:
        kind = state["kind"]

        if kind == "single_choice":
            alts = [letter for letter in state["valid"] if letter != state["current"]]
            if not alts:
                return "", 0
            return self.rng.choice(alts), 1

        if kind == "true_false":
            return ("F" if state["current"] == "T" else "T"), 1

        if kind == "multiple_choice":
            selected = list(state["current"])
            unselected = [letter for letter in state["valid"] if letter not in selected]
            choices: List[List[str]] = []
            if len(selected) >= 2:
                drop = self.rng.choice(selected)
                choices.append([l for l in selected if l != drop])
            if unselected:
                add = self.rng.choice(unselected)
                choices.append(selected + [add])
                if len(selected) == 1:
                    choices.append([add])
            if not choices:
                return "", 0
            picked = sorted(self.rng.choice(choices))
            return "".join(picked), 1

        if kind == "sorting":
            order = list(state["order"])
            pos = self.rng.randrange(len(order) - 1)
            order[pos], order[pos + 1] = order[pos + 1], order[pos]
            return " ".join(order), 1

        if kind == "letter_blanks":
            values = list(state["values"])
            pool = state["pool"]
            changed = 0
            for idx in sub_indices:
                alts = [letter for letter in pool if letter != values[idx]]
                if alts:
                    values[idx] = self.rng.choice(alts)
                    changed += 1
            return _format_numbered(values), changed

        if kind == "link_match":
            pairs = list(state["pairs"])
            option_letters = [chr(65 + i) for i in range(state["option_count"])]
            used = {letter for _, letter in pairs}
            unused = [letter for letter in option_letters if letter not in used]
            targets = list(sub_indices)
            if len(targets) >= 2:
                assigned = [pairs[i][1] for i in targets]
                rotated = assigned[1:] + assigned[:1]
                for i, new_letter in zip(targets, rotated):
                    pairs[i] = (pairs[i][0], new_letter)
                formatted = " ".join(f"{idx + 1}.{letter}" for idx, letter in pairs)
                return formatted, len(targets)
            if len(targets) == 1:
                idx = targets[0]
                if unused:
                    pairs[idx] = (pairs[idx][0], self.rng.choice(unused))
                    formatted = " ".join(f"{i + 1}.{l}" for i, l in pairs)
                    return formatted, 1
                if len(pairs) >= 2:
                    max_wrong = (total_units * 2) // 5 if (enforce_submit_floor and self.low >= 60.0) else total_units
                    if already_changed + 2 <= max_wrong:
                        other = self.rng.choice([i for i in range(len(pairs)) if i != idx])
                        p_idx, p_letter = pairs[idx]
                        o_idx, o_letter = pairs[other]
                        pairs[idx] = (p_idx, o_letter)
                        pairs[other] = (o_idx, p_letter)
                        formatted = " ".join(f"{i + 1}.{l}" for i, l in pairs)
                        return formatted, 2
            return "", 0

        if kind == "dropdown":
            values = list(state["values"])
            pools = state["pools"]
            changed = 0
            for idx in sub_indices:
                alts = [opt for opt in pools[idx] if opt.lower() != values[idx].strip().lower()]
                if alts:
                    values[idx] = self.rng.choice(alts)
                    changed += 1
            return _format_numbered(values), changed

        if kind == "text_blanks":
            original_values = list(state["values"])
            values = list(original_values)
            hints = state["hints"]
            pool = state["pool"]
            changed = 0
            for idx in sub_indices:
                new_val = self._pick_wrong_blank_value(original_values, idx, hints[idx], pool)
                if new_val and new_val.lower() != values[idx].strip().lower():
                    values[idx] = new_val
                    changed += 1
            return _format_numbered(values), changed

        return "", 0

    def _pick_wrong_blank_value(
        self,
        values: Sequence[str],
        idx: int,
        hint: str,
        pool: Sequence[str],
    ) -> str:
        current = (values[idx] or "").strip()
        if not current:
            return current
        prefix = (hint or "").strip().lower()
        is_numeric = bool(re.search(r"\d", current) and not re.search(r"[A-Za-z]", current))

        def compatible(candidate: str) -> bool:
            cand = candidate.strip()
            if not cand or cand.lower() == current.lower():
                return False
            cand_numeric = bool(re.search(r"\d", cand) and not re.search(r"[A-Za-z]", cand))
            if is_numeric != cand_numeric:
                return False
            if prefix:
                return len(cand) > len(prefix) and cand.lower().startswith(prefix)
            return True

        pool_alts = [opt for opt in pool if compatible(opt)]
        if pool_alts:
            return self.rng.choice(pool_alts)

        peer_alts = [
            val.strip()
            for j, val in enumerate(values)
            if j != idx and compatible(val)
        ]
        if peer_alts:
            return self.rng.choice(peer_alts)

        return _perturb_free_text(current, hint, self.rng)
