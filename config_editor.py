# -*- coding: utf-8 -*-
"""
UnipusAI 配置文件编辑器
用法: python config_editor.py
"""
import json, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import customtkinter as ctk
from tkinter import messagebox

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "url": "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome",
    "api_key": "",
    "base_url": "",
    "model": "",
    "max_tokens": 4096,
    "temperature": 0.3,
    "whisper_api": None,
    "debug_mode": False,
}


def load_config():
    """加载现有配置, 不存在则返回默认值"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # 合并默认值, 保证所有 key 存在
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(data: dict):
    """保存配置, token 字段自动转义内部双引号"""
    # 深拷贝
    out = dict(data)

    # token_full: 自动转义（json.dump 会自动处理内部双引号转义）
    token = out.get("token_full", "")
    if token and isinstance(token, str):
        token = token.strip()
        # 去掉控制台复制时带上的外层引号 '...' 或 "..."
        if (token.startswith("'") and token.endswith("'")) or \
           (token.startswith('"') and token.endswith('"')):
            token = token[1:-1].strip()
        # json.dump 会自动对内部 " 做转义，无需手动处理
        out["token_full"] = token

    # 删除 config.json 不需要的字段
    out.pop("learning_strategy", None)
    out.pop("timeout", None)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return True


# ── GUI ──
class ConfigEditor:
    def __init__(self):
        ctk.set_appearance_mode("system")
        self.root = ctk.CTk()
        self.root.title("UnipusAI 配置编辑器")
        self.root.geometry("620x720")
        self.root.minsize(550, 650)
        self.root.configure(fg_color=("#f2f1ed", "#1a1915"))

        self.cfg = load_config()
        self.entries = {}

        # 标题
        title = ctk.CTkLabel(
            self.root, text="UnipusAI 配置编辑器",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=("#26251e", "#e6e5e0")
        )
        title.pack(pady=(24, 4))

        sub = ctk.CTkLabel(
            self.root, text="编辑下方字段后点击[保存配置]",
            font=ctk.CTkFont(size=12),
            text_color=("#82817a", "#8c8b87")
        )
        sub.pack(pady=(0, 20))

        # 滚动区域
        scroll = ctk.CTkScrollableFrame(
            self.root, fg_color="transparent",
            width=560, height=520
        )
        scroll.pack(fill="both", expand=True, padx=30)

        # 字段定义: (key, label, placeholder, width, height, is_password, is_multiline)
        fields = [
            ("username", "账号", "输入U校园账号", 280, 36, False, False),
            ("password", "密码", "输入U校园密码", 280, 36, False, False),
            ("url", "登录地址", DEFAULT_CONFIG["url"], 500, 36, False, False),
            ("api_key", "API Key", "sk-xxxxxxxx", 500, 36, False, False),
            ("base_url", "API 地址", "https://api.deepseek.com", 500, 36, False, False),
            ("model", "模型名称", "deepseek-chat", 280, 36, False, False),
            ("max_tokens", "最大 Token 数", "4096", 150, 36, False, False),
            ("temperature", "温度 (0-2)", "0.3", 150, 36, False, False),
            ("token_full", "Token (反作弊)", "从浏览器控制台获取: localStorage.getItem('__token')", 500, 80, False, True),
        ]

        for i, (key, label, ph, w, h, is_pw, is_ml) in enumerate(fields):
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=(0, 14))

            lbl = ctk.CTkLabel(
                row, text=label,
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color=("#26251e", "#e6e5e0"),
                width=120, anchor="w"
            )
            lbl.pack(side="left", padx=(0, 8))

            if is_ml:
                entry = ctk.CTkTextbox(row, height=h, font=ctk.CTkFont(size=12),
                                        fg_color=("#ffffff", "#111110"),
                                        text_color=("#26251e", "#e6e5e0"),
                                        wrap="word")
                val = self.cfg.get(key, "")
                if val:
                    entry.insert("1.0", str(val))
                entry.pack(side="left", fill="x", expand=True)
            else:
                entry = ctk.CTkEntry(row, width=w, height=h,
                                      font=ctk.CTkFont(size=13),
                                      fg_color=("#ffffff", "#111110"),
                                      text_color=("#26251e", "#e6e5e0"))
                val = self.cfg.get(key, "")
                if val is not None and val != "":
                    entry.insert(0, str(val))
                entry.pack(side="left", fill="x", expand=True)

            self.entries[key] = entry

        # whisper_api (nullable)
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(row, text="Whisper API",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=("#26251e", "#e6e5e0"), width=120, anchor="w"
                     ).pack(side="left", padx=(0, 8))
        entry = ctk.CTkEntry(row, width=500, height=36, font=ctk.CTkFont(size=13),
                              fg_color=("#ffffff", "#111110"),
                              text_color=("#82817a", "#8c8b87"))
        entry.insert(0, "(留空使用本地语音模型)")
        val = self.cfg.get("whisper_api")
        if val:
            entry.delete(0, "end")
            entry.insert(0, str(val))
        entry.pack(side="left", fill="x", expand=True)
        self.entries["whisper_api"] = entry

        # debug_mode checkbox
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(row, text="调试模式",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=("#26251e", "#e6e5e0"), width=120, anchor="w"
                     ).pack(side="left", padx=(0, 8))
        self.debug_var = ctk.BooleanVar(value=self.cfg.get("debug_mode", False))
        ctk.CTkCheckBox(row, text="开启(输出完整API请求/响应)", variable=self.debug_var,
                         font=ctk.CTkFont(size=12),
                         text_color=("#26251e", "#e6e5e0"),
                         fg_color=("#f54e00", "#f54e00")
                         ).pack(side="left")
        self.entries["debug_mode"] = self.debug_var

        # ── 底部按钮 ──
        btn_row = ctk.CTkFrame(self.root, fg_color="transparent")
        btn_row.pack(pady=20)

        ctk.CTkButton(
            btn_row, text="保存配置",
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color=("#f54e00", "#f54e00"),
            hover_color=("#d94400", "#d94400"),
            text_color=("#ffffff", "#ffffff"),
            corner_radius=8,
            height=48,
            command=self._on_save
        ).pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            btn_row, text="关闭",
            font=ctk.CTkFont(size=14),
            fg_color=("#ebeae5", "#33322a"),
            text_color=("#26251e", "#e6e5e0"),
            hover_color=("#e1e0db", "#3d3c33"),
            corner_radius=8,
            height=48,
            command=self.root.destroy
        ).pack(side="left")

        self.status_label = ctk.CTkLabel(
            self.root, text="",
            font=ctk.CTkFont(size=12),
            text_color=("#1f8a65", "#2fba8a")
        )
        self.status_label.pack(pady=(0, 16))

    def _on_save(self):
        """收集字段值并保存"""
        data = {}
        for key, entry in self.entries.items():
            if key == "debug_mode":
                data[key] = entry.get()
            elif isinstance(entry, ctk.CTkTextbox):
                data[key] = entry.get("1.0", "end-1c").strip()
            else:
                data[key] = entry.get().strip()

        # 处理空的 whisper_api
        if data.get("whisper_api") == "(留空使用本地语音模型)" or not data.get("whisper_api"):
            data["whisper_api"] = None
        else:
            ws = data["whisper_api"]
            if ws.lower() == "null" or ws.lower() == "none":
                data["whisper_api"] = None

        # max_tokens 转 int
        try:
            data["max_tokens"] = int(data["max_tokens"])
        except:
            data["max_tokens"] = 4096

        # temperature 转 float
        try:
            data["temperature"] = float(data["temperature"])
        except:
            data["temperature"] = 0.3

        # 保存
        try:
            save_config(data)
            self.status_label.configure(text="[OK] 配置已保存到 config.json")
            try:
                import winsound
                winsound.MessageBeep()
            except:
                pass
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            self.status_label.configure(text=f"[FAIL] 保存失败: {e}",
                                         text_color=("#cf2d56", "#e04a6f"))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    editor = ConfigEditor()
    editor.run()
