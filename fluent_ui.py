# -*- coding: utf-8 -*-
import sys
import threading
import time
import webbrowser
import winsound
from typing import Dict

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QTextCursor
from PyQt5.QtWidgets import QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    CardWidget,
    CheckBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    MSFluentWindow,
    PrimaryPushButton,
    ProgressRing,
    PushButton,
    ScrollArea,
    SearchLineEdit,
    SwitchButton,
    TextEdit,
    Theme,
    ToolButton,
    setTheme,
)


class _MainThreadBridge(QObject):
    invoke = pyqtSignal(object)


class _BoolVar:
    def __init__(self, value=False):
        self._value = bool(value)

    def get(self):
        return self._value

    def set(self, value):
        self._value = bool(value)


class FluentModernGUI(MSFluentWindow):
    """Modern Fluent workbench for the existing UnipusAI automation workflow."""

    def __init__(self, driver, solver, bot, app_globals: Dict):
        self._qt_app = QApplication.instance() or QApplication(sys.argv)
        setTheme(Theme.DARK)
        super().__init__()

        self.driver = driver
        self.solver = solver
        self.bot = bot
        self._globals = app_globals
        self.app_version = app_globals.get("APP_VERSION", "")
        self._closing = False
        self.root = self

        self.gui_log_queue = app_globals["gui_log_queue"]
        self.logger = app_globals.get("logger")
        self.WebDriverWait = app_globals["WebDriverWait"]
        self.EC = app_globals["EC"]
        self.By = app_globals["By"]
        self.NoSuchElementException = app_globals["NoSuchElementException"]
        self.WebDriverHelper = app_globals["WebDriverHelper"]

        self._bridge = _MainThreadBridge()
        self._bridge.invoke.connect(lambda fn: fn())

        self._all_tabs = []
        self._tab_widget_items = []
        self._selected_tab_keys = set()
        self._tab_list_built = False
        self._auto_running = False
        self._quick_running = False
        self.is_dark = True
        self._labels = []
        self._panel_cards = []
        self._metric_boxes = []
        self._auto_total = 0
        self._auto_completed = 0

        self._init_window()
        self._build_ui()
        self._apply_theme()
        self.after(100, self._poll_logs)

    def _init_window(self):
        self.setWindowTitle("")
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)
        self.setMicaEffectEnabled(False)
        self.setSystemTitleBarButtonVisible(False)
        self.navigationInterface.hide()
        self.titleBar.raise_()
        self.titleBar.setTitle("")
        self.titleBar.titleLabel.hide()
        self.titleBar.titleLabel.setStyleSheet(
            "font-size: 18px; font-weight: 700; "
            "font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei';"
        )
        self.titleBar.setFixedHeight(30)

    def _build_ui(self):
        self.workbench = QWidget()
        self.workbench.setObjectName("workbench")
        self.stackedWidget.addWidget(self.workbench)
        self.stackedWidget.setCurrentWidget(self.workbench)

        root = QVBoxLayout(self.workbench)
        root.setContentsMargins(28, 0, 28, 24)
        root.setSpacing(12)

        top = QWidget()
        top.setObjectName("topStrip")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(14, 8, 14, 8)
        top_layout.setSpacing(12)
        top_layout.addWidget(self._label("UnipusAI Helper", 24, "#f5f7fb", 800))
        if self.app_version:
            version_label = self._label(f"v{self.app_version}", 13, "#c9d2dd", 700)
            version_label.setStyleSheet(version_label.styleSheet() + " padding: 7px 10px; border-radius: 6px; background: rgba(48, 217, 239, 0.12);")
            top_layout.addWidget(version_label)
        self.author_label = self._label("by YSJohnson", 13, "#c9d2dd", 600)
        self.author_label.setStyleSheet(self.author_label.styleSheet() + " padding: 7px 12px; border-radius: 6px; background: rgba(255, 255, 255, 0.06);")
        top_layout.addWidget(self.author_label)
        self.github_btn = PushButton("GitHub 仓库")
        self.github_btn.setIcon(FIF.GITHUB)
        self.github_btn.setToolTip("打开 GitHub 仓库")
        self.github_btn.clicked.connect(self._open_repository)
        top_layout.addWidget(self.github_btn)
        top_layout.addStretch(1)

        self.status_pill = self._status_pill("初始化", "#30d9ef")
        top_layout.addWidget(self.status_pill)
        top_layout.addSpacing(22)

        self.debug_var = _BoolVar(self._globals.get("DEBUG_MODE", False))
        top_layout.addWidget(self._label("调试模式", 14, "#a7b0bb", 600))
        self.debug_switch = SwitchButton()
        self.debug_switch.setText("")
        self.debug_switch.setChecked(self.debug_var.get())
        self.debug_switch.checkedChanged.connect(self._on_debug_toggle)
        top_layout.addWidget(self.debug_switch)
        top_layout.addSpacing(22)

        top_layout.addWidget(self._label("界面主题", 14, "#a7b0bb", 600))
        self.theme_switch = SwitchButton()
        self.theme_switch.setText("夜间")
        self.theme_switch.setChecked(True)
        self.theme_switch.checkedChanged.connect(self._on_theme_toggle)
        top_layout.addWidget(self.theme_switch)

        self.btn_quit = PushButton("安全退出软件")
        self.btn_quit.setIcon(FIF.POWER_BUTTON)
        self.btn_quit.setToolTip("关闭浏览器并退出软件")
        self.btn_quit.clicked.connect(self.on_quit_clicked)
        top_layout.addWidget(self.btn_quit)
        root.addWidget(top)

        main = QGridLayout()
        main.setSpacing(14)
        root.addLayout(main, 1)

        self.tasks_card = self._panel_card("任务")
        task_layout = self.tasks_card.body_layout
        task_toolbar = QHBoxLayout()
        task_toolbar.setSpacing(8)
        self.task_search = SearchLineEdit()
        self.task_search.setPlaceholderText("筛选任务名称")
        self.task_search.textChanged.connect(self._on_search_key)
        task_toolbar.addWidget(self.task_search)
        self.btn_select_all = PushButton("全选")
        self.btn_select_all.clicked.connect(self._on_select_all)
        task_toolbar.addWidget(self.btn_select_all)
        self.btn_select_compulsory = PushButton("必修")
        self.btn_select_compulsory.clicked.connect(self._on_select_compulsory)
        task_toolbar.addWidget(self.btn_select_compulsory)
        self.btn_deselect_all = PushButton("清空")
        self.btn_deselect_all.clicked.connect(self._on_deselect_all)
        task_toolbar.addWidget(self.btn_deselect_all)
        self.btn_select_visible = PrimaryPushButton("全选筛选结果")
        self.btn_select_visible.clicked.connect(self._on_select_visible)
        self.btn_select_visible.hide()
        task_toolbar.addWidget(self.btn_select_visible)
        task_layout.addLayout(task_toolbar)

        self.task_scroll = ScrollArea()
        self.task_scroll.setWidgetResizable(True)
        self.task_scroll.setFrameShape(QFrame.NoFrame)
        self.task_scroll.setStyleSheet("ScrollArea { background: transparent; border: none; } QWidget { background: transparent; }")
        self._tab_list_frame = QWidget()
        self._tab_list_layout = QVBoxLayout(self._tab_list_frame)
        self._tab_list_layout.setContentsMargins(0, 6, 0, 6)
        self._tab_list_layout.setSpacing(7)
        self._tab_list_layout.addWidget(self._empty_state())
        self._tab_list_layout.addStretch(1)
        self.task_scroll.setWidget(self._tab_list_frame)
        task_layout.addWidget(self.task_scroll, 1)
        main.addWidget(self.tasks_card, 0, 0)

        self.control_card = self._panel_card("控制")
        control_layout = self.control_card.body_layout
        status_row = QHBoxLayout()
        self.progress_ring = ProgressRing()
        self.progress_ring.setFixedSize(42, 42)
        self.progress_ring.hide()
        status_row.addWidget(self.progress_ring)
        status_text = QVBoxLayout()
        self.status_label = self._label("初始化中，等待登录流程。", 14, "#c9d2dd")
        self.status_label.setWordWrap(True)
        self.selected_label = self._label("已选 0 / 0", 12, "#9fa8b4")
        status_text.addWidget(self.status_label)
        status_text.addWidget(self.selected_label)
        status_row.addLayout(status_text, 1)
        control_layout.addLayout(status_row)

        self.btn_scan = PrimaryPushButton("扫描任务列表")
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self._on_scan_clicked)
        control_layout.addWidget(self.btn_scan)
        self.btn_quick = PrimaryPushButton("快速处理当前页")
        self.btn_quick.setEnabled(False)
        self.btn_quick.clicked.connect(self._on_quick_clicked)
        control_layout.addWidget(self.btn_quick)
        self.btn_auto = PrimaryPushButton("处理选中任务")
        self.btn_auto.setEnabled(False)
        self.btn_auto.clicked.connect(self._on_auto_clicked)
        control_layout.addWidget(self.btn_auto)
        self.btn_stop = PushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        control_layout.addWidget(self.btn_stop)

        meta = QWidget()
        meta.setStyleSheet("background: transparent;")
        meta_layout = QGridLayout(meta)
        meta_layout.setContentsMargins(0, 8, 0, 0)
        meta_layout.setHorizontalSpacing(8)
        meta_layout.setVerticalSpacing(8)
        self.account_value = self._metric("账号", self.bot.config.username[:16] or "-")
        self.model_value = self._metric("模型", self.bot.config.model or "-")
        self.task_value = self._metric("剩余任务", "0")
        meta_layout.addWidget(self.account_value, 0, 0)
        meta_layout.addWidget(self.model_value, 0, 1)
        meta_layout.addWidget(self.task_value, 1, 0, 1, 2)
        control_layout.addWidget(meta)
        control_layout.addStretch(1)
        main.addWidget(self.control_card, 0, 1)

        self.log_card = self._panel_card("日志")
        log_layout = self.log_card.body_layout
        self.log_area = TextEdit()
        self.log_area.setReadOnly(True)
        log_layout.addWidget(self.log_area, 1)
        main.addWidget(self.log_card, 1, 0, 1, 2)

        main.setColumnStretch(0, 6)
        main.setColumnStretch(1, 4)
        main.setRowStretch(0, 2)
        main.setRowStretch(1, 3)

    def _label(self, text, size=14, color="#f4f7fb", weight=400):
        label = QLabel(text)
        tone = "primary" if color.lower() in ("#f4f7fb", "#f5f7fb", "#eef8fb") else "secondary"
        label.setProperty("tone", tone)
        label.setProperty("fontSize", size)
        label.setProperty("fontWeight", weight)
        self._labels.append(label)
        self._style_label(label)
        return label

    def _style_label(self, label):
        tone = label.property("tone") or "primary"
        size = label.property("fontSize") or 14
        weight = label.property("fontWeight") or 400
        color = "#f5f7fb" if tone == "primary" and self.is_dark else "#1f2933" if tone == "primary" else "#a7b0bb" if self.is_dark else "#667085"
        label.setStyleSheet(
            f"background: transparent; color: {color}; font-size: {size}px; "
            f"font-weight: {weight}; font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei';"
        )

    def _panel_card(self, title):
        card = CardWidget()
        card.setObjectName("panelCard")
        self._panel_cards.append(card)
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 16, 18, 18)
        outer.setSpacing(10)
        outer.addWidget(self._label(title, 18, "#f5f7fb", 700))
        body = QVBoxLayout()
        body.setSpacing(8)
        outer.addLayout(body, 1)
        card.body_layout = body
        return card

    def _status_pill(self, text, color):
        frame = QFrame()
        frame.setObjectName("statusPill")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(8)
        dot = QFrame()
        dot.setFixedSize(9, 9)
        dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        self.status_dot = dot
        self.status_text = self._label(text, 15, "#eef8fb", 700)
        layout.addWidget(dot)
        layout.addWidget(self.status_text)
        return frame

    def _set_status(self, text, detail=None, color="#30d9ef"):
        self.status_text.setText(text)
        self.status_dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        if detail is not None:
            self.status_label.setText(detail)

    def _metric(self, label, value):
        box = QWidget()
        self._metric_boxes.append(box)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(1)
        value_label = self._label(str(value), 15, "#f5f7fb", 700)
        label_widget = self._label(label, 12, "#9fa8b4")
        layout.addWidget(value_label)
        layout.addWidget(label_widget)
        box.value_label = value_label
        return box

    def _empty_state(self):
        empty = QWidget()
        empty.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(empty)
        layout.setContentsMargins(0, 70, 0, 70)
        text = self._label("登录后点击“扫描任务列表”，课程任务会显示在这里。", 14, "#a7b0bb")
        text.setAlignment(Qt.AlignCenter)
        layout.addWidget(text)
        return empty

    def _apply_theme(self):
        if self.is_dark:
            palette = {
                "bg": "#17191d", "panel": "#202328", "panel_border": "#2c3138",
                "top": "#202328", "pill_bg": "#1c3238", "pill_border": "#24515b",
                "metric": "#242830", "log": "#14171b", "log_border": "#303640",
                "text": "#f4f7fb",
            }
            setTheme(Theme.DARK)
        else:
            palette = {
                "bg": "#f3f6fa", "panel": "#ffffff", "panel_border": "#dfe5ec",
                "top": "#ffffff", "pill_bg": "#e8f7fb", "pill_border": "#bcebf4",
                "metric": "#f1f5f9", "log": "#ffffff", "log_border": "#d7dee8",
                "text": "#1f2933",
            }
            setTheme(Theme.LIGHT)

        self.setBackgroundColor(QColor(palette["bg"]))
        self.setCustomBackgroundColor(QColor(palette["bg"]), QColor(palette["bg"]))
        self.stackedWidget.setStyleSheet(f"background: {palette['bg']}; border: none;")
        self.workbench.setStyleSheet(f"QWidget#workbench {{ background: {palette['bg']}; }}")
        self.setStyleSheet(f"""
            QWidget {{
                font-family: 'Segoe UI', 'Microsoft YaHei';
                font-size: 14px;
                color: {palette['text']};
            }}
            QWidget#topStrip {{
                background: {palette['top']};
                border: 1px solid {palette['panel_border']};
                border-radius: 10px;
            }}
            QFrame#statusPill {{
                background: {palette['pill_bg']};
                border: 1px solid {palette['pill_border']};
                border-radius: 8px;
            }}
            TextEdit {{
                background: {palette['log']};
                border: 1px solid {palette['log_border']};
                border-radius: 8px;
                color: {palette['text']};
                font-family: 'Cascadia Mono', Consolas, 'Microsoft YaHei UI';
                font-size: 14px;
            }}
            CheckBox, QCheckBox {{
                color: {palette['text']};
                padding: 8px 10px;
                font-size: 16px;
                font-weight: 600;
                font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei';
                spacing: 8px;
            }}
        """)
        for card in self._panel_cards:
            card.setStyleSheet(f"CardWidget#panelCard {{ background: {palette['panel']}; border: 1px solid {palette['panel_border']}; border-radius: 10px; }}")
        for box in self._metric_boxes:
            box.setStyleSheet(f"background: {palette['metric']}; border-radius: 8px;")
        for label in self._labels:
            self._style_label(label)
        if hasattr(self, "author_label"):
            self.author_label.setStyleSheet(
                self.author_label.styleSheet()
                + f" padding: 7px 12px; border-radius: 6px; background: {palette['metric']};"
            )

    def _update_task_stats(self):
        total = len(self._all_tabs)
        selected = len(self._selected_tab_keys)
        self.selected_label.setText(f"已选 {selected} / {total}")
        if self._auto_running:
            self.task_value.value_label.setText(str(max(self._auto_total - self._auto_completed, 0)))
        else:
            self.task_value.value_label.setText(str(selected))

    def after(self, ms, callback):
        if threading.current_thread() is threading.main_thread():
            QTimer.singleShot(ms, callback)
        else:
            self._bridge.invoke.emit(lambda: QTimer.singleShot(ms, callback))

    def mainloop(self):
        self.show()
        return self._qt_app.exec_()

    def destroy(self):
        self.close()

    def enable_scan_button(self):
        self.btn_scan.setEnabled(True)
        self.btn_quick.setEnabled(True)
        self._set_status("就绪", "登录完成，可以扫描任务列表或直接处理当前页面。", "#37d987")
        self.gui_log_queue.put("\n" + "=" * 60)
        self.gui_log_queue.put("系统就绪")
        self.gui_log_queue.put("模式1: 点击[扫描任务列表] -> 勾选 -> 自动处理")
        self.gui_log_queue.put("模式2: 手动翻到题目页 -> 点击[快速处理当前页]")
        try:
            winsound.MessageBeep()
        except Exception:
            pass

    def _set_busy(self, busy: bool, indeterminate: bool = True):
        self.progress_ring.setVisible(busy)
        if busy:
            if indeterminate:
                self.progress_ring.setRange(0, 0)
                self.progress_ring.resume()
            else:
                self.progress_ring.pause()
                self.progress_ring.setRange(0, 100)
        else:
            self.progress_ring.pause()
            self.progress_ring.setRange(0, 100)
            self.progress_ring.setValue(0)

    def _start_auto_progress(self, total: int):
        self._auto_total = total
        self._auto_completed = 0
        self.progress_ring.setVisible(True)
        self.progress_ring.pause()
        self.progress_ring.setRange(0, 100)
        self.progress_ring.setValue(0)
        self.selected_label.setText(f"进度 0 / {total}")
        self.task_value.value_label.setText(str(total))

    def _update_auto_progress(self, completed: int, total: int, task_name: str = ""):
        self._auto_total = total
        self._auto_completed = min(max(completed, 0), total)
        value = int((self._auto_completed / total) * 100) if total else 0
        self.progress_ring.setVisible(True)
        self.progress_ring.setRange(0, 100)
        self.progress_ring.setValue(value)
        remaining = max(total - self._auto_completed, 0)
        self.task_value.value_label.setText(str(remaining))
        self.selected_label.setText(f"进度 {self._auto_completed} / {total}")
        if task_name and self._auto_completed < total:
            self.status_label.setText(f"正在处理：{task_name}")

    def _finish_auto_progress(self):
        self._auto_completed = self._auto_total
        self.progress_ring.setVisible(True)
        self.progress_ring.setRange(0, 100)
        self.progress_ring.setValue(100 if self._auto_total else 0)
        self.task_value.value_label.setText("0")
        self.selected_label.setText(f"进度 {self._auto_total} / {self._auto_total}")

    def _solver_progress_callback(self, completed: int, total: int, task_name: str = ""):
        self.after(0, lambda: self._update_auto_progress(completed, total, task_name))

    def _on_scan_clicked(self):
        if not self.btn_scan.isEnabled():
            return
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("扫描中...")
        self._set_status("扫描中", "正在扫描课程目录和章节任务。", "#ffb25f")
        self._set_busy(True)
        threading.Thread(target=self._scan_tabs_thread, daemon=True).start()

    def _scan_tabs_thread(self):
        try:
            tabs = []
            course_tabs = self._scan_course_directory()
            if course_tabs:
                tabs = course_tabs
                self.gui_log_queue.put(f"课程目录页发现 {len(tabs)} 个任务")
            else:
                level1 = self.solver._get_level1_tabs()
                if level1:
                    for l1_idx, l1_tab in enumerate(level1):
                        if not self.WebDriverHelper.safe_click(self.driver, l1_tab["element"]):
                            continue
                        time.sleep(1.2)
                        level2 = self.solver._get_level2_tabs()
                        if not level2:
                            tabs.append({"l1_idx": l1_idx, "l2_idx": -1, "l1_title": l1_tab["title"], "l2_title": "", "display": l1_tab["title"], "is_l2": False, "is_compulsory": True})
                        else:
                            for l2_idx, l2_tab in enumerate(level2):
                                tabs.append({"l1_idx": l1_idx, "l2_idx": l2_idx, "l1_title": l1_tab["title"], "l2_title": l2_tab["title"], "display": f"  - {l2_tab['title']}", "is_l2": True, "is_compulsory": True})
                    self.gui_log_queue.put(f"章节内部页发现 {len(tabs)} 个任务")
            self._all_tabs = tabs
            self.after(0, self._build_tab_list_ui)
        except Exception as e:
            self.gui_log_queue.put(f"扫描失败: {str(e)[:80]}")
            if self.logger:
                self.logger.error(f"扫描异常: {e}", exc_info=True)
            self.after(0, self._reset_scan_button)

    def _scan_course_directory(self):
        tabs = []
        try:
            unit_container = self.WebDriverWait(self.driver, 5).until(
                self.EC.presence_of_element_located((self.By.CLASS_NAME, "unipus-tabs_unitTabScrollContainer__fXBxR"))
            )
            unit_tabs = unit_container.find_elements(self.By.CSS_SELECTOR, ":scope > *")
            self.gui_log_queue.put(f"课程目录页，找到 {len(unit_tabs)} 个 Unit")
            for unit_idx, unit_tab in enumerate(unit_tabs):
                name_counts = {}
                try:
                    self.driver.execute_script("arguments[0].click();", unit_tab)
                except Exception:
                    unit_tab.click()
                time.sleep(0.8)
                chapters = self.driver.find_elements(self.By.CLASS_NAME, "courses-unit_taskItemInnerLayout__DTYuN")
                for chapter in chapters:
                    try:
                        name_elem = chapter.find_element(self.By.CLASS_NAME, "courses-unit_taskTypeName__99BXj")
                        name = name_elem.text.strip()
                        if not name:
                            continue
                        try:
                            chapter.find_element(self.By.CLASS_NAME, "courses-unit_taskRequireIcon__zZldK")
                            is_compulsory = True
                        except self.NoSuchElementException:
                            is_compulsory = False
                        prefix = "[必修]" if is_compulsory else "[选修]"
                        section_title = self._extract_course_task_section(chapter)
                        name_occurrence = name_counts.get(name, 0)
                        name_counts[name] = name_occurrence + 1
                        section_part = f" - {section_title}" if section_title else f" - #{name_occurrence + 1}"
                        tabs.append({"l1_idx": len(tabs), "l2_idx": -1, "l1_title": name, "l2_title": section_title, "display": f"{prefix} Unit{unit_idx + 1}{section_part} - {name}", "is_l2": False, "is_compulsory": is_compulsory, "_element": name_elem, "_unit_idx": unit_idx, "_section_title": section_title, "_name_occurrence": name_occurrence})
                    except Exception:
                        continue
        except Exception as e:
            if self.logger:
                self.logger.debug(f"课程目录扫描未命中: {e}")
        return tabs

    def _extract_course_task_section(self, chapter) -> str:
        try:
            section = self.driver.execute_script("""
                const task = arguments[0];
                const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim();
                const root = task.closest('[class*="courses-unit"]') || task.parentElement;
                const taskText = clean(task.innerText);
                const sectionWords = /(section\\s*[a-z]|language\\s+focus|reading|listening|speaking|writing|grammar|vocabulary|pronunciation)/i;
                const headingSelectors = ['[class*="section"]', '[class*="title"]', '[class*="name"]', 'h1,h2,h3,h4,h5'];
                const headings = [];
                for (const selector of headingSelectors) {
                    for (const el of root.querySelectorAll(selector)) {
                        const text = clean(el.innerText);
                        if (!text || text === taskText || text.length > 80) continue;
                        if (sectionWords.test(text)) headings.push({ el, text });
                    }
                }
                const taskTop = task.getBoundingClientRect().top;
                const before = headings
                    .filter(item => item.el.getBoundingClientRect().top < taskTop)
                    .sort((a, b) => b.el.getBoundingClientRect().top - a.el.getBoundingClientRect().top)
                    .slice(0, 2)
                    .map(item => item.text)
                    .reverse();
                return [...new Set(before)].join(' / ');
            """, chapter)
            return (section or "").strip()
        except Exception:
            return ""

    def _clear_task_list(self):
        while self._tab_list_layout.count():
            item = self._tab_list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _build_tab_list_ui(self):
        self._set_busy(False)
        if not self._all_tabs:
            self.gui_log_queue.put("未扫描到任何 Tab")
            self._reset_scan_button()
            return

        self._clear_task_list()
        self._tab_widget_items = []
        self._selected_tab_keys = {
            self._tab_key(i, tab)
            for i, tab in enumerate(self._all_tabs)
            if tab.get("is_compulsory", False)
        }
        compulsory_count = sum(1 for t in self._all_tabs if t.get("is_compulsory", False))
        self.gui_log_queue.put(f"必修: {compulsory_count}, 选修: {len(self._all_tabs) - compulsory_count}")

        groups = {}
        for i, tab in enumerate(self._all_tabs):
            display = tab.get("display", "")
            unit_key = display.split("Unit")[1].split(" - ")[0].strip() if "Unit" in display else "其他"
            groups.setdefault(unit_key, []).append(i)

        for unit_name, indices in groups.items():
            unit_tabs = [self._all_tabs[i] for i in indices]
            comp = sum(1 for t in unit_tabs if t.get("is_compulsory", False))
            header = self._label(f"Unit {unit_name} ({len(indices)} 个任务 / {comp} 必修)", 14, "#9fa8b4", 600)
            header.setStyleSheet(header.styleSheet() + " padding: 10px 4px 4px 4px;")
            self._tab_list_layout.addWidget(header)
            for idx in indices:
                tab = self._all_tabs[idx]
                key = self._tab_key(idx, tab)
                checkbox = CheckBox(tab["display"])
                checkbox.setChecked(key in self._selected_tab_keys)
                checkbox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                checkbox.stateChanged.connect(lambda _=None, k=key, cb=checkbox: self._sync_tab_selection(k, cb))
                self._tab_list_layout.addWidget(checkbox)
                self._tab_widget_items.append({"index": idx, "key": key, "checkbox": checkbox})

        self._tab_list_layout.addStretch(1)
        self._tab_list_built = True
        self.btn_auto.setEnabled(True)
        self.btn_auto.setText(f"处理选中任务 ({len(self._selected_tab_keys)})")
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("重新扫描")
        self._set_status("已扫描", f"发现 {len(self._all_tabs)} 个任务，请选择处理范围。", "#37d987")
        self.gui_log_queue.put(f"\n扫描完成，共 {len(self._all_tabs)} 个任务可供选择")
        self._update_task_stats()

    def _tab_key(self, index: int, tab: Dict) -> str:
        return "|".join(str(tab.get(part, "")) for part in ("l1_idx", "l2_idx", "display")) or str(index)

    def _sync_tab_selection(self, key: str, checkbox):
        if checkbox.isChecked():
            self._selected_tab_keys.add(key)
        else:
            self._selected_tab_keys.discard(key)
        self.btn_auto.setText(f"处理选中任务 ({len(self._selected_tab_keys)})")
        self._update_task_stats()

    def _set_task_selected(self, item: Dict, selected: bool):
        item["checkbox"].setChecked(selected)
        if selected:
            self._selected_tab_keys.add(item["key"])
        else:
            self._selected_tab_keys.discard(item["key"])
        self.btn_auto.setText(f"处理选中任务 ({len(self._selected_tab_keys)})")
        self._update_task_stats()

    def _task_matches_query(self, tab: Dict, query: str) -> bool:
        return query == "" or query in tab.get("display", "").lower()

    def _on_select_all(self):
        for item in self._tab_widget_items:
            self._set_task_selected(item, True)

    def _on_select_compulsory(self):
        for item in self._tab_widget_items:
            tab = self._all_tabs[item["index"]]
            self._set_task_selected(item, tab.get("is_compulsory", False))

    def _on_deselect_all(self):
        for item in self._tab_widget_items:
            self._set_task_selected(item, False)

    def _on_select_visible(self):
        query = self.task_search.text().lower()
        for item in self._tab_widget_items:
            tab = self._all_tabs[item["index"]]
            if self._task_matches_query(tab, query):
                self._set_task_selected(item, True)

    def _on_search_key(self, *_):
        query = self.task_search.text().lower()
        for item in self._tab_widget_items:
            tab = self._all_tabs[item["index"]]
            item["checkbox"].setVisible(self._task_matches_query(tab, query))
        self.btn_select_visible.setVisible(bool(query))

    def _on_auto_clicked(self):
        if self._auto_running or not self.btn_auto.isEnabled():
            return
        selected = [
            tab for i, tab in enumerate(self._all_tabs)
            if self._tab_key(i, tab) in self._selected_tab_keys
        ]
        if not selected:
            InfoBar.warning("没有选择任务", "请先在任务列表中勾选至少一项。", duration=1800, position=InfoBarPosition.TOP_RIGHT, parent=self)
            self.gui_log_queue.put("没有勾选任何任务，请先勾选")
            return
        self.gui_log_queue.put(f"\n{'=' * 60}")
        self.gui_log_queue.put(f"用户选择了 {len(selected)} 个任务，开始全自动处理...")
        self.gui_log_queue.put(f"{'=' * 60}")
        self._auto_running = True
        self._start_auto_progress(len(selected))
        if hasattr(self.solver, "clear_stop"):
            self.solver.clear_stop()
        setattr(self.solver, "progress_callback", self._solver_progress_callback)
        self.btn_auto.setEnabled(False)
        self.btn_auto.setText("处理中...")
        self.btn_scan.setEnabled(False)
        self.btn_quick.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_stop.setText("停止")
        self._set_status("处理中", "正在处理选中任务，请勿操作浏览器。", "#ffb25f")
        threading.Thread(target=self._run_auto_task, args=(selected,), daemon=True).start()

    def _on_stop_clicked(self):
        if not (self._auto_running or self._quick_running):
            return
        if hasattr(self.solver, "request_stop"):
            self.solver.request_stop()
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("正在停止...")
        if self._auto_running:
            self._set_status("正在停止", "会在当前步骤结束后停止批量处理。", "#ffb25f")
            self.gui_log_queue.put("已请求停止批量处理，会在当前步骤结束后退出。")
        else:
            self._set_status("正在停止", "会在当前步骤结束后停止当前页处理。", "#ffb25f")
            self.gui_log_queue.put("已请求停止当前页处理，会在当前步骤结束后退出。")

    def _run_auto_task(self, selected):
        try:
            self.solver.process_selected_tabs(selected)
            if hasattr(self.solver, "_should_stop") and self.solver._should_stop():
                self.gui_log_queue.put("\n批量处理已停止。")
            else:
                self.gui_log_queue.put("\n全部选中任务处理完成！")
            winsound.MessageBeep()
        except Exception as e:
            self.gui_log_queue.put(f"\n自动处理异常: {str(e)}")
            if self.logger:
                self.logger.error(f"自动处理异常: {e}", exc_info=True)
        finally:
            self._auto_running = False
            self.after(0, self._reset_auto_button)

    def _reset_auto_button(self):
        self._finish_auto_progress()
        self.btn_auto.setEnabled(True)
        self.btn_auto.setText(f"处理选中任务 ({len(self._selected_tab_keys)})")
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("重新扫描")
        self.btn_quick.setEnabled(True)
        self.btn_quick.setText("快速处理当前页")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("停止")
        stopped = hasattr(self.solver, "_should_stop") and self.solver._should_stop()
        if stopped:
            self._set_status("已停止", "批量处理已停止。", "#ffb25f")
            InfoBar.info("已停止", "批量处理已停止。", duration=1800, position=InfoBarPosition.TOP_RIGHT, parent=self)
        else:
            self._set_status("完成", "任务完成。", "#37d987")
            InfoBar.success("任务完成", "选中的任务已经处理完毕。", duration=1800, position=InfoBarPosition.TOP_RIGHT, parent=self)
        try:
            winsound.MessageBeep()
        except Exception:
            pass

    def _on_quick_clicked(self):
        if self._quick_running or not self.btn_quick.isEnabled():
            return
        self.gui_log_queue.put("\n开始处理当前停留的页面...")
        self._quick_running = True
        if hasattr(self.solver, "clear_stop"):
            self.solver.clear_stop()
        self.btn_quick.setEnabled(False)
        self.btn_quick.setText("处理中...")
        self.btn_scan.setEnabled(False)
        if self.btn_auto.isVisible():
            self.btn_auto.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_stop.setText("停止")
        self._set_status("处理中", "正在处理当前页面。", "#ffb25f")
        self._set_busy(True)
        threading.Thread(target=self._run_quick_task, daemon=True).start()

    def _run_quick_task(self):
        self.solver.processed_hashes.clear()
        try:
            success = self.solver.solve_current_page()
            if hasattr(self.solver, "_should_stop") and self.solver._should_stop():
                self.gui_log_queue.put("\n当前页面处理已停止。")
            elif success:
                self.gui_log_queue.put("\n当前页面处理完成！")
            else:
                self.gui_log_queue.put("\n当前页面没有需要处理的题目")
        except Exception as e:
            self.gui_log_queue.put(f"\n处理异常: {str(e)}")
            if self.logger:
                self.logger.error(f"快速处理异常: {e}", exc_info=True)
        finally:
            self._quick_running = False
            self.after(0, self._reset_quick_button)

    def _reset_quick_button(self):
        self._set_busy(False)
        self.btn_quick.setEnabled(True)
        self.btn_quick.setText("快速处理当前页")
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("扫描任务列表")
        if self.btn_auto.isVisible():
            self.btn_auto.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("停止")
        stopped = hasattr(self.solver, "_should_stop") and self.solver._should_stop()
        if stopped:
            self._set_status("已停止", "当前页面处理已停止。", "#ffb25f")
            InfoBar.info("已停止", "当前页面处理已停止。", duration=1800, position=InfoBarPosition.TOP_RIGHT, parent=self)
        else:
            self._set_status("就绪", "当前页面处理结束。", "#37d987")
        try:
            winsound.MessageBeep()
        except Exception:
            pass

    def _reset_scan_button(self):
        self._set_busy(False)
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("重新扫描")
        self._set_status("扫描失败", "请确认已进入课程页面后重试。", "#ff6b6b")
        InfoBar.error("扫描失败", "请确认已进入课程页面后重试。", duration=2200, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _on_debug_toggle(self, checked=None):
        checked = self.debug_switch.isChecked() if checked is None else bool(checked)
        self.debug_var.set(checked)
        self._globals["DEBUG_MODE"] = checked
        self.gui_log_queue.put(f"调试模式: {'开启' if checked else '关闭'}")

    def _on_theme_toggle(self, checked=None):
        self.is_dark = self.theme_switch.isChecked() if checked is None else bool(checked)
        self.theme_switch.setText("夜间")
        self._apply_theme()

    def _open_repository(self):
        webbrowser.open("https://github.com/YSJohnson/UnipusAI-Helper")

    def _poll_logs(self):
        scrollbar = self.log_area.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        while not self.gui_log_queue.empty():
            msg = self.gui_log_queue.get()
            self.log_area.append(str(msg))
        max_blocks = 800
        document = self.log_area.document()
        while document.blockCount() > max_blocks:
            cursor = QTextCursor(document)
            cursor.movePosition(QTextCursor.Start)
            cursor.select(QTextCursor.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
        if was_at_bottom:
            self.log_area.moveCursor(QTextCursor.End)
        self.after(100, self._poll_logs)

    def on_quit_clicked(self):
        if self._closing:
            return
        self._closing = True
        self.gui_log_queue.put("正在关闭浏览器并释放资源...")
        try:
            self.driver.quit()
        except Exception:
            pass
        QApplication.quit()

    def closeEvent(self, event):
        if not self._closing:
            self.on_quit_clicked()
        event.accept()
