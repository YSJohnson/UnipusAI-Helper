"""离线验收：云作业答案提取（T1/T2）与段落匹配解析（T3）。

只依赖标准库；T3 需要本机 Edge + msedgedriver（不登录、不联网）。
结果同时写入 logs/check_answer_extraction.log（UTF-8），Windows 控制台中文会乱码。
"""

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = """第1题：1.quantity 2.concern 3.ripped 4.a chain of 5.as well as 6.interrelated 7.in combination 8.in a way 9.switching 10.absurd 11.be extinguished 12.harnessed 13.perpetual 14.narrowing 15.baffled 16.insulating 17.spreads 18.slice 19.ideology 20.supply

第2题：1.diet 2.with 3.wishing 4.get rid of 5.contemplate 6.takes 7.feat 8.than 9.stubborn 10.mixture 11.permanent 12.avoiding 13.unsuitable 14.include 15.in 16.to 17.Stay 18.equipped 19.as well as 20.balanced

填空题：
3. need-based aid
4. FAFSA
5. Direct Loan
6. application deadline
7. unsubsidized Stafford student loans
8. campus jobs
9. repayment plans
10. co-op programs
11. Stafford and Perkins loans
12. merit scholarships

单选题：
13.B
14.D
15.C
16.B
17.B
18.B
19.C
20.C
21.C
22.B
23.A
24.C
25.C
26.A
27.A
28.D
29.B
30.D
31.C
32.D"""

Q1 = ["quantity", "concern", "ripped", "a chain of", "as well as", "interrelated",
      "in combination", "in a way", "switching", "absurd", "be extinguished",
      "harnessed", "perpetual", "narrowing", "baffled", "insulating", "spreads",
      "slice", "ideology", "supply"]
Q2 = ["diet", "with", "wishing", "get rid of", "contemplate", "takes", "feat", "than",
      "stubborn", "mixture", "permanent", "avoiding", "unsuitable", "include", "in",
      "to", "Stay", "equipped", "as well as", "balanced"]
FILLS = ["need-based aid", "FAFSA", "Direct Loan", "application deadline",
         "unsubsidized Stafford student loans", "campus jobs", "repayment plans",
         "co-op programs", "Stafford and Perkins loans", "merit scholarships"]
CHOICES = list("BDCBBBCCCBACCAADBDCD")  # 13..32

FIXTURE2 = """填空题1：1.quantity 2.concern 3.ripped 4.a chain of 5.as well as 6.interrelated 7.in combination 8.in a way 9.switching 10.absurd 11.be extinguished 12.harnessed 13.perpetual 14.narrowing 15.preached 16.insulating 17.spreads 18.slice 19.ideology 20.supply
填空题2：1.diet 2.with 3.wishing 4.get rid of 5.contemplate 6.takes 7.feat 8.than 9.stubborn 10.mixture 11.permanent 12.avoiding 13.unsuitable 14.include 15.in 16.to 17.Stay 18.equipped 19.as well as 20.balanced
单选题：4.D 5.D 6.C 7.B 8.D 9.B 10.C 11.C 12.C 13.B 14.A 15.C 16.C 17.A 18.C 19.D 20.B 21.D 22.C 23.B"""


LINES = []


def log(message=""):
    LINES.append(str(message))


def check(name, condition, detail=""):
    log(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(condition)


def main():
    import UnipusAI_Helper as U

    ok = True
    log("=== T1/T2 答案按题块提取 ===")
    q1 = U.AnswerExecutor._parse_banked_answer(FIXTURE, 20, 1)
    q2 = U.AnswerExecutor._parse_banked_answer(FIXTURE, 20, 2)
    ok &= check("Q1 20 空", q1 == Q1, f"got={q1}")
    ok &= check("Q2 20 空", q2 == Q2, f"got={q2}")
    ok &= check("Q1 第20空 == supply", q1[19:20] == ["supply"], f"got={q1[19:20]}")

    fills = [U.AnswerExecutor._extract_answer_by_number(FIXTURE, number)
             for number in range(3, 13)]
    ok &= check("Q3-Q12 填空题", fills == FILLS, f"got={fills}")

    choices = [U.AnswerExecutor._extract_answer_by_number(FIXTURE, number)
               for number in range(13, 33)]
    ok &= check("Q13-Q32 单选", choices == CHOICES, f"got={''.join(choices)}")

    log()
    log("=== 另一家模型格式：填空题1：/单选题：连写 ===")
    q1b = U.AnswerExecutor._parse_banked_answer(FIXTURE2, 20, 1)
    q2b = U.AnswerExecutor._parse_banked_answer(FIXTURE2, 20, 2)
    ok &= check("填空题1 首尾", q1b[:1] + q1b[-1:] == ["quantity", "supply"], f"got={q1b[:1] + q1b[-1:]}")
    ok &= check("填空题2 首尾", q2b[:1] + q2b[-1:] == ["diet", "balanced"], f"got={q2b[:1] + q2b[-1:]}")
    inline = [U.AnswerExecutor._extract_answer_by_number(FIXTURE2, n) for n in range(4, 24)]
    ok &= check("单选题连写解析", "".join(inline) == "DDCBDBCCCBACCACDBDCB", f"got={''.join(inline)}")

    log()
    log("=== 坏格式必须判失败 ===")
    multiline = "第1题：1.quantity part\nmore text 2.a chain of 3.ripped"
    ok &= check("块内换行 → 空", U.AnswerExecutor._parse_banked_answer(multiline, 3, 1) == ["", "", ""])
    too_long = "第1题：1." + "x" * 60 + " 2.ok 3.ok"
    ok &= check("超 40 字符 → 空", U.AnswerExecutor._parse_banked_answer(too_long, 3, 1) == ["", "", ""])

    log()
    log("=== 6.4 课程模式回归（单题、无题块）===")
    ok &= check("无编号单答案保留", U.AnswerExecutor._extract_answer_by_number("B", 13) == "B")
    ok &= check("行首编号仍可解析",
                U.AnswerExecutor._extract_answer_by_number("1.B\n2.C", 2) == "C")
    ok &= check("有编号但缺本题 → 空",
                U.AnswerExecutor._extract_answer_by_number("1.B\n2.C", 9) == "")

    log()
    log("=== T3 段落信息匹配（静态 DOM + headless Edge）===")
    target = T3Condition()
    try:
        ok &= target.run()
    except Exception:
        log("T3 异常：")
        log(traceback.format_exc())
        ok = False

    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_answer_extraction.log").write_text("\n".join(LINES), encoding="utf-8")
    print("\n".join(LINES[-6:]))
    return 0 if ok else 1


def T3Condition():
    class _T3:
        def run(self):
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options

            html = build_t3_html()
            page = ROOT / "logs" / "t3_fixture.html"
            page.parent.mkdir(exist_ok=True)
            page.write_text(html, encoding="utf-8")

            opts = Options()
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1400,900")
            driver = webdriver.Edge(options=opts)
            try:
                driver.get(page.as_uri())
                import UnipusAI_Helper as U

                parser = U.QuestionParser(driver)
                parser.homework_mode = True
                containers = parser._find_homework_containers()
                passed = check("整节只收成 1 个容器", len(containers) == 1, f"got={len(containers)}")
                if not containers:
                    return False
                question = U.ParagraphMatchingStrategy().parse(containers[0], driver, 3)
                passed &= check("解析为 PARAGRAPH_MATCH",
                                question is not None and question.q_type == U.QuestionType.PARAGRAPH_MATCH)
                if question is None:
                    return False
                passed &= check("10 个空", len(question.inputs) == 10, f"got={len(question.inputs)}")
                passed &= check("字母池 A–N", question.banked_options == list("ABCDEFGHIJKLMN"),
                                f"got={question.banked_options}")
                builder = object.__new__(U.PromptBuilder)
                prompt = "\n".join(builder._build_paragraph_match(question))
                passed &= check("prompt 含「只返回 空号.字母」", "只返回 空号.字母" in prompt)
                letters = U.AnswerExecutor._parse_paragraph_letters("1.B 2.C 3.A", 10, question.banked_options)
                passed &= check("写入只取单个字母", letters[:3] == ["B", "C", "A"]
                                and all(len(x) <= 1 for x in letters), f"got={letters}")
                return bool(passed)
            finally:
                driver.quit()

    return _T3()


def build_t3_html():
    paragraphs = "".join(
        f"<div>{letter}) paragraph {letter} about financial aid tools.</div>"
        for letter in "ABCDEFGHIJKLMN")
    statements = "".join(
        f'<div class="question-common-abs-reply"><div class="question-common-abs-scoop '
        f'comp-scoop-reply fill-blank-reply"><div class="item"><div class="component-htmlview">'
        f'Statement number {i} about aid.'
        f'<span class="fe-scoop" data-scoop-index="0"><span class="scoop-input-wrapper">'
        f'<span class="question-number">{i}</span>'
        f'<div class="comp-abs-input input-user-answer"><div class="placeholder"></div>'
        f'<input type="text"></div></span></span></div></div></div></div>'
        for i in range(1, 11))
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="question-abs-question">
  <div class="layout-container"><div class="layoutBody-container vertical has-material has-reply">
    <div class="layout-material-container"><div class="question-common-abs-material">
      <div class="text-material-wrapper"><div class="component-htmlview">{paragraphs}</div></div>
    </div></div>
    <div class="layout-reply-container"><div class="reply-wrap">{statements}</div></div>
  </div></div>
</div></body></html>"""


if __name__ == "__main__":
    sys.exit(main())