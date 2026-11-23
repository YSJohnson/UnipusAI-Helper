"""P0-1 回归：作业稳定标识 + 按标识定位（名称不符必须拒绝）。"""

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


class FakeCard:
    def __init__(self, name):
        self.text = name + "\n课后作业"


def main():
    ok = True
    try:
        from unipus_helper import homework_catalog as hc

        log("=== 1. work_id 稳定性 ===")
        same = hc._work_identity("待作答", ["作业A", "课后作业"], "/app/homework/do/123")
        same2 = hc._work_identity("待作答", ["作业A", "课后作业"], "/x/do/123?name=a")
        check("同一作业 id 一致", same == same2 == "待作答:123", f"{same} / {same2}")
        digest = hc._work_identity("已批阅", ["作业A", "发布课程：C", "提交时间：11/22", "提交截止：11/23"])
        check("无链接时用 md5 兜底", digest.startswith("已批阅:") and len(digest) > 8, digest)
        check("不同页签不同 id", digest != hc._work_identity("待作答", ["作业A", "发布课程：C", "提交时间：11/22", "提交截止：11/23"]))

        log()
        log("=== 2. 按标识定位（列表变短）===")
        cards = [FakeCard("作业B"), FakeCard("作业C")]  # 作业A 已提交消失
        target = {"name": "作业C", "work_id": "待作答:3", "position": 1}
        reader = lambda c: {"work_id": "待作答:2" if c.text.startswith("作业B") else "待作答:3",
                            "name": c.text.split("\n")[0]}
        found = hc.locate_card(cards, target, reader)
        check("删掉第一张后仍定位到 C", found is cards[1], found.text.split("\n")[0])

        log()
        log("=== 3. 名称不符必须拒绝 ===")
        bad = {"name": "作业A", "work_id": "", "position": 0}
        try:
            hc.locate_card(cards, bad, reader)
            check("名称不符时抛错", False, "没有抛异常")
        except RuntimeError as exc:
            check("名称不符时抛错", "作业列表已变化" in str(exc), str(exc)[:60])

        log()
        log("=== 4. 找不到 work_id 也拒绝 ===")
        missing = {"name": "作业Z", "work_id": "待作答:99", "position": 0}
        try:
            hc.locate_card(cards, missing, reader)
            check("找不到时抛错", False, "没有抛异常")
        except RuntimeError as exc:
            check("找不到时抛错", "作业列表已变化" in str(exc), str(exc)[:60])
    except Exception:
        import traceback
        log("脚本异常：")
        log(traceback.format_exc())
        ok = False

    ok = ok and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_homework_identity.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())