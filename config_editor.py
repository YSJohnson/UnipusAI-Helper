# -*- coding: utf-8 -*-
import json
import os
import sys

from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QShortcut,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CardWidget,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PlainTextEdit,
    PrimaryPushButton,
    ScrollArea,
    Theme,
    ToolButton,
    setTheme,
)


if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

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
    # 提交前停留：非听力题 1 分半 ± 30 秒；听力题 = 音频时长 + 10~20 秒
    "hold_enabled": True,
    "hold_normal_seconds": 90,
    "hold_normal_jitter": 30,
    "hold_listening_pad_min": 10,
    "hold_listening_pad_max": 20,
    "hold_listening_fallback": 120,
    "audio_probe_timeout": 15,
    # 界面默认主题：dark / light / system
    "default_theme": "dark",
}

# 深色 / 浅色两套配色，按配置里的 default_theme 选用
PALETTES = {
    "dark": {
        "bg": "#17191d",
        "panel": "#202328",
        "panel_border": "#2c3138",
        "top": "#202328",
        "input": "#14171b",
        "input_border": "#303640",
        "text": "#f4f7fb",
        "muted": "#a7b0bb",
    },
    "light": {
        "bg": "#f3f6fa",
        "panel": "#ffffff",
        "panel_border": "#dfe5ec",
        "top": "#ffffff",
        "input": "#ffffff",
        "input_border": "#cbd5e1",
        "text": "#1f2933",
        "muted": "#667085",
    },
}

# 当前生效配色，启动时按配置切换
PALETTE = dict(PALETTES["dark"])


# 下拉框顺序 -> 配置取值的映射，与 _build_form_area 里的 addItem 顺序保持一致
_THEME_VALUES = ("dark", "light", "system")


def resolve_theme(raw) -> str:
    """把配置里的 default_theme 归一化成 dark / light / system。"""
    value = str(raw or "").strip().lower()
    if value in ("light", "白", "浅色", "bright", "day"):
        return "light"
    if value in ("system", "auto", "跟随系统"):
        return "system"
    return "dark"


def palette_is_dark(theme: str) -> bool:
    """system 时读系统配色明度，其余直接按取值判断。"""
    if theme == "light":
        return False
    if theme == "system":
        try:
            app = QApplication.instance()
            if app is not None:
                return app.palette().color(app.palette().Window).lightness() < 128
        except Exception:
            pass
    return True


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(data: dict):
    out = dict(data)
    token = out.get("token_full", "")
    if token and isinstance(token, str):
        token = token.strip()
        if (token.startswith("'") and token.endswith("'")) or (
            token.startswith('"') and token.endswith('"')
        ):
            token = token[1:-1].strip()
        out["token_full"] = token

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return True


class ConfigEditor(QWidget):
    def __init__(self):
        # 高 DPI 必须在 QApplication 创建前设置（4K 屏 200% 缩放）
        from dpi_helper import enable_high_dpi
        enable_high_dpi()
        self._qt_app = QApplication.instance() or QApplication(sys.argv)
        self.cfg = load_config()
        # 主题跟着配置走：深色就用深色 palette，浅色反之，跟随系统则读系统明度
        global PALETTE
        self.theme_mode = resolve_theme(self.cfg.get("default_theme"))
        self.is_dark = palette_is_dark(self.theme_mode)
        PALETTE = dict(PALETTES["dark" if self.is_dark else "light"])
        setTheme(Theme.DARK if self.is_dark else Theme.LIGHT)
        super().__init__()
        self.entries = {}
        self._drag_active = False
        self._drag_position = QPoint()
        self._init_window()
        self._build_ui()
        self._apply_theme()

    def _init_window(self):
        self.setObjectName("windowRoot")
        self.setWindowTitle("UnipusAI Helper 配置编辑器")
        # 按屏幕可用区域比例自适应，避免 4K 下窗口偏小、1080p 下超出屏幕
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        if avail is not None and avail.width() > 0:
            width = max(700, min(1000, int(avail.width() * 0.58)))
            height = max(720, min(1100, int(avail.height() * 0.88)))
            self.resize(width, height)
            self.move(
                avail.x() + (avail.width() - width) // 2,
                avail.y() + (avail.height() - height) // 2,
            )
        else:
            self.resize(820, 860)
        self.setMinimumSize(700, 720)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        if sys.platform == "darwin":
            # macOS 习惯用 Cmd+M / Cmd+W；Qt 里 Ctrl 在 darwin 上映射到 Command 键
            for seq, slot in (("Ctrl+M", self.showMinimized), ("Ctrl+W", self.close)):
                try:
                    QShortcut(QKeySequence(seq), self, slot)
                except Exception:
                    pass

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 18, 28, 24)
        root.setSpacing(14)

        root.addWidget(self._build_top_strip())
        root.addWidget(self._build_form_area(), 1)

    def _build_top_strip(self):
        self.top_strip = QFrame()
        self.top_strip.setObjectName("topStrip")
        self.top_strip.installEventFilter(self)
        layout = QHBoxLayout(self.top_strip)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(8)

        self.title_label = QLabel("UnipusAI Helper 配置编辑器")
        self.title_label.setObjectName("pageTitle")
        self.title_label.installEventFilter(self)
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.min_btn = ToolButton(FIF.MINIMIZE)
        self.min_btn.setToolTip("最小化")
        self.min_btn.setFixedSize(32, 32)
        self.min_btn.clicked.connect(self.showMinimized)
        layout.addWidget(self.min_btn)

        self.close_btn = ToolButton(FIF.CLOSE)
        self.close_btn.setToolTip("关闭")
        self.close_btn.setFixedSize(32, 32)
        self.close_btn.clicked.connect(self.close)
        layout.addWidget(self.close_btn)
        return self.top_strip

    def _build_form_area(self):
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        content = QWidget()
        content.setObjectName("contentHost")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)

        form_card, form = self._form_card()
        self._add_text_row(form, "username", "账号", "输入 U校园AI版账号")
        self._add_text_row(form, "password", "密码", "输入 U校园AI版密码")
        self._add_text_row(form, "url", "登录地址", "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome")
        self._add_text_row(form, "api_key", "API Key", "")
        self._add_text_row(form, "base_url", "API 地址", "兼容OpenAI接口")
        self._add_text_row(form, "model", "模型名称", "")
        self._add_text_row(form, "max_tokens", "最大 Token 数", "32768")
        self._add_text_row(form, "temperature", "温度 (0-2)", "0.3")
        self._add_text_row(form, "tts_engine", "录音题 TTS 引擎", "auto / edge / sapi / off")
        self._add_text_row(form, "tts_voice", "TTS 音色（可选）", "留空用默认英语音色，如 en-US-AriaNeural")
        self._add_text_row(form, "tts_proxy", "TTS 代理（可选）", "走代理时填，如 http://127.0.0.1:7890")
        self._add_text_row(form, "hold_normal_seconds", "普通题停留(秒)", "默认 90，即 1 分半")
        self._add_text_row(form, "hold_normal_jitter", "普通题抖动(秒)", "默认 30，即 ±30 秒")
        self._add_text_row(form, "hold_listening_pad_min", "听力题额外停留(最小秒)", "默认 10，音频时长之外再加")
        self._add_text_row(form, "hold_listening_pad_max", "听力题额外停留(最大秒)", "默认 20")
        self._add_text_row(form, "hold_listening_fallback", "听力题兜底停留(秒)", "取不到音频时长时用，默认 120")
        self._add_text_row(form, "audio_probe_timeout", "ffprobe 超时(秒)", "默认 15")
        self._add_text_row(
            form,
            "token_full",
            "Token（可选）",
            "留空使用登录会话；也可粘贴 __token 或 5.0 Authorization",
            multiline=True,
        )

        self.debug_check = CheckBox("开启调试输出")
        self.debug_check.setChecked(bool(self.cfg.get("debug_mode", False)))
        self.entries["debug_mode"] = self.debug_check
        form.addRow(self._field_label("调试模式"), self.debug_check)

        self.hold_check = CheckBox("提交前停留（模拟真人答题耗时）")
        self.hold_check.setChecked(bool(self.cfg.get("hold_enabled", True)))
        self.entries["hold_enabled"] = self.hold_check
        form.addRow(self._field_label("停留开关"), self.hold_check)

        self.theme_combo = ComboBox()
        theme_choices = [("深色", "dark"), ("浅色", "light"), ("跟随系统", "system")]
        for label, _value in theme_choices:
            self.theme_combo.addItem(label)
        current_theme = resolve_theme(self.cfg.get("default_theme"))
        for index, (_label, value) in enumerate(theme_choices):
            if value == current_theme:
                self.theme_combo.setCurrentIndex(index)
                break
        self.theme_combo.setToolTip(
            "软件启动时使用的界面主题。主界面顶栏的深色/浅色开关仍可临时切换，"
            "但每次启动都以这里的配置为准。")
        self.entries["default_theme"] = self.theme_combo
        form.addRow(self._field_label("默认主题"), self.theme_combo)
        content_layout.addWidget(form_card)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.save_btn = PrimaryPushButton("保存配置")
        self.save_btn.clicked.connect(self._on_save)
        action_row.addWidget(self.save_btn)
        content_layout.addLayout(action_row)

        scroll.setWidget(content)
        return scroll

    def _form_card(self):
        card = CardWidget()
        card.setObjectName("panelCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(14)
        layout.addLayout(form)
        return card, form

    def _field_label(self, title_text):
        title = QLabel(title_text)
        title.setObjectName("fieldTitle")
        return title

    def _create_input(self, key, placeholder, multiline=False):
        if multiline:
            entry = PlainTextEdit()
            entry.setFixedHeight(110)
            entry.setPlaceholderText(placeholder)
            entry.setPlainText(str(self.cfg.get(key, "") or ""))
        else:
            entry = LineEdit()
            entry.setPlaceholderText(placeholder)
            entry.setText(str(self.cfg.get(key, "") or ""))
            entry.setClearButtonEnabled(True)
        self.entries[key] = entry
        return entry

    def _add_text_row(self, form, key, title, placeholder, multiline=False):
        entry = self._create_input(key, placeholder, multiline=multiline)
        form.addRow(self._field_label(title), entry)

    def _apply_theme(self):
        self.setStyleSheet(
            f"""
            QWidget#windowRoot {{
                background: {PALETTE['bg']};
            }}
            QWidget {{
                background: transparent;
                font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei';
                font-size: 14px;
                color: {PALETTE['text']};
            }}
            QWidget#topStrip {{
                background: transparent;
                border: none;
            }}
            CardWidget#panelCard {{
                background: {PALETTE['panel']};
                border: 1px solid {PALETTE['panel_border']};
                border-radius: 10px;
            }}
            QLabel#pageTitle {{
                background: transparent;
                color: {PALETTE['text']};
                font-size: 24px;
                font-weight: 800;
            }}
            QLabel#fieldTitle {{
                background: transparent;
                color: {PALETTE['text']};
                font-size: 14px;
                font-weight: 700;
                padding-top: 8px;
            }}
            ScrollArea, QScrollArea {{
                background: transparent;
                border: none;
            }}
            LineEdit, PlainTextEdit {{
                background: {PALETTE['input']};
                border: 1px solid {PALETTE['input_border']};
                border-radius: 8px;
                color: {PALETTE['text']};
                selection-background-color: #2a6df4;
                padding: 8px 10px;
            }}
            PlainTextEdit {{
                font-family: 'Cascadia Mono', Consolas, 'Microsoft YaHei UI';
            }}
            CheckBox, QCheckBox {{
                color: {PALETTE['text']};
                padding: 6px 0;
                font-size: 14px;
                font-weight: 600;
                spacing: 8px;
            }}
            """
        )

    def eventFilter(self, obj, event):
        if obj in (getattr(self, "top_strip", None), getattr(self, "title_label", None)):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_active = True
                self._drag_position = event.globalPos() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove and self._drag_active and event.buttons() & Qt.LeftButton:
                self.move(event.globalPos() - self._drag_position)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._drag_active = False
                return True
        return super().eventFilter(obj, event)

    def _on_save(self):
        data = {}
        for key, entry in self.entries.items():
            if key in ("debug_mode", "hold_enabled"):
                data[key] = entry.isChecked()
            elif isinstance(entry, ComboBox):
                data[key] = _THEME_VALUES[max(0, entry.currentIndex())]
            elif isinstance(entry, PlainTextEdit):
                data[key] = entry.toPlainText().strip()
            else:
                data[key] = entry.text().strip()

        try:
            data["max_tokens"] = int(data["max_tokens"])
        except Exception:
            data["max_tokens"] = 32768

        try:
            data["temperature"] = float(data["temperature"])
        except Exception:
            data["temperature"] = 0.3

        for key, default in (
            ("hold_normal_seconds", 90.0),
            ("hold_normal_jitter", 30.0),
            ("hold_listening_pad_min", 10.0),
            ("hold_listening_pad_max", 20.0),
            ("hold_listening_fallback", 120.0),
            ("audio_probe_timeout", 15.0),
        ):
            try:
                data[key] = float(data[key])
            except Exception:
                data[key] = default

        if data["hold_listening_pad_max"] < data["hold_listening_pad_min"]:
            data["hold_listening_pad_min"], data["hold_listening_pad_max"] = (
                data["hold_listening_pad_max"], data["hold_listening_pad_min"])

        try:
            save_config(data)
            InfoBar.success(
                "保存成功",
                "配置已保存到 config.json",
                duration=1800,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            try:
                import winsound

                winsound.MessageBeep()
            except Exception:
                pass
        except Exception as e:
            MessageBox("保存失败", str(e), self).exec()
            InfoBar.error(
                "保存失败",
                str(e),
                duration=2400,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        if event.key() == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            self._on_save()
            return
        super().keyPressEvent(event)

    def run(self):
        self.show()
        return self._qt_app.exec_()


if __name__ == "__main__":
    editor = ConfigEditor()
    editor.run()


