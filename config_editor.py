# -*- coding: utf-8 -*-
import json
import os
import sys

from unipus_helper.paths import PROJECT_ROOT

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from unipus_helper.native_ui import configure_ui_font
from unipus_helper.paths import QML_DIR


BASE_DIR = str(PROJECT_ROOT)

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
    "browser": "edge",
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

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return True


class ConfigEditor(QObject):
    saved = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.cfg = load_config()

    @Property("QVariantMap", constant=True)
    def values(self):
        return self.cfg

    @Slot("QVariantMap")
    def save(self, values):
        data = dict(self.cfg)
        data.update(values)
        for key in DEFAULT_CONFIG:
            if key != "debug_mode" and key in data:
                data[key] = str(data[key]).strip()
        try:
            data["max_tokens"] = int(data["max_tokens"])
        except (ValueError, TypeError):
            data["max_tokens"] = 32768
        try:
            data["temperature"] = float(data["temperature"])
        except (ValueError, TypeError):
            data["temperature"] = 0.3
        try:
            save_config(data)
        except Exception as exc:
            self.saved.emit(f"保存失败：{exc}", False)
            return
        self.cfg = data
        self.saved.emit("配置已保存", True)

    def run(self):
        QQuickStyle.setStyle("Basic")
        app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
        configure_ui_font(app)
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("configEditor", self)
        engine.load(QUrl.fromLocalFile(str(QML_DIR / "ConfigEditor.qml")))
        if not engine.rootObjects():
            return 1
        return app.exec()


if __name__ == "__main__":
    sys.exit(ConfigEditor().run())
