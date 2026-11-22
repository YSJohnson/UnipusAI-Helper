"""云作业（homework）支持：列表扫描、打开、提交。

列表走「读 DOM」这条路：先把浏览器带到「我的作业」页面，再读卡片上的文字，
和课程目录扫描是同一套做法。这样不依赖手写接口请求的鉴权细节（页面自身
会用自己的会话把数据拉下来），也不会因为少一个 header 就整片读不到。
"""

import re
import time

from selenium.webdriver.common.by import By

from .window_guard import ensure_alive_window

HOMEWORK_LIST_URL = "https://ucloud.unipus.cn/app/homework/my-homework"

# 页面用三个页签分状态，只读当前页签会漏掉已交/批阅中的作业
TABS = (("待作答", 0), ("批阅中", 1), ("已批阅", 2))

CARD_SELECTOR = ".ucloud-work-card"
PRIMARY_BUTTON = ".ucloud-work-btn-primary"

CARD_JS = """
    const card = arguments[0];
    const buttons = [...card.querySelectorAll('button, a, [class*="ucloud-work-btn"]')]
        .filter(el => el.offsetParent !== null)
        .map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim())
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


def _parse_score(lines):
    """从卡片文字里读分数：支持「86 分」「得分：86」「成绩 86」几种写法。"""
    for line in lines:
        match = re.search(r'(?:得分|成绩)\s*[：:]?\s*([0-9]+(?:\.[0-9]+)?)', line)
        if match:
            return float(match.group(1))
        match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*分', line)
        if match:
            return float(match.group(1))
    return None


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
    ensure_alive_window(driver)
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
            "score": _parse_score(lines),
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
    # 扫描会把页面停在「已批阅」页签，这里必须无条件点回目标页签，
    # 否则可见卡片是别的页签的，position 会指到错误的作业。
    if not click_by_text(driver, tab):
        return False
    time.sleep(1.4)
    wait_for_list(driver, 10)
    cards = _visible_cards(driver)
    position = int(work.get("position") or 0)
    if position >= len(cards):
        return False
    card = cards[position]
    buttons = (card.find_elements(By.CSS_SELECTOR, PRIMARY_BUTTON)
               or card.find_elements(By.CSS_SELECTOR, '[class*="ucloud-work-btn"]')
               or card.find_elements(By.TAG_NAME, "button"))
    if not buttons:
        print("[homework] 卡片上没有可点的按钮")
        return False
    try:
        print(f"[homework] 打开卡片 {position}: {(card.text or '').replace(chr(10), ' ')[:50]}")
    except Exception:
        pass
    before = driver.window_handles
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", buttons[0])
    time.sleep(0.4)
    try:
        buttons[0].click()
    except Exception as exc:
        print(f"[homework] 原生点击失败，改用 JS 点击: {str(exc)[:60]}")
        driver.execute_script("arguments[0].click();", buttons[0])
    time.sleep(3.0)
    handles = driver.window_handles
    if len(handles) > len(before):
        driver.switch_to.window(handles[-1])
        # 新窗口有时先是 about:blank，等它自己导航过去
        for _ in range(12):
            try:
                if not (driver.current_url or "").startswith("about:"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        print(f"[homework] 新窗口: {driver.current_url[:90]}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if driver.find_elements(By.CSS_SELECTOR, ".question-abs-question"):
                time.sleep(1.0)
                return True
            url = driver.current_url or ""
            if "login" in url or "sso" in url:
                print(f"[homework] 被弹回登录页: {url[:90]}")
                return False
        except Exception:
            pass
        time.sleep(0.6)
    try:
        print(f"[homework] 等题目超时，当前地址: {driver.current_url[:90]}")
    except Exception:
        pass
    return False


def click_by_text(driver, text: str) -> bool:
    try:
        return bool(driver.execute_script(CLICK_BY_TEXT_JS, text))
    except Exception:
        return False


UNANSWERED_JS = """
    const placeholder = el => {
        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        return !text || text === '点击选择';
    };
    const dropdowns = [...document.querySelectorAll('.scoop-select-wrapper')]
        .filter(placeholder).length;
    // 真实页面的填空 input 没有 type 属性（024 快照里 input[type="text"] 命中 0），
    // 用 input[type="text"] 会把段落匹配/填空题整块漏掉，空卷也会被判「填完了」。
    const seen = new Set();
    document.querySelectorAll(
        '.question-abs-question input:not([type="hidden"]):not([type="radio"])' +
        ':not([type="checkbox"]):not([type="file"])'
    ).forEach(el => seen.add(el));
    // 并集去重：段匹配/填空的输入框在 .comp-abs-input 里，单独再收一遍更稳
    document.querySelectorAll('.comp-abs-input input').forEach(el => seen.add(el));
    const inputs = [...seen].filter(el => !(el.value || '').trim()).length;
    return {dropdowns, inputs, total: dropdowns + inputs};
"""


def unanswered_detail(driver) -> dict:
    """作业页空位明细：{dropdowns, inputs, total}。"""
    try:
        parts = driver.execute_script(UNANSWERED_JS) or {}
    except Exception:
        parts = {}
    dropdowns = int(parts.get("dropdowns") or 0)
    inputs = int(parts.get("inputs") or 0)
    return {"dropdowns": dropdowns, "inputs": inputs, "total": dropdowns + inputs}


def unanswered_count(driver):
    """作业页上还剩多少个空没填（下拉仍显示「点击选择」或输入框为空）。"""
    return unanswered_detail(driver)["total"]


def submit_work(driver, wait: float = 2.0, allow_incomplete: bool = False,
                work: dict | None = None, verify_timeout: float = 60.0) -> bool:
    """点「提交」→「确定」，并回读作业列表确认真的提交成功。

    作业只能提交一次，所以这里：① 同一个 driver 只提交一次；② 有空位时不提交；
    ③ 提交后必须从「待作答」里消失才算成功，否则打印「提交未确认」。
    """
    if getattr(driver, "_dsh_homework_submitted", False):
        print("    本次运行已经提交过作业，跳过重复提交")
        return True
    detail = unanswered_detail(driver)
    left = detail["total"]
    if left and not allow_incomplete:
        print(f"    还有 {left} 个空没填（下拉 {detail['dropdowns']} / "
              f"输入 {detail['inputs']}），先不提交（提交后无法修改）")
        return False
    if not click_by_text(driver, "提 交") and not click_by_text(driver, "提交"):
        return False
    time.sleep(wait)
    if click_by_text(driver, "确 定") or click_by_text(driver, "确定"):
        time.sleep(wait)
    try:
        setattr(driver, "_dsh_homework_submitted", True)
    except Exception:
        pass
    if work is None:
        ensure_alive_window(driver)
        return True
    return _confirm_submitted(driver, work, verify_timeout)


def _work_in_current_dom(driver, name: str) -> bool:
    """只读当前 DOM 判断是否已提交，避免每轮都整页刷新 + 点三个页签。"""
    try:
        text = (driver.execute_script(
            "return (document.body && document.body.innerText) || '';") or "")
    except Exception:
        return False
    if name not in text:
        return False
    return ("提交时间" in text) or ("已批阅" in text) or ("批阅中" in text)


def _confirm_submitted(driver, work: dict, timeout: float) -> bool:
    """回读确认提交：前 10 秒只读当前 DOM，之后整页扫描最多 3 次 / 总轮数 5。"""
    name = (work.get("name") or "").strip()
    if not name:
        return True
    deadline = time.time() + timeout
    scans = 0
    for round_index in range(5):
        if time.time() >= deadline:
            break
        time.sleep(10.0 if round_index == 0 else 6.0)
        if time.time() > deadline:
            break
        if round_index == 0:
            if _work_in_current_dom(driver, name):
                print(f"    提交已确认（当前页面）：{name}")
                return True
            continue
        if scans >= 3:
            break
        scans += 1
        try:
            works = scan_homework(driver)
        except Exception as exc:
            print(f"    回读作业列表失败: {str(exc)[:80]}")
            continue
        for item in works:
            if (item.get("name") or "").strip() != name:
                continue
            if item.get("status") != 0:
                print(f"    提交已确认：{name} → {item.get('status_text')}")
                return True
    print(f"    提交未确认，请人工检查：{name}")
    return False
