"""云作业（homework）支持：列表扫描、打开、提交。

和目录扫描一样，所有请求都在页面上下文里发出，复用浏览器自己的 cookie/JWT，
不引入任何额外的网络客户端。
"""

import time
from urllib.parse import quote

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys  # noqa: F401  (保留给后续快捷键扩展)

STATUS_NAMES = {0: "待作答", 1: "批阅中", 2: "已批阅"}

# 作业详情页：平台自己就是这么拼的（末尾的逗号是它拼串时留下的，去掉会进不去）
WORK_URL_TEMPLATE = (
    "https://ucloud.unipus.cn/app/homework/my-homework/do/{work_id}"
    "?name={name}&workType={work_type}&versionId={version_id}&from=home-work&umsiv=0,"
)

SCAN_HOMEWORK_JS = """
    const statuses = arguments[0];
    const done = arguments[arguments.length - 1];
    if (location.origin !== 'https://ucloud.unipus.cn') {
        done({items: [], failed: true});
        return;
    }
    const read = key => {
        try { return JSON.parse(localStorage.getItem(key) || '{}') || {}; }
        catch (_) { return {}; }
    };
    const legacy = read('__token'), portal = read('PORTAL_STATE_PERSISTENT');
    const jwt = legacy.jwt || portal.Authorization || (portal.tokenInfo || {}).jwt;
    const headers = {'Content-Type': 'application/json'};
    if (jwt) headers.Authorization = jwt;
    (async () => {
        const items = [];
        try {
            for (const status of statuses) {
                for (let page = 1; page <= 10; page += 1) {
                    const response = await fetch('/api/hwucs/student/work/list', {
                        method: 'POST', headers, credentials: 'same-origin',
                        body: JSON.stringify({pageNum: page, pageSize: 50, status})
                    });
                    if (!response.ok) break;
                    const body = await response.json();
                    if (!body || body.code !== 1 || !body.value) break;
                    const list = body.value.dataList || [];
                    list.forEach(work => items.push(Object.assign({status}, work)));
                    const total = body.value.total || 0;
                    const seen = items.filter(work => work.status === status).length;
                    if (!list.length || seen >= total) break;
                }
            }
            done({items, failed: false});
        } catch (error) {
            done({items, failed: true, error: String(error)});
        }
    })();
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


def work_url(work: dict) -> str:
    return WORK_URL_TEMPLATE.format(
        work_id=work.get("id", ""),
        name=quote(str(work.get("name") or ""), safe=""),
        work_type=work.get("workType", 2) or 2,
        version_id=work.get("versionId", 1) or 1,
    )


def scan_homework(driver, statuses=(0, 1, 2)):
    """读取作业列表（待作答 / 批阅中 / 已批阅）。"""
    payload = driver.execute_async_script(SCAN_HOMEWORK_JS, list(statuses)) or {}
    works = []
    for raw in payload.get("items") or []:
        status = raw.get("status")
        works.append({
            "id": str(raw.get("id") or ""),
            "name": (raw.get("name") or "").strip(),
            "course": (raw.get("courseName") or "").strip(),
            "teacher": (raw.get("teacherName") or "").strip(),
            "work_type": raw.get("workType"),
            "status": status,
            "status_text": STATUS_NAMES.get(status, "未知"),
            "can_answer": raw.get("canAnswer") == 1,
            "submit_status": raw.get("submitStatus"),
            "score": raw.get("score"),
            "end_time": raw.get("endTime") or 0,
            "url": work_url(raw),
        })
    return works


def open_work(driver, work: dict, timeout: float = 30.0) -> bool:
    """在同一个标签页打开作业详情，等到题目容器出现。"""
    driver.get(work["url"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if driver.find_elements(By.CSS_SELECTOR, ".question-abs-question"):
                time.sleep(1.0)
                return True
        except Exception:
            pass
        time.sleep(0.8)
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