import re
import time
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .task_grading import (READ_GRADING_JS, assign_task_records, build_record_index,
                           build_unit_names, grading_text, score_text)


def describe_directory_page_state(driver) -> dict:
    fallback_url = ''
    fallback_title = ''
    try:
        raw_url = getattr(driver, 'current_url', '')
        if isinstance(raw_url, str):
            fallback_url = raw_url
        raw_title = getattr(driver, 'title', '')
        if isinstance(raw_title, str):
            fallback_title = raw_title
    except Exception:
        pass
    try:
        res = driver.execute_script("""
            const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
            const vis = el => !!el && !!el.getClientRects().length;
            return {
                url: location.href || '',
                title: document.title || '',
                ready_state: document.readyState || '',
                body_head: clean(document.body && document.body.innerText).slice(0, 80),
                unit_count: document.querySelectorAll('[class*="unitTabScrollContainer"] > *').length,
                task_count: [...document.querySelectorAll(
                    '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]')].filter(vis).length,
                spinning: [...document.querySelectorAll('.ant-spin-spinning, [aria-busy="true"]')].some(vis)
            };
        """)
        if isinstance(res, dict):
            return res
    except Exception:
        pass
    return {
        'url': fallback_url,
        'title': fallback_title,
        'ready_state': '',
        'body_head': '',
        'unit_count': 0,
        'task_count': 0,
        'spinning': False,
    }


def _wait_unit_statuses_settled(driver, target, state_script, stop_requested=None,
                                timeout: float = 3.0):
    deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
    while time.monotonic() < deadline:
        if stop_requested is not None:
            if stop_requested.is_set() or stop_requested.wait(0.1):
                raise TimeoutException('单元切换已停止')
        else:
            time.sleep(0.1)
        try:
            state = driver.execute_script(state_script, target)
        except Exception:
            return
        if not isinstance(state, dict):
            return
        if state.get('ready') and not state.get('pendingStatus'):
            return


def select_course_unit(driver, target, stop_requested=None,
                       timeout: float = 12.0,
                       stable_same_signature_sec: float = 2.5,
                       settle_timeout: float = 0.0):
    if stop_requested is not None and stop_requested.is_set():
        raise TimeoutException('单元切换已停止')
    state_script = """
        const tab = arguments[0];
        const visible = el => !!el && !!el.getClientRects().length;
        const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
        const cls = String(tab.className);
        const selected = tab.getAttribute('aria-selected');
        const active = selected === 'true' || (selected !== 'false' &&
            (/tabactive|slideractive/i.test(cls) ||
             !!tab.querySelector('[class*="sliderActive"]') ||
             /(?:^|[\\s_-])(active|selected|current)(?:$|[\\s_-])/i.test(cls)));
        const panelId = tab.getAttribute('aria-controls');
        const panel = panelId ? document.getElementById(panelId) :
            [...document.querySelectorAll('[aria-labelledby]')].find(
                el => tab.id && el.getAttribute('aria-labelledby') === tab.id);
        const root = panel || document;
        const tasks = [...root.querySelectorAll(
            '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]')].filter(visible);
        const headers = [...root.querySelectorAll('.ant-collapse-header, [class*="unitTitle"], [class*="unitName"]')]
            .filter(el => visible(el) && !el.closest('[class*="unitTabScrollContainer"]'))
            .map(el => clean(el.innerText));
        const loading = [...root.querySelectorAll('.ant-spin-spinning, [aria-busy="true"]')].some(visible);
        const statuses = tasks.map(el => {
            const tip = el.querySelector(
                '[class*="nodePassStateTip"], [class*="taskPassStateTip"], [class*="PassStateTip"], [class*="passStateTip"]'
            ) || el.querySelector('[class*="taskStateContainer"], [class*="nodeStateContainer"]');
            return clean(tip && tip.innerText);
        });
        const isPlaceholderStatus = s => !s || /^(已锁定|加载中\\.{0,3}|loading\\.{0,3}|locked)$/i.test(s);
        const pendingStatus = statuses.length > 0 && statuses.every(isPlaceholderStatus);
        const taskSig = tasks.map(el => {
            const ds = el.dataset ? JSON.stringify(el.dataset) : '';
            return ds + '|' + el.innerHTML;
        }).join('\\n');
        return {active, linked: !!panel, ready: (!panel || visible(panel)) && !loading && tasks.length > 0,
                loading, taskCount: tasks.length, pendingStatus,
                signature: headers.join('|') + '\\n' + taskSig};
    """
    before = driver.execute_script(state_script, target)
    previous = driver.find_elements(By.CSS_SELECTOR, CourseCatalogMixin.TASK_ITEM_SELECTOR)
    driver.execute_script("arguments[0].click();", target)

    stable_since = None
    last_state = None

    def ready(_):
        nonlocal stable_since, last_state
        if stop_requested is not None and stop_requested.is_set():
            raise TimeoutException('单元切换已停止')
        state = driver.execute_script(state_script, target)
        last_state = state
        if not state['active'] or not state['ready']:
            stable_since = None
            return False
        if before['active'] or state['linked'] or state['signature'] != before['signature']:
            return True
        all_gone = True
        for element in previous:
            try:
                if element.is_displayed():
                    all_gone = False
                    break
            except StaleElementReferenceException:
                pass
        if all_gone:
            return True
        now = time.monotonic()
        if stable_since is None:
            stable_since = now
            return False
        return (now - stable_since) >= stable_same_signature_sec

    try:
        WebDriverWait(driver, timeout).until(ready)
    except TimeoutException:
        if stop_requested is not None and stop_requested.is_set():
            raise
        if isinstance(last_state, dict):
            raise TimeoutException(
                f"切换Unit超时({timeout:g}s, active={last_state.get('active')}, "
                f"ready={last_state.get('ready')}, tasks={last_state.get('taskCount', '?')})"
            ) from None
        raise
    if settle_timeout > 0 and isinstance(last_state, dict) and last_state.get('pendingStatus'):
        _wait_unit_statuses_settled(
            driver, target, state_script, stop_requested=stop_requested, timeout=settle_timeout
        )


class CourseCatalogMixin:

    def scan_tasks(self, current_unit_only=False):
        tabs = self._scan_course_directory(current_unit_only)
        if tabs:
            self.gui_log_queue.put(f"课程目录页发现 {len(tabs)} 个任务")
            return tabs
        stop_event = self._scan_stop_event()
        if current_unit_only or (stop_event is not None and stop_event.is_set()):
            return []
        for l1_idx, l1_tab in enumerate(self.solver._get_level1_tabs() or []):
            if not self.WebDriverHelper.safe_click(self.driver, l1_tab["element"]):
                continue
            time.sleep(1.2)
            level2 = self.solver._get_level2_tabs()
            if not level2:
                tabs.append({"l1_idx": l1_idx, "l2_idx": -1,
                             "l1_title": l1_tab["title"], "l2_title": "",
                             "display": l1_tab["title"], "is_l2": False,
                             "is_compulsory": True, "status": "unknown"})
            else:
                for l2_idx, l2_tab in enumerate(level2):
                    tabs.append({"l1_idx": l1_idx, "l2_idx": l2_idx,
                                 "l1_title": l1_tab["title"], "l2_title": l2_tab["title"],
                                 "display": f"  - {l2_tab['title']}", "is_l2": True,
                                 "is_compulsory": True, "status": "unknown"})
        self.gui_log_queue.put(f"章节内部页发现 {len(tabs)} 个任务")
        return tabs

    TASK_ITEM_SELECTOR = '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]'

    READ_TASKS_JS = """
        const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
        const all = [...document.querySelectorAll(
            '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]')];
        const out = [];
        all.forEach((el, index) => {

          if (!el.getClientRects().length) return;
          const nameEl = el.querySelector('[class*="taskTypeName"], [class*="nodeTypeName"]');
          const statusEl = el.querySelector(
              '[class*="nodePassStateTip"], [class*="taskPassStateTip"], [class*="PassStateTip"], [class*="passStateTip"]'
          ) || el.querySelector('[class*="taskStateContainer"], [class*="nodeStateContainer"]');
          const requireEl = el.querySelector('[class*="taskRequireIcon"], [class*="nodeRequireIcon"]');
          const path = [];
          let p = el;
          while ((p = p.parentElement)) {
            const head = p.querySelector(':scope > .ant-collapse-header');
            if (head) {
              const text = clean(head.innerText).replace(/必修|选修/g, '').trim();
              if (text) path.unshift(text);
            }
          }
          out.push({
            index,
            name: clean(nameEl && nameEl.innerText),
            status: clean(statusEl && statusEl.innerText),
            compulsory: !!requireEl,
            section: path.join(' / ')
          });
        });
        return out;
    """

    UNIT_LABEL_JS = """
        const el = arguments[0];
        const node = el.querySelector('[class*="unitName"], [class*="richName"], [class*="name-item"]');
        const title = node && (node.getAttribute('title') || node.innerText);
        return (title || el.innerText || '').replace(/\\s+/g, ' ').trim();
    """

    UNIT_PANEL_NAME_JS = """
        const visible = el => !!el && !!el.getClientRects().length;
        const node = [...document.querySelectorAll(
            '[class*="unitNameLabel"], [class*="unitName__"], [class*="unitTitle"]')]
            .find(el => visible(el) && !el.closest('[class*="unitTabScrollContainer"]') && (el.innerText || '').trim());
        return node ? (node.innerText || '').replace(/\\s+/g, ' ').trim() : '';
    """

    VISIBLE_STATUS_PENDING_JS = """
        const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
        const visible = el => !!el && !!el.getClientRects().length;
        const isPlaceholderStatus = s => !s || /^(已锁定|加载中\\.{0,3}|loading\\.{0,3}|locked)$/i.test(s);
        if ([...document.querySelectorAll('.ant-spin-spinning, [aria-busy="true"]')].some(visible)) {
            return true;
        }
        const tasks = [...document.querySelectorAll(
            '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]')]
            .filter(visible);
        if (!tasks.length) return true;
        return tasks.every(el => {
            const tip = el.querySelector(
                '[class*="nodePassStateTip"], [class*="taskPassStateTip"], [class*="PassStateTip"], [class*="passStateTip"]'
            ) || el.querySelector('[class*="taskStateContainer"], [class*="nodeStateContainer"]');
            return isPlaceholderStatus(clean(tip && tip.innerText));
        });
    """

    def _scan_stop_event(self):
        return getattr(self, '_stop_requested', None) or getattr(getattr(self, 'solver', None), 'stop_requested', None)

    @staticmethod
    def _is_placeholder_unit_label(label: str) -> bool:
        return not re.search(r'[A-Za-z0-9\u4e00-\u9fff]', (label or '').strip())

    @staticmethod
    def _player_directory_target(route: str):
        parsed = urlsplit(route or '')
        if parsed.hostname != 'ucontent.unipus.cn':
            return '', ''
        query = parse_qs(parsed.query)
        resource = (query.get('courseResourceId') or [''])[0].strip()
        if not resource:
            callback = (query.get('sourceCallBackUrl') or [''])[0]
            if callback:
                resource = (parse_qs(urlsplit(callback).query).get('courseResourceId') or [''])[0].strip()
        if not resource.isdigit():
            return '', ''
        match = re.search(r'/courseware/([^/]+)', parsed.fragment or '')
        return resource, (match.group(1) if match else '')

    def _wait_visible_statuses_settled(self, timeout: float = 3.0, stop_requested=None) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        while True:
            if stop_requested is not None and stop_requested.is_set():
                raise TimeoutException('扫描已停止')
            try:
                pending = bool(self.driver.execute_script(self.VISIBLE_STATUS_PENDING_JS))
            except Exception:
                return
            if not pending or time.monotonic() >= deadline:
                return
            if stop_requested is not None:
                if stop_requested.wait(0.1):
                    raise TimeoutException('扫描已停止')
            else:
                time.sleep(0.1)

    def _resolve_unit_display(self, info, duplicate_label: bool = False):
        unit_display = info["display"]
        unit_is_preview = info["is_preview"]
        if not duplicate_label and not self._is_placeholder_unit_label(info.get("label", "")):
            return unit_display, unit_is_preview
        try:
            panel_name = (self.driver.execute_script(self.UNIT_PANEL_NAME_JS) or "").strip()
        except Exception:
            panel_name = ""
        if not panel_name:
            return unit_display, unit_is_preview
        if re.search(r'unit\s*([0-9]+)', panel_name, re.I):
            return self._parse_unit_label(panel_name)
        is_preview = bool(re.search(
            r'preview|intro|预备|预习|准备|starter|start\s*up|导学', panel_name, re.I
        ))
        return panel_name, is_preview

    @staticmethod
    def _parse_unit_label(label: str):
        text = (label or "").strip()
        match = re.search(r'unit\s*([0-9]+)', text, re.I)
        if match:
            return f"Unit {match.group(1)}", False
        if re.search(r'preview|intro|预备|预习|准备|starter|start\s*up', text, re.I):
            return f"预备单元 · {text}", True
        if not re.search(r'\d', text):
            return f"预备单元 · {text}", True
        return text or "其他单元", False

    @staticmethod
    def _status_key(text: str) -> str:
        value = (text or "").strip()
        lowered = value.lower()
        if any(kw in lowered for kw in (
            "进行中", "学习中", "作答中", "未完成", "部分完成", "继续", "未通过", "未达标", "重做",
            "in progress", "ongoing", "incomplete", "failed",
            "not completed", "uncompleted", "unfinished", "not finished", "not passed",
        )) or re.search(r'\bnot\s+(?:yet\s+)?(?:completed|finished|passed)\b', lowered):
            return "doing"
        if any(kw in lowered for kw in (
            "未开始", "未学习", "未做", "待学习", "待完成", "待作答",
            "not started", "unstarted", "to do", "not done", "undone",
        )) or re.search(r'\bnot\s+(?:yet\s+)?done\b', lowered):
            return "todo"
        if any(kw in lowered for kw in (
            "已完成", "完成", "已通过", "通过", "满分", "已提交", "已达标",
        )):
            return "done"
        if (not re.search(r'\bnot\s+(?:yet\s+)?(?:completed|finished|passed|done)\b', lowered)
                and re.search(r'\b(?:completed|finished|passed|done)\b', lowered)):
            return "done"
        return "unknown"

    def _scan_course_directory(self, current_unit_only: bool = False):
        tabs = []
        stop_event = self._scan_stop_event()
        is_publish = False
        resource = ''
        navigated_to_directory = False
        try:
            player_unit = ''
            is_publish = self.solver._is_publish_page()
            route = self.driver.current_url or ''
            resource, player_unit = self._player_directory_target(route)
            if is_publish and not resource:
                raise ValueError('播放器地址缺少 courseResourceId，无法定位课程目录')
            if is_publish or resource:
                if current_unit_only and not player_unit:
                    raise ValueError('播放器地址缺少单元标识，无法确认当前单元')
                self.gui_log_queue.put('从播放器返回本教材课程目录，读取任务列表')
                navigated_to_directory = True
                self.driver.get(f'https://ucloud.unipus.cn/app/cmgt/resource-detail/{resource}')
            unit_container = self.WebDriverWait(self.driver, 5).until(
                self.EC.presence_of_element_located((self.By.CSS_SELECTOR, '[class*="unitTabScrollContainer"]'))
            )
            unit_tabs = unit_container.find_elements(self.By.CSS_SELECTOR, ":scope > *")
            self.gui_log_queue.put(f"课程目录页，找到 {len(unit_tabs)} 个 Unit")

            unit_infos = []
            for unit_idx, unit_tab in enumerate(unit_tabs):
                try:
                    label = self.driver.execute_script(self.UNIT_LABEL_JS, unit_tab) or ""
                except Exception:
                    label = (unit_tab.text or "").strip()
                display_name, is_preview = self._parse_unit_label(label)
                unit_id = re.fullmatch(r'id-(.+)-tab', unit_tab.get_attribute('id') or '')
                unit_infos.append({
                    "index": unit_idx,
                    "label": label,
                    "display": display_name,
                    "is_preview": is_preview,
                    "element": unit_tab,
                    "unit_id": unit_id.group(1) if unit_id else '',
                })

            raw_label_counts = Counter(info.get("label", "") for info in unit_infos)
            active_info = next((i for i in unit_infos if self._is_active_unit(i["element"])), None)
            if current_unit_only and player_unit:
                active_info = next((i for i in unit_infos
                                    if i['element'].get_attribute('id') == f'id-{player_unit}-tab'), None)
                if active_info is None:
                    raise ValueError('课程目录中找不到播放器当前单元')
            directory_url = self.driver.current_url

            def collect_unit(info, click_tab: bool):
                name_counts = {}
                if stop_event is not None and stop_event.is_set():
                    raise TimeoutException('扫描已停止')
                if click_tab:
                    try:
                        select_course_unit(
                            self.driver, info["element"],
                            stop_requested=stop_event, settle_timeout=3.0
                        )
                    except Exception:
                        if stop_event is not None and stop_event.is_set():
                            raise
                        self.gui_log_queue.put(f"单元 {info['display']} 未确认加载完成，跳过扫描")
                        return
                else:
                    self._wait_visible_statuses_settled(stop_requested=stop_event)
                duplicate_label = raw_label_counts.get(info.get("label", ""), 0) > 1
                unit_display, unit_is_preview = self._resolve_unit_display(
                    info, duplicate_label=duplicate_label
                )
                tasks = self.driver.execute_script(self.READ_TASKS_JS) or []
                elements = self.driver.find_elements(self.By.CSS_SELECTOR, self.TASK_ITEM_SELECTOR)
                for task in tasks:
                    name = (task.get("name") or "").strip()
                    if not name:
                        continue
                    element = None
                    dom_index = task.get("index")
                    if isinstance(dom_index, int) and 0 <= dom_index < len(elements):
                        try:
                            element = elements[dom_index].find_element(
                                self.By.CSS_SELECTOR,
                                '[class*="taskTypeName"], [class*="nodeTypeName"]')
                        except Exception:
                            element = elements[dom_index]
                    section_title = (task.get("section") or "").strip()
                    occurrence = name_counts.get(name, 0)
                    name_counts[name] = occurrence + 1
                    status_key = self._status_key(task.get("status", ""))
                    compulsory = bool(task.get("compulsory"))
                    tabs.append({
                        "l1_idx": len(tabs),
                        "l2_idx": -1,
                        "l1_title": name,
                        "l2_title": section_title,
                        "display": self._format_task_display(
                            compulsory, unit_display, section_title, name, task.get("status", "")),
                        "is_l2": False,
                        "is_compulsory": compulsory,
                        "unit_label": unit_display,
                        "unit_is_preview": unit_is_preview,
                        "section": section_title,
                        "status": status_key,
                        "status_text": (task.get("status") or "").strip(),
                        "_element": element,
                        "_unit_idx": info["index"],
                        "_unit_id": info.get("unit_id", ""),
                        "_section_title": section_title,
                        "_name_occurrence": occurrence,
                        "_directory_url": directory_url,
                    })

            if current_unit_only:
                info = active_info or {
                    "index": 0,
                    "label": "",
                    "display": "当前单元",
                    "is_preview": False,
                }
                self.gui_log_queue.put(f"仅扫描当前单元: {info['display']}（不切换其它单元）")
                collect_unit(info, click_tab=bool(player_unit))
            else:
                for info in unit_infos:
                    if stop_event is not None and stop_event.is_set():
                        raise TimeoutException('扫描已停止')
                    collect_unit(info, click_tab=True)
        except Exception as e:
            if stop_event is not None and stop_event.is_set():
                self.gui_log_queue.put('课程目录扫描已停止')
                return []
            if is_publish or resource:
                if navigated_to_directory:
                    self.gui_log_queue.put(
                        f'已从播放器跳转至课程目录页，但目录加载失败: {str(e)[:100]}（请确认目录页就绪后重试）'
                    )
                else:
                    self.gui_log_queue.put(f'播放器任务扫描失败: {str(e)[:100]}')
            if self.logger:
                self.logger.debug(f"课程目录扫描未命中: {e}")
        if tabs and not (stop_event is not None and stop_event.is_set()):
            self._enrich_task_grading(tabs)
        return tabs

    def _enrich_task_grading(self, tabs):
        index = {}
        unit_names = {}
        try:
            route = urlsplit(self.driver.current_url)
            resource = re.fullmatch(r'/app/cmgt/resource-detail/(\d+)/?', route.path)
            unit_ids = list(dict.fromkeys(t['_unit_id'] for t in tabs if t.get('_unit_id')))
            if route.hostname == 'ucloud.unipus.cn' and resource and unit_ids:
                self.gui_log_queue.put('正在读取学习记录中的计分标记...')
                result = self.driver.execute_async_script(READ_GRADING_JS, resource.group(1), unit_ids) or {}
                units = result.get('units') or []
                index = build_record_index(units)
                unit_names = build_unit_names(units)
        except Exception:
            self.gui_log_queue.put('学习记录计分标记读取失败，保留任务并标记为计分未知')
        before_status_texts = [t.get('status_text', '') for t in tabs]
        before_unit_labels = [t.get('unit_label', '') for t in tabs]
        assign_task_records(tabs, index)
        for tab, old_status_text, old_unit_label in zip(tabs, before_status_texts, before_unit_labels):
            raw_unit_label = tab.get('unit_label', '')
            if (raw_unit_label.startswith('预备单元 · ')
                    and self._is_placeholder_unit_label(raw_unit_label.replace('预备单元 · ', '', 1))
                    and unit_names.get(tab.get('_unit_id', ''))):
                api_unit_name = unit_names[tab['_unit_id']]
                if re.search(r'unit\s*([0-9]+)', api_unit_name, re.I):
                    parsed_name, parsed_preview = self._parse_unit_label(api_unit_name)
                    tab['unit_label'] = parsed_name
                    tab['unit_is_preview'] = parsed_preview
                else:
                    tab['unit_label'] = api_unit_name
                    tab['unit_is_preview'] = bool(re.search(
                        r'preview|intro|预备|预习|准备|starter|start\s*up|导学', api_unit_name, re.I
                    ))
            if (tab.get('status_text', '') != old_status_text
                    or tab.get('unit_label', '') != old_unit_label):
                tab['display'] = self._format_task_display(
                    tab.get('is_compulsory'),
                    tab.get('unit_label', ''),
                    tab.get('section', ''),
                    tab.get('l1_title', ''),
                    tab.get('status_text', ''),
                )
            tab['display'] += f" · {grading_text(tab['is_scored'])}"
            if tab.get('score') is not None:
                tab['display'] += f" · 已有 {score_text(tab['score'])} 分"
        ungraded = sum(t['is_scored'] is False for t in tabs)
        unknown = sum(t['is_scored'] is None for t in tabs)
        graded = sum(t.get('score') is not None for t in tabs)
        self.gui_log_queue.put(
            f'计分标记：计分 {len(tabs) - ungraded - unknown}，不计分 {ungraded}，未知 {unknown}'
            + ('；未知任务不会被排除，请按需检查学习记录' if unknown else ''))
        if graded:
            self.gui_log_queue.put(f'学习记录里已有 {graded} 个任务带分数')

    @staticmethod
    def _format_task_display(compulsory, unit_name, section, name, status_text):
        prefix = "[必修]" if compulsory else "[选修]"
        section_parts = [part.strip() for part in (section or "").split("/") if part.strip()]
        section = " / ".join(dict.fromkeys(section_parts))
        if section.strip().lower() == (name or "").strip().lower():
            section = ""
        parts = [prefix, unit_name]
        if section:
            parts.append(section)
        parts.append(name)
        display = " · ".join(parts)
        if status_text:
            display += f" · {status_text}"
        return display

    def _is_active_unit(self, element) -> bool:
        try:
            return bool(self.driver.execute_script("""
                const el = arguments[0];
                if (el.getAttribute('aria-selected') === 'true') return true;
                if (el.getAttribute('aria-selected') === 'false') return false;
                const cls = String(el.className);

                if (/tabactive|slideractive/i.test(cls)) return true;
                if (el.querySelector('[class*="sliderActive"]')) return true;
                return /(?:^|[\\s_-])(active|selected|current)(?:$|[\\s_-])/i.test(cls);
            """, element))
        except Exception:
            return False

    def _extract_course_task_section(self, chapter) -> str:
        try:
            section = self.driver.execute_script("""
                const task = arguments[0];
                const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim();
                const root = task.closest('[class*="courses-unit"]') || task.parentElement;
                const taskText = clean(task.innerText);
                const sectionWords = /(section\\s*[a-z]|language\\s+focus|reading|listening|speaking|writing|grammar|vocabulary|pronunciation)/i;
                const headingSelectors = ['[class*="section"]', '[class*="title"]', '[class*="name"]', 'h1,h2,h3,h4,h5'];
                const headings = [];
                for (const selector of headingSelectors) {
                    for (const el of root.querySelectorAll(selector)) {
                        const text = clean(el.innerText);
                        if (!text || text === taskText || text.length > 80) continue;
                        if (sectionWords.test(text)) headings.push({ el, text });
                    }
                }
                const taskTop = task.getBoundingClientRect().top;
                const before = headings
                    .filter(item => item.el.getBoundingClientRect().top < taskTop)
                    .sort((a, b) => b.el.getBoundingClientRect().top - a.el.getBoundingClientRect().top)
                    .slice(0, 2)
                    .map(item => item.text)
                    .reverse();
                return [...new Set(before)].join(' / ');
            """, chapter)
            return (section or "").strip()
        except Exception:
            return ""
