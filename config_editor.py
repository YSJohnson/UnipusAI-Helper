# -*- coding: utf-8 -*-
import json
import os
import sys

from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CardWidget,
    CheckBox,
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
    "api_key": "",
    "base_url": "",
    "model": "",
    "max_tokens": 4096,
    "temperature": 0.3,
    "debug_mode": False,
}

PALETTE = {
    "bg": "#17191d",
    "panel": "#202328",
    "panel_border": "#2c3138",
    "top": "#202328",
    "input": "#14171b",
    "input_border": "#303640",
    "text": "#f4f7fb",
    "muted": "#a7b0bb",
}


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

    out.pop("learning_strategy", None)
    out.pop("timeout", None)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return True


class ConfigEditor(QWidget):
    def __init__(self):
        self._qt_app = QApplication.instance() or QApplication(sys.argv)
        setTheme(Theme.DARK)
        super().__init__()
        self.cfg = load_config()
        self.entries = {}
        self._drag_active = False
        self._drag_position = QPoint()
        self._init_window()
        self._build_ui()
        self._apply_theme()

    def _init_window(self):
        self.setObjectName("windowRoot")
        self.setWindowTitle("UnipusAI Helper 配置编辑器")
        self.resize(820, 860)
        self.setMinimumSize(700, 720)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

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
        layout.setSpacing(12)

        self.title_label = QLabel("UnipusAI Helper 配置编辑器")
        self.title_label.setObjectName("pageTitle")
        self.title_label.installEventFilter(self)
        layout.addWidget(self.title_label)
        layout.addStretch(1)

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
        self._add_text_row(form, "max_tokens", "最大 Token 数", "4096")
        self._add_text_row(form, "temperature", "温度 (0-2)", "0.3")
        self._add_text_row(
            form,
            "token_full",
            "Token (反作弊)",
            "从浏览器控制台获取 localStorage.getItem('__token')",
            multiline=True,
        )

        self.debug_check = CheckBox("开启调试输出")
        self.debug_check.setChecked(bool(self.cfg.get("debug_mode", False)))
        self.entries["debug_mode"] = self.debug_check
        form.addRow(self._field_label("调试模式"), self.debug_check)
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

    def _create_input(self, key, placeholder, password=False, multiline=False):
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

    def _add_text_row(self, form, key, title, placeholder, password=False, multiline=False):
        entry = self._create_input(key, placeholder, password=password, multiline=multiline)
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
            if key == "debug_mode":
                data[key] = entry.isChecked()
            elif isinstance(entry, PlainTextEdit):
                data[key] = entry.toPlainText().strip()
            else:
                data[key] = entry.text().strip()

        try:
            data["max_tokens"] = int(data["max_tokens"])
        except Exception:
            data["max_tokens"] = 4096

        try:
            data["temperature"] = float(data["temperature"])
        except Exception:
            data["temperature"] = 0.3

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
    app = QApplication.instance() or QApplication(sys.argv)
    editor = ConfigEditor()
    editor.run()
