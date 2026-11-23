"""整页离线干跑：024 真实快照 → 解析 23 题 → 取答案 → 写入打桩 → 提交闸门。

不写页面、不点提交、不联网、不登录。日志：logs/check_homework_dryrun.log（UTF-8）。
"""

import contextlib
import io
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT / "output" / "probe" / "024_222542_goto" / "page_main.html"

FIXTURE = """第1题：1.quantity 2.concern 3.ripped 4.a chain of 5.as well as 6.interrelated 7.in combination 8.in a way 9.switching 10.absurd 11.be extinguished 12.harnessed 13.perpetual 14.narrowing 15.baffled 16.insulating 17.spreads 18.slice 19.ideology 20.supply
第2题：1.diet 2.with 3.wishing 4.get rid of 5.contemplate 6.takes 7.feat 8.than 9.stubborn 10.mixture 11.permanent 12.avoiding 13.unsuitable 14.include 15.in 16.to 17.Stay 18.equipped 19.as well as 20.balanced
第3题：1.B 2.C 3.A 4.D 5.E 6.F 7.G 8.H 9.I 10.J
单选题：
4.B
5.D
6.C
7.B
8.B
9.B
10.C
11.C
12.C
13.B
14.A
15.C
16.C
17.A
18.A
19.D
20.B
21.D
22.C
23.D"""

Q1 = ["quantity", "concern", "ripped", "a chain of", "as well as", "interrelated",
      "in combination", "in a way", "switching", "absurd", "be extinguished",
      "harnessed", "perpetual", "narrowing", "baffled", "insulating", "spreads",
      "slice", "ideology", "supply"]
Q2_FIRST = "diet"
Q3 = list("BCADEFGHIJ")
CHOICES = list("BDCBBBCCCBACCAADBDCD")

LOG = []


def log(message=""):
    LOG.append(str(message))


def check(name, condition, detail=""):
    log(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(condition)


def build_solver(U, driver, target=60):
    base = U.Config.from_json(str(ROOT / "config.json"))
    config = U.Config(
        url=base.url, username=base.username, password=base.password,
        api_key=base.api_key, token_full=base.token_full, base_url=base.base_url,
        model=base.model, temperature=base.temperature, max_tokens=base.max_tokens,
        tts_engine="off", tts_voice="", tts_proxy="",
        screenshot=False, browser=base.browser, target_accuracy=target,
        homework_submit=True, allow_force_submit=base.allow_force_submit)
    return U.AISolver(driver, config)


def stub_solver(U, solver, reply, record):
    """把 AI / 执行 / 提交都换成打桩，保留解析与答案提取的真实逻辑。"""
    solver.ai_client.ask = lambda *a, **k: reply
    solver._wait_for_submit_complete = lambda *a, **k: None
    solver._preprocess_video_if_needed = lambda *a, **k: False
    solver._preprocess_audio_if_needed = lambda *a, **k: False
    solver._repair_questions = lambda questions, directions="": (record.setdefault(
        "repair", []).append([q.number for q in questions]) or 0)

    def execute(question, answer):
        record.setdefault("executed", []).append(
            (question.number, question.q_type.name,
             answer if len(answer) <= 40 else answer[:40] + "…"))
        if question.q_type == U.QuestionType.PARAGRAPH_MATCH:
            written = []
            original = solver.executor._fill_text_input_verified
            solver.executor._fill_text_input_verified = (
                lambda inp, value: (written.append(value), True)[1])
            try:
                ok = solver.executor._fill_paragraph_match(question, answer)
            finally:
                solver.executor._fill_text_input_verified = original
            record.setdefault("paragraph_written", []).append(written)
            return ok
        return True

    solver.executor.execute = execute
    solver.executor.submit = lambda *a, **k: (record.setdefault(
        "executor_submit", []).append(1) or True)

    def fake_submit_work(driver, **kwargs):
        record.setdefault("submit_work", []).append(kwargs.get("work"))
        return True

    return fake_submit_work


def main():
    import UnipusAI_Helper as U
    from unipus_helper import homework_catalog

    ok = True
    driver = U.driver_api and None  # 占位，真正创建在下面
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    if not SNAPSHOT.exists():
        log(f"SKIP: 快照缺失（{SNAPSHOT}）——快照不进仓库，跳过本套件")
        (ROOT / "logs").mkdir(exist_ok=True)
        (ROOT / "logs" / "check_homework_dryrun.log").write_text("SKIP: 快照缺失", encoding="utf-8")
        print("SKIP: 快照缺失")
        return 0
    driver = webdriver.Edge(options=opts)
    original_submit = U.submit_work
    try:
        driver.get(SNAPSHOT.as_uri())

        # ---------- 正常路径 ----------
        record = {}
        solver = build_solver(U, driver)
        U.submit_work = stub_solver(U, solver, FIXTURE, record)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            solved = solver.solve_homework_page("dryrun", submit=True)
        output = buffer.getvalue()

        log("=== 场景1 解析题数与题型 ===")
        distribution = {}
        for number, qtype, _ in record.get("executed", []):
            distribution[qtype] = distribution.get(qtype, 0) + 1
        check("解析到 23 题", len(record.get("executed", [])) == 23,
              f"got={len(record.get('executed', []))}")
        check("题型分布 2 下拉 + 1 段匹配 + 20 单选",
              distribution == {"DROPDOWN_SELECT": 2, "PARAGRAPH_MATCH": 1,
                               "SINGLE_CHOICE": 20}, f"got={distribution}")

        log()
        log("=== 场景2 题号连续 1..23 ===")
        numbers = [item[0] for item in record.get("executed", [])]
        check("题号 == 1..23", numbers == list(range(1, 24)), f"got={numbers}")

        log()
        log("=== 场景3 第1/2题答案 ===")
        answers = {}
        for number, qtype, answer in record.get("executed", []):
            answers.setdefault(number, answer)
        from UnipusAI_Helper import AnswerExecutor as AE
        q1 = AE._parse_banked_answer(FIXTURE, 20, 1)
        q2 = AE._parse_banked_answer(FIXTURE, 20, 2)
        check("第1题 20 个答案", len(q1) == 20, f"got={len(q1)}")
        check("第1题第20空 == supply", q1[19] == "supply", f"got={q1[19]}")
        check("第1题整列正确", q1 == Q1)
        check("第2题第1空 == diet", q2[0] == Q2_FIRST, f"got={q2[0]}")

        log()
        log("=== 场景4/5 段匹配与单选字母 ===")
        written = (record.get("paragraph_written") or [[]])[0]
        check("第3题字母 B C A D E F G H I J", written == Q3, f"got={written}")
        letters = [AE._extract_answer_by_number(FIXTURE, number)
                   for number in range(4, 24)]
        check("第4..23题字母逐一对应", letters == CHOICES, f"got={''.join(letters)}")

        log()
        log("=== 场景6 正确率口径 ===")
        check("日志含「平台近似总分 / 可改分值 / 需丢 X 分」",
              "平台近似总分" in output and "可改分值" in output and "需丢" in output)
        questions = build_questions(U)
        total = sum(solver._question_weight(q) for q in questions)
        check("总分 ≈ 32", abs(total - 32) < 0.01, f"got={total}")

        log()
        log("=== 场景7 提交只调一次 ===")
        check("solve_homework_page 返回 True", solved is True)
        check("submit_work 调用 1 次", len(record.get("submit_work", [])) == 1,
              f"got={len(record.get('submit_work', []))}")
        check("页面内 executor.submit 0 次",
              len(record.get("executor_submit", [])) == 0,
              f"got={len(record.get('executor_submit', []))}")

        # ---------- 失败路径 ----------
        log()
        log("=== 场景8 段匹配非法字母 → 修正失败 → 不提交 ===")
        bad_record = {}
        bad_solver = build_solver(U, driver)
        bad_reply = FIXTURE.replace("第3题：1.B 2.C 3.A 4.D 5.E 6.F 7.G 8.H 9.I 10.J",
                                    "第3题：1.Z")
        U.submit_work = stub_solver(U, bad_solver, bad_reply, bad_record)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            bad_solved = bad_solver.solve_homework_page("dryrun-bad", submit=True)
        check("整页返回 False", bad_solved is False)
        check("二次修正被调用", bool(bad_record.get("repair")), f"got={bad_record.get('repair')}")
        check("失败路径 submit_work 0 次",
              len(bad_record.get("submit_work", [])) == 0,
              f"got={len(bad_record.get('submit_work', []))}")

        # ---------- 闸门联动 ----------
        log()
        log("=== 场景9 空位闸门 ===")
        U.submit_work = original_submit
        driver.get(SNAPSHOT.as_uri())
        left = homework_catalog.unanswered_count(driver)
        check("unanswered_count >= 10", left >= 10, f"got={left}")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            submitted = homework_catalog.submit_work(driver)
        gate_log = buffer.getvalue()
        check("空卷被拒绝提交", submitted is False, f"got={submitted}")
        check("打印「还有 N 个空没填」", "个空没填" in gate_log, gate_log.strip()[:60])
    except Exception:
        log("脚本异常：")
        log(traceback.format_exc())
        ok = False
    finally:
        U.submit_work = original_submit
        driver.quit()

    ok = ok and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_homework_dryrun.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok else 1


def build_questions(U):
    questions = []
    number = 1
    for _ in range(2):
        questions.append(U.Question(
            number=number, text="cloze", q_type=U.QuestionType.DROPDOWN_SELECT, element=None,
            banked_blanks=[{"index": i, "element": None} for i in range(20)],
            banked_options=["word"]))
        number += 1
    questions.append(U.Question(
        number=number, text="paragraph match", q_type=U.QuestionType.PARAGRAPH_MATCH,
        element=None, inputs=[object() for _ in range(10)],
        banked_options=list("ABCDEFGHIJKLMN"),
        banked_blanks=[{"index": i, "statement": f"s{i}"} for i in range(10)]))
    number += 1
    for _ in range(20):
        questions.append(U.Question(
            number=number, text="choice", q_type=U.QuestionType.SINGLE_CHOICE, element=None,
            options=[U.Option(letter=l, text=l, element=None) for l in "ABCD"]))
        number += 1
    return questions


if __name__ == "__main__":
    sys.exit(main())