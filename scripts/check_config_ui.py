"""P1-5 回归：配置编辑器「全部编辑 → 保存全部」必须保留文本修改（offscreen QML）。"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QMetaObject, QTimer, QUrl, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402

from unipus_helper.native_ui import ConfigEditor, configure_ui_font  # noqa: E402
from unipus_helper.paths import QML_DIR  # noqa: E402

LOG = []


def log(m=""):
    LOG.append(str(m))


def check(name, cond, detail=""):
    log(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(cond)


def main():
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="cfgqml_"))
    cfg = tmp / "config.json"
    cfg.write_text(json.dumps({
        "username": "old", "password": "p", "api_key": "k",
        "base_url": "https://x/v1", "model": "m",
        "homework_submit": True, "homework": {"allow_force_submit": True},
    }), encoding="utf-8")

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    configure_ui_font(app)
    editor = ConfigEditor(cfg)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("configEditor", editor)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "ConfigEditor.qml")))
    if not engine.rootObjects():
        log("FAIL  QML 未加载")
        return 1
    root = engine.rootObjects()[0]

    def run():
        nonlocal ok
        try:
            root.setProperty("allEditing", True)
            root.setProperty("drafts", {"username": "abc123"})
            button = root.contentItem().findChild(object, "saveButton") or root.findChild(object, "saveButton")
            log(f"saveButton 找到: {button is not None}")
            clicked = QMetaObject.invokeMethod(button, "click", Qt.DirectConnection)
            log(f"click 调用结果: {clicked}")
            data = json.loads(cfg.read_text(encoding="utf-8"))
            ok &= check("批量编辑写入 username=abc123", data.get("username") == "abc123",
                        data.get("username"))
            ok &= check("布尔项仍保留", data.get("homework_submit") is True)
            editor.saveOne("homework_submit", "false")
            data = json.loads(cfg.read_text(encoding="utf-8"))
            ok &= check("布尔项可单独切换为 false", data.get("homework_submit") is False)
        except Exception:
            import traceback
            log("脚本异常：")
            log(traceback.format_exc())
            ok = False
        finally:
            app.quit()

    QTimer.singleShot(600, run)
    app.exec()

    ok = ok and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_config_ui.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())