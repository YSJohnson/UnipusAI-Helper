"""Browser window guards shared by course/homework flows."""


def ensure_alive_window(driver):
    """切回一个可用的窗口句柄。

    作业提交后详情页会自己关掉，driver 会停在失效句柄上，后续任何操作都会抛
    no such window。这里按句柄列表逐个试，找到第一个还能读到 URL 的窗口。
    """
    if driver is None:
        return False
    try:
        driver.current_url
        return True
    except Exception:
        pass
    try:
        handles = list(driver.window_handles)
    except Exception:
        return False
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            driver.current_url
            return True
        except Exception:
            continue
    return False


def window_failure_hint() -> str:
    return "浏览器窗口已关闭，已尝试切回主窗口；若仍失败请重启浏览器。"