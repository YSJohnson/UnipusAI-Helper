"""云作业（homework）支持：列表扫描、打开、提交。

列表走「读 DOM」这条路：先把浏览器带到「我的作业」页面，再读卡片上的文字，
和课程目录扫描是同一套做法。这样不依赖手写接口请求的鉴权细节（页面自身
会用自己的会话把数据拉下来），也不会因为少一个 header 就整片读不到。
"""

import re
import time

from selenium.webdriver.common.by import By

HOMEWORK_LIST_URL = "https://ucloud.unipus.cn/app/homework/my-homework"

# 页面用三个页签分状态，只读当前页签会漏掉已交/批阅中的作业
TABS = (("待作答", 0), ("批阅中", 1), ("已批阅", 2))

CARD_SELECTOR = ".ucloud-work-card"
PRIMARY_BUTTON = ".ucloud-work-btn-primary"

CARD_JS = """
    const card = arguments[0];
    const buttons = [...card.querySelectorAll('button')]
        .filter(btn => btn.offsetParent !== null)
        .map(btn => (btn.innerText || '').replace(/\\s+/g, ' ').trim())
        .filter(Boolean);
    return {
        text: (card.innerText || '').replace(/\\n{2,}/g, '\\n'),
        buttons
    };
"""

CLICK_BY_TEXT_JS = """
    const text = arguments[0];
    const nodes = [...document.querySelectorAll('button, a, span, div')]
        .filter(el => el.offsetParent !== null && (el.innerText || '').trim() === text);
    if (!nodes.length) return false;
    const target = nodes.sort((a, b) => a.children.length - b.children.length)[0];
    (target.closest('button, a') || target).click();
    return true;
"""


def _line_value(lines, *prefixes):
    for line in lines:
        for prefix in prefixes:
            if line.startswith(prefix):
                return line[len(prefix):].strip(" ：:，,")
    return ""


def _visible_cards(driver):
    """只取当前页签里真正显示出来的卡片；三个页签的面板会同时留在 DOM 里。"""
    cards = []
    for card in driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR):
        try:
            if card.is_displayed():
                cards.append(card)
        except Exception:
            continue
    return cards


def wait_for_list(driver, timeout=25.0):
    """等作业列表渲染出来（有卡片，或明确显示「暂无作业」）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR):
                return True
            empty = driver.find_elements(By.XPATH, "//*[contains(text(), '暂无作业')]")
            if empty:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def scan_homework(driver, timeout=25.0):
    """逐个页签读取「我的作业」：待作答 / 批阅中 / 已批阅。"""
    driver.get(HOMEWORK_LIST_URL)
    if not wait_for_list(driver, timeout):
        raise RuntimeError("作业列表没有加载出来，请确认已登录后再试。")
    works = []
    for label, status in TABS:
        if label != TABS[0][0]:
            if not click_by_text(driver, label):
                continue
            time.sleep(1.2)
            wait_for_list(driver, 10)
        for position, card in enumerate(_visible_cards(driver)):
            try:
                raw = driver.execute_script(CARD_JS, card) or {}
            except Exception:
                continue
            lines = [line.strip() for line in (raw.get("text") or "").split("\n") if line.strip()]
            buttons = raw.get("buttons") or []
            can_answer = any("继续作答" in text or text == "作答" for text in buttons)
            works.append({
                "tab": label,
                "position": position,
                "name": lines[0] if lines else "云作业",
                "work_type": _line_value(lines, "作业类型") or next(
                    (line for line in lines[1:4]
                     if line in ("课后作业", "课前作业", "测验", "考试")), ""),
                "mode": _line_value(lines, "作答方式") or next(
                    (line for line in lines[1:5] if line in ("个人作答", "小组作答")), ""),
                "course": _line_value(lines, "发布课程"),
                "teacher": _line_value(lines, "发布教师"),
                "end_text": _line_value(lines, "提交截止"),
                "submit_text": _line_value(lines, "提交时间"),
                "status": status,
                "status_text": label,
                "can_answer": can_answer and status == 0,
                "buttons": buttons,
            })
    return works


def open_work(driver, work, timeout=30.0):
    """切到作业所在的页签，点开对应卡片的「继续作答」，等题目容器出现。"""
    driver.get(HOMEWORK_LIST_URL)
    if not wait_for_list(driver):
        return False
    tab = work.get("tab") or "待作答"
    if tab != TABS[0][0] and not click_by_text(driver, tab):
        return False
    if tab != TABS[0][0]:
        time.sleep(1.2)
        wait_for_list(driver, 10)
    cards = _visible_cards(driver)
    position = int(work.get("position") or 0)
    if position >= len(cards):
        return False
    card = cards[position]
    buttons = card.find_elements(By.CSS_SELECTOR, PRIMARY_BUTTON) or card.find_elements(By.TAG_NAME, "button")
    if not buttons:
        return False
    before = driver.window_handles
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", buttons[0])
    time.sleep(0.3)
    buttons[0].click()
    time.sleep(2.5)
    handles = driver.window_handles
    if len(handles) > len(before):
        driver.switch_to.window(handles[-1])
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if driver.find_elements(By.CSS_SELECTOR, ".question-abs-question"):
                time.sleep(1.0)
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def click_by_text(driver, text: str) -> bool:
    try:
        return bool(driver.execute_script(CLICK_BY_TEXT_JS, text))
    except Exception:
        return False


def submit_work(driver, wait: float = 2.0) -> bool:
    """点「提交」，再确认「确定」。作业只能提交一次，调用前要想清楚。"""
    if not click_by_text(driver, "提 交") and not click_by_text(driver, "提交"):
        return False
    time.sleep(wait)
    if click_by_text(driver, "确 定") or click_by_text(driver, "确定"):
        time.sleep(wait)
    return True