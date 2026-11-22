"""配置读写往返回归（纯标准库 + 临时文件，不依赖 Qt）。

验收任务 A：保存一次配置后下次启动不崩、布尔不被字符串化、target_accuracy 是 int。
日志：logs/check_config_roundtrip.log（UTF-8）。
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG = []


def log(message=""):
    LOG.append(str(message))


def check(name, condition, detail=""):
    log(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(condition)


def main():
    ok = True
    try:
        import UnipusAI_Helper as U
        from unipus_helper import native_ui as ui

        tmp = Path(tempfile.mkdtemp(prefix="cfgcheck_"))
        target = tmp / "config.json"

        log("=== 1. 默认配置往返 ===")
        data = dict(ui.DEFAULT_CONFIG)
        data.update({
            "username": "13800000000", "password": "pw", "api_key": "sk-test",
            "base_url": "https://example.com/v1", "model": "deepseek-v4.1-flash",
            "target_accuracy": 60, "homework_submit": False,
        })
        ui.save_config(target, data)
        saved = json.loads(target.read_text(encoding="utf-8"))
        check("homework 仍是 dict", isinstance(saved.get("homework"), dict), f"got={type(saved.get('homework'))}")
        check("allow_force_submit 默认 True", saved["homework"].get("allow_force_submit") is True)
        check("homework_submit 是 bool False", saved.get("homework_submit") is False,
              f"got={saved.get('homework_submit')!r}")
        check("target_accuracy 是 int 60", saved.get("target_accuracy") == 60
              and isinstance(saved.get("target_accuracy"), int))

        loaded = ui.load_config(target)
        check("load_config 后 homework 仍是 dict", isinstance(loaded.get("homework"), dict))
        config = U.Config.from_json(str(target))
        check("Config.from_json 不抛异常", config is not None)
        check("from_json homework_submit=False", config.homework_submit is False)
        check("from_json target_accuracy=60", config.target_accuracy == 60)

        log()
        log("=== 2. 手工写坏的 config（homework 是字符串）===")
        bad = tmp / "bad.json"
        payload = dict(saved)
        payload["homework"] = "False"
        payload["homework_submit"] = "False"
        bad.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        config = U.Config.from_json(str(bad))
        check("坏的 homework 不崩", config is not None)
        check("坏 homework → allow_force_submit=False", config.allow_force_submit is False)
        check("字符串 False → homework_submit=False", config.homework_submit is False)

        log()
        log("=== 3. target_accuracy 宽松解析 ===")
        for raw, expect in (("abc", 85), (200, 100), (-5, 0), ("61", 61)):
            item = dict(saved)
            item["target_accuracy"] = raw
            path = tmp / "acc.json"
            path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
            got = U.Config.from_json(str(path)).target_accuracy
            check(f"target_accuracy={raw!r} → {expect}", got == expect, f"got={got}")

        log()
        log("=== 4. 单字段校验（saveOne 的校验函数）===")
        check("target_accuracy=999 被拒", ui.validate_field("target_accuracy", "999")[1] is not None)
        check("target_accuracy=61 通过且为 int", ui.validate_field("target_accuracy", "61") == (61, None))
        check("temperature=3 被拒", ui.validate_field("temperature", "3")[1] is not None)
        check("homework_submit='False' → False",
              ui.validate_field("homework_submit", "False") == (False, None))
        check("allow_force_submit='true' → True",
              ui.validate_field("homework.allow_force_submit", "true") == (True, None))

        log()
        log("=== 5. ConfigEditor.saveOne 写盘 ===")
        editor_path = tmp / "editor.json"
        editor_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        editor = ui.ConfigEditor(editor_path)
        check("saveOne 保存 target_accuracy=61", editor.saveOne("target_accuracy", "61") is True)
        on_disk = json.loads(editor_path.read_text(encoding="utf-8"))
        check("落盘是 int 61", on_disk.get("target_accuracy") == 61
              and isinstance(on_disk.get("target_accuracy"), int))
        check("saveOne 保存 allow_force_submit=True",
              editor.saveOne("homework.allow_force_submit", "true") is True)
        on_disk = json.loads(editor_path.read_text(encoding="utf-8"))
        check("homework 仍是对象且值为 True",
              isinstance(on_disk.get("homework"), dict)
              and on_disk["homework"].get("allow_force_submit") is True)
        check("saveOne 拒绝非法值 999", editor.saveOne("target_accuracy", "999") is False)
        check("拒绝时不写盘", json.loads(editor_path.read_text(encoding="utf-8"))["target_accuracy"] == 61)
    except Exception:
        log("脚本异常：")
        log(traceback.format_exc())
        ok = False

    ok = ok and all(line.startswith("PASS") for line in LOG if line.startswith(("PASS", "FAIL")))
    log()
    log("结果：" + ("全部通过" if ok else "存在失败项"))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "check_config_roundtrip.log").write_text("\n".join(LOG), encoding="utf-8")
    print("\n".join(LOG[-3:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())