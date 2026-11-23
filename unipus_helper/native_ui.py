"""Qt Quick workbench. No web server or browser engine is used by this UI."""
from __future__ import annotations

import logging
import json
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, Property,
                            QTimer, Qt, QUrl, Signal, Slot, QtMsgType, qInstallMessageHandler)
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from shiboken6 import delete as delete_qt_object

from .paths import PROJECT_ROOT, QML_DIR
from .task_grading import score_text
from .workbench_controller import WorkbenchController


DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "url": "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome",
    "token_full": "",
    "api_key": "",
    "base_url": "",
    "model": "",
    "max_tokens": 32768,
    "temperature": 0.3,
    "debug_mode": False,
    # 录音题（Exercise 2 这类主观题）没有原声，需要 TTS 生成语音
    "tts_engine": "auto",
    "tts_voice": "",
    "tts_proxy": "",
    "browser": "edge",
    # 目标正确率：100 = 全部按 AI 答案作答；低于 100 会故意做错一部分，
    # 让整份得分落在该值附近（误差约 ±10 分）
    "target_accuracy": 85,
    # 云作业填完后自动提交；关掉则停在提交前等待人工确认
    "homework_submit": True,
    # 有空位仍强制提交（默认关闭，避免把没填完的作业交上去）
    "homework": {"allow_force_submit": True},
    # 每批发给模型的题数；页面上题多时会分批请求
    "request_batch_size": 20,
    # 题目截图一并发给支持识图的模型（qwen-vl、gpt-4o 等），默认关闭
    "screenshot": False,
}


TRUE_TOKENS = {"true", "1", "yes", "on", "是", "开"}

TEXT_FIELDS = ("username", "password", "url", "api_key", "base_url", "model",
               "tts_engine", "tts_voice", "tts_proxy", "browser", "token_full")
BOOL_FIELDS = ("debug_mode", "screenshot", "homework_submit")


def _as_bool(value, default=False):
    """宽松布尔："False"/"false"/"0"/"no"/"" 都是 False，别让 bool("False") 变 True。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    return text in TRUE_TOKENS


def _as_int(value, default, low, high):
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_float(value, default, low, high):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def get_field(data: dict, key: str):
    """支持 "homework.allow_force_submit" 这种点号路径。"""
    current = data
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def set_field(data: dict, key: str, value):
    parts = key.split(".")
    current = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def normalize_config(data: dict) -> dict:
    """保存前统一类型：字符串字段 strip，数值/布尔归一，homework 保持 dict。"""
    out = dict(data or {})
    for key in TEXT_FIELDS:
        if key in out:
            out[key] = "" if out[key] is None else str(out[key]).strip()
    out["max_tokens"] = _as_int(out.get("max_tokens"), 32768, 100, 200000)
    out["target_accuracy"] = _as_int(out.get("target_accuracy"), 85, 0, 100)
    out["request_batch_size"] = _as_int(out.get("request_batch_size"), 20, 1, 50)
    out["temperature"] = _as_float(out.get("temperature"), 0.3, 0, 2)
    for key in BOOL_FIELDS:
        out[key] = _as_bool(out.get(key), DEFAULT_CONFIG.get(key, False))
    # 防御：任何含点号的顶层键都归并进嵌套结构，避免 "homework.allow_force_submit" 被写平
    for key in [k for k in list(out) if isinstance(k, str) and "." in k]:
        value = out.pop(key)
        nested = {}
        set_field(nested, key, value)
        for head, child in nested.items():
            if isinstance(child, dict) and isinstance(out.get(head), dict):
                out[head] = {**out[head], **child}
            else:
                out[head] = child
    homework = out.get("homework")
    if not isinstance(homework, dict):
        homework = {}
    homework = dict(homework)
    homework["allow_force_submit"] = _as_bool(homework.get("allow_force_submit"), True)
    out["homework"] = homework
    return out


def validate_field(key: str, value):
    """单字段校验：返回 (归一后的值, 错误信息)。错误信息为 None 表示通过。"""
    if key == "target_accuracy":
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None, "目标正确率需为 0–100 的整数"
        if not 0 <= number <= 100:
            return None, "目标正确率需为 0–100 的整数"
        return number, None
    if key == "request_batch_size":
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None, "每批题数需为 1–50 的整数"
        if not 1 <= number <= 50:
            return None, "每批题数需为 1–50 的整数"
        return number, None
    if key == "max_tokens":
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None, "最大 Token 数需为 100–200000 的整数"
        if not 100 <= number <= 200000:
            return None, "最大 Token 数需为 100–200000 的整数"
        return number, None
    if key == "temperature":
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None, "温度需为 0–2 之间的数字"
        if not 0 <= number <= 2:
            return None, "温度需为 0–2 之间的数字"
        return number, None
    if key.endswith("allow_force_submit") or key in ("debug_mode", "screenshot", "homework_submit"):
        return _as_bool(value), None
    return ("" if value is None else str(value)).strip(), None


def load_config(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return normalize_config(merged)
    return normalize_config(dict(DEFAULT_CONFIG))


def save_config(path, data: dict):
    out = normalize_config(data)
    token = out.get("token_full", "")
    if token and isinstance(token, str):
        if (token.startswith("'") and token.endswith("'")) or (
            token.startswith('"') and token.endswith('"')
        ):
            out["token_full"] = token[1:-1].strip()

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return True


REQUIRED_FIELDS = (("username", "账号"), ("password", "密码"), ("api_key", "API Key"),
                   ("base_url", "API 地址"), ("model", "模型名称"))


class ConfigEditor(QObject):
    saved = Signal(str, bool)
    changed = Signal()

    def __init__(self, path, first_run=False):
        super().__init__()
        self.path = path
        self.first_run = first_run
        self.completed = False
        self.cfg = load_config(path)

    @Property("QVariantMap", notify=changed)
    def values(self):
        return self.cfg

    @Property("QVariantMap", constant=True)
    def defaults(self):
        import copy
        return copy.deepcopy(DEFAULT_CONFIG)

    @Property(bool, constant=True)
    def firstRun(self):
        return self.first_run

    def _persist(self, data: dict):
        save_config(self.path, data)
        self.cfg = normalize_config(data)
        self.changed.emit()

    @Slot(str, str, result=bool)
    def saveOne(self, key, value):
        """行内单字段保存：校验失败不写盘，直接 emit 错误提示。"""
        normalized, error = validate_field(key, value)
        if error:
            self.saved.emit(error, False)
            return False
        data = dict(self.cfg)
        if key.endswith("allow_force_submit"):
            homework = data.get("homework")
            if not isinstance(homework, dict):
                homework = {}
            homework = dict(homework)
            homework["allow_force_submit"] = normalized
            data["homework"] = homework
        else:
            data[key] = normalized
        try:
            self._persist(data)
        except Exception as exc:
            self.saved.emit(f"保存失败：{exc}", False)
            return False
        self.completed = True
        self.saved.emit("已保存", True)
        return True

    @Slot("QVariantMap")
    def save(self, values):
        data = dict(self.cfg)
        for key, value in (values or {}).items():
            if isinstance(key, str) and "." in key:
                set_field(data, key, value)
            else:
                data[key] = value
        data = normalize_config(data)
        missing = [label for key, label in REQUIRED_FIELDS if not data.get(key)]
        if missing:
            self.saved.emit("请填写必填项：" + "、".join(missing), False)
            return
        try:
            self._persist(data)
        except Exception as exc:
            self.saved.emit(f"保存失败：{exc}", False)
            return
        self.completed = True
        self.saved.emit("配置已保存" if self.first_run else "配置已保存，重新启动后生效", True)


def setup_first_run():
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    configure_ui_font(app)
    editor = ConfigEditor(PROJECT_ROOT / "config.json", first_run=True)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("configEditor", editor)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "ConfigEditor.qml")))
    if not engine.rootObjects():
        raise RuntimeError("配置界面加载失败")
    app.exec()
    delete_qt_object(engine)
    return app, editor.completed


def configure_ui_font(app):
    families = set(QFontDatabase.families())
    # Prefer the platform's UI face rather than a document font with light
    # named subfamilies. Keep explicit fallbacks for macOS and Linux.
    preferred = (("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Source Han Sans SC")
                 if sys.platform == "win32" else
                 ("PingFang SC", "Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei UI"))
    family = next((name for name in preferred if name in families), app.font().family())
    font = QFont(family, 10, QFont.Weight.Medium)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)
    return family


class TaskListModel(QAbstractListModel):
    """Keep delegates and scroll position when only selection/status changes."""
    fields = ("taskId", "title", "isRequired", "status", "chosen", "scored", "statusName",
              "sectionHeader", "selectable", "scoreText", "scoreAbnormal", "accuracy")
    roles = {Qt.UserRole + i + 1: name.encode() for i, name in enumerate(fields)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def roleNames(self):
        return self.roles

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        name = self.roles.get(role)
        return self.rows[index.row()].get(name.decode()) if name else None

    def update(self, rows):
        # Notify insertions/removals explicitly for empty search transitions.
        if self.rows and not rows:
            self.beginRemoveRows(QModelIndex(), 0, len(self.rows) - 1)
            self.rows = []
            self.endRemoveRows()
            return
        if rows and not self.rows:
            self.beginInsertRows(QModelIndex(), 0, len(rows) - 1)
            self.rows = rows
            self.endInsertRows()
            return
        if [r["taskId"] for r in rows] != [r["taskId"] for r in self.rows]:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()
            return
        for i, row in enumerate(rows):
            previous = self.rows[i]
            roles = [role for role, name in self.roles.items()
                     if previous.get(name.decode()) != row.get(name.decode())]
            if roles:
                self.rows[i] = row
                self.dataChanged.emit(self.index(i), self.index(i), roles)


def task_section_label(section, title):
    """Keep task names in rows rather than repeating them in section headings."""
    parts = []
    for part in section.split("/"):
        part = part.strip()
        if part and (not parts or part.casefold() != parts[-1].casefold()):
            parts.append(part)
    if parts and parts[-1].casefold() == title.strip().casefold():
        parts.pop()
    return " / ".join(parts)


class NativeBridge(QObject):
    changed = Signal()
    unitsChanged = Signal()
    noticeChanged = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._state = {}
        self._units = []
        self._summary = {}
        self._unit = "all"
        self._filter = "all"
        self._query = ""
        self._notice = ""
        self.task_model = TaskListModel(self)
        self.refresh()
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @Property("QVariantMap", notify=changed)
    def state(self):
        return self._state

    @Property("QVariantList", notify=unitsChanged)
    def units(self):
        return self._units

    @Property("QVariantMap", notify=changed)
    def summary(self):
        return self._summary

    @Property(str, notify=changed)
    def activeUnit(self):
        return self._unit

    @Property(str, notify=changed)
    def activeFilter(self):
        return self._filter

    @Property(str, notify=noticeChanged)
    def notice(self):
        return self._notice

    def _result(self, result):
        if not result.get("ok"):
            self._notice = result.get("error", "操作未完成，请重试")
            self.noticeChanged.emit()
        self.refresh(force=True)

    @Slot()
    def dismissNotice(self):
        self._notice = ""
        self.noticeChanged.emit()

    @Slot()
    def resetFilters(self):
        self._filter = "all"
        self._query = ""
        self.refresh(force=True)

    @Slot(str)
    def chooseUnit(self, unit):
        self._unit = unit
        self.refresh(force=True)

    @Slot(str)
    def chooseFilter(self, value):
        self._filter = value
        self.refresh(force=True)

    @Slot(str)
    def search(self, query):
        self._query = query.casefold().strip()
        self.refresh(force=True)

    @Slot(str, bool)
    def selectTask(self, task_id, chosen):
        self._result(self.controller.set_selection([task_id], chosen))

    @Slot(str)
    def selectVisible(self, mode):
        self._result(self.controller.select_tasks(mode, [r["taskId"] for r in self.task_model.rows]))

    @Slot()
    def toggleVisible(self):
        rows = [r for r in self.task_model.rows if r["selectable"]]
        self._result(self.controller.set_selection(
            [r["taskId"] for r in rows], not (rows and all(r["chosen"] for r in rows))))

    @Slot()
    def clearSelection(self):
        self._result(self.controller.select_tasks("none", [t["id"] for t in self._state["tasks"]]))

    @Slot(bool)
    def excludeUngraded(self, value):
        self._result(self.controller.set_exclude_ungraded(value))

    @Slot(str, result=bool)
    def start(self, action):
        result = self.controller.start_action(action)
        self._result(result)
        return bool(result.get("ok"))

    @Slot(int, result=bool)
    def setAccuracyForSelection(self, value):
        ids = [t["id"] for t in self._state.get("tasks", []) if t.get("selected")]
        result = self.controller.set_accuracy(ids, value) if ids else {
            "ok": False, "error": "请先勾选任务，再设置正确率。"}
        self._result(result)
        if result.get("ok"):
            self._notice = "已为 %d 个任务设置正确率" % len(ids)
            self.noticeChanged.emit()
        return bool(result.get("ok"))

    @Slot(str, int, result=bool)
    def setTaskAccuracy(self, task_id, value):
        result = self.controller.set_accuracy([task_id], value)
        self._result(result)
        return bool(result.get("ok"))

    @Slot()
    def stop(self):
        self._result(self.controller.stop())

    @Slot()
    def restartBrowser(self):
        self._result(self.controller.restart_browser())

    @Slot()
    def copyLogs(self):
        QGuiApplication.clipboard().setText("\n".join(
            f'{row["time"]}  {row["message"]}' for row in self._state["logs"]))
        self._notice = "运行日志已复制"
        self.noticeChanged.emit()

    @Slot()
    def refresh(self, force=False):
        state = self.controller.snapshot()
        if not force and state == self._state:
            return
        tasks = state["tasks"]
        units = []
        for unit in dict.fromkeys(t["unit"] for t in tasks):
            group = [t for t in tasks if t["unit"] == unit]
            units.append({"name": unit, "title": group[0]["unitName"], "total": len(group),
                          "done": sum(t["status"] == "done" for t in group)})
        if units != self._units:
            self._units = units
            self.unitsChanged.emit()
        if self._unit != "all" and not any(u["name"] == self._unit for u in self._units):
            self._unit = "all"
        scoped = [t for t in tasks if self._unit == "all" or t["unit"] == self._unit]
        counts = {"all": len(scoped), "pending": sum(t["status"] != "done" for t in scoped),
                  "done": sum(t["status"] == "done" for t in scoped),
                  "required": sum(t["required"] for t in scoped)}
        visible = [t for t in scoped if (
            self._query in (f'{t["title"]} {t["section"]} {t["unit"]} {score_text(t.get("score"))} ' + (
                "不计分" if t["scored"] is False else "计分未知" if t["scored"] is None else "计分")).casefold()) and (
            self._filter == "all" or self._filter == "pending" and t["status"] != "done"
            or self._filter == "done" and t["status"] == "done"
            or self._filter == "required" and t["required"])]
        statuses = {"done": "已完成", "doing": "进行中", "todo": "未开始", "unknown": "待确认"}
        rows = [dict(t, taskId=t["id"], chosen=t["selected"], isRequired=t["required"],
                     accuracy=t.get("accuracy"),
                     statusName=statuses.get(t["status"], "待确认"),
                     scoreText=f'{score_text(t.get("score"))} 分' if t.get("score") is not None else "",
                     scoreAbnormal=bool(t.get("score_abnormal")),
                     selectable=not (state["excludeUngraded"] and t["scored"] is False))
                for t in visible]
        previous_group = None
        for row in rows:
            section = task_section_label(row["section"], row["title"])
            group = (row["unit"], section)
            label = " / ".join(part for part in (row["unit"], section) if part) if self._unit == "all" else section
            row["sectionHeader"] = label if group != previous_group else ""
            previous_group = group
        self.task_model.update(rows)
        selected = [t for t in tasks if t["selected"]]
        selectable = [t for t in visible if not (state["excludeUngraded"] and t["scored"] is False)]
        selected_visible = sum(t["selected"] for t in visible)
        self._summary = {
            "counts": counts, "visible": len(visible), "selected": len(selected),
            "selectedUnits": len(set(t["unit"] for t in selected)),
            "selectableVisible": len(selectable),
            "selectedVisible": selected_visible, "selectedHidden": len(selected) - selected_visible,
            "allChecked": bool(selectable) and all(t["selected"] for t in selectable),
            "someChecked": any(t["selected"] for t in selectable),
            "total": len(tasks),
            "unitTitle": scoped[0]["unitName"] if self._unit != "all" and scoped else "",
        }
        self._state = state
        self.changed.emit()


class NativeModernGUI(WorkbenchController):
    def mainloop(self, *, screenshot=None, close_after=None):
        QQuickStyle.setStyle("Basic")
        app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
        app.setApplicationName("UnipusAI-Helper")
        app.setOrganizationName("UnipusAI-Helper")
        app.setWindowIcon(QIcon(str(QML_DIR / "icons" / "app.svg")))
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
        configure_ui_font(app)
        log_path = PROJECT_ROOT / "logs" / "native-ui.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        qt_logger = logging.getLogger("UnipusAI.Native")
        qt_logger.setLevel(logging.WARNING)
        qt_logger.propagate = False
        log_file = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        log_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        qt_logger.addHandler(log_file)

        def qt_message(kind, context, message):
            if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                qt_logger.warning("%s:%s %s", context.file or "Qt", context.line, message)
                print(f"Qt: {message}", file=sys.stderr)

        previous_handler = qInstallMessageHandler(qt_message)
        engine = QQmlApplicationEngine()
        bridge = NativeBridge(self)
        config_editor = ConfigEditor(PROJECT_ROOT / "config.json")
        engine.rootContext().setContextProperty("configEditor", config_editor)
        engine.rootContext().setContextProperty("workbench", bridge)
        engine.rootContext().setContextProperty("taskModel", bridge.task_model)
        # Python signal handlers run whenever Qt returns to this timer's callback.
        pulse = QTimer()
        pulse.timeout.connect(lambda: None)
        pulse.start(100)
        signals = [signal.SIGINT]
        if hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)
        previous_signals = {sig: signal.getsignal(sig) for sig in signals}
        for sig in signals:
            signal.signal(sig, lambda *_: app.quit())
        app.aboutToQuit.connect(bridge.timer.stop)
        app.aboutToQuit.connect(self.close)
        deferred = []
        def schedule(milliseconds, callback):
            timer = QTimer(bridge)
            timer.setSingleShot(True)
            timer.timeout.connect(callback)
            timer.start(milliseconds)
            deferred.append(timer)
        try:
            engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
            if not engine.rootObjects():
                raise RuntimeError(f"QML 界面加载失败，诊断日志：{log_path}")
            # Visible rows are lightweight. Finish their creation synchronously
            # so rapid filtering cannot cancel delegates with obsolete indices.
            engine.setIncubationController(None)
            window = engine.rootObjects()[0]
            if screenshot:
                def capture():
                    destination = Path(screenshot).resolve()
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grabWindow().save(str(destination)):
                        qt_logger.warning("截图保存失败：%s", destination)
                schedule(1000, capture)
            if close_after:
                schedule(close_after, app.quit)
            result = app.exec()
        finally:
            pulse.stop()
            bridge.timer.stop()
            for timer in deferred:
                timer.stop()
            app.aboutToQuit.disconnect(bridge.timer.stop)
            app.aboutToQuit.disconnect(self.close)
            for sig, previous in previous_signals.items():
                signal.signal(sig, previous)
            self.close()
            # Destroy QML bindings while their Python objects still exist.
            delete_qt_object(engine)
            delete_qt_object(bridge)
            qInstallMessageHandler(previous_handler)
            qt_logger.removeHandler(log_file)
            log_file.close()
        return result
