"""预取/分批/预取失败三路径回归（不需要浏览器、不需要账号）。

复现的回归：预取答案就绪后直接跳过逐题填写 → 必然 0/N 中止。
日志：logs/check_prefetch_fill.log（UTF-8）。
"""

import contextlib
import io
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG = []


def log(message=""):
    LOG.append(str(message))


def check(name, condition, detail=""):
    log(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(condition)


def make_questions(U, count=4):
    return [U.Question(number=i + 1, text=f"Q{i + 1}", q_type=U.QuestionType.TEXT,
                       element=None, inputs=[]) for i in range(count)]


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.suspicious = []

    def execute(self, question, answer):
        self.calls.append(question.number)
        return True

    def _parse_sorting_order(self, *a, **k):
        return []

    def submit(self):
        return False


class FakePrefetch:
    def __init__(self, response="答案", raise_error=False):
        self.elapsed = 1.0
        self.materials = ()
        self.response = response
        self.raise_error = raise_error
        self.waited = 0

    def wait(self, timeout=180):
        self.waited += 1
        if self.raise_error:
            raise RuntimeError("prefetch boom")
        return self.response


def build_solver(U, questions, prefetch=None, batch_size=20, ai_response="答案"):
    """构造一个只跑控制流的假 solver：真实方法 + 打桩 IO。"""
    solver = object.__new__(U.AISolver)
    solver.chapter_name = "test"
    solver.config = types.SimpleNamespace(request_batch_size=batch_size, target_accuracy=100,
                                          allow_force_submit=True)
    solver.homework_mode = False
    solver.self_check_handled = False
    solver.processed_hashes = set()
    solver._publish_processed_groups = set()
    solver.content_handlers = []
    solver._harvest_from_vocabulary_tab = lambda: False
    solver._last_question_types = set()
    solver._retry_hint = ""
    solver.stop_requested = types.SimpleNamespace(is_set=lambda: False, wait=lambda s: False)
    solver.executor = FakeExecutor()
    solver._should_stop = lambda: False
    solver._is_publish_page = lambda: False
    solver._generate_content_hash_from_direction = lambda: "hash"
    solver._skip_transcription = lambda tab: True
    solver._preprocess_video_if_needed = lambda *a, **k: False
    solver._preprocess_audio_if_needed = lambda *a, **k: False
    solver._harvest_vocabulary = lambda: None
    solver._vocab = lambda: []
    solver._needs_vocabulary = lambda qs: False
    solver._generate_questions_signature = lambda qs: "sig"
    solver._question_screenshot = lambda qs: None
    solver._requires_image_context = lambda qs: False
    solver._find_next_question_button = lambda: None
    solver._repair_questions = lambda failed, directions="": 0
    solver._wait_for_submit_complete = lambda: None
    solver._handle_confirm_dialog = lambda: None
    solver._plan_spoilers = types.MethodType(U.AISolver._plan_spoilers, solver)
    solver._fill_from_response = types.MethodType(U.AISolver._fill_from_response, solver)
    solver._apply_target_accuracy = types.MethodType(U.AISolver._apply_target_accuracy, solver)
    solver._extract_single_answer = types.MethodType(U.AISolver._extract_single_answer, solver)
    solver.parser = types.SimpleNamespace(
        homework_mode=False,
        parse_all=lambda: (questions, ""),
    )
    solver.prompt_builder = types.SimpleNamespace(build=lambda chunk, directions: "prompt")
    solver.ai_client = types.SimpleNamespace(
        ask=lambda *a, **k: ai_response,
        material_messages=[],
    )
    return solver


def run_flow(U, prefetch, batch_size=20, ai_response="答案"):
    questions = make_questions(U)
    solver = build_solver(U, questions, prefetch, batch_size, ai_response)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        ok = U.AISolver._process_current_tab_content(
            solver, "chapter", "tab", 0, 0,
            prefetch=prefetch,
            prefetch_signature="sig")
    return ok, solver.executor.calls, buffer.getvalue()


def main():
    ok_all = True
    try:
        import UnipusAI_Helper as U

        # 1. 预取路径：必须真的逐题填写
        log("=== 1. 预取路径 ===")
        ok, calls, out = run_flow(U, FakePrefetch("答案"), batch_size=20)
        ok_all &= check("返回 True", ok is True)
        ok_all &= check("execute 调用 4 次", calls == [1, 2, 3, 4], f"got={calls}")
        ok_all &= check("日志 4/4", "本页成功填写 4/4" in out,
                        [l for l in out.splitlines() if "本页成功填写" in l][-1:])

        # 2. 无预取 + 每批 2 题
        log()
        log("=== 2. 无预取、每批 2 题 ===")
        ok, calls, out = run_flow(U, None, batch_size=2)
        ok_all &= check("返回 True", ok is True)
        ok_all &= check("execute 4 次", calls == [1, 2, 3, 4], f"got={calls}")
        ok_all &= check("出现 2 个批次", out.count("【分批】") == 2, f"got={out.count('【分批】')}")
        ok_all &= check("日志 4/4", "本页成功填写 4/4" in out)

        # 3. 预取返回 None → 回退分批
        log()
        log("=== 3. 预取超时（wait 返回 None）===")
        ok, calls, out = run_flow(U, FakePrefetch(None), batch_size=2)
        ok_all &= check("返回 True", ok is True)
        ok_all &= check("回退分批并填完 4 题", calls == [1, 2, 3, 4], f"got={calls}")
        ok_all &= check("日志提示改为同步请求", "预取未返回" in out)

        # 4. 预取抛异常 → 不崩、回退分批
        log()
        log("=== 4. 预取抛异常 ===")
        try:
            ok, calls, out = run_flow(U, FakePrefetch(raise_error=True), batch_size=2)
            ok_all &= check("未抛出异常且返回 True", ok is True)
            ok_all &= check("回退分批并填完 4 题", calls == [1, 2, 3, 4], f"got={calls}")
        except Exception as exc:
            ok_all &= check(f"未抛出异常（实际 {type(exc).__name__}: {exc}）", False)

        # 5. 正确率口径：每页只规划一次
        log()
        log("=== 5. 每页只调用一次 _plan_spoilers ===")
        questions = make_questions(U)
        solver = build_solver(U, questions, None, batch_size=2)
        counter = {"n": 0}
        original = U.AISolver._plan_spoilers

        def counting(self, qs):
            counter["n"] += 1
            return set()

        solver._plan_spoilers = types.MethodType(counting, solver)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            U.AISolver._process_current_tab_content(
                solver, "chapter", "tab", 0, 0, prefetch=None, prefetch_signature="sig")
        ok_all &= check("整页只调用 1 次", counter["n"] == 1, f"got={counter['n']}")
    except Exception:
        import traceback
        log("脚本异常：")
        log(traceback.format_exc())
        ok_all = False

    ok_all = ok_all and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok_all else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_prefetch_fill.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())