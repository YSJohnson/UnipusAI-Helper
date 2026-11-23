"""提交链路回归：按作业幂等 + 提交失败必须向上传播。

不依赖浏览器/账号：把 DOM 交互全部打桩，只验证状态机。
日志：logs/check_submit_flow.log（UTF-8）。
"""

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


class FakeDriver:
    """只用来挂属性；DOM 相关全部打桩。"""


def main():
    ok = True
    try:
        import UnipusAI_Helper as U
        from unipus_helper import homework_catalog as hc

        # ---------- 1. 按作业幂等 ----------
        log("=== 1. 同一作业只提交一次，换作业还能提交 ===")
        driver = FakeDriver()
        clicks = {"n": 0}
        hc.unanswered_detail = lambda d: {"dropdowns": 0, "inputs": 0, "total": 0}
        hc.ensure_alive_window = lambda d: True
        hc.click_by_text = lambda d, text: clicks.__setitem__("n", clicks["n"] + 1) or True
        hc._confirm_submitted = lambda d, w, t: True
        hc.time.sleep = lambda s: None

        first = hc.submit_work(driver, work={"id": "hw-1", "name": "作业A"})
        ok &= check("第一份作业提交成功", first is True)
        clicks_after_first = clicks["n"]
        again = hc.submit_work(driver, work={"id": "hw-1", "name": "作业A"})
        ok &= check("同一作业重复调用被跳过", again is True and clicks["n"] == clicks_after_first,
                    f"clicks={clicks['n']} vs {clicks_after_first}")
        second = hc.submit_work(driver, work={"id": "hw-2", "name": "作业B"})
        ok &= check("第二份作业仍会提交", second is True and clicks["n"] > clicks_after_first,
                    f"clicks={clicks['n']}")

        # ---------- 2. 确认失败时不记幂等、且返回 False ----------
        log()
        log("=== 2. 回读确认失败 → 返回 False 且不标记已提交 ===")
        driver2 = FakeDriver()
        hc._confirm_submitted = lambda d, w, t: False
        before = clicks["n"]
        failed = hc.submit_work(driver2, work={"id": "hw-3", "name": "作业C"})
        ok &= check("确认失败返回 False", failed is False)
        retry = hc.submit_work(driver2, work={"id": "hw-3", "name": "作业C"})
        ok &= check("失败不写幂等，重试会再次点提交",
                    retry is False and clicks["n"] >= before + 2, f"clicks={clicks['n']}")

        # ---------- 3. solve_homework_page 传播失败 ----------
        log()
        log("=== 3. 提交失败时 solve_homework_page 返回 False ===")
        solver = object.__new__(U.AISolver)
        solver.clear_stop = lambda: None
        solver.parser = types.SimpleNamespace(homework_mode=False)
        solver.homework_mode = False
        solver.solve_current_page = lambda name: True
        solver.driver = FakeDriver()
        solver.config = types.SimpleNamespace(allow_force_submit=True)
        original_submit = U.submit_work
        try:
            U.submit_work = lambda *a, **k: False
            result = solver.solve_homework_page("作业D", submit=True, work={"id": "hw-4"})
            ok &= check("提交失败 → 返回 False", result is False, f"got={result}")
            U.submit_work = lambda *a, **k: True
            result2 = solver.solve_homework_page("作业D", submit=True, work={"id": "hw-4"})
            ok &= check("提交成功 → 返回 True", result2 is True, f"got={result2}")
        finally:
            U.submit_work = original_submit

        # ---------- 4. allow_force_submit 缺省值统一为 True ----------
        log()
        log("=== 4. allow_force_submit 缺省值 ===")
        check("Config 默认 True", U.Config.__dataclass_fields__["allow_force_submit"].default is True)
    except Exception:
        import traceback
        log("脚本异常：")
        log(traceback.format_exc())
        ok = False

    ok = ok and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_submit_flow.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())