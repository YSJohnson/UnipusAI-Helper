"""P1-4 回归：提交确认只看目标卡片状态（页签文字不得造成假阳性）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG = []


def log(m=""):
    LOG.append(str(m))


def check(name, cond, detail=""):
    log(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(cond)


class FakeDriver:
    def __init__(self, payload):
        self.payload = payload

    def execute_script(self, script, name):
        return self.payload


def main():
    ok = True
    try:
        from unipus_helper.homework_catalog import _work_in_current_dom

        log("=== 1. 卡片仍在「继续作答」→ 未提交 ===")
        page = {"buttons": ["继续作答"], "text": "作业A 课后作业 发布课程：C 提交截止：11/23"}
        check("返回 False", _work_in_current_dom(FakeDriver(page), "作业A") is False)

        log()
        log("=== 2. 卡片出现「查看报告」→ 已提交 ===")
        page = {"buttons": ["查看报告"], "text": "作业A 课后作业 提交时间：11/23 02:27"}
        check("返回 True", _work_in_current_dom(FakeDriver(page), "作业A") is True)

        log()
        log("=== 3. 找不到卡片 → 无法确认 ===")
        check("返回 False", _work_in_current_dom(FakeDriver(None), "作业A") is False)

        log()
        log("=== 4. 有提交时间但按钮仍是继续作答 → 未提交 ===")
        page = {"buttons": ["继续作答"], "text": "作业A 提交时间：11/23 02:27"}
        check("返回 False（按钮优先）", _work_in_current_dom(FakeDriver(page), "作业A") is False)

        log()
        log("=== 5. 空作业名 → False ===")
        check("空名字返回 False", _work_in_current_dom(FakeDriver(page), "") is False)
    except Exception:
        import traceback
        log("脚本异常：")
        log(traceback.format_exc())
        ok = False

    ok = ok and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_submit_confirm.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())