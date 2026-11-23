"""P0-2 / P0-3 回归：正确率单一数据源 + 课程任务状态回写原对象。"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG = []


def log(m=""):
    LOG.append(str(m))


def check(name, cond, detail=""):
    log(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(cond)


class FakeSolver:
    def __init__(self):
        self.target_accuracy = None
        self.seen = []
        self.stopped = False

    def process_selected_tabs(self, selected, on_task_done=None):
        self.seen.append([(t["id"], t.get("accuracy")) for t in selected])
        for task in selected:
            if on_task_done:
                on_task_done(task)
        return True

    def solve_homework_page(self, name, submit=True, work=None):
        self.seen.append(("homework", name, self.target_accuracy))
        return True


def main():
    ok = True
    try:
        from unipus_helper import workbench_controller as wc
        import unipus_helper.homework_catalog as hc

        # P0-2: _accuracy_for 单一数据源
        log("=== 1. _accuracy_for ===")
        c = wc.WorkbenchController()
        c._ready = True
        task = {"id": "hw-待作答:1", "l1_title": "作业A", "work": {"id": "1", "name": "作业A"}}
        check("未设置 → None", c._accuracy_for(task) is None)
        c._accuracy["hw-待作答:1"] = 60
        check("设置 60 → 60", c._accuracy_for(task) == 60, f"got={c._accuracy_for(task)}")

        log()
        log("=== 2. 作业运行按任务 id 取正确率 ===")
        c.solver = FakeSolver()
        c.bot = types.SimpleNamespace(config=types.SimpleNamespace(homework_submit=False))
        c.driver = object()
        wc.open_work = lambda driver, work: True
        wc.ensure_alive_window = lambda driver: True
        c._run_homework([task])
        check("solver.target_accuracy == 60", c.solver.target_accuracy == 60,
              f"got={c.solver.target_accuracy}")
        task2 = {"id": "hw-待作答:2", "l1_title": "作业B", "work": {"id": "2", "name": "作业B"}}
        c._run_homework([task2])
        check("未设置时 target_accuracy 为 None", c.solver.target_accuracy is None,
              f"got={c.solver.target_accuracy}")

        log()
        log("=== 3. 课程批量就地写 accuracy（不造副本）===")
        c2 = wc.WorkbenchController()
        c2._ready = True
        c2.solver = FakeSolver()
        c2._task_mode = "course"
        c2._tasks = [{"id": "task-0", "l1_title": "T0", "status": "todo"},
                     {"id": "task-1", "l1_title": "T1", "status": "todo"}]
        c2._selected = {"task-0", "task-1"}
        c2._accuracy["task-1"] = 70
        c2._run_operation("run")
        check("原任务被标记 done",
              all(t["status"] == "done" for t in c2._tasks),
              [t["status"] for t in c2._tasks])
        check("accuracy 写进原任务", c2._tasks[1].get("accuracy") == 70,
              c2._tasks[1].get("accuracy"))
        check("solver 收到 2 个任务且带 accuracy",
              c2.solver.seen and c2.solver.seen[0][1] == ("task-1", 70),
              c2.solver.seen)
    except Exception:
        import traceback
        log("脚本异常：")
        log(traceback.format_exc())
        ok = False

    ok = ok and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_workbench_state.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())