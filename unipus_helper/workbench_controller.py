"""UI-independent course state and operations for the native workbench."""
from __future__ import annotations

import copy
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from types import SimpleNamespace

from .course_catalog import CourseCatalogMixin
from .task_grading import selectable_task


def log_level(message):
    """Classify the backend's plain-text messages for the native log view."""
    text = message.casefold()
    if any(word in text for word in ("失败", "异常", "错误", "阻断")) or re.search(r"\b(error|exception|traceback|fatal)\b", text):
        return "error"
    if (any(word in text for word in ("警告", "重试", "超时", "跳过", "未找到", "未能", "没有找到", "请手动"))
            or re.search(r"(未|没有).{0,6}(完成|成功|提交|就绪)", text)
            or re.search(r"\b(warning|warn|retry|timeout|skipped)\b", text)):
        return "warning"
    if any(word in text for word in ("成功", "完成", "就绪", "已提交")):
        return "success"
    return "info"


class WorkbenchController(CourseCatalogMixin):
    """Python state and operations shared with the existing automation backend."""
    def __init__(self, driver=None, solver=None, bot=None, app_globals=None):
        self.driver, self.solver, self.bot = driver, solver, bot
        self._globals = app_globals or {}
        self.logger = self._globals.get("logger")
        self.gui_log_queue = self._globals.get("gui_log_queue", queue.Queue())
        for name in ("WebDriverWait", "EC", "By", "NoSuchElementException", "WebDriverHelper"):
            setattr(self, name, self._globals.get(name))
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._stop_requested = threading.Event()
        self._tasks = []
        self._selected = set()
        self._logs = []
        self._log_id = 0
        self._ready = False
        self._operation = None
        self._worker = None
        self._status = "正在登录"
        self._error = ""
        self._exclude_ungraded = False
        self._completed = 0
        self._total = 0
        self._log_worker = threading.Thread(target=self._collect_logs, daemon=True, name="workbench-logs")
        self._log_worker.start()

    @staticmethod
    def after(ms, callback):
        if ms:
            timer = threading.Timer(ms / 1000, callback)
            timer.daemon = True
            timer.start()
        else:
            callback()

    def enable_scan_button(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._ready = True
            self._status = "就绪"
        self._add_log("账号登录成功，等待扫描课程。", "success")

    def _redact(self, message):
        config = getattr(self.bot, "config", None)
        for name in ("password", "api_key", "token_full", "username"):
            secret = str(getattr(config, name, "") or "")
            if secret:
                message = str(message).replace(secret, "••••")
        return str(message)

    def _add_log(self, message, level="info"):
        message = self._redact(message)
        with self._lock:
            self._log_id += 1
            self._logs.append({"id": self._log_id, "time": datetime.now().strftime("%H:%M:%S"),
                               "message": str(message), "level": level})
            self._logs = self._logs[-500:]

    def _collect_logs(self):
        while not self._closed.is_set():
            try:
                message = str(self.gui_log_queue.get(timeout=.5)).strip()
            except queue.Empty:
                continue
            if not message or set(message) <= {"=", "*", "-", " "}:
                continue
            self._add_log(message, log_level(message))

    def snapshot(self):
        with self._lock:
            tasks = [{"id": t["id"], "title": t.get("l1_title") or t.get("display", ""),
                      "unit": t.get("unit_label") or "当前单元", "unitName": t.get("unit_name", ""),
                      "section": t.get("section") or t.get("l2_title") or "课程任务",
                      "required": bool(t.get("is_compulsory")),
                      "scored": t.get("is_scored"), "status": t.get("status", "unknown"),
                      "selected": t["id"] in self._selected} for t in self._tasks]
            config = getattr(self.bot, "config", SimpleNamespace(username="", model=""))
            username = str(getattr(config, "username", ""))
            account = f"{username[:3]}••••{username[-4:]}" if len(username) > 7 else "已连接账号" if username else "未连接账号"
            return copy.deepcopy({
                "version": self._globals.get("APP_VERSION", "3.9.7"),
                "ready": self._ready, "busy": self._operation is not None, "operation": self._operation,
                "status": self._status, "error": self._error,
                "excludeUngraded": self._exclude_ungraded, "debug": bool(self._globals.get("DEBUG_MODE")),
                "tasks": tasks, "logs": self._logs, "account": account,
                "model": getattr(config, "model", "") or "未配置",
                "progress": {"completed": self._completed, "total": self._total},
            })

    def set_selection(self, ids, selected):
        with self._lock:
            if self._operation:
                return {"ok": False, "error": "请先停止当前操作，再调整任务。"}
            valid = {t["id"] for t in self._tasks
                     if not selected or selectable_task(t, self._exclude_ungraded)}
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                return {"ok": False, "error": "任务选择无效。"}
            chosen = set(ids) & valid
            self._selected.update(chosen) if selected else self._selected.difference_update(chosen)
            return {"ok": True}

    def select_tasks(self, mode, ids):
        if mode not in ("all", "required", "pending", "none") or not isinstance(ids, list):
            return {"ok": False, "error": "选择方式无效。"}
        with self._lock:
            if self._operation:
                return {"ok": False, "error": "当前操作进行中。"}
            scope = {item for item in ids if isinstance(item, str)}
            self._selected.difference_update(scope)
            if mode != "none":
                self._selected.update(t["id"] for t in self._tasks if t["id"] in scope
                                      and selectable_task(t, self._exclude_ungraded)
                                      and (mode == "all" or t.get("is_compulsory"))
                                      and (mode != "pending" or t.get("status") != "done"))
            return {"ok": True}

    def set_exclude_ungraded(self, enabled):
        with self._lock:
            if self._operation:
                return {"ok": False, "error": "当前操作进行中。"}
            self._exclude_ungraded = bool(enabled)
            if enabled:
                self._selected.difference_update(t["id"] for t in self._tasks if t.get("is_scored") is False)
        return {"ok": True}

    def start_action(self, action):
        if action not in ("scan", "scan_unit", "run", "quick"):
            return {"ok": False, "error": "无法识别此操作。"}
        with self._lock:
            if self._closed.is_set() or not self._ready or self._operation:
                return {"ok": False, "error": "请等待登录或当前操作完成。"}
            if action == "run" and not self._selected:
                return {"ok": False, "error": "请先选择至少一项任务。"}
            self._operation = action
            self._error = ""
            self._completed = 0
            self._total = len(self._selected) if action == "run" else 0
            self._stop_requested.clear()
            if self.solver:
                self.solver.clear_stop()
            self._status = "正在扫描" if action.startswith("scan") else "正在处理"
            self._worker = threading.Thread(target=self._run_operation, args=(action,),
                                            daemon=True, name="course-operation")
            self._worker.start()
        return {"ok": True}

    def _run_operation(self, action):
        try:
            if action.startswith("scan"):
                tasks = self.scan_tasks(current_unit_only=action == "scan_unit")
                if not tasks:
                    raise RuntimeError("没有找到课程任务。请先在浏览器中打开课程目录，再重新扫描。")
                with self._lock:
                    for index, task in enumerate(tasks):
                        task["id"] = f"task-{index}"
                    self._tasks = tasks
                    self._selected = {t["id"] for t in tasks if t.get("is_compulsory")
                                      and t.get("status") != "done" and selectable_task(t, self._exclude_ungraded)}
                self._add_log(f"扫描完成，共发现 {len(tasks)} 项任务。", "success")
            else:
                if action == "run":
                    with self._lock:
                        selected = [t for t in self._tasks if t["id"] in self._selected]
                    self.solver.process_selected_tabs(selected, on_task_done=self._task_done)
                else:
                    self.solver.processed_hashes.clear()
                    if not self.solver.solve_current_page() and not self.solver._should_stop():
                        raise RuntimeError("当前页面未处理完成，请查看运行日志。")
            with self._lock:
                stopped = self._stop_requested.is_set() or (self.solver is not None and self.solver._should_stop())
                self._status = "已停止" if stopped else "就绪"
        except Exception as exc:
            if self._closed.is_set():
                return
            self._add_log(str(exc), "error")
            with self._lock:
                self._status = "需要检查"
                self._error = self._redact(exc)[:250]
            if self.logger:
                self.logger.error("Workbench operation failed", exc_info=True)
        finally:
            with self._lock:
                self._operation = None

    def _task_done(self, task):
        with self._lock:
            task["status"] = "done"
            self._selected.discard(task["id"])
            self._completed += 1
        self._add_log(f"已完成：{task.get('l1_title', '')}", "success")

    def stop(self):
        self._stop_requested.set()
        if self.solver:
            self.solver.request_stop()
        with self._lock:
            if self._operation:
                self._status = "正在停止"
        return {"ok": True}

    def close(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
        if self.bot and hasattr(self.bot, "request_shutdown"):
            self.bot.request_shutdown()
        self.stop()
        if self._log_worker is not threading.current_thread():
            self._log_worker.join(timeout=.6)
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=.8)
        if self.driver:
            driver, self.driver = self.driver, None
            def quit_driver():
                try:
                    driver.quit()
                except Exception:
                    pass
            worker = threading.Thread(target=quit_driver, daemon=True, name="browser-cleanup")
            worker.start()
            worker.join(timeout=6)
            if worker.is_alive():
                service = getattr(driver, "service", None)
                process = getattr(service, "process", None)
                if process and process.poll() is None:
                    try:
                        if sys.platform == "win32":
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           capture_output=True, timeout=3,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                        else:
                            process.terminate()
                    except (OSError, subprocess.TimeoutExpired):
                        pass
