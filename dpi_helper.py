"""高 DPI 支持。

必须在创建 QApplication 之前调用 enable_high_dpi()，否则 Qt 已经按
非 DPI 感知模式初始化，之后再设置无效。

问题背景：4K 屏 + 200% 缩放时，如果进程没有声明 DPI 感知，Windows 会把
3840x2160 的桌面缩放到 1920x1080 再交给程序，程序按 96 DPI 排版，
结果就是界面特别小、字发虚。
"""

_initialized = False


def enable_high_dpi():
    """开启 Windows / Qt 的高 DPI 感知。重复调用安全。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    # ---- 第一步：让进程声明 Per-Monitor DPI 感知（必须在 Qt 之前）----
    try:
        import ctypes
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        # 先试 V2（Win10 1703+），失败再退 PER_MONITOR_AWARE(2) / SYSTEM_AWARE(1)
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    # ---- 第二步：Qt 的高 DPI 缩放开关（必须在 QApplication 之前）----
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication

        # Qt5.14+ 默认开启，显式设置兼容旧版
        for attribute in ('AA_EnableHighDpiScaling', 'AA_UseHighDpiPixmaps'):
            flag = getattr(Qt, attribute, None)
            if flag is not None:
                QApplication.setAttribute(flag, True)

        # 125%/150% 这类非整数缩放时，按实际缩放比例取整，避免字体边缘模糊
        rounding = getattr(Qt, 'HighDpiScaleFactorRoundingPolicy', None)
        if rounding is not None:
            policy = getattr(rounding, 'PassThrough', None)
            if policy is not None:
                try:
                    QApplication.setHighDpiScaleFactorRoundingPolicy(policy)
                except Exception:
                    pass
    except Exception:
        pass
