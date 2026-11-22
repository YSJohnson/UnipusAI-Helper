"""离线端到端回归：真实作业快照 → 容器划分 → 解析 → 提取 → 写入打桩。

约束：快照只读（file:// 打开，不改文件）、不登录、不联网、不点提交。
日志：logs/check_homework_offline_e2e.log（UTF-8）。
"""

import re
import sys
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 说明：027_223624_solve 的快照在保存时 .question-common-abs-choice 只剩 CSS（DOM 命中 0），
# 用它做容器断言会得到 7 个容器；009/024 与真实运行时一致（7 section / 20 选择题容器 /
# 10 个 comp-abs-input / 40 个下拉），所以这里用 024。
SNAPSHOT = ROOT / "output" / "probe" / "024_222542_goto" / "page_main.html"
LOG_LINES = []


def log(message=""):
    LOG_LINES.append(str(message))


def check(name, condition, detail=""):
    log(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(condition)


def new_driver():
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    return webdriver.Edge(options=opts)


def main():
    import UnipusAI_Helper as U

    ok = True
    if not SNAPSHOT.exists():
        log(f"FAIL  快照不存在: {SNAPSHOT}")
        finish(False)
        return 1

    driver = new_driver()
    try:
        driver.get(SNAPSHOT.as_uri())
        ok &= scenario_containers(U, driver)
        ok &= scenario_paragraph(U, driver)
        ok &= scenario_prompt(U, driver)
        ok &= scenario_write(U, driver)
        ok &= scenario_two_sections(U, driver)
        ok &= scenario_spoilers(U)
        ok &= scenario_force_gate(U, driver)
        ok &= scenario_course_mode(U)
        ok &= scenario_paragraph_fallback(U, driver)
        ok &= scenario_unanswered(U, driver)
    except Exception:
        log("脚本异常：")
        log(traceback.format_exc())
        ok = False
    finally:
        driver.quit()

    finish(ok)
    return 0 if ok else 1


def finish(ok):
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_homework_offline_e2e.log").write_text(
        "\n".join(LOG_LINES), encoding="utf-8")
    print("\n".join(LOG_LINES[-3:]))


def scenario_containers(U, driver):
    log("=== 场景1 容器划分 ===")
    parser = U.QuestionParser(driver)
    parser.homework_mode = True
    containers = parser._find_homework_containers()
    check("容器数 == 23", len(containers) == 23, f"got={len(containers)}")
    counts = {}
    for index, container in enumerate(containers, 1):
        try:
            question = parser._parse_single(container, index)
        except Exception as exc:
            question = None
            log(f"  容器{index} 解析异常: {str(exc)[:80]}")
        if question is None:
            log(f"  容器{index}: <解析失败>")
            continue
        blanks = len(question.inputs or question.banked_blanks or [])
        counts[question.q_type.name] = counts.get(question.q_type.name, 0) + 1
        log(f"  容器{index}: {question.q_type.name} 空位={blanks}")
    expected = {"DROPDOWN_SELECT": 2, "PARAGRAPH_MATCH": 1, "SINGLE_CHOICE": 20}
    check("题型分布符合预期", counts == expected, f"got={counts}")
    return counts == expected


def scenario_paragraph(U, driver):
    log()
    log("=== 场景2 段落匹配解析 ===")
    parser = U.QuestionParser(driver)
    parser.homework_mode = True
    section = next(iter(parser._find_homework_containers()), None)
    match_section = None
    for container in parser._find_homework_containers():
        if U.ParagraphMatchingStrategy().can_parse(container, driver):
            match_section = container
            break
    if match_section is None:
        check("找到段落匹配容器", False)
        return False
    question = U.ParagraphMatchingStrategy().parse(match_section, driver, 3)
    passed = check("解析成功", question is not None)
    if not question:
        return False
    passed &= check("10 个空", len(question.inputs) == 10, f"got={len(question.inputs)}")
    passed &= check("字母池 A–N", question.banked_options == list("ABCDEFGHIJKLMN"),
                    f"got={question.banked_options}")
    statements = [item.get("statement", "") for item in question.banked_blanks]
    passed &= check("10 条陈述非空", all(s.strip() for s in statements), f"got={len(statements)}")
    return bool(passed)


def scenario_prompt(U, driver):
    log()
    log("=== 场景3 prompt 形状 ===")
    parser = U.QuestionParser(driver)
    parser.homework_mode = True
    for container in parser._find_homework_containers():
        if U.ParagraphMatchingStrategy().can_parse(container, driver):
            question = U.ParagraphMatchingStrategy().parse(container, driver, 3)
            prompt = "\n".join(object.__new__(U.PromptBuilder)._build_paragraph_match(question))
            passed = check("含「只返回 空号.字母」", "只返回 空号.字母" in prompt)
            passed &= check("含字母池", "A B C D E F G H I J K L M N" in prompt)
            passed &= check("题号为 3", prompt.startswith("3. 【段落信息匹配】"))
            return bool(passed)
    check("找到段落匹配容器", False)
    return False


def scenario_write(U, driver):
    log()
    log("=== 场景4 新格式提取 + 写入落位 ===")
    parser = U.QuestionParser(driver)
    parser.homework_mode = True
    question = None
    for container in parser._find_homework_containers():
        if U.ParagraphMatchingStrategy().can_parse(container, driver):
            question = U.ParagraphMatchingStrategy().parse(container, driver, 3)
            break
    if question is None:
        check("找到段落匹配题", False)
        return False

    answer = ("第1题：1.quantity 2.concern 20.supply\n"
              "第2题：1.diet 2.with 20.balanced\n"
              "第3题：1.B 2.C 3.A 4.D 5.E 6.F 7.G 8.H 9.I 10.J\n"
              "单选题：\n4.B\n5.D\n")
    executor = U.AnswerExecutor(driver)
    written = []

    def fake_write(self, inp, value):
        written.append(value)
        return True

    executor._fill_text_input_verified = types.MethodType(fake_write, executor)
    ok = executor._fill_paragraph_match(question, answer)
    check("写入返回 True", ok)
    check("10 个字母按序落位", written == list("BCADEFGHIJ"), f"got={written}")
    return bool(ok and written == list("BCADEFGHIJ"))


def scenario_two_sections(U, driver):
    log()
    log("=== 场景5 两节段匹配不串字母 ===")
    page = ROOT / "logs" / "two_sections.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(build_two_section_html(), encoding="utf-8")
    driver.get(page.as_uri())
    parser = U.QuestionParser(driver)
    parser.homework_mode = True
    containers = parser._find_homework_containers()
    check("两节各 1 个容器", len(containers) == 2, f"got={len(containers)}")
    if len(containers) != 2:
        return False
    q3 = U.ParagraphMatchingStrategy().parse(containers[0], driver, 3)
    q4 = U.ParagraphMatchingStrategy().parse(containers[1], driver, 4)
    answer = ("第3题：1.B 2.C 3.A 4.D 5.E 6.F 7.G 8.H 9.I 10.J\n"
              "第4题：1.N 2.M 3.L 4.K 5.J 6.I 7.H 8.G 9.F 10.E\n")
    letters3 = U.AnswerExecutor._parse_paragraph_letters(answer, 10, q3.banked_options, 3)
    letters4 = U.AnswerExecutor._parse_paragraph_letters(answer, 10, q4.banked_options, 4)
    passed = check("第3题取到自己的字母", letters3[:3] == ["B", "C", "A"], f"got={letters3[:3]}")
    passed &= check("第4题取到自己的字母", letters4[:3] == ["N", "M", "L"], f"got={letters4[:3]}")
    passed &= check("两节字母不同源", letters3 != letters4)
    return bool(passed)


def scenario_spoilers(U):
    log()
    log("=== 场景6 60% 分值口径 ===")
    config = U.Config.from_json(str(ROOT / "config.json"))
    solver = object.__new__(U.AISolver)
    solver.config = U.Config(
        url=config.url, username=config.username, password=config.password,
        api_key=config.api_key, token_full=config.token_full, base_url=config.base_url,
        model=config.model, temperature=config.temperature, max_tokens=config.max_tokens,
        tts_engine=config.tts_engine, tts_voice=config.tts_voice, tts_proxy=config.tts_proxy,
        screenshot=config.screenshot, browser=config.browser, target_accuracy=60,
        homework_submit=config.homework_submit, allow_force_submit=config.allow_force_submit)
    questions = build_questions(U)
    total = sum(solver._question_weight(q) for q in questions)
    check("合并后总分 ≈ 32", abs(total - 32) < 0.01, f"got={total}")
    plan = solver._plan_spoilers(questions)
    lost = sum(solver._question_weight(q) for q in questions if q.number in plan)
    check("计划丢分 ≈ 12.8", abs(lost - 12.8) <= 1.01, f"got={lost}")
    log(f"  计划做错 {len(plan)} 题，约丢 {lost:g} 分")
    return abs(total - 32) < 0.01


def scenario_force_gate(U, driver):
    log()
    log("=== 场景7 强设未验证不计成功 ===")
    driver.get(SNAPSHOT.as_uri())
    wrappers = driver.find_elements("css selector", ".scoop-select-wrapper")
    if not wrappers:
        check("找到下拉容器", False)
        return False
    question = U.Question(number=1, text="cloze", q_type=U.QuestionType.DROPDOWN_SELECT,
                          element=wrappers[0],
                          banked_blanks=[{"index": 0, "element": wrappers[0]}],
                          banked_options=["control"])
    executor = U.AnswerExecutor(driver)
    executor._force_select_by_js = types.MethodType(lambda self, w, v: "unverified", executor)
    import UnipusAI_Helper as _U
    original_actions = _U.ActionChains

    class _DummyActions:
        def __init__(self, *a, **k):
            pass

        def move_to_element(self, *a, **k):
            return self

        def send_keys(self, *a, **k):
            return self

        def click(self):
            return self

        def perform(self):
            return None

    _U.ActionChains = _DummyActions
    try:
        ok = executor._fill_dropdown_select(question, "1.control")
    finally:
        _U.ActionChains = original_actions
    passed = check("返回 False（不计成功）", ok is False, f"got={ok}")
    passed &= check("进入 suspicious", question in executor.suspicious)
    return bool(passed)


def scenario_course_mode(U):
    log()
    log("=== 场景8 课程模式回归（无「第N题：」）===")
    passed = check("无编号单答案", U.AnswerExecutor._extract_answer_by_number("B", 13) == "B")
    passed &= check("行首编号", U.AnswerExecutor._extract_answer_by_number("1.B\n2.C", 2) == "C")
    passed &= check("缺本题 → 空", U.AnswerExecutor._extract_answer_by_number("1.B\n2.C", 9) == "")
    return bool(passed)


def build_questions(U):
    questions = []
    number = 1
    for _ in range(2):
        blanks = [{"index": i, "element": None} for i in range(20)]
        questions.append(U.Question(number=number, text="cloze",
                                    q_type=U.QuestionType.DROPDOWN_SELECT,
                                    element=None, banked_blanks=blanks,
                                    banked_options=["word"]))
        number += 1
    questions.append(U.Question(number=number, text="paragraph match",
                                q_type=U.QuestionType.PARAGRAPH_MATCH, element=None,
                                inputs=[object() for _ in range(10)],
                                banked_options=list("ABCDEFGHIJKLMN"),
                                banked_blanks=[{"index": i, "statement": f"s{i}"} for i in range(10)]))
    number += 1
    for _ in range(20):
        questions.append(U.Question(number=number, text="choice",
                                    q_type=U.QuestionType.SINGLE_CHOICE, element=None,
                                    options=[U.Option(letter=l, text=l, element=None)
                                             for l in "ABCD"]))
        number += 1
    return questions


def scenario_paragraph_fallback(U, driver):
    log()
    log("=== 场景9 段匹配识别失败要告警并退回填空 ===")
    page = ROOT / "logs" / "no_letters.html"
    page.write_text(build_no_letter_html(), encoding="utf-8")
    driver.get(page.as_uri())
    parser = U.QuestionParser(driver)
    parser.homework_mode = True
    import contextlib
    import io

    section = driver.find_elements("css selector", ".question-abs-question")[0]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        recognized = parser._is_paragraph_match_section(section)
        containers = parser._find_homework_containers()
    output = buffer.getvalue()
    passed = check("识别为 False（不放宽判定）", recognized is False)
    passed &= check("日志出现告警", "段落匹配识别失败" in output,
                    output.strip().splitlines()[0][:60] if output.strip() else "")
    question = parser._parse_single(containers[0], 1) if containers else None
    passed &= check("退回 FILL_IN", question is not None and question.q_type.name == "FILL_IN",
                    question.q_type.name if question else "None")
    return bool(passed)


def scenario_unanswered(U, driver):
    log()
    log("=== 场景10 提交闸门空位计数（不依赖 type 属性）===")
    from unipus_helper.homework_catalog import UNANSWERED_JS, unanswered_count

    driver.get(SNAPSHOT.as_uri())
    parts = driver.execute_script(UNANSWERED_JS) or {}
    check("空位 = 40 下拉 + 10 填空",
          parts == {"dropdowns": 40, "inputs": 10, "total": 50}, f"got={parts}")
    check("unanswered_count == 50", unanswered_count(driver) == 50,
          f"got={unanswered_count(driver)}")

    old_js = """
        const dropdowns = [...document.querySelectorAll('.scoop-select-wrapper')]
            .filter(el => {
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                return !text || text === '点击选择';
            }).length;
        const inputs = [...document.querySelectorAll(
            '.question-abs-question input[type="text"]')]
            .filter(el => !(el.value || '').trim()).length;
        return {dropdowns, inputs, total: dropdowns + inputs};
    """
    old = driver.execute_script(old_js) or {}
    check("旧选择器对填空完全失明（inputs=0）", old.get("inputs") == 0, f"got={old.get('inputs')}")

    driver.execute_script(
        "const i = document.querySelectorAll('.comp-abs-input input')[0];"
        "if (!i) return;"
        "const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(i), 'value');"
        "if (d && d.set) { d.set.call(i, 'x'); } else { i.value = 'x'; }")
    after = driver.execute_script(UNANSWERED_JS) or {}
    check("填一个空后 inputs=9 / total=49",
          after == {"dropdowns": 40, "inputs": 9, "total": 49}, f"got={after}")
    return (parts == {"dropdowns": 40, "inputs": 10, "total": 50}
            and after == {"dropdowns": 40, "inputs": 9, "total": 49})


def build_no_letter_html():
    statements = "".join(
        f'<div class="question-common-abs-reply"><div class="question-common-abs-scoop">'
        f'<div class="item"><div class="component-htmlview">Statement {i} without letters.'
        f'<span class="fe-scoop"><span class="scoop-input-wrapper">'
        f'<span class="question-number">{i}</span>'
        f'<div class="comp-abs-input"><div class="placeholder"></div><input type="text">'
        f'</div></span></span></div></div></div></div>'
        for i in range(1, 3))
    return ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            '<div class="question-abs-question"><div class="layout-container">'
            '<div class="layoutBody-container vertical has-material has-reply">'
            '<div class="layout-material-container"><div class="component-htmlview">'
            'A passage without any paragraph letters.</div></div>'
            f'<div class="layout-reply-container"><div class="reply-wrap">{statements}'
            '</div></div></div></div></div></body></html>')


def build_two_section_html():
    def section(start, letters):
        paragraphs = "".join(
            f"<div>{letter}) paragraph {letter}.</div>" for letter in "ABCDEFGHIJKLMN")
        statements = "".join(
            f'<div class="question-common-abs-reply"><div class="question-common-abs-scoop">'
            f'<div class="item"><div class="component-htmlview">'
            f'Section {letters[0]} statement {i}.'
            f'<span class="fe-scoop"><span class="scoop-input-wrapper">'
            f'<span class="question-number">{i}</span>'
            f'<div class="comp-abs-input"><div class="placeholder"></div><input type="text">'
            f'</div></span></span></div></div></div></div>'
            for i in range(1, 11))
        return (f'<div class="question-abs-question"><div class="layout-container">'
                f'<div class="layoutBody-container vertical has-material has-reply">'
                f'<div class="layout-material-container"><div class="question-common-abs-material">'
                f'<div class="text-material-wrapper"><div class="component-htmlview">{paragraphs}'
                f'</div></div></div></div>'
                f'<div class="layout-reply-container"><div class="reply-wrap">{statements}'
                f'</div></div></div></div></div>')

    return ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + section(3, "ABCDEFGHIJKLMN") + section(4, "NMLKJIHGFEDCBA")
            + "</body></html>")


if __name__ == "__main__":
    sys.exit(main())