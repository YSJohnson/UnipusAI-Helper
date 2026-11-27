import hashlib, json, logging, os, sys, random, re, tempfile, threading, time, base64
try:
    import winsound
except ImportError:
    class _MockWinsound:
        @staticmethod
        def MessageBeep(*args, **kwargs):
            try:
                print('\a', end='', flush=True)
            except Exception:
                pass
    winsound = _MockWinsound()
import shutil
import subprocess
import urllib.request
import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from glob import glob
from typing import List, Optional, Dict, Any, Tuple, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from unipus_helper.paths import PROJECT_ROOT

BASE_DIR = str(PROJECT_ROOT)


def ensure_writable_temp_dir() -> str:
    candidates = []
    for name in ('TEMP', 'TMP'):
        value = os.environ.get(name)
        if value and value not in candidates:
            candidates.append(value)
    local_appdata = os.environ.get('LOCALAPPDATA')
    if local_appdata:
        app_temp = os.path.join(local_appdata, 'UnipusAI-Helper', 'tmp')
        if app_temp not in candidates:
            candidates.append(app_temp)
    program_temp = os.path.join(BASE_DIR, 'logs', 'tmp')
    if program_temp not in candidates:
        candidates.append(program_temp)
    workspace_temp = os.path.join(BASE_DIR, '.tmp')
    if workspace_temp not in candidates:
        candidates.append(workspace_temp)

    original_temp = os.environ.get('TEMP', '')
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, f'.ucampus_write_probe_{os.getpid()}')
            with open(probe, 'w', encoding='utf-8') as handle:
                handle.write('ok')
            os.remove(probe)
        except Exception:
            continue
        os.environ['TEMP'] = candidate
        os.environ['TMP'] = candidate
        try:
            tempfile.tempdir = candidate
        except Exception:
            pass
        if original_temp and os.path.abspath(original_temp) != os.path.abspath(candidate):
            print(f" 系统临时目录不可写，已切换到: {candidate}")
        return candidate
    return ''


# 必须先确保临时目录可写，再导入 whisper/torch：
# 系统 temp 不可写时 torch 的 DLL 初始化会直接抛出 WinError 1114。
ensure_writable_temp_dir()

from unipus_helper.accuracy_control import (AccuracyController, effective_score_threshold,
                                            parse_target_accuracy)
from unipus_helper.answer_api import OfficialAnswerProvider
from unipus_helper.audio_recognizer import AudioTranscriber
from unipus_helper.browser import DEFAULT_BROWSER, driver_api, get_spec, normalize_browser, quit_driver
from unipus_helper.environment_checker import EnvironmentChecker
from unipus_helper.course_catalog import describe_directory_page_state, select_course_unit
from unipus_helper.record_skip import (normalize_skip_record_mode,
                                       should_skip_publish_group_fast,
                                       should_skip_record_question)
from openai import OpenAI
from selenium.common.exceptions import (InvalidSessionIdException, NoSuchDriverException,
                                        NoSuchElementException, TimeoutException,
                                        StaleElementReferenceException,
                                        SessionNotCreatedException, WebDriverException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

gui_log_queue = queue.Queue()

DEBUG_MODE = False

APP_VERSION = "3.9.19"

DEFAULT_LOGIN_URL = "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome"
UCLOUD_HOME_URL = "https://ucloud.unipus.cn/home"

logger = logging.getLogger('UCampusBot')


def setup_logging():

    def clean_all_logs(log_dir):
        try:
            log_pattern = os.path.join(log_dir, 'ucampus_*.log')
            log_files = glob(log_pattern)
            for old_file in log_files:
                try:
                    os.remove(old_file)
                except:
                    pass
        except Exception:
            pass

    logger = logging.getLogger('UCampusBot')
    if logger.handlers:
        # 已经初始化过：重复调用会把 sys.stdout（此时已被 PrintRedirector 接管）
        # 再包一层 StreamHandler，日志会自我递归直到 RecursionError
        return logger
    logger.setLevel(logging.DEBUG)
    logger.handlers = []
    log_dir = os.path.join(BASE_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    clean_all_logs(log_dir)
    log_file = os.path.join(log_dir, f'ucampus_{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.log')

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(funcName)s:%(lineno)d]\n%(message)s\n',
        datefmt='%H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    class ConsoleFilter(logging.Filter):
        def filter(self, record):
            if record.levelno >= logging.ERROR:
                record.msg = f" {record.msg}"
            return True

    console_handler.addFilter(ConsoleFilter())
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    class PrintRedirector:
        def __init__(self, logger, level=logging.INFO):
            self.logger = logger
            self.level = level
            self.buffer = ""

        def write(self, text):
            if text.strip():
                if any(x in text for x in ['Error', 'Exception', 'Traceback']):
                    self.logger.error(text.strip())
                elif 'Warning' in text:
                    self.logger.warning(text.strip())
                else:
                    self.logger.info(text.strip())
                gui_log_queue.put(text.strip())

        def flush(self):
            pass

    sys.stdout = PrintRedirector(logger)
    return logger


def _as_bool(value: Any, default: bool = False) -> bool:
    """兼容 JSON 布尔与字符串布尔的开关解析。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "none", "null"):
        return default
    return text not in ("0", "false", "no", "off", "disabled", "disable")


@dataclass(frozen=True)
class Config:
    url: str
    username: str
    password: str
    api_key: str
    token_full: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    tts_engine: str = "auto"
    tts_voice: str = ""
    tts_proxy: str = ""
    screenshot: bool = False
    browser: str = DEFAULT_BROWSER
    record_score_threshold: float = 70
    score_threshold: float = 80
    target_accuracy: str = "100"
    target_accuracy_min: float = 100.0
    target_accuracy_max: float = 100.0
    skip_record_mode: str = "off"
    answer_api_enabled: bool = True
    answer_api_cache: bool = True
    answer_api_open_id: str = ""
    answer_api_secret: str = ""
    answer_api_host: str = ""
    answer_api_timeout: float = 30.0

    @property
    def ai_configured(self) -> bool:
        return bool(
            (self.api_key or "").strip()
            and (self.base_url or "").strip()
            and (self.model or "").strip()
        )

    @classmethod
    def from_json(cls, path: str = "config.json") -> "Config":
        with open(path, "r", encoding="UTF-8") as f:
            data = json.load(f)
        for key, default in (("record_score_threshold", 70), ("score_threshold", 80)):
            try:
                data[key] = float(data.get(key, default))
                if not 0 <= data[key] <= 100:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(f"{key} 必须是 0–100 之间的分数") from None
        acc_low, acc_high, acc_text = parse_target_accuracy(data.get("target_accuracy", 100))
        skip_record_mode = normalize_skip_record_mode(data.get("skip_record_mode", "off"))
        try:
            answer_api_timeout = float(data.get("answer_api_timeout") or 30)
        except (ValueError, TypeError):
            answer_api_timeout = 30.0
        answer_api_timeout = min(120.0, max(5.0, answer_api_timeout))
        global DEBUG_MODE
        DEBUG_MODE = data.get("debug_mode", False)
        return cls(
            url=data.get("url") or DEFAULT_LOGIN_URL,
            username=(data.get("username") or "").strip(),
            password=data.get("password") or "",
            token_full=data.get("token_full") or "",
            api_key=(data.get("api_key") or "").strip(),
            base_url=(data.get("base_url") or "").strip(),
            model=(data.get("model") or "").strip(),
            temperature=data.get("temperature", 0.3),
            max_tokens=data.get("max_tokens", 32768),
            tts_engine=(data.get("tts_engine") or "auto").strip(),
            tts_voice=(data.get("tts_voice") or "").strip(),
            tts_proxy=(data.get("tts_proxy") or "").strip(),
            screenshot=bool(data.get("screenshot", False)),
            browser=normalize_browser(data.get("browser")),
            record_score_threshold=data["record_score_threshold"],
            score_threshold=data["score_threshold"],
            target_accuracy=acc_text,
            target_accuracy_min=acc_low,
            target_accuracy_max=acc_high,
            skip_record_mode=skip_record_mode,
            answer_api_enabled=_as_bool(data.get("answer_api_enabled"), True),
            answer_api_cache=_as_bool(data.get("answer_api_cache"), True),
            answer_api_open_id=(data.get("answer_api_open_id") or "").strip(),
            answer_api_secret=(data.get("answer_api_secret") or "").strip(),
            answer_api_host=(data.get("answer_api_host") or "").strip(),
            answer_api_timeout=answer_api_timeout,
        )


def _parse_auth_token(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    parsed = raw if isinstance(raw, dict) else None
    token = "" if parsed is not None else str(raw or "").strip()

    for _ in range(2):
        if parsed is not None or not token:
            break
        try:
            decoded = json.loads(token)
        except (TypeError, ValueError):
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
                token = token[1:-1].strip()
                continue
            break
        if isinstance(decoded, str):
            token = decoded.strip()
        else:
            parsed = decoded

    if isinstance(parsed, dict):
        token_info = parsed.get("tokenInfo")
        jwt = parsed.get("jwt") or parsed.get("Authorization")
        if not jwt and isinstance(token_info, dict):
            jwt = token_info.get("jwt")
        if isinstance(jwt, str) and jwt.lower().startswith("bearer "):
            jwt = jwt[7:].strip()
        if not isinstance(jwt, str) or jwt.count(".") != 2:
            return None, None

        legacy_keys = {"rt", "jwt", "rtExpire", "jwtExpire"}
        legacy = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) \
            if legacy_keys.issubset(parsed) else None
        return legacy, jwt

    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.count(".") == 2 and all(token.split(".")):
        return None, token
    return None, None


class QuestionType(Enum):
    SINGLE_CHOICE = auto()
    MULTIPLE_CHOICE = auto()
    FILL_IN = auto()
    TEXT = auto()
    SORTING = auto()
    VOCABULARY_FLASHCARD = auto()
    BANKED_CLOZE = auto()
    VOCABULARY_TEST = auto()
    VIDEO = auto()
    AUDIO = auto()
    DISCUSSION_BOARD = auto()
    SELF_CHECK = auto()
    MY_VOICE_TEXT = auto()
    DROPDOWN_SELECT = auto()
    LISTENING_FILL_IN = auto()
    LISTENING_CHOICE = auto()
    VIDEO_CHOICE = auto()
    TRUE_FALSE = auto()
    MATCHING = auto()
    RECORD = auto()
    ORAL_ALOUD = auto()
    LINK_MATCH = auto()
    PARAGRAPH_MATCH = auto()


@dataclass
class Option:
    letter: str
    text: str
    element: Any
    is_selected: bool = False


@dataclass
class Question:
    number: int
    text: str
    q_type: QuestionType
    element: Any
    options: List[Option] = field(default_factory=list)
    inputs: List[Any] = field(default_factory=list)
    banked_options: List[str] = field(default_factory=list)
    banked_blanks: List[Dict] = field(default_factory=list)
    directions: str = ""
    group: Any = None
    needs_upload: bool = False
    blank_count: int = 0
    record_items: List[Dict] = field(default_factory=list)
    link_items: List[Dict] = field(default_factory=list)
    is_evaluation: bool = False

    @property
    def answer_count(self) -> int:
        return self.blank_count or len(self.inputs)

    def is_interactive(self) -> bool:
        if self.q_type in [QuestionType.VIDEO, QuestionType.AUDIO,
                           QuestionType.VOCABULARY_FLASHCARD,
                           QuestionType.DISCUSSION_BOARD,
                           QuestionType.RECORD,
                           QuestionType.ORAL_ALOUD,
                           QuestionType.SELF_CHECK]:
            return True
        return bool(self.options or self.inputs or self.banked_blanks)

    @property
    def is_phrase_mode(self) -> bool:
        if not self.banked_options:
            return False
        phrase_count = sum(1 for opt in self.banked_options if ' ' in opt.strip() or len(opt) > 15)
        return phrase_count / len(self.banked_options) > 0.3

    @property
    def needs_speech_script(self) -> bool:
        items = self.record_items or []
        if not items:
            return False
        return any(not (item.get('url') or '').strip()
                   and not (item.get('text') or '').strip() for item in items)

    @property
    def is_self_submitting_record(self) -> bool:
        if self.q_type == QuestionType.ORAL_ALOUD:
            return True
        if self.q_type == QuestionType.RECORD:
            items = self.record_items or []
            return bool(items) and (bool(items[0].get('vocab')) or bool(items[0].get('role_play')))
        return False


CJK_HINT_PATTERN = re.compile(r'[（(]\s*([\u4e00-\u9fa5][^（）()]{0,40}?)\s*[）)]')
CJK_CHAR_PATTERN = re.compile(r'[\u4e00-\u9fa5]')

EXPRESSION_BANK_PATTERN = re.compile(
    r'expressions?\s+given\s+below|fill\s+in\s+the\s+blanks\s+with\s+the\s+expressions',
    re.I,
)
EXPRESSION_FORM_PATTERN = re.compile(r'change\s+the\s+form', re.I)
EXPRESSION_ONCE_PATTERN = re.compile(r'each\s+expression|expressions?\s+can\s+be\s+used', re.I)

EXPRESSION_BANK_HINTS = [
    "【短语变形填空】请输出按句子语法变形后的完整表达，不要照抄词库原形。",
    "1. 不规则动词整体变化：dig up→dug up、take up→took up、come to the rescue→came to the rescue。",
    "2. 空格若缺助动词或 be 动词，要一并写进空里：will get away from、are equipped with（be 随主语变位）。",
    "3. take ... for granted 的 ... 要换成句子需要的宾语；put one's feet up 的 one's 要按主语换成 their / his / her。",
    "4. 词库里每个表达恰好用一次：不要重复、不要漏用；先通读全部句子做全局分配，再逐空给答案。",
    "5. 每个空可以包含多个单词，答案里不要带括号注释、词形解释或其它说明。",
    "6. 只有空位在句首时首字母才大写：At the heart of / Early on / In the course of；句中一律保持小写：distinguish / come up with / wrestled with / rather than / refrain from。平台大小写也计分。",
]


def is_expression_bank_question(q: "Question") -> bool:
    text = f"{getattr(q, 'directions', '') or ''}\n{getattr(q, 'text', '') or ''}"
    if EXPRESSION_BANK_PATTERN.search(text):
        return True
    return bool(EXPRESSION_FORM_PATTERN.search(text)
                and EXPRESSION_ONCE_PATTERN.search(text))

CLOZE_DROPDOWN_PLACEHOLDER = '点击选择'
DROPDOWN_TRIGGER_SELECTOR = (
    '.scoop-select-wrapper, .ant-select, .ant-select-selector, '
    '.ant-dropdown-trigger, [role="combobox"], select'
)

DROPDOWN_TRIGGER_JS = """
const root = arguments[0];
const SEL = %s;
const LABEL = %s;
const visible = (el) => el && el.getClientRects && el.getClientRects().length > 0;
const byText = [];
const seen = new Set();
const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
let node;
while ((node = walker.nextNode())) {
    if ((node.textContent || '').indexOf(LABEL) < 0) continue;
    let el = node.parentElement;
    let picked = null;
    for (let i = 0; i < 4 && el && el !== root; i++) {
        if (el.matches('a,button,[role="button"],[role="combobox"],[class*="select"],[class*="dropdown"],[tabindex]')) {
            picked = el;
            break;
        }
        el = el.parentElement;
    }
    const target = picked || node.parentElement;
    if (visible(target) && !seen.has(target)) { seen.add(target); byText.push(target); }
}
if (byText.length) return byText;
const bySelector = [];
root.querySelectorAll(SEL).forEach(function (el) {
    if (visible(el) && !seen.has(el)) { seen.add(el); bySelector.push(el); }
});
return bySelector;
""" % (json.dumps(DROPDOWN_TRIGGER_SELECTOR, ensure_ascii=False),
       json.dumps(CLOZE_DROPDOWN_PLACEHOLDER, ensure_ascii=False))


def find_dropdown_triggers(driver, root):
    try:
        return driver.execute_script(DROPDOWN_TRIGGER_JS, root) or []
    except Exception:
        return []


def has_visible_dropdown(driver, root) -> bool:
    return bool(find_dropdown_triggers(driver, root))


PREFIX_STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'on', 'at', 'for', 'with', 'by',
    'as', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'that', 'this', 'these',
    'those', 'it', 'its', 'their', 'his', 'her', 'our', 'your', 'my', 'from', 'into',
    'about', 'than', 'then', 'so', 'but', 'if', 'when', 'while', 'can', 'will',
    'would', 'should', 'could', 'may', 'might', 'must', 'have', 'has', 'had', 'do',
    'does', 'did', 'not', 'no', 'more', 'most', 'very', 'also', 'such', 'which',
    'who', 'whom', 'whose', 'there', 'here', 'we', 'you', 'they', 'he', 'she', 'i',
    'per', 'all', 'any', 'some', 'each', 'every', 'both', 'other', 'same', 'own',
}


def looks_like_given_prefix(hint: str) -> bool:
    text = (hint or '').strip()
    if not text or len(text) > 5:
        return False
    if text.lower() in PREFIX_STOPWORDS:
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", text))


def extract_cjk_hints(text: str) -> List[str]:
    return [hint.strip() for hint in CJK_HINT_PATTERN.findall(text or '') if hint.strip()]


def blank_cjk_hints(q: "Question") -> List[str]:
    inputs = getattr(q, 'inputs', None) or []
    count = len(inputs)
    if count <= 0:
        return []

    aligned = []
    for blank in (getattr(q, 'banked_blanks', None) or [])[:count]:
        around = f"{blank.get('right_context') or ''} {blank.get('context') or ''}"
        found = extract_cjk_hints(around)
        aligned.append(found[0] if found else '')
    if len(aligned) == count and all(aligned):
        return aligned

    hints = extract_cjk_hints(getattr(q, 'text', '') or '')
    return hints if len(hints) == count else []


def _normalize_vocab_key(text: str) -> str:
    value = (text or '').strip()
    value = re.sub(r'[（(].*?[)）]', '', value)
    value = re.sub(r'[\s，,。.；;、:：!！?？\'"“”‘’…—-]+', '', value)
    return value.lower()


class VocabularyStore:

    MAX_PAIRS = 80

    def __init__(self):
        self.pairs: Dict[str, str] = {}
        self.rows: List[Tuple[str, str]] = []

    def __len__(self) -> int:
        return len(self.rows)

    def add_rows(self, rows) -> int:
        added = 0
        for row in rows or []:
            if isinstance(row, dict):
                en, zh = row.get('en') or row.get('english'), row.get('zh') or row.get('chinese')
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                en, zh = row[0], row[1]
            else:
                continue
            if self.add(en, zh):
                added += 1
        return added

    def add(self, en: str, zh: str) -> bool:
        en = re.sub(r'\s+', ' ', (en or '')).strip(' .;:-')
        en = re.sub(r'\.(wav|mp3|m4a|ogg|flac)$', '', en, flags=re.I).strip()
        zh = re.sub(r'\s+', ' ', (zh or '')).strip(' .;:-')
        if not en or not zh:
            return False
        if not re.search(r'[A-Za-z]{2}', en) or not CJK_CHAR_PATTERN.search(zh):
            return False
        if len(en) > 80 or len(zh) > 40:
            return False
        key = _normalize_vocab_key(zh)
        if not key:
            return False
        if self.pairs.get(key, '').lower() == en.lower():
            return False
        if key not in self.pairs and len(self.rows) >= self.MAX_PAIRS:
            return False
        self.pairs[key] = en
        self.rows = [row for row in self.rows if _normalize_vocab_key(row[1]) != key]
        self.rows.append((en, zh))
        return True

    def lookup(self, hint: str) -> Optional[str]:
        key = _normalize_vocab_key(hint)
        if not key:
            return None
        if key in self.pairs:
            return self.pairs[key]

        for candidate, en in self.pairs.items():
            if not candidate:
                continue
            if len(key) >= 2 and (key in candidate or candidate in key):
                return en

        best, best_score = None, 0.0
        for candidate, en in self.pairs.items():
            score = _similarity(key, candidate)
            if score > best_score:
                best, best_score = en, score
        return best if best_score >= 0.84 else None

    def prompt_lines(self, limit: int = 40) -> List[str]:
        lines = []
        for en, zh in self.rows[:limit]:
            lines.append(f"     {zh} = {en}")
        return lines

    def clear(self):
        self.pairs.clear()
        self.rows.clear()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    common = len(set(left) & set(right))
    base = max(len(set(left)), len(set(right))) or 1
    return common / base * 0.9


class Selectors:
    CHOICE_OPTIONS = [
        '.option.isNotReview',
        'div.option',
        '.MultipleChoice--checkbox-item-34A_-',
        'ul[class*="single-choice"] li label',
        '.option-wrap',
    ]
    OPTION_CAPTION = ['.caption', 'span[class*="index"]', '.MultipleChoice--checkbox-opt-2F4xY']
    OPTION_CONTENT = ['.component-htmlview.content', 'div.html-view[class*="content"]', '.html-view', '.content', 'p']
    QUESTION_TITLE = [
        '.ques-title',
        '.component-htmlview.ques-title',
        '.question-inputbox-header',
        '.component-htmlview',
        '.title',
        'p',
        '.question-stem',
    ]
    SUBMIT_BUTTON = [
        'button[type="submit"]',
        'button[class*="submit"]',
        'button[class*="confirm"]',
        '.submit-bar-pc--btn-1_Xvo',
        '.btns-submit button.submit-btn',
        'button.submit-btn',
        '.btn',
    ]
    LEVEL1_TABS = [
        '.pc-header-tabs-container .pc-tab-row > .tab',
        '.pc-header-tabs-container .ant-col.tab',
        '.pc-tab-row > [class*="pc-header-tab"]',
    ]
    LEVEL2_TABS = [
        '.pc-header-tasks-row > .pc-task',
        ':scope > div > div > .pc-header-tasks-row > .pc-task',
    ]


class WebDriverHelper:

    @staticmethod
    def safe_find_element(driver, selectors: List[str], parent=None) -> Optional[Any]:
        search_context = parent if parent else driver
        wait = WebDriverWait(search_context, 5)
        for selector in selectors:
            try:
                element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                if element.is_displayed():
                    return element
            except (TimeoutException, NoSuchElementException):
                continue
        return None

    @staticmethod
    def safe_find_elements(driver, selectors: List[str], parent=None) -> List[Any]:
        search_context = parent if parent else driver
        for selector in selectors:
            try:
                elements = [
                    e for e in search_context.find_elements(By.CSS_SELECTOR, selector)
                    if e.is_displayed()
                ]
                if elements:
                    return elements
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue
        return []

    LONG_TEXT_JS_THRESHOLD = 120

    @staticmethod
    def human_like_delay(base_delay: float = 0.1) -> None:
        delay = base_delay * (0.8 + random.random() * 0.4)
        time.sleep(delay)

    @staticmethod
    def _scroll_into_view_instant(driver, element) -> None:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'});",
                element,
            )
        except Exception:
            pass

    @staticmethod
    def _is_element_focused(driver, element) -> bool:
        try:
            return bool(driver.execute_script("return document.activeElement === arguments[0];", element))
        except Exception:
            return False

    @staticmethod
    def ensure_focused(driver, element, retries: int = 3) -> bool:
        for _ in range(max(1, retries)):
            WebDriverHelper._scroll_into_view_instant(driver, element)
            time.sleep(0.05)
            if WebDriverHelper._is_element_focused(driver, element):
                return True
            try:
                element.click()
            except Exception:
                try:
                    ActionChains(driver).move_to_element(element).click().perform()
                except Exception:
                    pass
            if WebDriverHelper._is_element_focused(driver, element):
                return True
            try:
                driver.execute_script(
                    "try { arguments[0].focus({preventScroll: true}); } "
                    "catch (e) { arguments[0].focus(); }",
                    element,
                )
            except Exception:
                pass
            if WebDriverHelper._is_element_focused(driver, element):
                return True
            time.sleep(0.1)
        return WebDriverHelper._is_element_focused(driver, element)

    @staticmethod
    def set_value_via_js(driver, element, value: str, dispatch_blur: bool = True) -> None:
        driver.execute_script("""
            const el = arguments[0];
            const value = arguments[1];
            const dispatchBlur = Boolean(arguments[2]);
            try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
            const prev = el.value;
            const proto = el.tagName === 'TEXTAREA'
                ? HTMLTextAreaElement.prototype
                : (el.tagName === 'INPUT' ? HTMLInputElement.prototype : Object.getPrototypeOf(el));
            const desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
            if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
            const tracker = el._valueTracker;
            if (tracker) {
                tracker.setValue(prev === value ? (value ? '' : ' ') : prev);
            }
            try {
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true, cancelable: true,
                    inputType: value ? 'insertText' : 'deleteContentBackward',
                    data: value || null
                }));
            } catch (e) {
                el.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
            }
            el.dispatchEvent(new Event('change', { bubbles: true }));
            if (dispatchBlur) {
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }
        """, element, value, dispatch_blur)

    @staticmethod
    def clear_input_verified(driver, element) -> bool:
        try:
            element.clear()
        except Exception:
            pass
        try:
            current = element.get_attribute('value') or ''
        except Exception:
            current = ''
        if current:
            try:
                element.send_keys(Keys.CONTROL, 'a')
                element.send_keys(Keys.BACKSPACE)
                current = element.get_attribute('value') or ''
            except Exception:
                pass
        if current:
            try:
                WebDriverHelper.set_value_via_js(driver, element, '', dispatch_blur=False)
                current = element.get_attribute('value') or ''
            except Exception:
                pass
        return not current

    @staticmethod
    def simulate_typing(driver, element, text: str) -> None:
        WebDriverHelper.ensure_focused(driver, element)
        WebDriverHelper.human_like_delay(0.1)
        WebDriverHelper.clear_input_verified(driver, element)
        if not WebDriverHelper._is_element_focused(driver, element):
            WebDriverHelper.ensure_focused(driver, element, retries=2)
        WebDriverHelper.human_like_delay(0.1)
        if len(text) > WebDriverHelper.LONG_TEXT_JS_THRESHOLD:
            WebDriverHelper.set_value_via_js(driver, element, text, dispatch_blur=True)
            WebDriverHelper.human_like_delay(0.1)
            return
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.01, 0.05))
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));
        """, element)
        WebDriverHelper.human_like_delay(0.1)

    @staticmethod
    def safe_click(driver, element, retries: int = 3) -> bool:
        for i in range(retries):
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    element
                )
                time.sleep(0.3)

                try:
                    element.click()
                except:
                    driver.execute_script("arguments[0].click();", element)
                return True

            except StaleElementReferenceException:
                if i < retries - 1:
                    time.sleep(1)
                    continue
            except Exception as e:
                if i < retries - 1:
                    time.sleep(0.5)
                    continue
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
        return False


class OpenAICompatibleClient:

    SYSTEM_PROMPT = """你是一个专业的英语教学助手，擅长分析英语题目。
请根据题目要求给出准确答案，注意区分不同题型：
- 词汇匹配题：根据英文选中文，或根据中文选英文
- 选词填空：选择最合适的单词填入
- 阅读理解：基于文章内容作答
- 听力选择题：基于音频转写内容作答
- 视频选择题：基于视频转写内容作答
- 排序题：根据材料出现顺序返回选项字母序列"""

    def __init__(self, config: Config):
        self.config = config
        self.enabled = bool(
            (getattr(config, 'api_key', '') or '').strip()
            and (getattr(config, 'base_url', '') or '').strip()
            and (getattr(config, 'model', '') or '').strip()
        )
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url) if self.enabled else None
        self.conversation_history: List[Dict] = []
        self.material_messages: List[Dict] = []
        self.current_chapter_id: Optional[str] = None
        self.accumulated_passages: set = set()

    def start_new_chapter(self, chapter_id: str):
        self.current_chapter_id = chapter_id
        print(f" 记录章节: {chapter_id[:50]}")

    def force_reset(self, chapter_id: str):
        self.conversation_history = []
        self.material_messages = []
        self.current_chapter_id = chapter_id
        self.accumulated_passages = set()
        print(f" 强制重置章节: {chapter_id[:50]}")

    def add_passage_if_new(self, passage: str) -> bool:
        return self._add_context_if_new(passage, "阅读材料", "材料")

    def add_audio_transcript_if_new(self, transcript: str) -> bool:
        return self._add_context_if_new(transcript, "听力音频转写", "音频转写")

    def add_video_transcript_if_new(self, transcript: str) -> bool:
        return self._add_context_if_new(transcript, "视频转写", "视频转写")

    def _add_context_if_new(self, content: str, label: str, ack_label: str) -> bool:
        if not content or not content.strip() or (label == '阅读材料' and len(content) < 50):
            return False

        passage_hash = hashlib.md5(content.encode()).hexdigest()[:16]

        if passage_hash in self.accumulated_passages:
            print(f"    {ack_label}已存在，跳过")
            return False

        self.accumulated_passages.add(passage_hash)
        material_index = len(self.accumulated_passages)

        passage_msg = {
            "role": "user",
            "content": f"【{label} {material_index}】\n\n{content}\n\n请理解以上内容，等待后续问题。"
        }
        self.material_messages.append(passage_msg)
        self.material_messages.append({
            "role": "assistant",
            "content": f"我已理解{ack_label} {material_index}。请提出问题。"
        })

        print(f"    新增{ack_label}（{len(content)}字符），当前共{len(self.accumulated_passages)}份上下文")
        return True

    @staticmethod
    def _is_image_unsupported_error(exc: Exception) -> bool:
        text = str(exc).lower()
        if not any(word in text for word in
                   ('image', 'vision', 'multimodal', 'image_url', 'unsupported content')):
            return False
        status = getattr(exc, 'status_code', None)
        if status is None:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
        return status is None or status in (400, 404, 413, 415, 422)

    def ask(self, prompt: str, retry_count: int = 3, stop_requested: Optional[threading.Event] = None,
            screenshot: Optional[bytes] = None, remember: bool = True,
            require_image: bool = False) -> Optional[str]:
        if not getattr(self, 'enabled', True) or self.client is None:
            print("    未配置 AI 接口，无法调用大模型作答")
            return None
        if require_image and not screenshot:
            print("   需要题目图片上下文但截图不可用，停止作答")
            return None
        print(f"当前ai对话历史共{len(self.conversation_history)}条")
        max_tokens = max(self.config.max_tokens, 32768)
        for attempt in range(retry_count):
            if stop_requested is not None and stop_requested.is_set():
                return None
            try:
                ask_prompt = prompt
                if attempt > 0:
                    ask_prompt = prompt + "\n\n【重要】请直接给出最终答案，不要输出推理过程，不要解释。"
                user_message: Dict[str, Any] = {"role": "user", "content": ask_prompt}
                if screenshot:
                    user_message = {"role": "user", "content": [
                        {"type": "text", "text": ask_prompt},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64," +
                                   base64.b64encode(screenshot).decode('ascii')}},
                    ]}
                messages = [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    *getattr(self, 'material_messages', []),
                    *self.conversation_history,
                    user_message,
                ]

                if DEBUG_MODE:
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 请求详情")
                    print(f"   base_url: {self.config.base_url}")
                    print(f"   model:    {self.config.model}")
                    print(f"   temperature: {self.config.temperature}")
                    print(f"   max_tokens:  {max_tokens}")
                    print(f"   消息条数: {len(messages)}")
                    for i, msg in enumerate(messages):
                        role = msg["role"]
                        content = msg["content"]
                        preview = content[:300] + "..." if len(content) > 300 else content
                        print(f"   [{i}] {role}: {preview}")
                    print("=" * 60 + "\n")

                shot = f"，附截图 {len(screenshot) // 1024} KB" if screenshot else ""
                print(f"【请求】已向 AI 发送请求（{len(ask_prompt)} 字{shot}，max_tokens={max_tokens}，"
                      f"第 {attempt + 1}/{retry_count} 次），等待 {self.config.model} 返回...")
                request_started = time.time()

                request_tokens = max_tokens
                while True:
                    try:
                        response = self.client.chat.completions.create(
                            model=self.config.model,
                            messages=messages,
                            temperature=self.config.temperature,
                            max_tokens=request_tokens
                        )
                        break
                    except Exception as exc:
                        message = str(exc).lower()
                        rejects_budget = ('max_tokens' in message or 'max output' in message
                                          or 'too large' in message or 'maximum context' in message)
                        if rejects_budget and request_tokens > 4096:
                            print(f"    模型不接受 max_tokens={request_tokens}，减半到 {request_tokens // 2} 重试")
                            request_tokens //= 2
                            continue
                        raise
                max_tokens = request_tokens

                elapsed = time.time() - request_started
                finish_reason = response.choices[0].finish_reason
                answer = (response.choices[0].message.content or "").strip()
                print(f"【返回】AI 已返回（耗时 {elapsed:.1f}s，finish_reason={finish_reason}，"
                      f"{len(answer)} 字）")

                if DEBUG_MODE:
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 响应详情")
                    print(f"   model:        {response.model}")
                    print(f"   finish_reason:{finish_reason}")
                    print(f"   usage:        {response.usage}")
                    print(f"   answer:       {answer[:500]}{'...' if len(answer) > 500 else ''}")
                    print("=" * 60 + "\n")

                if not answer:
                    if finish_reason == 'length':
                        if attempt == retry_count - 1:
                            print("推理耗尽 token，已达到最大重试次数")
                            return None
                        if max_tokens < 32768:
                            max_tokens = min(max_tokens * 2, 32768)
                        print(f"推理耗尽 token（finish_reason=length），使用 max_tokens="
                              f"{max_tokens} 和简化问法重试")
                        continue
                    print(f"模型返回空内容（finish_reason={finish_reason}）")
                    return None

                if remember:
                    self.conversation_history.append({"role": "user", "content": prompt})
                    self.conversation_history.append({"role": "assistant", "content": answer})

                    self.conversation_history = self.conversation_history[-20:]

                print(f"AI回答: {answer}")
                return answer

            except Exception as e:
                if screenshot is not None and self._is_image_unsupported_error(e):
                    if require_image:
                        print("   接口无法识别题目图片，停止作答")
                        return None
                    screenshot = None
                    print("   接口拒绝图片，改用纯文本重试")
                if DEBUG_MODE:
                    import traceback
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 调用异常详情")
                    print(f"   异常类型: {type(e).__name__}")
                    print(f"   异常信息: {e}")
                    if hasattr(e, 'response'):
                        try:
                            print(f"   HTTP 状态码: {e.response.status_code}")
                            print(f"   响应头: {dict(e.response.headers)}")
                            print(f"   响应体: {e.response.text[:1000]}")
                        except:
                            pass
                    if hasattr(e, 'body'):
                        try:
                            print(f"   错误body: {e.body}")
                        except:
                            pass
                    if hasattr(e, 'status_code'):
                        print(f"   status_code: {e.status_code}")
                    print(f"   完整堆栈:")
                    traceback.print_exc()
                    print("=" * 60 + "\n")

                if attempt < retry_count - 1:
                    delay = (2 ** attempt) + random.random()
                    if stop_requested is not None:
                        if stop_requested.wait(delay):
                            return None
                    else:
                        time.sleep(delay)
                error_msg = str(e)
                print(f"AI调用失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)

        return None


class QuestionParserStrategy(ABC):

    @staticmethod
    def _is_multiple_choice(container, directions: str = "") -> bool:
        class_name = (container.get_attribute('class') or '').lower()
        if 'multiplechoice' in class_name or 'multiple-choice' in class_name:
            return True
        if container.find_elements(By.CSS_SELECTOR,
                                   'input[type="checkbox"], [role="checkbox"], .multipleChoice, '
                                   '[class*="MultipleChoice"], [class*="multiple-choice"]'):
            return True
        instructions = ' '.join([directions, *[e.text for e in container.find_elements(By.CSS_SELECTOR, '.ques-title')]])
        if re.search(
                r'\b(?:choose|select)\s+(?:the\s+)?true\s+statements\b|'
                r'\bwhich\s+statements\s+are\s+true\b',
                instructions, re.IGNORECASE):
            return True
        return bool(re.search(
            r'多选|\b(?:choose|select|check|tick|mark)\s+(?:all|two|three|four|five|six|seven|eight|nine|ten|[2-9]|multiple|more than one)\b',
            instructions, re.IGNORECASE))

    @abstractmethod
    def can_parse(self, container, driver) -> bool:
        pass

    @abstractmethod
    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        pass


class QuestionParser:

    def __init__(self, driver):
        self.driver = driver
        self.incomplete = False
        self.non_answer_containers = 0
        self.strategies: List[QuestionParserStrategy] = [
            RolePlayStrategy(),
            ParagraphMatchingStrategy(),
            VocabularyReadAloudStrategy(),
            OralAloudStrategy(),
            VideoDubStrategy(),
            VideoStrategy(),
            AudioStrategy(),
            DiscussionBoardStrategy(),
            RecordStrategy(),
            ChecklistStrategy(),
            SelfCheckStrategy(),
            SortingStrategy(),
            VocabularyFlashcardStrategy(),
            VocabularyTestStrategy(),
            DropdownSelectStrategy(),
            BankedClozeStrategy(),
            MatchingStrategy(),
            ListeningFillInStrategy(),
            StandardChoiceStrategy(),
            MyVoiceTextStrategy(),
            TextInputStrategy(),
            FillInStrategy(),
            GenericReplyStrategy(),
        ]

    def _find_reading_question_containers(self) -> List[Any]:
        try:
            direction_text = ""
            try:
                direction_elem = self.driver.find_element(
                    By.CSS_SELECTOR,
                    ".layout-direction-container .component-htmlview"
                )
                direction_text = direction_elem.text.lower()
            except:
                pass

            is_multi_question_type = any(kw in direction_text for kw in [
                'answer', 'question', 'according to',
                'translate',
            ])

            if not is_multi_question_type:
                return []

            body_selectors = [
                '.layoutBody-container.has-material.has-reply',
                '.layoutBody-container.has-reply',
            ]

            for selector in body_selectors:
                body_containers = self.driver.find_elements(By.CSS_SELECTOR, selector)

                for body in body_containers:
                    has_scoop = body.find_elements(By.CSS_SELECTOR, '.fe-scoop')
                    if has_scoop:
                        continue

                    has_options = body.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options')
                    if has_options:
                        continue

                    reply_containers = body.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply')

                    valid_replies = []
                    for reply in reply_containers:
                        try:
                            if reply.is_displayed() and reply.find_elements(By.CSS_SELECTOR, '.question-inputbox'):
                                valid_replies.append(reply)
                        except:
                            continue

                    if len(valid_replies) >= 2:
                        return valid_replies

            return []

        except Exception as e:
            logger.debug(f"查找问答题失败: {e}")
            return []

    def _extract_directions_from_page(self) -> str:
        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".layout-direction-container .component-htmlview"
            )
            return direction_elem.text.strip()
        except:
            pass

        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".abs-direction .content"
            )
            return direction_elem.text.strip()
        except:
            pass

        try:
            direction_elem = self.driver.find_element(
                By.CSS_SELECTOR,
                ".direction-container"
            )
            return direction_elem.text.strip()
        except:
            pass

        return ""

    def parse_all(self) -> Tuple[List[Question], str]:
        try:
            return self._parse_all_once()
        except StaleElementReferenceException:
            print("     页面已更新，重新获取题目")
            return self._parse_all_once()

    def _parse_all_once(self) -> Tuple[List[Question], str]:
        self.incomplete = False
        self.non_answer_containers = 0
        if self._is_discussion_board_page():
            print("     检测到讨论板页面，交给发帖处理器")
            try:
                element = self.driver.find_element(By.TAG_NAME, 'body')
            except Exception:
                element = None
            topic = ""
            for selector in ('.ds-discussion-title', '.discussion-title'):
                try:
                    topic = (self.driver.find_element(By.CSS_SELECTOR, selector).text or '').strip()
                    if topic:
                        break
                except Exception:
                    continue
            return [Question(number=1, text=topic or "讨论区发帖",
                             q_type=QuestionType.DISCUSSION_BOARD, element=element)], ""

        containers = self._find_containers()
        questions = []
        directions = self._extract_directions_from_page()

        print(f"     找到 {len(containers)} 个题目容器")

        for idx, container in enumerate(containers, 1):
            try:
                if not self._is_really_visible(container):
                    print(f"      容器 {idx} 不可见，跳过")
                    continue

                question = self._parse_single(container, idx, directions)
                if question:
                    if question.is_interactive():
                        questions.append(question)
                        print(f"      题目 {idx}: {question.q_type.name} - {question.text[:50]}...")
                    else:
                        print(f"      题目 {idx} 非交互类型: {question.q_type.name}")
                        if self._has_answer_widgets(container):
                            self.incomplete = True
                        else:
                            self.non_answer_containers += 1
                else:
                    print(f"      容器 {idx} 解析为None")
                    if self._has_answer_widgets(container):
                        self.incomplete = True
                    else:
                        self.non_answer_containers += 1

            except StaleElementReferenceException:
                raise
            except Exception as e:
                error_msg = str(e)
                print(f"       解析容器 {idx} 失败:{error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                if self._has_answer_widgets(container):
                    self.incomplete = True
                continue

        return questions, directions

    @staticmethod
    def _has_answer_widgets(container) -> bool:
        selector = ', '.join(ANSWER_WIDGET_SELECTORS)
        return any(element.is_displayed()
                   for element in container.find_elements(By.CSS_SELECTOR, selector))

    def _is_discussion_board_page(self) -> bool:
        try:
            strong_indicators = [
                '.discussion-course-page-sdk',
                '.discussion-title',
                '.ds-discussion-bottom-textArea-container',
                '.discussion-cloud-recordList'
            ]

            score = 0
            for indicator in strong_indicators:
                if self.driver.find_elements(By.CSS_SELECTOR, indicator):
                    score += 1

            if score >= 2:
                print(f"     讨论板检测得分: {score}/{len(strong_indicators)}")
                return True
            return False
        except Exception as e:
            error_msg = str(e)
            print(f"操作失败: {error_msg[:50]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return False

    def _find_containers(self) -> List[Any]:
        if self._is_discussion_board_page():
            print("     检测到讨论板页面，跳过")
            return []

        role_play = [c for c in self.driver.find_elements(
            By.CSS_SELECTOR, '.layoutBody-container')
                     if c.is_displayed() and c.find_elements(By.CSS_SELECTOR, '.role-play-wrapper')]
        if role_play:
            return role_play

        try:
            vocab_containers = [c for c in self.driver.find_elements(
                By.CSS_SELECTOR, VOCAB_READ_ALOUD_SELECTOR) if c.is_displayed()]
        except Exception:
            vocab_containers = []
        if vocab_containers:
            print(f"     找到 {len(vocab_containers)} 个词汇跟读页")
            return vocab_containers

        try:
            aloud_containers = [c for c in self.driver.find_elements(
                By.CSS_SELECTOR, '.p-oral-aloud') if c.is_displayed()
                and c.find_elements(By.CSS_SELECTOR, '.p-oral-aloud-button')]
        except Exception:
            aloud_containers = []
        if aloud_containers:
            print(f"     找到 {len(aloud_containers)} 个段落朗读题")
            return aloud_containers

        try:
            dub_containers = [c for c in self.driver.find_elements(
                By.CSS_SELECTOR, '.question-video-dub-pc') if c.is_displayed()]
        except Exception:
            dub_containers = []
        if dub_containers:
            print(f"     找到 {len(dub_containers)} 个视频配音题")
            return dub_containers

        record_containers = self._find_record_containers()
        if record_containers:
            print(f"     找到 {len(record_containers)} 个跟读录音题容器")
            return record_containers

        checklist_containers = []
        for container in self.driver.find_elements(By.CSS_SELECTOR, '.layoutBody-container'):
            try:
                text = (container.text or '').lower()
                if 'got it' not in text:
                    continue
                has_table = container.find_elements(By.CSS_SELECTOR, 'table, .ant-table-tbody')
                has_icon = container.find_elements(By.CSS_SELECTOR, '.anticon-border, [aria-label="border"]')
                if has_table and has_icon and container.is_displayed():
                    checklist_containers.append(container)
            except Exception:
                continue
        if checklist_containers:
            print(f"     找到 {len(checklist_containers)} 个自评清单容器")
            return checklist_containers

        reading_containers = self._find_reading_question_containers()
        if reading_containers:
            print(f"     找到 {len(reading_containers)} 道阅读问答题（共享材料）")
            return reading_containers

        sequence_containers = []
        for reply in self.driver.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply'):
            try:
                if reply.is_displayed() and reply.find_elements(By.CSS_SELECTOR, '.sequence-view, .sortable-list-wrapper'):
                    sequence_containers.append(reply)
            except:
                continue
        if sequence_containers:
            print(f"     找到 {len(sequence_containers)} 道排序题")
            return sequence_containers

        choice_containers = self.driver.find_elements(
            By.CSS_SELECTOR,
            '.question-common-abs-reply > .question-common-abs-choice'
        )

        if len(choice_containers) >= 2:
            reply_containers = []
            for choice in choice_containers:
                try:
                    reply = choice.find_element(By.XPATH,
                                                './parent::div[contains(@class, "question-common-abs-reply")]')
                    if reply not in reply_containers:
                        reply_containers.append(reply)
                except:
                    pass

            if reply_containers:
                print(f"     找到 {len(reply_containers)} 道独立选择题")
                return reply_containers

        banked_containers = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container.has-material.has-reply']
        )
        valid_banked = []
        for container in banked_containers:
            has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option')
            has_blanks = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .comp-abs-input input')
            if has_options and has_blanks:
                valid_banked.append(container)

        if valid_banked:
            print(f"     找到 {len(valid_banked)} 个选词填空容器")
            return valid_banked

        self_check_containers = []
        for container in self.driver.find_elements(By.CSS_SELECTOR, '.layoutBody-container'):
            try:
                has_table = container.find_elements(By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody')
                has_got_it = 'got it' in container.text.lower()
                if has_table and has_got_it and container.is_displayed():
                    self_check_containers.append(container)
            except:
                continue

        if self_check_containers:
            print(f"     找到 {len(self_check_containers)} 个Self-check词汇勾选容器")
            return self_check_containers

        video_containers = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container:has(video)', '.question-video-point-read', '.video-box']
        )
        if video_containers:
            for container in video_containers:
                has_questions = container.find_elements(By.CSS_SELECTOR,
                                                        '.question-common-abs-choice, .question-inputbox, .option, .fe-scoop')
                if not has_questions:
                    print(f"     找到纯视频容器")
                    return [container]

        containers = self.driver.find_elements(By.CSS_SELECTOR, '.layout-container')
        valid_containers = []
        for c in containers:
            try:
                has_content = (
                        c.find_elements(By.CSS_SELECTOR, '.question-inputbox, .option, .fe-scoop, textarea') or
                        c.find_elements(By.CSS_SELECTOR, 'input[type="text"]')
                )
                if not has_content:
                    has_content = has_visible_dropdown(self.driver, c)
                if has_content and c.is_displayed():
                    valid_containers.append(c)
            except:
                continue

        if valid_containers:
            print(f"     找到 {len(valid_containers)} 个有效题目容器（layout-container）")
            return valid_containers

        fallback = WebDriverHelper.safe_find_elements(
            self.driver,
            ['.layoutBody-container', '.layout-reply-container', '.reply-wrap']
        )
        if fallback:
            print(f"     备用方案找到 {len(fallback)} 个容器")
            return fallback

        reply_fallback = []
        for reply in self.driver.find_elements(By.CSS_SELECTOR, '.question-common-abs-reply'):
            try:
                if not reply.is_displayed():
                    continue
                if reply.find_elements(By.CSS_SELECTOR, 'textarea, .fe-scoop input'):
                    reply_fallback.append(reply)
            except Exception:
                continue
        if reply_fallback:
            print(f"     兜底找到 {len(reply_fallback)} 个作答容器")
            return reply_fallback

        return []

    def _find_record_containers(self) -> List[Any]:
        buttons = find_record_buttons(self.driver)
        if not buttons:
            return []

        rows = []
        for button in buttons:
            row = None
            for xpath in (
                "ancestor::*[contains(@class,'oral-study-sentence')][1]",
                "ancestor::div[contains(@class,'oral-paragraph-container')][1]",
                "ancestor::div[contains(@class,'record-button-group')][1]",
                "ancestor::div[contains(@class,'layoutBody-container')][1]",
            ):
                try:
                    row = button.find_element(By.XPATH, xpath)
                    break
                except Exception:
                    continue
            if row is None:
                try:
                    row = button.find_element(
                        By.XPATH, "ancestor::div[contains(@class,'question-')][1]")
                except Exception:
                    row = None
            if row is None:
                continue
            if row not in rows:
                rows.append(row)
        return rows

    def _is_really_visible(self, element) -> bool:
        try:
            if not element.is_displayed():
                return False

            parent = element
            for _ in range(3):
                try:
                    parent = parent.find_element(By.XPATH, '..')
                    parent_display = self.driver.execute_script(
                        "return window.getComputedStyle(arguments[0]).display",
                        parent
                    )
                    if parent_display == 'none':
                        return False
                except StaleElementReferenceException:
                    raise
                except:
                    break

            return True
        except StaleElementReferenceException:
            raise
        except:
            return False

    def _parse_single(self, container, number: int, directions: str = "") -> Optional[Question]:
        for strategy in self.strategies:
            try:
                if strategy.can_parse(container, self.driver):
                    print(f"      使用策略: {strategy.__class__.__name__}")
                    question = strategy.parse(container, self.driver, number, directions)
                    if question:
                        if question.number is None:
                            question.number = number
                        print(f"      解析成功: {question.q_type.name}")
                        return question
                    else:
                        print(f"      策略返回None")
            except StaleElementReferenceException:
                raise
            except Exception as e:
                error_msg = str(e)
                print(f"      策略 {strategy.__class__.__name__} 失败: {error_msg[:50]} ")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue
        print(f"      没有匹配的策略")
        return None


class RolePlayStrategy(QuestionParserStrategy):
    def can_parse(self, container, driver) -> bool:
        return bool(container.find_elements(By.CSS_SELECTOR, '.role-play-wrapper'))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(number=question_number, text=directions or '角色扮演录音',
                        q_type=QuestionType.RECORD, element=container, directions=directions,
                        record_items=[{'role_play': True, 'text': '角色扮演'}])


class DiscussionBoardStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        discussion_features = [
            '.discussion-course-page-sdk',
            '.ds-discussion-reply',
            '.discussion-cloud-recordList-title',
        ]

        has_discussion_feature = any(
            container.find_elements(By.CSS_SELECTOR, feature)
            for feature in discussion_features
        )

        if not has_discussion_feature:
            return False

        banked_features = [
            '.question-material-banked-cloze-reply',
            '.banked-options',
            '.fe-scoop[data-scoop-index]',
        ]

        has_banked_feature = any(
            container.find_elements(By.CSS_SELECTOR, feature)
            for feature in banked_features
        )
        if has_banked_feature:
            return False

        return True

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="讨论板页面（无需作答）",
            q_type=QuestionType.DISCUSSION_BOARD,
            element=container
        )


class SelfCheckStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        direction_text = ""
        try:
            direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            direction_text = direction_elem.text.lower()
        except:
            pass

        has_table = bool(container.find_elements(By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody'))
        has_got_it = 'got it' in container.text.lower()
        has_check_instruction = any(kw in direction_text for kw in (
            'check in the box', "you've learned", 'you have learned', 'self-assessment', 'checklist'))
        return has_table and has_got_it and has_check_instruction

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        rows = []
        for row in container.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)'):
            try:
                word_elem = row.find_element(By.CSS_SELECTOR, '.content-text')
                word = word_elem.text.strip()
                got_it_icon = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2) .anticon')
                if word and got_it_icon:
                    rows.append({'word': word, 'element': got_it_icon})
            except:
                continue

        return Question(
            number=question_number,
            text=f"Self-check 词汇勾选（共{len(rows)}项）",
            q_type=QuestionType.SELF_CHECK,
            element=container,
            banked_blanks=rows,
            directions=directions,
        )


class SortingStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        return bool(container.find_elements(By.CSS_SELECTOR, '.sequence-view, .sortable-list-wrapper'))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        option_elements = container.find_elements(By.CSS_SELECTOR, '.sequence-reply-view-item-text')
        options = []

        for idx, elem in enumerate(option_elements):
            full_text = elem.text.strip()
            if not full_text:
                continue

            letter = ""
            text = full_text
            try:
                spans = elem.find_elements(By.TAG_NAME, 'span')
                if spans:
                    letter = spans[0].text.strip().replace('.', '').replace(')', '').upper()
                    if len(spans) > 1:
                        text = spans[1].text.strip()
            except:
                pass

            if not letter:
                match = re.match(r'^([A-Z])[\s.、)]*(.+)$', full_text, re.DOTALL)
                if match:
                    letter = match.group(1).upper()
                    text = match.group(2).strip()
                else:
                    letter = chr(65 + idx)

            options.append(Option(letter=letter, text=text, element=elem))

        if len(options) < 2:
            return None

        question_text = "排序题：请根据材料出现顺序重新排列选项"
        if directions:
            question_text = f"{directions}\n{question_text}"

        return Question(
            number=question_number,
            text=question_text,
            q_type=QuestionType.SORTING,
            element=container,
            options=options,
            directions=directions,
        )


class VocabularyTestStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        if self._is_multiple_choice(container):
            return False
        options = self._extract_options(container, driver)
        if len(options) < 2:
            return False

        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        if not title_elem:
            return False

        text = title_elem.text.strip()
        text = re.sub(r"^\d+[.、)\]]\s*", "", text)

        is_eng_word = bool(re.match(r"^[a-zA-Z\-]+$", text)) and 1 < len(text) <= 20
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))

        option_texts = [opt.text for opt in options]
        has_eng_opts = any(re.search(r"[a-zA-Z]{3,}", t) for t in option_texts)
        has_chi_opts = any(re.search(r"[\u4e00-\u9fff]", t) for t in option_texts)

        return (is_eng_word and has_chi_opts) or (has_chinese and has_eng_opts)

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else ""

        options = self._extract_options(container, driver)

        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.VOCABULARY_TEST,
            element=container,
            options=options,
            directions=directions
        )

    def _extract_options(self, container, driver) -> List[Option]:
        options = []
        option_elements = WebDriverHelper.safe_find_elements(driver, Selectors.CHOICE_OPTIONS, container)

        for opt_elem in option_elements:
            letter = ""
            text = ""

            caption_elem = WebDriverHelper.safe_find_element(driver, Selectors.OPTION_CAPTION, opt_elem)
            if caption_elem:
                letter = caption_elem.text.strip().replace('.', '').replace(')', '').replace('、', '')

            content_elem = WebDriverHelper.safe_find_element(driver, Selectors.OPTION_CONTENT, opt_elem)
            if content_elem:
                text = content_elem.text.strip()
            else:
                full_text = opt_elem.text.strip()
                text = re.sub(rf"^{re.escape(letter)}[.)、\\s]*", "", full_text)

            is_selected = 'selected' in (opt_elem.get_attribute('class') or '').lower()

            if letter or text:
                options.append(Option(letter=letter, text=text, element=opt_elem, is_selected=is_selected))

        return options


class BankedClozeStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        has_options = bool(
            container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option, .option-wrapper .option-placeholder') or
            container.find_elements(By.CSS_SELECTOR, '.banked-options .option')
        )
        has_blanks = bool(container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .comp-abs-input input'))
        return has_options and has_blanks

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        banked_options = []

        placeholder_elements = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option-placeholder')
        for elem in placeholder_elements:
            text = elem.text.strip()
            if text and text not in banked_options:
                banked_options.append(text)

        if not banked_options:
            option_elements = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option')
            for elem in option_elements:
                text = elem.text.strip()
                if text and text not in banked_options:
                    banked_options.append(text)

        if not banked_options:
            option_elements = container.find_elements(By.CSS_SELECTOR,
                                                      '.banked-options .option, [data-rbd-draggable-id^="options-"]')
            for elem in option_elements:
                text = elem.text.strip()
                if text and text not in banked_options:
                    banked_options.append(text)

        print(f"      选项池（短语）: {banked_options}")

        inputs = []
        banked_blanks = []

        scoops = container.find_elements(By.CSS_SELECTOR, '.fe-scoop')

        for i, scoop in enumerate(scoops):
            context = ""
            try:
                context_elem = scoop.find_element(By.XPATH, './ancestor::p')
                context = driver.execute_script("""
                    const p = arguments[0];
                    const clone = p.cloneNode(true);
                    for (const el of clone.querySelectorAll('input, textarea')) {
                        el.replaceWith(document.createTextNode(' ____ '));
                    }
                    return (clone.textContent || '').trim();
                """, context_elem) or ""
                context = re.sub(r'\s+', ' ', context).strip()
            except:
                try:
                    context = scoop.text.strip()
                except:
                    context = ""

            input_box = None
            try:
                input_box = scoop.find_element(By.CSS_SELECTOR, 'input')
                inputs.append(input_box)
            except:
                pass

            banked_blanks.append({
                'index': i,
                'context': context,
                'input': input_box,
                'element': scoop
            })

        print(f"      找到 {len(banked_blanks)} 个填空位置")

        material = FillInStrategy._page_material(container, limit=6000)
        question_text = f"选词填空（{len(banked_blanks)}个空）"
        if material:
            question_text += f"\n【全文】{material}"
        if banked_options:
            question_text += f"\n可选选项: {', '.join(banked_options)}"


        return Question(
            number=question_number,
            text=question_text,
            q_type=QuestionType.BANKED_CLOZE,
            element=container,
            inputs=inputs,
            banked_options=banked_options,
            banked_blanks=banked_blanks,
            directions=directions,
        )


class StandardChoiceStrategy(QuestionParserStrategy):
    def _is_listening_choice_page(self, driver, directions: str = "") -> bool:
        direction_text = (directions or "").lower()
        if not direction_text:
            try:
                direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                direction_text = direction_elem.text.lower()
            except:
                direction_text = ""

        has_listening_hint = any(kw in direction_text for kw in ['listen', 'audio', 'hear', 'conversation', 'passage', 'news'])
        has_choice_hint = any(kw in direction_text for kw in ['choose', 'answer', 'best answer'])
        return has_listening_hint and has_choice_hint

    def _is_video_choice_page(self, driver, directions: str = "") -> bool:
        direction_text = (directions or "").lower()
        if not direction_text:
            try:
                direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                direction_text = direction_elem.text.lower()
            except:
                direction_text = ""

        has_video_hint = any(kw in direction_text for kw in ['watch', 'video', 'clip'])
        has_choice_hint = any(kw in direction_text for kw in ['choose', 'answer', 'decide', 'true or false', 'statements'])
        return has_video_hint and has_choice_hint

    def can_parse(self, container, driver) -> bool:
        if container.tag_name == 'div' and 'question-common-abs-choice' in (container.get_attribute('class') or ''):
            options = container.find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')
        else:
            choices = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
            if choices:
                if len(choices) > 1:
                    return False
                options = choices[0].find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')
            else:
                options = container.find_elements(By.CSS_SELECTOR, '.option-wrap .option, .option.isNotReview')

        return len(options) >= 2

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        if 'question-common-abs-choice' in (container.get_attribute('class') or ''):
            choice_container = container
        else:
            choices = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
            choice_container = choices[0] if choices else container

        title_elem = choice_container.find_element(By.CSS_SELECTOR, '.ques-title')
        text = title_elem.text.strip() if title_elem else ""

        vocab_strategy = VocabularyTestStrategy()
        options = vocab_strategy._extract_options(container, driver)

        is_listening_choice = self._is_listening_choice_page(driver, directions)
        is_video_choice = self._is_video_choice_page(driver, directions)
        is_multi = self._is_multiple_choice(container, directions)
        if is_video_choice and not is_multi:
            q_type = QuestionType.VIDEO_CHOICE
        elif is_listening_choice and not is_multi:
            q_type = QuestionType.LISTENING_CHOICE
        else:
            q_type = QuestionType.MULTIPLE_CHOICE if is_multi else QuestionType.SINGLE_CHOICE

        return Question(
            number=question_number,
            text=text,
            q_type=q_type,
            element=container,
            options=options,
            directions=directions
        )


class MyVoiceTextStrategy(QuestionParserStrategy):

    TEXTAREA_SELECTORS = [
        '.question-multi-file-upload textarea.ant-input',
        '.question-multi-file-upload textarea',
        'textarea[placeholder*="输入文字作答"]',
    ]

    def can_parse(self, container, driver) -> bool:
        textarea = self._find_textarea(container)
        if not textarea:
            return False

        has_upload = bool(container.find_elements(By.CSS_SELECTOR, '.question-multi-file-upload, .unipus-upload'))
        if not has_upload:
            return False

        page_text = self._collect_page_text(container, driver).lower()
        return any(kw in page_text for kw in ['record', 'upload', 'introduction', 'my voice', '输入文字作答'])

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        textarea = self._find_textarea(container)
        if not textarea:
            return None

        material_text = self._extract_material_text(container, driver)
        full_text = ""
        if directions:
            full_text += f"【题目要求】{directions}\n\n"
        if material_text:
            full_text += f"【任务材料】\n{material_text}\n\n"
        full_text += "请根据以上要求，直接写一段可粘贴到输入框的英文介绍，控制在500字符以内。"

        return Question(
            number=question_number,
            text=full_text,
            q_type=QuestionType.MY_VOICE_TEXT,
            element=container,
            inputs=[textarea],
            directions=directions,
        )

    def _find_textarea(self, container):
        for selector in self.TEXTAREA_SELECTORS:
            try:
                for elem in container.find_elements(By.CSS_SELECTOR, selector):
                    if elem.is_displayed():
                        return elem
            except:
                continue
        return None

    def _collect_page_text(self, container, driver) -> str:
        parts = []
        for source in [container, driver]:
            try:
                text = source.text.strip()
                if text:
                    parts.append(text)
            except:
                pass
        return "\n".join(parts)

    def _extract_material_text(self, container, driver) -> str:
        selectors = [
            '.layout-material-container',
            '.question-common-abs-material',
            '.text-material-wrapper',
        ]
        for selector in selectors:
            try:
                elem = container.find_element(By.CSS_SELECTOR, selector)
                text = elem.text.strip()
                if text:
                    return text
            except:
                continue

        for selector in selectors:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, selector)
                text = elem.text.strip()
                if text:
                    return text
            except:
                continue
        return ""


VOCAB_READ_ALOUD_SELECTOR = '.vocabulary-wrapper'

VOCAB_CARD_JS = r"""
return (function (root) {
  const scope = root || document;
  const wrapper = (scope.matches && scope.matches('.vocabulary-wrapper'))
      ? scope
      : (scope.querySelector('.vocabulary-wrapper') ||
         document.querySelector('.vocabulary-wrapper'));
  if (!wrapper) { return {}; }
  const slide = wrapper.querySelector('.swiper-slide-active') ||
                wrapper.querySelector('.swiper-vocabulary-slide');
  if (!slide) { return {}; }
  const txt = function (sel) {
    const n = slide.querySelector(sel);
    return n ? (n.innerText || n.textContent || '').trim() : '';
  };
  const audioNode = slide.querySelector('.soundWrap .question-audio audio, .soundWrap audio');
  const raw = audioNode
      ? (audioNode.getAttribute('src') || audioNode.getAttribute('title') || '')
      : '';
  let duration = 0;
  const dm = raw.match(/duration=([0-9.]+)/);
  if (dm) { duration = parseFloat(dm[1]); }
  let name = '';
  const nm = raw.match(/name=([^&]*)/);
  if (nm) { try { name = decodeURIComponent(nm[1]); } catch (e) { name = nm[1]; } }
  let index = 0, total = 0;
  const spans = wrapper.querySelectorAll('.progressBar .ratio span');
  if (spans.length >= 2) {
    index = parseInt((spans[0].innerText || spans[0].textContent || '').trim(), 10) || 0;
    total = parseInt((spans[1].innerText || spans[1].textContent || '')
        .replace(/[^0-9]/g, ''), 10) || 0;
  }
  const button = slide.querySelector('.ucomp-recorder .button-record, .button-record');
  const recordings = Array.from(slide.querySelectorAll('.practice-recorder audio'))
      .map(a => a.getAttribute('src') || '').filter(Boolean);
  return {
    word: txt('.vocWord .component-htmlview'),
    definition: txt('.explanations'),
    audio: raw,
    duration: duration,
    name: name,
    index: index,
    total: total,
    hasButton: !!button,
    recording: !!(button && button.querySelector('svg rect[fill="currentColor"]')),
    recordings: recordings
  };
})(arguments[0]);
"""

VOCAB_NEXT_JS = r"""
return (function (root, reset) {
  const scope = root || document;
  const wrapper = (scope.matches && scope.matches('.vocabulary-wrapper'))
      ? scope
      : (scope.querySelector('.vocabulary-wrapper') ||
         document.querySelector('.vocabulary-wrapper'));
  if (!wrapper) { return ''; }
  const sw = wrapper.querySelector('.swiper-container') ||
             wrapper.querySelector('[class*="swiper-container"]');
  const move = function (inst) {
    const method = reset ? 'slideTo' : 'slideNext';
    if (!inst || typeof inst[method] !== 'function') { return false; }
    if (reset) { inst.slideTo(0); } else { inst.slideNext(); }
    return true;
  };
  if (sw && move(sw.swiper)) {
    return 'swiper';
  }
  const btn = !reset && wrapper.querySelector(
      '.swiper-button-next, [class*="vocNext"], [class*="next-btn"], [class*="nextBtn"]');
  if (btn) { btn.click(); return 'button'; }
  for (const key of ['vocSwiper', 'swiper', 'mySwiper']) {
    const inst = window[key];
    if (move(inst)) {
      return 'window.' + key;
    }
  }
  return '';
})(arguments[0], arguments[1]);
"""

RECORD_BUTTON_SELECTOR = '.record-icon.button-record'

RECORD_BUTTON_FALLBACKS = (
    '.button-record',
    '.oral-record-btn',
    '.record-btn',
    '.record-button',
    '[class*="record"][class*="btn"]',
)


def record_button_selectors() -> Tuple[str, ...]:
    return (RECORD_BUTTON_SELECTOR,) + RECORD_BUTTON_FALLBACKS


SKIP_ASR_TITLE_KEYWORDS = ('skill', '技能')

ANSWER_WIDGET_SELECTORS = (
    '.question-common-abs-choice',
    '.option-wrapper .option',
    '.option-wrap',
    '.banked-options',
    '.fe-scoop',
    '.question-common-abs-scoop',
    '.comp-abs-input input',
    '.question-inputbox',
    '.question-common-abs-reply',
    '.question-common-abs-textmatch .input-wrapper',
    '.record-icon.button-record',
    '.button-record',
    '.blank-item',
    '[class*="cloze"]',
    '[class*="fill-blank"]',
    'input[type="text"]',
    'input[type="radio"]',
    'input[type="checkbox"]',
    'textarea',
    'select',
    '[contenteditable="true"]',
)


def find_record_buttons(driver, scope=None) -> List[Any]:
    root = driver if scope is None else scope
    for selector in record_button_selectors():
        try:
            buttons = root.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        if not isinstance(buttons, (list, tuple)):
            continue
        usable = []
        for button in buttons:
            try:
                if not button.is_displayed():
                    continue
                size = button.size or {}
                if (size.get('width') or 0) > 180 or (size.get('height') or 0) > 180:
                    continue
            except Exception:
                continue
            usable.append(button)
        if usable:
            return usable
    return []


VOCAB_PAIRS_JS = r"""
return (function () {
  const out = [];
  const seen = new Set();
  const CJK = /[\u4e00-\u9fa5]/;
  const push = function (en, zh) {
    en = (en || '').replace(/\s+/g, ' ').trim().replace(/^[\s"'“”]+|[\s"'“”]+$/g, '');
    zh = (zh || '').replace(/\s+/g, ' ').trim();
    if (!en || !zh) { return; }
    if (!/[A-Za-z]{2}/.test(en) || CJK.test(en)) { return; }
    if (!CJK.test(zh)) { return; }
    if (en.length > 80 || zh.length > 40) { return; }
    const key = en.toLowerCase() + '|' + zh;
    if (seen.has(key)) { return; }
    seen.add(key);
    out.push({ en: en, zh: zh });
  };
  const decodeName = function (raw) {
    const match = raw.match(/name=([^&]*)/);
    if (!match) { return ''; }
    try { return decodeURIComponent(match[1]); } catch (e) { return match[1]; }
  };
  const rowText = function (row) {
    const lines = (row.innerText || '').split('\n').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    let en = '', zh = '';
    for (const line of lines) {
      if (!zh && CJK.test(line)) { zh = line; continue; }
      if (!en && /[A-Za-z]{2}/.test(line) && !CJK.test(line) && line.length <= 80) { en = line; }
    }
    return { en: en, zh: zh };
  };
  const wrapRows = Array.from(document.querySelectorAll('.record-button-wrap'));
  for (const wrap of wrapRows) {
    const row = wrap.closest('.oral-study-sentence') || wrap.closest('.record-button-group') ||
      wrap.parentElement;
    if (!row) { continue; }
    const text = rowText(row);
    const audio = wrap.querySelector('audio');
    const fromAudio = audio ? decodeName(audio.getAttribute('src') || '') : '';
    push(fromAudio || text.en, text.zh);
  }

  const listRows = Array.from(document.querySelectorAll(
    '.vocabulary-list li, .word-list li, .vocab-list li, .vocabulary-item, .word-item'));
  for (const row of listRows) {
    const text = rowText(row);
    push(text.en, text.zh);
  }
  return out;
})();
"""

# 关键细节：录音器开始拉流后的前 ~0.6 秒必丢块并把丢失部分补成静音，
# 所以参考音频前面要垫 LEAD_PAD 秒静音（实测 0.8s 时逐词满分）。
RECORD_MIC_BOOTSTRAP_JS = r"""
(function () {
  if (window.__ucRec) { return; }
  const md = navigator.mediaDevices || (navigator.mediaDevices = {});
  const api = { buf: null, ctx: null, pad: 0.8, calls: 0, dur: 0, srcDur: 0, maxDur: 0 };
  api.ctxOf = function () {
    const ctx = api.ctx || (api.ctx = new AudioContext());
    try { ctx.resume(); } catch (e) {}
    return ctx;
  };
  api.silent = function () {
    return api.ctxOf().createMediaStreamDestination().stream;
  };
  api.stream = function () {
    const ctx = api.ctxOf();
    if (!api.buf) { return ctx.createMediaStreamDestination().stream; }
    const padSec = api.pad || 0;
    const sr = api.buf.sampleRate;

    let srcFrames = api.buf.length;
    if (api.maxDur > 0 && srcFrames > Math.round(api.maxDur * sr)) {
      srcFrames = Math.round(api.maxDur * sr);
    }
    const frames = srcFrames + Math.round(padSec * sr);
    const buf = ctx.createBuffer(api.buf.numberOfChannels, frames, sr);
    const fade = Math.min(Math.round(0.25 * sr), Math.floor(srcFrames / 4));
    for (let c = 0; c < api.buf.numberOfChannels; c++) {
      const src = api.buf.getChannelData(c);
      const dst = buf.getChannelData(c);
      dst.set(src.subarray(0, srcFrames), Math.round(padSec * sr));
      for (let i = 0; i < fade; i++) {
        dst[frames - 1 - i] *= i / fade;
      }
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const dest = ctx.createMediaStreamDestination();
    src.connect(dest);
    src.start();
    api.calls += 1;
    api.dur = buf.duration;
    return dest.stream;
  };
  md.getUserMedia = function () { return Promise.resolve(api.stream()); };
  md.getDisplayMedia = md.getDisplayMedia || function () { return Promise.resolve(api.silent()); };
  window.__ucRec = api;
})();
"""

RECORD_MIC_JS = r"""
const done = arguments[arguments.length - 1];
const b64 = arguments[0] || '';
const pad = (typeof arguments[1] === 'number') ? arguments[1] : 0.8;
const maxDur = (typeof arguments[2] === 'number') ? arguments[2] : 0;
(async () => {
  const api = window.__ucRec;
  if (!api) { done({ ok: false, err: 'bootstrap missing' }); return; }
  api.pad = pad;
  api.maxDur = maxDur;
  if (!b64) { done({ ok: true, dur: api.dur, srcDur: api.srcDur, pad: pad, reused: true }); return; }
  const ctx = api.ctxOf();
  const raw = atob(b64);
  const u8 = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) u8[i] = raw.charCodeAt(i);
  const buf = await ctx.decodeAudioData(u8.buffer);
  api.buf = buf;
  api.srcDur = buf.duration;
  const usable = maxDur > 0 ? Math.min(buf.duration, maxDur) : buf.duration;
  api.dur = usable + pad;
  done({ ok: true, dur: api.dur, srcDur: api.srcDur, sr: buf.sampleRate, pad: pad, maxDur: maxDur });
})().catch((e) => done({ ok: false, err: String(e) }));
"""

RECORD_ITEMS_JS = r"""
const root = arguments[0] || document;
const tidy = function (value) {
  let text = (value || '');
  for (const mark of ['(w)', '(W)', '(s)', '(S)']) {
    text = text.split(mark).join(' ');
  }
  return text.split(' ').filter(function (part) { return part.length > 0; })
    .join(' ').trim();
};
const oneLine = function (value) {
  const parts = (value || '').split(String.fromCharCode(10));
  const out = [];
  for (const part of parts) {
    const clean = tidy(part);
    if (clean) { out.push(clean); }
  }
  return out.join(' ');
};
const audioOf = function (scope) {
  if (!scope) { return null; }
  let nodes = [];
  try { nodes = Array.from(scope.querySelectorAll('audio')); } catch (e) { nodes = []; }
  const usable = nodes.filter(function (audio) {
    if (audio.closest('.audio-replay')) { return false; }
    const src = audio.getAttribute('src') || audio.getAttribute('data-src') ||
      audio.getAttribute('title') || '';
    return src.indexOf('http') === 0 || src.indexOf('data:') === 0;
  });
  const origin = usable.find(function (audio) {
    return !!audio.closest('.question-audio');
  });
  return origin || usable[0] || null;
};
return Array.from(arguments[1]).map(function (button) {
  const wrap = button.closest('.record-button-wrap');
  const paragraph = button.closest('.oral-paragraph-container');
  const group = button.closest('.record-button-group');


  const oralBox = button.closest('.oral-personal-state-wrapper');
  const row = button.closest('.oral-study-sentence') || paragraph || root;
  let audio = null;
  for (const scope of [paragraph, group, row]) {
    audio = audioOf(scope);
    if (audio) { break; }
  }
  if (!audio) { audio = audioOf(wrap); }
  const song = (group && group.querySelector('.question-audio.audio-origin')) ||
    (row && row.querySelector('.question-audio.audio-origin'));
  const raw = audio
    ? (audio.getAttribute('src') || audio.getAttribute('data-src') ||
       audio.getAttribute('title') || '')
    : (song ? (song.getAttribute('title') || '') : '');
  let passage = null;
  for (const scope of [paragraph, row]) {
    if (!scope) { continue; }
    passage = scope.querySelector('.sentence-html-container') ||
      scope.querySelector('.oral-personal-state-sentence-container');
    if (passage) { break; }
  }
  const text = passage ? oneLine(passage.innerText || passage.textContent || '') : '';

  const promptBox = oralBox ? oralBox.querySelector('.oral-personal-state-oral-container') : null;
  const promptText = promptBox
    ? oneLine(promptBox.innerText || promptBox.textContent || '') : '';
  let lines = [];
  if (row) {
    lines = (row.innerText || '').split(String.fromCharCode(10))
      .map(function (part) { return tidy(part); })

      .filter(function (part) {
        return part.length > 0 && !/^\d{1,2}:\d{2}$/.test(part) &&
          part.indexOf('点击按钮') === -1 && part.indexOf('结束录音') === -1;
      });
  }
  const rows = Array.from(document.querySelectorAll('.oral-study-sentence'));
  return {
    raw: raw,
    name: text || promptText || (lines.length ? lines[0] : ''),
    text: text,

    prompt: promptText,
    lines: lines.slice(0, 4),
    src: song ? (song.getAttribute('title') || '') : '',
    rowIndex: rows.indexOf(row)
  };
});
"""

RECORD_ROW_STATE_JS = r"""
return (function (root, idx, selectors) {
  const blank = { text: '', score: null, stop: false, wave: '', recorded: false,
                  htmlLen: 0, blob: 0, uploaded: 0 };
  if (!root) { return blank; }
  const sel = (selectors && selectors.length) ? selectors.join(',') : '.record-icon.button-record';
  let rows;
  try {
    rows = (root.matches && root.matches('.oral-study-sentence'))
      ? [root]
      : Array.from(root.querySelectorAll('.oral-study-sentence'));
  } catch (e) { rows = []; }
  if (!rows.length) {
    try {
      rows = (root.matches && root.matches('.question-common-abs-reply > .item'))
        ? [root]
        : Array.from(root.querySelectorAll('.question-video-dub-pc .question-common-abs-reply > .item'));
    } catch (e) { rows = []; }
  }
  if (!rows.length) {
    try { rows = Array.from(root.querySelectorAll('.record-button-group')); } catch (e) { rows = []; }
  }
  if (!rows.length) { rows = [root]; }
  const row = rows[idx] || rows[0];
  if (!row) { return blank; }



  let scope = row;
  const outer = row.closest('.oral-paragraph-container, .p-oral-personal-state');
  if (outer && !row.querySelector('.score_layout') &&
      outer.querySelectorAll(sel).length === 1) {
    scope = outer;
  }
  const node = row.querySelector('.score_layout') ||
    (row.matches && row.matches('.item') ? row.querySelector('.score') : null) ||
    scope.querySelector('.score_layout');
  const text = node ? ((node.innerText || node.textContent || '').trim()) : '';
  const match = text.match(/(\d{1,3})/);
  const icon = row.querySelector(sel);

  const stop = !!(icon && icon.querySelector('svg rect[fill="currentColor"]'));
  const waveNode = scope.querySelector('.audio-control-box.audio-control-wave');


  const audios = Array.from(scope.querySelectorAll('audio')).filter(function (a) {
    if (a.closest('.paragraphPlayerContainer')) { return false; }
    if (a.closest('.record-button-wrap') && !a.closest('.audio-replay')) { return false; }
    return ((a.getAttribute('src') || '').length > 0);
  });
  const blob = audios.filter(function (a) {
    const src = a.getAttribute('src') || '';
    return src.indexOf('blob:') === 0 || src.indexOf('data:') === 0;
  }).length;
  const uploaded = audios.length - blob;
  return {
    text: text,
    score: match ? parseInt(match[1], 10) : null,
    stop: stop,
    wave: waveNode ? waveNode.className : '',
    recorded: (blob + uploaded) > 0,
    htmlLen: (scope.innerHTML || '').length,
    blob: blob,
    uploaded: uploaded
  };
})(arguments[0], arguments[1], arguments[2]);
"""
ORAL_ALOUD_STATE_JS = r"""
const score = document.querySelector('.score_layout');
const btn = document.querySelector('.p-oral-aloud-button');
const timer = document.querySelector('.count-down-time');
const text = score ? (score.innerText || '').replace(/[^0-9.]/g, '') : '';
return {
    score: text,
    btn: btn ? (btn.innerText || '').replace(/\s+/g, ' ').trim() : '',
    timer: timer ? (timer.innerText || '').trim() : '',
    recording: !!document.querySelector('[class*="record-sentence-container"]'),
    done: !!document.querySelector('.p-oral-aloud [class*="luyin"], .p-oral-aloud [class*="huifang"]')
};
"""


RECORD_STATE_JS = r"""
return (function () {

  const rows = Array.from(document.querySelectorAll('.oral-study-sentence, .record-button-group'));
  const pick = (row) => {
    const score = row.querySelector('.score_layout');
    const wave = row.querySelector('.audio-control-box.audio-control-wave');
    return {
      score: score ? (score.innerText || score.textContent || '').trim() : '',
      wave: wave ? (wave.className || '') : '',
      text: (row.innerText || '').trim().slice(0, 80).replace(/\s+/g, ' '),
    };
  };
  return {
    rows: rows.map(pick),
    buttons: document.querySelectorAll('.record-icon.button-record').length,
    calls: (window.__ucRec && window.__ucRec.calls) || 0,
  };
})();
"""


class TTSEngine:

    EDGE_VOICE = "en-US-AriaNeural"
    EDGE_FAILURE_LIMIT = 2
    CACHE_LIMIT = 16
    WORDS_PER_SECOND = 2.4
    SAPI_RATE = 0

    def __init__(self, engine: str = "auto", voice: str = "", proxy: str = "", rate: str = "+0%"):
        self.engine = (engine or "auto").strip().lower()
        self.voice = (voice or "").strip()
        self.proxy = (proxy or os.environ.get("UNIPUS_TTS_PROXY", "") or "").strip()
        self.rate = rate or "+0%"
        self.last_error = ""
        self._edge_failures = 0
        self._cache: Dict[str, Tuple[str, bytes]] = {}

    @property
    def enabled(self) -> bool:
        return self.engine not in ("off", "none", "disable", "disabled")

    def synthesize(self, text: str, max_seconds: float = 110.0) -> Tuple[str, bytes]:
        if not self.enabled:
            self.last_error = "TTS 已在配置里关闭"
            return "", b""

        spoken = self.prepare_text(text, max_seconds)
        if not spoken:
            self.last_error = "没有可朗读的英文文本"
            return "", b""

        key = hashlib.md5(f"{spoken}|{self.voice}|{self.rate}".encode()).hexdigest()
        cached = self._cache.get(key)
        if cached:
            print(f"    TTS 复用缓存（{len(spoken.split())} 词）")
            return cached

        errors = []
        for name in self._engine_order():
            fmt, data = self._run_engine(name, spoken)
            if data:
                if len(self._cache) >= self.CACHE_LIMIT:
                    self._cache.clear()
                self._cache[key] = (fmt, data)
                print(f"    TTS({name}) 合成 {len(spoken.split())} 词 / {len(data)} 字节")
                return fmt, data
            errors.append(f"{name}: {self.last_error}")
        self.last_error = "; ".join(e for e in errors if e)
        return "", b""

    def _engine_order(self) -> Tuple[str, ...]:
        if self.engine in ("sapi", "system", "windows"):
            return ("sapi",)
        if self.engine == "edge":
            return ("edge",)
        return ("edge", "sapi")

    def _run_engine(self, name: str, text: str) -> Tuple[str, bytes]:
        try:
            if name == "edge":
                return self._synth_edge(text)
            return self._synth_sapi(text)
        except Exception as exc:
            self.last_error = f"{name} 合成异常: {str(exc)[:120]}"
            logger.error("TTS 合成失败", exc_info=True)
            return "", b""

    def prepare_text(self, text: str, max_seconds: float = 110.0) -> str:
        raw = (text or '').replace('\r', '\n')
        raw = re.sub(r'^[ \t]*```[a-zA-Z]*[ \t]*$', '', raw, flags=re.M)
        raw = re.sub(r'```', ' ', raw)
        raw = re.sub(r'[*_`#>]+', ' ', raw)

        kept = []
        for line in raw.split('\n'):
            line = line.strip()
            if not line:
                continue
            if not re.search(r'[A-Za-z]{2}', line):
                continue
            line = re.sub(r'^[\s\-–—•\d]+[.、)）:：]\s*', '', line)
            kept.append(line)
        spoken = ' '.join(kept)
        spoken = re.sub(r'[（(][^）)]*[\u4e00-\u9fa5][^）)]*[）)]', ' ', spoken)
        spoken = re.sub(r'\s+', ' ', spoken).strip()
        if not spoken:
            return ""
        return self._trim_to_seconds(spoken, max_seconds)

    def _trim_to_seconds(self, text: str, max_seconds: float) -> str:
        budget = int(max(15.0, float(max_seconds or 0)) * self.WORDS_PER_SECOND)
        words = text.split()
        if len(words) <= budget:
            return text

        clipped = ' '.join(words[:budget])
        end = max(clipped.rfind('. '), clipped.rfind('! '), clipped.rfind('? '))
        if end >= len(clipped) * 0.6:
            clipped = clipped[:end + 1]
        return clipped.strip()

    def _synth_edge(self, text: str) -> Tuple[str, bytes]:
        if self._edge_failures >= self.EDGE_FAILURE_LIMIT:
            self.last_error = "edge-tts 连续失败，本轮改用离线引擎"
            return "", b""
        try:
            import edge_tts
        except Exception:
            self._edge_failures = self.EDGE_FAILURE_LIMIT
            self.last_error = "未安装 edge-tts（pip install edge-tts 后自动启用）"
            return "", b""

        voice = self.voice or self.EDGE_VOICE
        path = os.path.join(tempfile.gettempdir(),
                            f"unipus_tts_{int(time.time() * 1000)}_{random.randint(100, 999)}.mp3")
        kwargs = {"rate": self.rate}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            try:
                self._edge_save(edge_tts, text, voice, path, kwargs)
            except TypeError:
                self._edge_save(edge_tts, text, voice, path, {})
            with open(path, 'rb') as handle:
                data = handle.read()
            if not data:
                raise RuntimeError("edge-tts 返回空音频")
            self._edge_failures = 0
            return "mp3", data
        except Exception as exc:
            self._edge_failures += 1
            self.last_error = f"edge-tts 合成失败: {str(exc)[:120]}"
            return "", b""
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _edge_save(edge_tts, text: str, voice: str, path: str, kwargs: Dict[str, Any]):
        import asyncio

        async def run():
            communicate = edge_tts.Communicate(text, voice, **kwargs)
            await communicate.save(path)

        asyncio.run(run())

    @staticmethod
    def _decode_console(raw: bytes) -> str:
        for encoding in ('utf-8', 'mbcs', 'gbk'):
            try:
                return raw.decode(encoding)
            except Exception:
                continue
        return raw.decode('utf-8', 'ignore')

    def _synth_sapi(self, text: str) -> Tuple[str, bytes]:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.last_error = "未找到 powershell，无法使用 Windows 语音合成"
            return "", b""

        stamp = f"{int(time.time() * 1000)}_{random.randint(100, 999)}"
        txt_path = os.path.join(tempfile.gettempdir(), f"unipus_tts_{stamp}.txt")
        wav_path = os.path.join(tempfile.gettempdir(), f"unipus_tts_{stamp}.wav")
        with open(txt_path, 'w', encoding='utf-8') as handle:
            handle.write(text)

        script = SAPI_TTS_SCRIPT.replace('__RATE__', str(int(self.SAPI_RATE)))
        env = dict(os.environ)
        env['UNIPUS_TTS_TEXT'] = txt_path
        env['UNIPUS_TTS_OUT'] = wav_path
        env['UNIPUS_TTS_VOICE'] = self.voice

        creation_flags = 0x08000000 if os.name == 'nt' else 0
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, timeout=180, env=env,
                creationflags=creation_flags,
            )
            if completed.returncode != 0:
                detail = self._decode_console(completed.stderr or completed.stdout or b'')
                self.last_error = f"SAPI 失败: {detail.strip()[:160]}"
                return "", b""
            with open(wav_path, 'rb') as handle:
                data = handle.read()
            if not data:
                self.last_error = "SAPI 返回空音频"
                return "", b""
            return "wav", data
        except Exception as exc:
            self.last_error = f"SAPI 异常: {str(exc)[:120]}"
            return "", b""
        finally:
            for path in (txt_path, wav_path):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass


SAPI_TTS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $synth.Rate = __RATE__ } catch { }
$installed = @($synth.GetInstalledVoices())

$candidates = New-Object System.Collections.ArrayList
if ($env:UNIPUS_TTS_VOICE) { [void]$candidates.Add($env:UNIPUS_TTS_VOICE) }
foreach ($item in $installed) {
  if ($item.Enabled -and $item.VoiceInfo.Culture.Name -like 'en*') {
    [void]$candidates.Add($item.VoiceInfo.Name)
  }
}
foreach ($item in $installed) {
  if ($item.Enabled) { [void]$candidates.Add($item.VoiceInfo.Name) }
}

foreach ($name in $candidates) {
  if (-not $name) { continue }
  try { $synth.SelectVoice($name); break } catch { }
}

$text = [System.IO.File]::ReadAllText($env:UNIPUS_TTS_TEXT, [System.Text.Encoding]::UTF8)
$synth.SetOutputToWaveFile($env:UNIPUS_TTS_OUT)
$synth.Speak($text)
$synth.Dispose()
"""


class VocabularyReadAloudStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        try:
            if container.find_elements(By.CSS_SELECTOR, VOCAB_READ_ALOUD_SELECTOR):
                return True
            return bool(container.find_elements(By.CSS_SELECTOR, '.vocContainer .vocWord'))
        except Exception:
            return False

    def parse(self, container, driver, question_number: int,
              directions: str = "") -> Optional[Question]:
        try:
            info = driver.execute_script(VOCAB_CARD_JS, container) or {}
        except Exception:
            info = {}
        word = (info.get('word') or '').strip()
        raw = info.get('audio') or ''
        url = raw.split('#')[0].strip()
        item = {
            'index': 0,
            'url': url,
            'name': word or (info.get('name') or ''),
            'duration': float(info.get('duration') or 0),
            'text': word,
            'lines': [],
            'element': None,
            'needs_tts': not url,
            'vocab': True,
            'vocab_total': int(info.get('total') or 0),
            'vocab_index': int(info.get('index') or 0),
        }
        text = word or '词汇跟读'
        definition = (info.get('definition') or '').strip()
        if definition:
            text += '\n' + definition[:200]
        if directions:
            text = directions.strip() + '\n' + text
        print(f"    词汇跟读卡片: {word or '(未读到)'} | "
              f"原声 {'有' if url else '无'} | "
              f"进度 {item['vocab_index']}/{item['vocab_total']}")
        return Question(
            number=question_number,
            text=text[:1200],
            q_type=QuestionType.RECORD,
            element=container,
            directions=directions,
            record_items=[item],
        )


class OralAloudStrategy(QuestionParserStrategy):

    CONTAINER_SELECTOR = '.p-oral-aloud'

    def can_parse(self, container, driver) -> bool:
        try:
            return bool(container.find_elements(By.CSS_SELECTOR, '.p-oral-aloud-button'))
        except Exception:
            return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        text = ''
        for selector in ('.p-oral-aloud-sentence .component-htmlview',
                         '.p-oral-aloud-content .component-htmlview',
                         '.p-oral-aloud-sentence'):
            try:
                nodes = container.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                nodes = []
            for node in nodes:
                value = (node.text or '').strip()
                if len(value) > 40:
                    text = re.sub(r'\s+', ' ', value)
                    break
            if text:
                break
        if not text:
            return None
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.ORAL_ALOUD,
            element=container,
            directions=directions,
            record_items=[{'url': '', 'name': '', 'text': text, 'duration': 0.0}],
        )


class RecordStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        try:
            if container.find_elements(By.CSS_SELECTOR, RECORD_BUTTON_SELECTOR):
                return True
        except Exception:
            pass
        return bool(find_record_buttons(driver, container))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        buttons = find_record_buttons(driver, container)
        if not buttons:
            return None

        try:
            metas = driver.execute_script(
                RECORD_ITEMS_JS, container, buttons) or []
        except Exception:
            metas = []

        items = []
        for idx, button in enumerate(buttons):
            meta = {'index': idx, 'url': '', 'name': '', 'duration': 0.0,
                    'lines': [], 'element': button}
            if idx < len(metas):
                info = metas[idx] or {}
                raw = (info.get('raw') or '') or (info.get('src') or '')
                meta['url'] = raw.split('#')[0].strip()
                match = re.search(r'name=([^&]*)', raw)
                if match:
                    meta['name'] = self._decode_audio_name(match.group(1))
                match = re.search(r'duration=([0-9.]+)', raw)
                if match:
                    meta['duration'] = float(match.group(1))
                lines = [str(line).strip() for line in (info.get('lines') or []) if str(line).strip()]
                meta['lines'] = lines
                meta['text'] = (info.get('text') or '').strip()
                meta['prompt'] = (info.get('prompt') or '').strip()
                try:
                    row_index = int(info.get('rowIndex'))
                except (TypeError, ValueError):
                    row_index = -1
                if row_index >= 0:
                    meta['row_index'] = row_index
                if not meta['name']:
                    meta['name'] = (info.get('name') or '').strip()
            if not meta['name']:
                meta['name'] = (button.get_attribute('title') or '').strip()
            meta['needs_tts'] = not meta['url']
            items.append(meta)

        if not items:
            return None

        text = self._question_text(container, items, directions)

        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.RECORD,
            element=container,
            directions=directions,
            record_items=items,
        )

    @staticmethod
    def _decode_audio_name(raw: str) -> str:
        try:
            return unquote(raw).strip()
        except Exception:
            return (raw or '').strip()

    @staticmethod
    def _question_text(container, items: List[Dict], directions: str) -> str:
        prompt_selectors = ('.oral-personal-state-sentence-container',
                            '.component-htmlview',
                            '.sentence-html-container',
                            '.question-title', '.topic-title',
                            '.layoutBody-title', '.direction-content')

        candidates = []
        scopes = [container]
        for xpath in ("ancestor::div[contains(@class,'oral-personal-state-wrapper')][1]",
                      "ancestor::div[contains(@class,'question-abs-question')][1]"):
            try:
                scopes.append(container.find_element(By.XPATH, xpath))
            except Exception:
                continue

        for scope in scopes:
            for selector in prompt_selectors:
                try:
                    nodes = scope.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                for node in nodes:
                    value = (node.text or '').strip()
                    if value and value not in candidates:
                        candidates.append(value)
            if candidates:
                break

        if not candidates:
            try:
                body_text = (container.text or '').strip()
            except Exception:
                body_text = ''
            if body_text:
                candidates.append(body_text)
        if directions:
            candidates.insert(0, directions.strip())

        joined = "\n".join(c for c in candidates if c)
        if joined:
            return joined[:1200]
        if items and items[0].get('name'):
            return items[0]['name']
        return "录音题（跟读/自由作答）"


class VideoDubStrategy(QuestionParserStrategy):

    CONTAINER_SELECTOR = '.question-video-dub-pc'
    ROW_SELECTOR = '.question-common-abs-reply > .item'

    def can_parse(self, container, driver) -> bool:
        try:
            if not container.find_elements(By.CSS_SELECTOR, self.ROW_SELECTOR):
                return False
            return bool(container.find_elements(
                By.CSS_SELECTOR, '.ucomp-recorder .button-record, .record-button-group'))
        except Exception:
            return False

    def parse(self, container, driver, question_number: int,
              directions: str = "") -> Optional[Question]:
        if not container.find_elements(By.CSS_SELECTOR, self.ROW_SELECTOR):
            try:
                container = container.find_element(By.CSS_SELECTOR, self.CONTAINER_SELECTOR)
            except Exception:
                return None

        rows = container.find_elements(By.CSS_SELECTOR, self.ROW_SELECTOR)
        items = []
        for idx, row in enumerate(rows):
            try:
                text = (row.find_element(
                    By.CSS_SELECTOR, '.component-htmlview').text or '').strip()
            except Exception:
                text = ''
            if not text:
                continue
            items.append({
                'index': idx,
                'text': text,
                'name': text,
                'url': '',
                'duration': 0.0,
                'needs_tts': True,
                'element': row,
                'row_index': idx,
            })
        if not items:
            return None

        title = f"视频配音（共{len(items)}句）"
        text = f"【题目要求】{directions}\\n\\n{title}" if directions else title
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.RECORD,
            element=container,
            directions=directions,
            record_items=items,
        )


class TextInputStrategy(QuestionParserStrategy):

    WRITING_KEYWORDS = ['topic', 'topic sentence', 'outline', 'things to do',
                        'concluding sentence', 'more topics']
    READING_MIN_LENGTH = 400

    def can_parse(self, container, driver) -> bool:
        textareas = container.find_elements(By.CSS_SELECTOR,
                                            'textarea.question-textarea-content, textarea.question-inputbox-input, textarea.scoopFill_textarea')
        if not textareas:
            return False

        container_class = container.get_attribute('class') or ''
        is_single_reply = 'question-common-abs-reply' in container_class

        if is_single_reply:
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            if has_inputbox:
                return True

            has_scoop_container = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-scoop, .fe-scoop')
            if has_scoop_container:
                return True

            return False

        has_material = container.find_elements(By.CSS_SELECTOR, '.layout-material-container')

        if has_material:
            material_text = ""
            try:
                material = container.find_element(By.CSS_SELECTOR, '.layout-material-container')
                material_text = material.text.lower()
            except:
                pass

            if any(kw in material_text for kw in self.WRITING_KEYWORDS):
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if has_scoop:
                    return True

            if any(kw in material_text for kw in ['model', 'example', '示例', '例句']):
                return True

        direction_text = ""
        try:
            direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container .component-htmlview')
            direction_text = direction_elem.text.lower()
        except:
            direction_elem = WebDriverHelper.safe_find_element(
                driver, ['.layout-direction-container .content', '.abs-direction .content'], container)
            if direction_elem:
                direction_text = direction_elem.text.lower()

        if direction_text:
            if any(kw in direction_text for kw in ['write', 'essay', 'composition', 'paragraph']):
                has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options, .option-wrap')
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if not has_options and has_scoop:
                    return True

            if any(kw in direction_text for kw in ['answer', 'question', 'according to']):
                has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
                has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
                if has_inputbox and not has_scoop:
                    return True

        if len(textareas) >= 2:
            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            if has_inputbox and not has_scoop:
                return True

        if len(textareas) == 1:
            rows = textareas[0].get_attribute('rows')
            is_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')
            if rows and int(rows) >= 5 and is_scoop:
                return True

        return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        try:
            effective_directions = directions

            if not effective_directions:
                try:
                    direction_elem = driver.find_element(By.CSS_SELECTOR,
                                                         ".layout-direction-container .component-htmlview")
                    effective_directions = direction_elem.text.strip()
                except:
                    pass

            container_class = container.get_attribute('class') or ''
            is_single_reply = 'question-common-abs-reply' in container_class

            has_inputbox = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
            has_scoop = container.find_elements(By.CSS_SELECTOR, '.fe-scoop, .question-common-abs-scoop')

            if is_single_reply:
                if has_inputbox and not has_scoop:
                    is_writing = False
                elif has_scoop and not has_inputbox:
                    is_writing = True
                else:
                    is_writing = self._check_is_writing_by_material(container, driver)
            else:
                is_writing = self._check_is_writing_by_material(container, driver)

            material_text = self._extract_material_text(container, driver)

            items = []

            if is_writing:
                scoop_containers = container.find_elements(By.CSS_SELECTOR, '.fe-scoop')
                for i, scoop in enumerate(scoop_containers, 1):
                    try:
                        number_elem = scoop.find_element(By.CSS_SELECTOR, '.question-number')
                        number = number_elem.text.strip()

                        textarea = scoop.find_element(By.CSS_SELECTOR, 'textarea.question-textarea-content')
                        placeholder = textarea.get_attribute('placeholder') or "写作"

                        items.append({
                            'index': i,
                            'words': f"题{number}: {placeholder}",
                            'input': textarea,
                            'question_text': placeholder
                        })
                    except Exception as e:
                        continue
            else:
                if is_single_reply:
                    input_boxes = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
                else:
                    input_boxes = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')

                for i, box in enumerate(input_boxes, question_number):
                    try:
                        header = box.find_element(By.CSS_SELECTOR, '.question-inputbox-header')
                        question_text = header.text.strip()
                        question_text = re.sub(rf'^\d+[\s.、)]+', '', question_text)

                        textarea = box.find_element(By.CSS_SELECTOR, 'textarea')

                        items.append({
                            'index': i,
                            'words': question_text,
                            'input': textarea,
                            'question_text': question_text
                        })
                    except Exception as e:
                        continue

            if not items:
                return None

            full_text = ""
            if effective_directions:
                full_text += f"【题目要求】{effective_directions}\n\n"

            if material_text:
                if is_writing:
                    full_text += f"【写作提纲/材料】\n{material_text[:500]}\n\n"
                else:
                    full_text += f"【阅读材料】\n{material_text[:6000]}\n\n"

            full_text += "【问题列表】\n"
            for item in items:
                if is_writing:
                    full_text += f"{item['index']}. {item['words']}\n"
                else:
                    full_text += f"{item['index']}. {item['question_text']}\n"

            if is_single_reply and not is_writing and len(items) == 1:
                item = items[0]
                return Question(
                    number=item['index'],
                    text=full_text,
                    q_type=QuestionType.TEXT,
                    element=container,
                    inputs=[item['input']],
                    banked_blanks=[item],
                    directions=effective_directions,
                )

            return Question(
                number=question_number,
                text=full_text,
                q_type=QuestionType.TEXT,
                element=container,
                inputs=[item['input'] for item in items],
                banked_blanks=items,
                directions=effective_directions,
            )

        except Exception as e:
            error_msg = str(e)
            print(f"      TextInputStrategy解析失败: {error_msg[:100]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return None

    def _check_is_writing_by_material(self, container=None, driver=None) -> bool:
        material_text = self._extract_material_text(container, driver)
        if material_text:
            material_lower = material_text.lower()
            return any(kw in material_lower for kw in self.WRITING_KEYWORDS)
        return False

    def _extract_material_text(self, container=None, driver=None) -> str:
        material_text = ""
        try:
            if container:
                try:
                    material = container.find_element(By.CSS_SELECTOR, '.layout-material-container')
                    material_text = material.text.strip()
                except:
                    pass
            if not material_text and driver:
                try:
                    material = driver.find_element(By.CSS_SELECTOR, '.layout-material-container')
                    material_text = material.text.strip()
                except:
                    pass
        except:
            pass
        return material_text


class ListeningFillInStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        has_blanks = bool(container.find_elements(By.CSS_SELECTOR, '.fe-scoop input'))
        has_option_pool = bool(container.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options'))
        if not has_blanks or has_option_pool:
            return False

        try:
            direction = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            text = direction.text.lower()
            has_media_hint = any(kw in text for kw in [
                'listen', 'audio', 'hear', 'talk', 'conversation',
                'watch', 'video', 'clip', 'view'
            ])
            has_fill_hint = any(kw in text for kw in [
                'fill in', 'complete', 'blank', 'blanks'
            ])
            return has_media_hint and has_fill_hint
        except:
            return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        if not inputs:
            return None

        media_label = "视频填空题" if self._is_video_fill_page(directions) else "听力填空题"
        full_text = f"{media_label}（共{len(inputs)}个空）"
        if directions:
            full_text = f"【题目要求】{directions}\n\n{full_text}"

        blank_contexts = []
        for i, inp in enumerate(inputs):
            scoop = None
            try:
                scoop = inp.find_element(By.XPATH, './ancestor::span[@class="fe-scoop"]')
                sentence = self._extract_blank_sentence(scoop)
            except Exception:
                sentence = ""
            left_context, right_context = self._split_blank_context(sentence, i + 1)
            blank_contexts.append({
                'index': i,
                'sentence': sentence,
                'label': self._extract_row_label(scoop, sentence) if scoop is not None else '',
                'left_context': left_context,
                'right_context': right_context,
                'input': inp
            })

        return Question(
            number=question_number,
            text=full_text,
            q_type=QuestionType.LISTENING_FILL_IN,
            element=container,
            inputs=inputs,
            banked_blanks=blank_contexts,
            directions=directions,
        )

    @staticmethod
    def _is_video_fill_page(directions: str) -> bool:
        direction_text = (directions or "").lower()
        return any(kw in direction_text for kw in ['watch', 'video', 'clip', 'view'])

    def _extract_blank_sentence(self, scoop) -> str:
        for xpath in ['./ancestor::td[1]', './ancestor::p[1]', './ancestor::div[1]']:
            try:
                text = scoop.find_element(By.XPATH, xpath).text.strip()
                if text:
                    return text
            except:
                continue
        return ""

    @staticmethod
    def _extract_row_label(scoop, sentence: str) -> str:
        if scoop is None:
            return ""
        try:
            cells = scoop.find_elements(By.XPATH, './ancestor::tr[1]/*[self::td or self::th]')
        except Exception:
            return ""
        if len(cells) < 2:
            return ""
        try:
            label = (cells[0].text or "").strip()
        except Exception:
            return ""
        return "" if label == sentence else label

    @staticmethod
    def _split_blank_context(sentence: str, blank_number: int) -> Tuple[str, str]:
        if not sentence:
            return "", ""
        patterns = [
            rf'{blank_number}\s*[）).、:：]',
            rf'{blank_number}\s*[_—-]+',
            rf'{blank_number}\s*[）).、:：]?\s*空\s*{blank_number}?\s*[:：]?',
            rf'空\s*{blank_number}\s*[:：]?',
        ]
        for pattern in patterns:
            match = re.search(pattern, sentence)
            if match:
                return sentence[:match.start()].strip(), sentence[match.end():].strip()
        match = re.search(rf'(?<!\d){blank_number}(?!\d)', sentence)
        if match:
            return sentence[:match.start()].strip(), sentence[match.end():].strip()
        return sentence, ""

class FillInStrategy(QuestionParserStrategy):

    FILL_INPUTS = [
        'input.fill-blank--bc-input-DelG1',
        '.fe-scoop input:not([type="hidden"])',
        '.comp-abs-input input',
        '.blankinput',
        'input[type="text"]',
    ]

    def can_parse(self, container, driver) -> bool:
        scoops = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input:not([type="hidden"])')
        has_options = container.find_elements(By.CSS_SELECTOR, '.option-wrapper .option, .banked-options')
        if scoops and not has_options:
            return True

        has_material_container = container.find_elements(
            By.CSS_SELECTOR, '.layout-material-container'
        )
        if has_material_container:
            return False

        has_textarea = container.find_elements(
            By.CSS_SELECTOR, 'textarea.question-textarea-content'
        )
        if has_textarea:
            return False

        inputs = WebDriverHelper.safe_find_elements(driver, self.FILL_INPUTS, container)

        if len(inputs) >= 2:
            return True

        if len(inputs) == 1:
            inp = inputs[0]
            placeholder = inp.get_attribute('placeholder') or ''
            if 'word' in placeholder.lower() or '不少于' in placeholder:
                return False
            return True

        return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else ""
        text = re.sub(r"^\d+[.、)\]]\s*", "", text)

        scoops = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input:not([type="hidden"])')
        if scoops:
            inputs = MatchingStrategy._sort_inputs(scoops)
        else:
            inputs = WebDriverHelper.safe_find_elements(driver, self.FILL_INPUTS, container)
        visible_inputs = []
        for inp in inputs:
            try:
                if inp.getClientRects():
                    visible_inputs.append(inp)
            except Exception:
                continue
        if visible_inputs:
            inputs = visible_inputs

        blanks = []
        segments = MatchingStrategy._container_segments(driver, container)
        given_initials = bool(re.search(r'\b(?:first|initial)\s+letters?\b', directions, re.I))
        for index, inp in enumerate(inputs):
            context = MatchingStrategy._text_before_blank(driver, container, inp, segments)
            context = re.sub(r'\s+', ' ', context).strip()
            if not context:
                context = MatchingStrategy._statement_for(inp)
            context = context[-180:] if len(context) > 180 else context
            hint = MatchingStrategy._hint_before(inp)
            if given_initials:
                prefix = re.search(r'\b([A-Za-z]{1,4})$', context)
                hint = prefix.group(1) if prefix else ''
            blanks.append({
                'index': index,
                'context': context,
                'right_context': (
                    MatchingStrategy._leading_text(inp).get('trailing') or ''
                ).strip(),
                'hint': hint,
                'input': inp,
            })

        header = f"填空题（共{len(inputs)}个空）"
        material = self._page_material(container, inputs=inputs)
        if material:
            header += f"\n【题面/词库】{material}"
        if scoops:
            header += "\n按题面【空N】的编号逐空作答，每个输入框单独给出答案，不要把多个空合并为一个短语答案。"
        if directions:
            header = f"【题目要求】{directions}\n{header}"

        return Question(
            number=question_number,
            text=header,
            q_type=QuestionType.FILL_IN,
            element=container,
            inputs=inputs,
            banked_blanks=blanks,
            directions=directions,
        )

    @staticmethod
    def _page_material(container, limit: int = 6000, inputs=None) -> str:
        try:
            text = container.parent.execute_script("""
                const root = arguments[0];
                const clone = root.cloneNode(true);
                const originals = [...root.querySelectorAll('input, textarea')];
                const copies = [...clone.querySelectorAll('input, textarea')];
                arguments[1].forEach((input, i) => {
                    const copy = copies[originals.indexOf(input)];
                    if (copy) {
                        const blank = copy.closest('.fe-scoop') || copy;
                        blank.replaceWith(document.createTextNode('【空' + (i + 1) + '】'));
                    }
                });
                for (const el of clone.querySelectorAll('input, textarea, .fe-scoop')) { el.remove(); }
                return (clone.textContent || '').replace(/\\s+/g, ' ').trim();
            """, container, inputs or []) or ""
        except Exception:
            text = ""
        return text[:limit] if len(text) > limit else text


class AudioStrategy(QuestionParserStrategy):

    ANSWER_SELECTOR = ('video, textarea, input:not([type="hidden"]), .fe-scoop, '
                       '.question-common-abs-choice, .option, .option-wrap, '
                       '.ant-radio-wrapper, .ant-checkbox-wrapper, '
                       '.oral-paragraph-container, .oral-study-sentence')

    def can_parse(self, container, driver) -> bool:
        if container.find_elements(By.CSS_SELECTOR, self.ANSWER_SELECTOR):
            return False
        if find_record_buttons(driver, container):
            return False
        return bool(container.find_elements(By.CSS_SELECTOR, 'audio[src]:not([src=""]), audio source[src]'))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(number=question_number, text="音频听读页面",
                        q_type=QuestionType.AUDIO, element=container, directions=directions)

    @classmethod
    def is_audio_only_page(cls, driver) -> bool:
        containers = driver.find_elements(By.CSS_SELECTOR, '.layoutBody-container')
        visible = [node for node in containers if node.is_displayed()]
        if not visible:
            return False
        if driver.find_elements(
                By.CSS_SELECTOR,
                '.sequence-view, .sortable-list-wrapper, .question-common-abs-fill, '
                '.question-common-abs-choice, .question-common-abs-dropdown'):
            return False
        return all(cls().can_parse(node, driver) for node in visible)


class VideoStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        videos = container.find_elements(By.TAG_NAME, 'video')
        if not videos:
            videos = container.find_elements(By.CSS_SELECTOR,
                                             '.video-js, .video-box, .question-video-player, video')

        if not videos:
            return False

        popup_questions = container.find_elements(By.CSS_SELECTOR,
                                                  '.popupBox .question-common-abs-choice, .questionReplyBox .question-common-abs-choice')

        if popup_questions:
            return False

        has_real_questions = (
                container.find_elements(By.CSS_SELECTOR,
                                        '.question-common-abs-choice:not(.popupBox *), '
                                        '.question-inputbox:not(.popupBox *), '
                                        '.option-wrap:not(.popupBox *), '
                                        '.fe-scoop') or
                container.find_elements(By.CSS_SELECTOR,
                                        'input[type="text"]:not(.ant-input)')
        )

        if has_real_questions:
            return False

        return True

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="视频观看页面",
            q_type=QuestionType.VIDEO,
            element=container
        )


class VocabularyFlashcardStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        flashcard_indicators = [
            '.vocContainer',
            '.vocabulary-flashcard',
            '.flashcard-container',
            '.vocActions',
            '.vocabulary-actions'
        ]

        for indicator in flashcard_indicators:
            if container.find_elements(By.CSS_SELECTOR, indicator):
                has_choice = container.find_elements(By.CSS_SELECTOR, '.option-wrap, .question-common-abs-choice')
                if not has_choice:
                    return True

        return False

    def parse(self, container, driver, question_number: int, direction: str = "") -> Optional[Question]:
        return Question(
            number=question_number,
            text="单词闪卡",
            q_type=QuestionType.VOCABULARY_FLASHCARD,
            element=container
        )


class DropdownSelectStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        selects = container.find_elements(By.CSS_SELECTOR, DROPDOWN_TRIGGER_SELECTOR)
        return bool(selects) or has_visible_dropdown(driver, container)

    @staticmethod
    def _cloze_material(driver, container) -> str:
        try:
            raw = driver.execute_script("""
                const clone = arguments[0].cloneNode(true);
                const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
                let n;
                while ((n = walker.nextNode())) {
                    if ((n.textContent || '').trim() === '点击选择') n.textContent = ' ____ ';
                }
                return (clone.textContent || '').trim();
            """, container) or ""
        except Exception:
            return ""
        return re.sub(r'\s+', ' ', raw).strip()

    @staticmethod
    def _peek_dropdown_options(driver, trigger) -> List[str]:
        try:
            ActionChains(driver).move_to_element(trigger).click().perform()
            time.sleep(0.5)
            items = driver.find_elements(By.CSS_SELECTOR,
                                         '.ant-select-item-option, .ant-dropdown-menu-item, '
                                         '[role="option"], li[class*="option"]')
            options = []
            for item in items:
                try:
                    if not item.is_displayed():
                        continue
                    label = (item.text or '').strip()
                    if label and label != CLOZE_DROPDOWN_PLACEHOLDER and label not in options:
                        options.append(label)
                except Exception:
                    continue
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            return options
        except Exception:
            return []

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        blanks = []
        select_elements = container.find_elements(By.CSS_SELECTOR, '.scoop-select-wrapper')
        if not select_elements:
            select_elements = find_dropdown_triggers(driver, container)

        for i, elem in enumerate(select_elements):
            context = ""
            try:
                context_elem = elem.find_element(By.XPATH,
                                                 './ancestor::li[1] | ./ancestor::p[1]')
                raw = driver.execute_script("""
                    const box = arguments[0], trigger = arguments[1];
                    const clone = box.cloneNode(true);
                    const label = (trigger.textContent || '').trim();
                    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
                    let n, done = false;
                    while ((n = walker.nextNode())) {
                        if (!done && label && (n.textContent || '').trim() === label) {
                            n.textContent = ' ____ ';
                            done = true;
                        }
                    }
                    return (clone.textContent || '').trim();
                """, context_elem, elem) or ""
                context = re.sub(r'\s+', ' ', raw).strip()
            except Exception:
                try:
                    context = elem.find_element(By.XPATH, './ancestor::p[1]').text.strip()
                except Exception:
                    context = ""

            options = []
            try:
                hidden_div = elem.find_element(By.CSS_SELECTOR, 'div[style*="visibility: hidden"]')
                option_elems = hidden_div.find_elements(By.TAG_NAME, 'i')
                for opt in option_elems:
                    text = opt.text.strip()
                    if text and text not in options:
                        options.append(text)
            except:
                try:
                    select = elem.find_element(By.TAG_NAME, 'select')
                    option_elems = select.find_elements(By.TAG_NAME, 'option')
                    for opt in option_elems:
                        text = opt.text.strip()
                        if text and text not in ['', CLOZE_DROPDOWN_PLACEHOLDER]:
                            options.append(text)
                except:
                    pass

            blanks.append({
                'index': i,
                'context': context,
                'element': elem,
                'options': options
            })

        title_elem = WebDriverHelper.safe_find_element(driver, Selectors.QUESTION_TITLE, container)
        text = title_elem.text.strip() if title_elem else "下拉选择填空"
        if directions:
            text = directions + text
        material = self._cloze_material(driver, container)
        if material:
            text += "\n【全文】" + material[:6000]

        banked_options = list(dict.fromkeys(
            opt for b in blanks for opt in b['options']))
        if not banked_options and blanks:
            banked_options = self._peek_dropdown_options(driver, blanks[0]['element'])
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.DROPDOWN_SELECT,
            element=container,
            banked_blanks=blanks,
            banked_options=banked_options
        )


class ParagraphMatchingStrategy(QuestionParserStrategy):

    REPLY_SELECTOR = '.question-common-abs-textmatch .input-wrapper'
    INPUT_SELECTOR = REPLY_SELECTOR + ' input.textbox'

    def can_parse(self, container, driver) -> bool:
        return len(container.find_elements(
            By.CSS_SELECTOR, self.INPUT_SELECTOR)) >= 2

    @staticmethod
    def _paragraph_material(driver, container) -> Dict:
        try:
            raw = driver.execute_script("""
                var root = arguments[0];
                var scope = root.querySelector('.layout-material-container') || root;
                var out = [], letters = [];
                var title = scope.querySelector('.component-htmlview > p');
                if (title) out.push((title.innerText || title.textContent || '').trim());
                var ol = scope.querySelector('ol');
                if (ol) {
                    var lis = [].filter.call(ol.children, function (n) {
                        return n.tagName === 'LI';
                    });
                    for (var i = 0; i < lis.length; i++) {
                        var L = String.fromCharCode(65 + i);
                        letters.push(L);
                        out.push(L + '. ' + (lis[i].innerText || lis[i].textContent || '').trim());
                    }
                }
                if (!out.length) out.push((scope.innerText || scope.textContent || '').trim());
                return {text: out.join('\\n'), letters: letters};
            """, container)
            if not isinstance(raw, dict):
                return {'text': '', 'letters': []}
            text = re.sub(r'\n{3,}', '\n\n', raw.get('text') or '').strip()
            return {'text': text, 'letters': raw.get('letters') or []}
        except Exception:
            return {'text': '', 'letters': []}

    @staticmethod
    def _statements(driver, container) -> List[str]:
        try:
            raw = driver.execute_script("""
                var root = arguments[0];
                return [].map.call(
                    root.querySelectorAll('.question-common-abs-textmatch .input-wrapper'),
                    function (w) {
                        var p = w.querySelector('.component-htmlview');
                        return p ? (p.innerText || p.textContent || '').trim() : '';
                    });
            """, container) or []
        except Exception:
            return []
        return [re.sub(r'\s+', ' ', str(s)).strip() for s in raw]

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, self.INPUT_SELECTOR)
        statements = self._statements(driver, container)
        if len(statements) < len(inputs):
            statements += [''] * (len(inputs) - len(statements))
        material = self._paragraph_material(driver, container)
        text = material.get('text') or ''
        if directions:
            text = '【题目要求】' + directions + '\n' + text
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.PARAGRAPH_MATCH,
            element=container,
            inputs=inputs,
            banked_options=material.get('letters') or [],
            banked_blanks=[{'index': i, 'statement': s}
                           for i, s in enumerate(statements[:len(inputs)])],
            directions=directions,
        )


class MatchingStrategy(QuestionParserStrategy):

    MATCH_DIRECTION = re.compile(
        r'match\s+the\s+(?:people|persons?|speakers?|statements?|answers?)'
        r'|fill(?:ing)?\s+(?:in\s+)?the\s+blanks?\s+with\s+(?:the\s+)?(?:letters?\s+)?'
        r'(?:[A-Z]\s*[-–—~]\s*[A-Z])'
        r'|choose\s+the\s+correct\s+letter',
        re.I)

    @classmethod
    def is_matching_direction(cls, directions: str) -> bool:
        return bool(cls.MATCH_DIRECTION.search(directions or ""))

    @staticmethod
    def _page_direction(driver) -> str:
        for selector in (
                '.layout-direction-container .component-htmlview',
                '.layout-direction-container',
                '.abs-direction .content',
                '.abs-direction'):
            try:
                text = driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

    def can_parse(self, container, driver) -> bool:
        blanks = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        if len(blanks) < 2:
            return False
        return self.is_matching_direction(self._page_direction(driver))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        inputs = self._sort_inputs(inputs)

        segments = self._container_segments(driver, container)
        numbered = []
        for item in segments:
            text = re.sub(r'\s+', ' ', (item.get('text') or '')).strip()
            if re.match(r'^\d{1,2}\s*[.、)]', text):
                numbered.append(self._clean_statement(text))

        statements = []
        for index, inp in enumerate(inputs):
            hint = GenericReplyStrategy._hint_before(inp)
            if len(numbered) == len(inputs):
                statement = numbered[index]
            else:
                raw = self._text_before_blank(driver, container, inp, segments)
                statement = self._clean_statement(re.sub(r'\s+', ' ', raw))
            if not statement:
                statement = self._clean_statement(re.sub(r'\s+', ' ', self._statement_for(inp)))
            statements.append({
                'index': index,
                'statement': statement,
                'hint': hint,
                'input': inp,
            })

        letters = self._option_letters(container)
        if not letters:
            letters = [chr(65 + i) for i in range(min(8, max(2, len(inputs))))]

        text_lines = ["【匹配题】把陈述与人物字母对应起来，每个空只填一个字母。"]
        if directions:
            text_lines.append(f"题目要求：{directions}")
        text_lines.append(f"可选字母：{' / '.join(letters)}")
        text_lines.append("陈述：")
        for item in statements:
            text_lines.append(f"  {item['index'] + 1}. {item['statement']}")

        return Question(
            number=question_number,
            text="\n".join(text_lines),
            q_type=QuestionType.MATCHING,
            element=container,
            inputs=inputs,
            banked_blanks=statements,
            banked_options=letters,
            options=[Option(letter=letter, text="", element=None) for letter in letters],
            directions=directions,
        )

    @staticmethod
    def _clean_statement(segment: str) -> str:
        text = (segment or "").strip()
        if not text:
            return ""
        text = re.sub(r'^(?:[A-H]\s*){2,}', '', text).strip()
        parts = re.split(r'(?=(?<!\d)\d{1,2}\s*[.、)]\s)', text)
        parts = [p.strip() for p in parts if p.strip()]
        if parts:
            text = parts[-1]
        return text[-200:]

    @staticmethod
    def _sort_inputs(inputs: List[Any]) -> List[Any]:
        def key(inp):
            try:
                scoop = inp.find_element(By.XPATH, './ancestor::span[contains(@class, "fe-scoop")][1]')
                return int(scoop.get_attribute('data-scoop-index') or 0)
            except Exception:
                return 0

        ordered = list(inputs)
        try:
            ordered.sort(key=key)
        except Exception:
            pass
        return ordered

    @staticmethod
    def _statement_for(inp) -> str:
        for xpath in ('./ancestor::td[1]', './ancestor::p[1]', './ancestor::li[1]', './ancestor::div[1]'):
            try:
                node = inp.find_element(By.XPATH, xpath)
                text = MatchingStrategy._node_text_without_input(node)
                if text:
                    return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _blank_context(inp, cache: Dict[int, List[str]] = None) -> Tuple[str, str]:
        data = MatchingStrategy._leading_text(inp)
        text = (data.get('leading') or '').strip()
        hint = (data.get('hint') or '').strip()
        if cache is not None and 'segments' in cache and 'position' in cache:
            segments = cache['segments']
            position = cache['position']
            if 0 <= position < len(segments):
                segment = segments[position].strip()
                if len(segment) > len(text):
                    text = segment
        if not text:
            text = MatchingStrategy._statement_for(inp)
        return text[-300:] if len(text) > 300 else text, hint

    @staticmethod
    def _container_segments(driver, container) -> List[str]:
        try:
            return driver.execute_script("""
                const root = arguments[0];
                const out = [];
                let buf = '';
                const walk = (n) => {
                  for (const child of n.childNodes) {
                    if (child.nodeType === 3) { buf += child.textContent; continue; }
                    if (child.nodeType !== 1) continue;
                    if (child.tagName === 'INPUT') { continue; }
                    if (child.classList && child.classList.contains('fe-scoop')) {
                      const idx = child.getAttribute('data-scoop-index');
                      out.push({text: buf, idx: idx === null ? null : Number(idx)});
                      buf = '';
                      continue;
                    }
                    walk(child);
                  }
                };
                walk(root);
                out.push({text: buf, idx: null});
                return out;
            """, container) or []
        except Exception:
            return []

    @staticmethod
    def _text_before_blank(driver, container, inp, segments=None) -> str:
        segments = segments if segments is not None else MatchingStrategy._container_segments(driver, container)
        if not segments or not isinstance(segments[0], dict):
            return ""
        try:
            scoop = inp.find_element(By.XPATH, './ancestor::span[contains(@class, "fe-scoop")][1]')
            index = int(scoop.get_attribute('data-scoop-index') or 0)
        except Exception:
            return ""
        for item in segments:
            if item.get('idx') == index:
                return (item.get('text') or '').strip()
        return ""

    @staticmethod
    def _scoop_positions(driver, container) -> List[int]:
        try:
            return driver.execute_script("""
                const root = arguments[0];
                const scoops = [...root.querySelectorAll('.fe-scoop')];
                const result = [];
                for (const scoop of scoops) {
                  const input = scoop.querySelector('input');
                  if (input) result.push(Number(scoop.getAttribute('data-scoop-index') || result.length));
                }
                return result;
            """, container) or []
        except Exception:
            return []

    @staticmethod
    def _leading_text(inp) -> Dict[str, str]:
        try:
            return inp.parent.execute_script("""
                const input = arguments[0];
                const scoop = input.closest('.fe-scoop');
                if (!scoop) return {leading: '', hint: ''};
                let node = scoop.previousSibling, out = '';
                for (let guard = 0; guard < 12 && node; guard++) {
                  if (node.nodeType === 1 && node.querySelector && node.querySelector('.fe-scoop')) break;
                  out = (node.textContent || '') + out;
                  if (node.nodeType === 1 && /^(P|DIV|TD|LI|TR|TABLE|SECTION)$/.test(node.tagName)) break;
                  node = node.previousSibling;
                }
                const rawTail = out;
                out = out.replace(/\\s+/g, ' ').trim();


                let hint = '';
                const m = rawTail.match(/([A-Za-z]{1,4})$/);
                if (m && !/\\s$/.test(rawTail)) {
                  hint = m[1];
                  out = out.slice(0, out.length - hint.length).trim();
                }

                let next = scoop.nextSibling, after = '';
                for (let guard = 0; guard < 8 && next; guard++) {
                  if (next.nodeType === 1 && next.querySelector && next.querySelector('.fe-scoop')) break;
                  after += (next.textContent || '');
                  if (next.nodeType === 1 && /^(P|DIV|TD|LI|TR|TABLE|SECTION)$/.test(next.tagName)) break;
                  next = next.nextSibling;
                }
                after = after.replace(/\\s+/g, ' ').trim();
                return {leading: out, hint: hint, trailing: after.slice(0, 90)};
            """, inp) or {}
        except Exception:
            return {}

    @staticmethod
    def _hint_before(inp) -> str:
        try:
            hint = (MatchingStrategy._leading_text(inp).get('hint') or '').strip()
        except Exception:
            return ""
        return hint if looks_like_given_prefix(hint) else ""

    @staticmethod
    def _segments(node) -> List[str]:
        try:
            return node.parent.execute_script("""
                const node = arguments[0];
                const out = [];
                let buf = '';
                const walk = (n) => {
                  for (const child of n.childNodes) {
                    if (child.nodeType === 3) { buf += child.textContent; continue; }
                    if (child.nodeType !== 1) continue;
                    if (child.classList && child.classList.contains('fe-scoop')) {
                      out.push(buf); buf = ''; continue;
                    }
                    if (child.tagName === 'INPUT') { out.push(buf); buf = ''; continue; }
                    walk(child);
                  }
                };
                walk(node);
                out.push(buf);
                return out.map(s => (s || '').replace(/\\s+/g, ' ').trim());
            """, node) or []
        except Exception:
            return []

    @staticmethod
    def _input_index_in(node, target) -> int:
        try:
            return node.parent.execute_script("""
                const node = arguments[0], target = arguments[1];
                const inputs = [...node.querySelectorAll('input')];
                return inputs.indexOf(target);
            """, node, target)
        except Exception:
            return -1

    @staticmethod
    def _node_text_without_input(node) -> str:
        try:
            text = node.parent.execute_script("""
                const node = arguments[0];
                const clone = node.cloneNode(true);
                for (const el of clone.querySelectorAll('input, .fe-scoop')) { el.remove(); }

                return (clone.textContent || '').replace(/\\s+/g, ' ').trim();
            """, node)
        except Exception:
            text = ''
        if not text:
            try:
                text = re.sub(r'\s+', ' ', (node.text or '')).strip()
            except Exception:
                text = ''
        return text

    @staticmethod
    def _option_letters(container) -> List[str]:
        letters = []
        try:
            cells = container.find_elements(By.CSS_SELECTOR, 'td, .caption')
        except Exception:
            cells = []
        for cell in cells:
            try:
                text = (cell.text or '').strip()
            except Exception:
                continue
            if re.fullmatch(r'[A-H]', text) and text not in letters:
                letters.append(text)
        if letters:
            return sorted(letters)
        try:
            body = (container.text or '')
        except Exception:
            body = ''
        found = re.findall(r'\b([A-H])\b', body)
        unique = sorted(set(found))
        return unique if len(unique) >= 2 else []


class ChecklistStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        text = ""
        try:
            text = (container.text or '').lower()
        except Exception:
            return False
        if 'got it' not in text:
            return False
        has_table = bool(container.find_elements(By.CSS_SELECTOR, 'table, .ant-table-tbody'))
        has_icons = bool(container.find_elements(By.CSS_SELECTOR, '.anticon-border, [aria-label="border"]'))
        return has_table and has_icons

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        rows = []
        try:
            for row in container.find_elements(By.CSS_SELECTOR, 'tr.ant-table-row'):
                if 'category-name' in (row.get_attribute('class') or ''):
                    continue
                try:
                    text = row.find_element(By.CSS_SELECTOR, '.content-text').text.strip()
                except Exception:
                    continue
                try:
                    icon = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2) .anticon')
                except Exception:
                    continue
                if text:
                    rows.append({'word': text, 'element': icon})
        except Exception:
            rows = []

        if not rows:
            return None

        return Question(
            number=question_number,
            text=f"自我评估清单（共{len(rows)}项，全部标记 Got it）",
            q_type=QuestionType.SELF_CHECK,
            element=container,
            banked_blanks=rows,
            directions=directions,
        )


class GenericReplyStrategy(QuestionParserStrategy):

    def can_parse(self, container, driver) -> bool:
        return bool(self._textareas(container)) or bool(self._scoop_inputs(container))

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        textareas = self._textareas(container)
        if textareas:
            material = self._material_text(container, textareas[0])
            lines = []
            if directions:
                lines.append(f"【题目要求】{directions}")
            if material:
                lines.append(f"【原文/材料】{material}")
            prompts = self._collect_sub_prompts(container, textareas)
            distinct = {prompt.strip() for prompt in prompts if prompt and prompt.strip()}
            if len(distinct) > 1:
                lines.append(f"【小题列表】（本页共 {len(textareas)} 个小题）")
                for stem_index, prompt in enumerate(prompts, 1):
                    stem = re.sub(r'^\d+\s*[.、)]\s*', '', (prompt or '').strip()).strip()
                    lines.append(f"{stem_index}. {stem if stem else '（本小题无独立题干，按材料与题目要求作答）'}")
                lines.append("【要求】上面每个小题都必须单独作答；回答时严格按 1、2、… 编号逐题输出，格式：1.第一题答案 2.第二题答案 …")
            elif len(textareas) > 1:
                lines.append(f"【作答方式】本页共 {len(textareas)} 个输入框：请按输入框顺序用 1、2、… 编号逐个作答，每个输入框都必须有内容。")
            if re.search(r'translat|翻译', directions or '', re.I):
                lines.append("请把上面的原文完整翻译成中文，只输出译文。")
            elif re.search(r'summari|总结|概括|main idea|general idea', directions or '', re.I):
                lines.append("请根据视频/音频内容用英文写 3-4 句总结，直接输出正文。")
            question = Question(
                number=question_number,
                text="\n\n".join(lines) or "请完成本页作答",
                q_type=QuestionType.TEXT,
                element=container,
                inputs=textareas,
                directions=directions,
            )
            return question

        inputs = self._scoop_inputs(container)
        if not inputs:
            return None
        blanks = []
        for index, inp in enumerate(inputs):
            blanks.append({
                'index': index,
                'context': MatchingStrategy._statement_for(inp),
                'hint': self._hint_before(inp),
                'input': inp,
            })
        return Question(
            number=question_number,
            text=f"填空题（共{len(inputs)}个空）",
            q_type=QuestionType.FILL_IN,
            element=container,
            inputs=inputs,
            banked_blanks=blanks,
            directions=directions,
        )

    @staticmethod
    def _textareas(container) -> List[Any]:
        found = []
        try:
            for area in container.find_elements(By.CSS_SELECTOR, 'textarea'):
                try:
                    if area.is_displayed():
                        found.append(area)
                except Exception:
                    continue
        except Exception:
            return []
        return found

    @staticmethod
    def _scoop_inputs(container) -> List[Any]:
        try:
            return MatchingStrategy._sort_inputs(
                container.find_elements(By.CSS_SELECTOR, '.fe-scoop input'))
        except Exception:
            return []

    @staticmethod
    def _collect_sub_prompts(container, textareas) -> List[str]:
        """尽量收集每个 textarea 对应的小题题干（输入框头或占位符）。"""
        prompts = [''] * len(textareas)
        try:
            boxes = container.find_elements(By.CSS_SELECTOR, '.question-inputbox')
        except Exception:
            boxes = []
        if boxes and len(boxes) == len(textareas):
            for index, box in enumerate(boxes):
                text = ''
                try:
                    header = box.find_element(By.CSS_SELECTOR, '.question-inputbox-header')
                    text = (header.text or '').strip()
                except Exception:
                    text = ''
                if not text:
                    try:
                        text = (box.text or '').strip()
                    except Exception:
                        text = ''
                prompts[index] = text
        if not any(prompt.strip() for prompt in prompts):
            for index, area in enumerate(textareas):
                prompt = ''
                for attr in ('placeholder', 'aria-label', 'title'):
                    try:
                        value = area.get_attribute(attr)
                    except Exception:
                        value = None
                    if value and value.strip():
                        prompt = value.strip()
                        break
                prompts[index] = prompt
        return prompts

    @staticmethod
    def _material_text(container, textarea) -> str:
        for selector in ('.question-inputbox-header', '.layout-material-container',
                         '.question-common-abs-material', '.text-material-wrapper'):
            try:
                node = container.find_element(By.CSS_SELECTOR, selector)
                text = (node.text or '').strip()
                if len(text) > 20:
                    return text
            except Exception:
                continue
        try:
            raw = (container.text or '').strip()[:2000]
        except Exception:
            return ""
        noise = ['倍速', 'Words & tips', 'Words &amp; tips', '播放', '暂停']
        lines = []
        for line in raw.splitlines():
            item = line.strip()
            if not item:
                continue
            if re.fullmatch(r'[\d:：/\s]+', item):
                continue
            if any(token in item for token in noise):
                continue
            lines.append(item)
        text = ' '.join(lines).strip()
        return text if len(text) > 20 else ""

    @staticmethod
    def _hint_before(inp) -> str:
        return MatchingStrategy._hint_before(inp)


class PromptBuilder:

    def __init__(self, ai_client, vocab_store: Optional["VocabularyStore"] = None):
        self.ai_client = ai_client
        self.vocab_store = vocab_store

    def build(self, questions: List[Question], global_directions: str = "") -> str:
        lines = []

        if len(self.ai_client.accumulated_passages) > 1:
            lines.append(f"【注意】本章节共有 {len(self.ai_client.accumulated_passages)} 份材料/音频转写，请根据问题判断使用哪份。")
            lines.append("")

        effective_directions = global_directions
        if not effective_directions and questions:
            effective_directions = questions[0].directions

        if effective_directions:
            lines.append(f"【题目指示】{effective_directions}")
            lines.append("")

        lines.extend(self._vocabulary_block(questions))

        type_counts = {}
        for q in questions:
            type_counts[q.q_type] = type_counts.get(q.q_type, 0) + 1

        if QuestionType.VOCABULARY_TEST in type_counts:
            lines.extend(self._vocabulary_test_hints())

        if QuestionType.BANKED_CLOZE in type_counts:
            lines.extend(self._banked_cloze_hints())

        text_question_count = type_counts.get(QuestionType.TEXT, 0)
        for q in questions:
            if q.q_type == QuestionType.TEXT:
                lines.extend(self._build_text(
                    q, effective_directions, multi_text=text_question_count > 1
                ))
            else:
                builder_method = self._get_builder_method(q.q_type)
                lines.extend(builder_method(q))

        objective_types = {
            QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
            QuestionType.LISTENING_CHOICE, QuestionType.VIDEO_CHOICE,
            QuestionType.TRUE_FALSE, QuestionType.SORTING,
            QuestionType.FILL_IN, QuestionType.LISTENING_FILL_IN,
        }
        if any(q.q_type in objective_types for q in questions):
            lines.append("【核对要求】作答前逐题回到材料原文核对：选择题排除与原文矛盾的选项；"
                         "填空题逐空定位原句并抄写缺失词，不要添加修饰词，"
                         "排序题按各选项在原文中首次出现的先后排列。")
            lines.append("")

        lines.extend(self._format_instructions(type_counts))

        return '\n'.join(lines)

    def _needs_vocabulary(self, questions: List[Question]) -> bool:
        for q in questions:
            if q.q_type == QuestionType.RECORD:
                return True
            if q.q_type in (QuestionType.FILL_IN, QuestionType.TEXT,
                            QuestionType.BANKED_CLOZE, QuestionType.MY_VOICE_TEXT):
                if extract_cjk_hints(q.text or '') or CJK_CHAR_PATTERN.search(q.text or ''):
                    return True
        return False

    def _vocabulary_block(self, questions: List[Question]) -> List[str]:
        if not self.vocab_store or not len(self.vocab_store):
            return []
        if not self._needs_vocabulary(questions):
            return []
        lines = ["【Vocabulary 对照表（来自本单元 Vocabulary 页，填空/口语稿的用词以它为准）】"]
        lines.extend(self.vocab_store.prompt_lines())
        lines.append("【要求】中文括注的空位若在下表里出现，用词必须取自表里的英文写法，不要自己意译或换同义词。")
        lines.append("【词形】词形由你按句子语法判断——该用 -ing / -ed / 复数 / 首字母大写时就改，"
                     "但词干必须与表里一致；改完要保证整句读起来通顺。")
        lines.append("")
        return lines

    def _vocabulary_test_hints(self) -> List[str]:
        return [
            "【重要提示】这是词汇测试题，包含以下类型：",
            "- 类型A（英文→中文）：题干是英文单词，选项是中文释义",
            "- 类型B（中文→英文）：题干是中文释义，选项是英文单词",
            "- 类型C（语境填空）：题干是英文句子，选项是单词填入",
            "请仔细分析每道题的具体类型，选择最准确的答案。\n"
        ]

    def _banked_cloze_hints(self) -> List[str]:
        return [
            "【重要提示】这是选词填空题，请从给定的单词/短语列表中选择最合适的填入空白处。\n",
            "【格式要求】只需返回答案本身，不要添加括号注释、不要解释、不要变形说明！",
            "【词形】若 Directions 要求 Change the form where necessary，答案必须是变形后的形式，不是词库原样。"
        ]

    def _get_builder_method(self, q_type: QuestionType) -> Callable[[Question], List[str]]:
        builders = {
            QuestionType.VOCABULARY_TEST: self._build_vocab_test,
            QuestionType.BANKED_CLOZE: self._build_banked_cloze,
            QuestionType.DROPDOWN_SELECT: self._build_dropdown_select,
            QuestionType.SINGLE_CHOICE: self._build_single_choice,
            QuestionType.MULTIPLE_CHOICE: self._build_multiple_choice,
            QuestionType.TRUE_FALSE: self._build_true_false,
            QuestionType.SORTING: self._build_sorting,
            QuestionType.FILL_IN: self._build_fill_in,
            QuestionType.TEXT: self._build_text,
            QuestionType.MY_VOICE_TEXT: self._build_my_voice_text,
            QuestionType.VIDEO: lambda q: [],
            QuestionType.VOCABULARY_FLASHCARD: lambda q: [],
            QuestionType.LISTENING_FILL_IN: self._build_listening_fill_in,
            QuestionType.LISTENING_CHOICE: self._build_listening_choice,
            QuestionType.VIDEO_CHOICE: self._build_video_choice,
            QuestionType.MATCHING: self._build_matching,
            QuestionType.PARAGRAPH_MATCH: self._build_paragraph_match,
            QuestionType.LINK_MATCH: self._build_link_match,
        }
        return builders.get(q_type, self._build_unknown)

    def _build_link_match(self, q: Question) -> List[str]:
        prompts = [item for item in q.link_items if item.get('side') == 'right']
        lines = [
            f"{q.number}. 【连线匹配】把每个提示和它对应的选项连起来",
            "   提示:",
        ]
        for index, item in enumerate(prompts):
            label = PublishQuestionParser._link_label(item.get('text', ''), number=True)
            lines.append(f"     {index + 1}. {label}")
        lines.append("   选项:")
        for opt in q.options:
            lines.append(f"     {opt.letter}. {opt.text}")
        lines.extend([
            "   请判断每个提示对应哪个选项（同一选项不应重复使用），",
            f"   只返回 {{提示序号}}.{{选项字母}}，例如 1.D 2.C 3.A 4.B（共 {len(prompts)} 组）",
            "",
        ])
        return lines

    def _build_matching(self, q: Question) -> List[str]:
        letters = q.banked_options or [opt.letter for opt in q.options]
        lines = [
            f"{q.number}. 【匹配题】把陈述和人物字母配对",
            "   本题的空里只需要填一个字母，不要写单词或句子。",
            f"   可选字母: {' / '.join(letters)}",
        ]
        for item in q.banked_blanks:
            lines.append(f"   空{item['index'] + 1}: {item.get('statement', '')}")
        lines.extend([
            "   请根据音频/视频转写判断每个人说了哪一句，",
            f"   只返回 {{空号}}.{{字母}}，例如 1.A 2.C（共 {len(q.banked_blanks)} 个空）",
            "   同一个字母可以在不同题里重复使用，但如果人数与陈述数相同则不要重复。",
            "",
        ])
        return lines

    def _build_paragraph_match(self, q: Question) -> List[str]:
        letters = q.banked_options or list("ABCDEFGHIJKLMNO")
        lines = [
            f"{q.number}. 【段落信息匹配】阅读带字母的段落，判断每条陈述出自哪一段",
            f"   可选段落字母: {' '.join(letters)}",
            "   【阅读材料】",
            (q.text or "")[:12000],
            "",
            "   【陈述】",
        ]
        for item in q.banked_blanks:
            lines.append(f"   {item['index'] + 1}. {item.get('statement', '')}")
        lines.extend([
            "",
            "   要求：每条陈述只填一个段落字母，不要写单词或句子；同一个段落可以被多条陈述重复选用。",
            f"   只返回 空号.字母，例如 1.{letters[0]} 2.{letters[-1]}（共 {len(q.banked_blanks)} 个空）",
            "",
        ])
        return lines

    def _build_sorting(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【排序题】{q.text}",
            "   请先在材料/转写中定位每个选项的原句，再按原句首次出现的先后排序。",
            "   不要按常识、重要性或选项当前顺序排列。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        lines.append("   要求：只返回排序后的选项字母，格式如：B E A D C F")
        lines.append("")
        return lines

    def _build_video_choice(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【视频选择题】{q.text}",
            "   请根据前面提供的视频转写内容选择最佳答案。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_listening_choice(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【听力选择题】{q.text}",
            "   请结合前面提供的音频转写内容和题干、选项中的图片信息选择最佳答案。",
        ]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_listening_fill_in(self, q: Question) -> List[str]:
        media_label = "视频填空题" if any(kw in (q.directions or "").lower() for kw in ['watch', 'video', 'clip', 'view']) else "听力填空题"
        lines = [
            f"{q.number}. 【{media_label}】",
            f"{q.text}",
            "",
            "【答题要求】",
            "1. 这是一个音频/视频理解题，请同时根据转写内容和每个空的左右上下文作答",
            "2. 答案优先照抄转写原文里的片段，取刚好能填进空的最短片段，保留原文的冠词、介词、数字和单位写法，"
            "不要同义改写或概括",
            "3. 每空只填写缺失部分，不要重复空格左边或右边已经出现的词",
            "4. 如果左边已有 be going to / I'm going to / we're 等结构，答案要能直接拼进原句并保持语法通顺",
            "5. 表格或列表题可参考行标题、列标题判断该空该填哪一类信息；同一个信息有几种说法时，"
            "选最具体、最贴合标题的那一种",
            "6. 答案形态要与空位一致：行标题本身已经表达的含义（如长度、宽度、时长）不要在答案里再写一遍；"
            "同一信息有形容词和名词两种写法时，选能直接嵌进整行的那一种",
            "7. 不要填整句，不要串用其他空的答案",
            ""
        ]

        for blank in q.banked_blanks:
            lines.append(f"   空{blank['index'] + 1}: {blank['sentence']}")
            if blank.get('label'):
                lines.append(f"      行标题: {blank['label']}")
            if blank.get('left_context') or blank.get('right_context'):
                lines.append(f"      左侧: {blank.get('left_context', '')}")
                lines.append(f"      右侧: {blank.get('right_context', '')}")

        lines.append("")
        return lines

    def _build_vocab_test(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【词汇题】{q.text}"]
        text_clean = re.sub(r"^\d+[.、)\]]\s*", "", q.text).strip()

        if bool(re.match(r"^[a-zA-Z\-]+$", text_clean)) and len(text_clean) <= 20:
            lines.append("   → 选择该英文单词的正确中文释义")
        elif bool(re.search(r"[\u4e00-\u9fff]", text_clean)):
            lines.append("   → 选择该中文释义对应的正确英文表达")
        elif "_" in text_clean or len(text_clean) > 50:
            lines.append("   → 根据句子语境选择最合适的单词")

        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_banked_cloze(self, q: Question) -> List[str]:
        is_phrase = q.is_phrase_mode
        lines = [
            f"{q.number}. 【选词填空】请从以下选项中选择最合适的{'短语' if is_phrase else '单词'}填入空白处：",
            f"   可选{'短语' if is_phrase else '单词'}: {', '.join(q.banked_options)}",
        ]
        material = ""
        for line in (q.text or "").splitlines():
            if line.startswith("【全文】"):
                material = line[len("【全文】"):].strip()
                break
        if material:
            lines.append(f"   全文: {material[:1800]}")
        lines.append("   注意：题干里的词库一般每个词只能用一次，且必须按语法改变词形。")
        if (q.directions or '').strip():
            lines.append(f"   Directions: {q.directions.strip()[:400]}")
        if is_expression_bank_question(q):
            lines.extend(EXPRESSION_BANK_HINTS)

        if is_phrase:
            lines.extend([
                "注意：这是短语填空！请填写完整短语（如 'in advance' 而不是 'advance'）。",
                "必要时需要改变短语的形式（如时态、单复数等）。",
            ])
        else:
            lines.extend([
                " 注意：必要时需要改变单词形式（如时态、单复数等）。",
            ])

        lines.append("")

        for i, blank in enumerate(q.banked_blanks, 1):
            context = blank['context'][:400] + "..." if len(blank['context']) > 400 else blank['context']
            context = re.sub(r'<[^>]+>', '', context)
            lines.append(f"   空{i}: {context}")

        lines.append("")
        lines.append("   要求：")

        if is_phrase:
            lines.append("1. 必须填写完整短语（不要只填部分）")
            lines.append("2. 按顺序给出答案，格式：1.in advance 2.make the most of ...")
        else:
            lines.append("1. 按顺序给出答案，格式：1.word1 2.word2 ...")

        lines.append("")
        return lines

    def _build_single_choice(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【单选】{q.text}"]
        for opt in q.options:
            lines.append(f" {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _build_multiple_choice(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【多选】{q.text}", "   （注意：本题有多个正确答案）"]
        has_option_images = False
        elements = [getattr(q, 'element', None)]
        elements.extend(getattr(opt, 'element', None) for opt in q.options)
        for element in elements:
            try:
                if element and element.find_elements(By.CSS_SELECTOR, 'img[src], img[data-src]'):
                    has_option_images = True
                    break
            except Exception:
                continue
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        if has_option_images:
            lines.append("   图片选项必须核对图片人物与听力中的说话者是否对应，包括 he/she；不能只因活动关键词出现在转写中就选该项。")
        lines.append("")
        return lines

    def _build_true_false(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【判断】{q.text}",
            "   请根据材料/音频/视频内容判断正误，只回答 T（正确）或 F（错误）。",
            "",
        ]
        return lines

    def _build_fill_in(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【填空】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 个空）")
        if is_expression_bank_question(q):
            lines.extend(EXPRESSION_BANK_HINTS)
        if q.banked_options:
            lines.append(f"   可选词: {', '.join(q.banked_options)}")
            lines.append("   （只能从可选词中选择；必要时改变词形：单复数/时态/词性/派生词）")
            lines.append("   （若空位位于句子开头，首字母必须大写，否则会扣分）")
        hints = [b for b in q.banked_blanks if b.get('hint')]
        contexts = [b for b in q.banked_blanks if b.get('context')]

        cjk_hints = blank_cjk_hints(q)
        vocab_hits = []
        if cjk_hints and self.vocab_store:
            for i, hint in enumerate(cjk_hints):
                english = self.vocab_store.lookup(hint)
                if english:
                    vocab_hits.append(f"     空{i + 1}: {hint} → {english}")
        if vocab_hits:
            lines.append("   中文括注对应 Vocabulary 里的原词（用词以它为准）:")
            lines.extend(vocab_hits)
            lines.append("   （词形按句子语法自行判断：该用 -ing / -ed / 复数 / 首字母大写时就改，词干不要换词）")
        elif cjk_hints:
            lines.append("   中文括注提示:")
            lines.extend(f"     空{i + 1}: {hint}" for i, hint in enumerate(cjk_hints))
            lines.append("   （答案是从本单元 Vocabulary 学过的英文表达，请用教材里的固定搭配）")

        if hints:
            lines.append("   已给出的字母（题干已印在输入框外面，答案请给完整单词，程序会自动去掉这部分）:")
            lines.append("   优先使用上下文里 Learning 页出现的搭配词汇，不要换成同前缀的其它词；按当前句子语法调整单复数、时态等词形。")
            for item in hints:
                line = f"     空{item['index'] + 1}: 已给出 {item['hint']}"
                candidates = item.get('candidates') or []
                if candidates:
                    line += f"；课文里以它开头的词: {', '.join(candidates)}"
                lines.append(line)
        if contexts:
            lines.append("   上下文（只给空位附近的一小段）:")
            for item in contexts:
                line = f"     空{item['index'] + 1}: {item['context'][:140]}"
                right = (item.get('right_context') or '').strip()
                if right:
                    line += f"  ___  {right[:80]}"
                lines.append(line)
        lines.append("")
        return lines

    def _build_text(self, q: Question, directions: str = "", *, multi_text: bool = False) -> List[str]:
        lines = [f"{q.number}. 【简答题】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 小题）")
        combined_dir = f"{q.directions or ''}\n{directions or ''}"
        q_text = q.text or ''
        dir_to_en = bool(re.search(r'into\s+english|(?:翻?译[成为]|汉译|中译)英[文语]?', combined_dir, re.I))
        dir_to_zh = bool(re.search(r'into\s+chinese|(?:翻?译[成为]|英译)[中汉][文语]?', combined_dir, re.I))

        target_lang = ''
        if dir_to_en and not dir_to_zh:
            target_lang = 'en'
        elif dir_to_zh and not dir_to_en:
            target_lang = 'zh'
        else:
            if re.search(r'(?:翻?译[成为]|汉译|中译)英[文语]?', q_text):
                target_lang = 'en'
            elif re.search(r'(?:翻?译[成为]|英译)[中汉][文语]?', q_text):
                target_lang = 'zh'
            elif re.search(r'translat|翻译', combined_dir, re.I):
                target_lang = 'zh' if not CJK_CHAR_PATTERN.search(q_text) else 'generic'

        if len(q.inputs) > 1:
            stems = []
            for item in (q.banked_blanks or []):
                stem = ''
                if isinstance(item, dict):
                    stem = str(item.get('words') or item.get('question_text') or '').strip()
                if stem:
                    stems.append(stem)
            if len(stems) == len(q.inputs) and len(set(stems)) > 1:
                lines.append(f"   【小题列表】（共 {len(stems)} 个小题，每题都必须作答）")
                for stem_index, stem in enumerate(stems, 1):
                    lines.append(f"   {stem_index}. {stem}")
                lines.append("   （回答时严格按 1、2、… 编号逐题输出，按题号对应每个小题，不要合并、不要遗漏）")
            elif not stems:
                lines.append(f"   （本题为 {len(q.inputs)} 个小题的简答组：请按题号 1、2、… 分别作答，每个小题都要有单独内容）")
            lines.append(f"   （共 {len(q.inputs)} 小题）")

        multi_item = multi_text or len(q.inputs) > 1
        if target_lang == 'en':
            suffix = "请按题号分别编号输出" if multi_item else "不要保留中文原文或题号"
            lines.append(f"   （翻译题：把原文完整翻译成英文，只输出英文译文，{suffix}）")
        elif target_lang == 'zh':
            suffix = "请按题号分别编号输出" if multi_item else "不要保留英文原文或题号"
            lines.append(f"   （翻译题：把原文完整翻译成中文，只输出中文译文，{suffix}）")
        elif target_lang == 'generic':
            suffix = "请按题号分别编号输出" if multi_item else "不要保留原文或题号"
            lines.append(f"   （翻译题：请按题目指示完成翻译，只输出译文，{suffix}）")
        else:
            lines.append("   （请提供简洁准确的回答，如果不是翻译题，那么只用英文回答）")
        lines.append("")
        return lines

    def _build_my_voice_text(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【My voice文字作答】{q.text}",
            "   要求：只输出一段可直接填写到输入框的英文介绍。",
            "   限制：500字符以内，不要说明自己无法录音或上传。",
            "",
        ]
        return lines

    def build_speaking_script(self, q: Question, directions: str = "",
                              target_seconds: float = 90.0) -> str:
        target_seconds = float(target_seconds or 90.0)
        words_low = max(60, int(target_seconds * 2.1))
        words_high = max(words_low + 20, int(target_seconds * 2.6))

        lines = [
            "【口语录音题】页面上只有一个计时录音按钮，没有参考答案，需要你自己说一段英文。",
            f"【题目要求】{(q.text or '').strip()[:900]}",
        ]
        if directions and directions.strip() and directions.strip() not in (q.text or ''):
            lines.append(f"【题目指示】{directions.strip()[:400]}")
        if self.vocab_store and len(self.vocab_store):
            lines.append("【本单元 Vocabulary（尽量自然地用上这些表达，不要硬塞）】")
            lines.extend(self.vocab_store.prompt_lines(24))
        lines.extend([
            "【输出要求】",
            "1. 只输出可以直接朗读的英文正文；不要标题、不要 markdown、不要括号说明、不要中文。",
            f"2. 长度约 {words_low}-{words_high} 词，句子短、口语化，适合朗读。",
            "3. 紧扣题目要求组织内容，并结合相关材料给出具体回答。",
        ])
        return "\n".join(lines)

    def _build_unknown(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【题】{q.text}"]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _format_instructions(self, type_counts: Dict[QuestionType, int]) -> List[str]:
        lines = ["-" * 50, "请按以下格式回答："]

        has_single = (
                QuestionType.SINGLE_CHOICE in type_counts or
                QuestionType.VOCABULARY_TEST in type_counts or
                QuestionType.LISTENING_CHOICE in type_counts or
                QuestionType.VIDEO_CHOICE in type_counts
        )
        has_multiple = QuestionType.MULTIPLE_CHOICE in type_counts

        if has_single and not has_multiple:
            lines.append("单选题: 直接返回选项字母，如：A 或 1.A")
            lines.append("注意：每道题只选一个答案！")

        elif has_multiple and not has_single:
            lines.append("多选题: 返回多个字母，如：AB 或 1.AB")
            lines.append("注意：每道题可能有一个或多个正确答案！")

        elif has_single and has_multiple:
            lines.append("混合题型：")
            lines.append("- 单选题: 返回单个字母，如：A")
            lines.append("- 多选题: 返回多个字母，如：AB")
            lines.append("请仔细判断每道题是单选还是多选！")
            lines.append("判断依据：题目明确标注'多选'或有多个正确选项时选多个，否则单选")

        if QuestionType.BANKED_CLOZE in type_counts or QuestionType.DROPDOWN_SELECT in type_counts:
            lines.append("选词/选择填空: 1.答案1 2.答案2 ...（答案可以是多词短语）")

        if QuestionType.FILL_IN in type_counts:
            lines.append("填空题: 每空一行，按空号编号：\n1. 答案1\n2. 答案2 ...（可多词，保留小数）")

        if QuestionType.LISTENING_FILL_IN in type_counts:
            lines.append("听力/视频填空题: 每空一行，按空号编号：\n1. 答案1\n2. 答案2 ...（照抄转写原文，保留小数）")

        if QuestionType.TRUE_FALSE in type_counts:
            lines.append("判断题: 每题回答 T 或 F，如：1.T 2.F")

        if QuestionType.TEXT in type_counts:
            lines.append("简答题: 逐题编号作答，如：1.第一题答案 2.第二题答案")
            lines.append("注意：每个小题都必须作答，逐题编号，不要合并成一段，也不要遗漏任何一个题号！")

        if QuestionType.MY_VOICE_TEXT in type_counts:
            lines.append("My voice文字作答: 1.英文介绍内容（500字符以内）")

        if QuestionType.SORTING in type_counts:
            lines.append("排序题: 按题号返回排序后的字母，如：1.B E A D C F")

        if QuestionType.MATCHING in type_counts:
            lines.append("匹配题: 每空一个字母，格式：1.B 2.A 3.F（不要写单词）")

        if QuestionType.PARAGRAPH_MATCH in type_counts:
            lines.append("段落信息匹配: 1.H 2.N ...（只填段落字母，同一段可重复）")

        if QuestionType.LINK_MATCH in type_counts:
            lines.append("连线匹配: 提示序号配选项字母，格式：1.D 2.C 3.A 4.B（不要写选项原文）")

        lines.append("-" * 50)
        return lines

    def _build_dropdown_select(self, q: Question) -> List[str]:
        if not q.banked_blanks:
            lines = [f"{q.number}. 【选择填空】{q.text}"]
            total = (q.answer_count
                     or len(re.findall(r'\(\d+\)|\b\d{1,2}\)', q.text))
                     or 1)
            lines.append(f"   （共 {total} 个空，按空号 1..{total} 顺序作答）")
            if q.banked_options:
                lines.append(f"   可选选项: {', '.join(q.banked_options)}")
                lines.append("   （只能从选项中选择；必要时按语法改变词形，句首记得大写）")
            lines.append("   要求：按顺序给出答案，格式：1.答案1 2.答案2 ...")
            lines.append("")
            return lines

        lines = [
            f"{q.number}. 【选择填空】请从选项中选择合适的词填入空白处：",
            f"   可选选项: {', '.join(q.banked_options)}",
            ""
        ]
        for line in (q.text or "").splitlines():
            if line.startswith("【全文】"):
                lines.append(f"   全文: {line[len('【全文】'):].strip()[:6000]}")
                break
        lines.append("")

        for i, blank in enumerate(q.banked_blanks, 1):
            context = blank['context'][:400] + "..." if len(blank['context']) > 400 else blank['context']
            context = re.sub(r'<[^>]+>', '', context)
            lines.append(f"   空{i}: {context}")

        lines.append("")
        lines.append("要求：按顺序给出答案，格式：1.do 2.make ...")
        lines.append("")
        return lines


class AnswerExecutor:

    ANSWER_LABEL_PATTERN = r'(?:My voice文字作答|简答题|选词/选择填空|填空题|答案|选词填空|翻译|Answer)'
    NUMBER_PREFIX_PATTERN = r'(?:空\s*)?\d+\s*[.、:：\)\]]|Blank\s*\d+\s*[.、:：\)\]]'

    def __init__(self, driver, tts: Optional["TTSEngine"] = None, stop_requested=None):
        self.driver = driver
        self.tts = tts
        self.stop_requested = stop_requested if stop_requested is not None else threading.Event()

    def execute(self, question: Question, answer: str) -> bool:
        executors = {
            QuestionType.SINGLE_CHOICE: self._fill_single_choice,
            QuestionType.LISTENING_CHOICE: self._fill_single_choice,
            QuestionType.VIDEO_CHOICE: self._fill_single_choice,
            QuestionType.VOCABULARY_TEST: self._fill_single_choice,
            QuestionType.MULTIPLE_CHOICE: self._fill_multiple_choice,
            QuestionType.SORTING: self._fill_sorting,
            QuestionType.BANKED_CLOZE: self._fill_banked_cloze,
            QuestionType.DROPDOWN_SELECT: self._fill_dropdown_select,
            QuestionType.FILL_IN: self._fill_fill_in,
            QuestionType.TEXT: self._fill_text,
            QuestionType.MY_VOICE_TEXT: self._fill_text,
            QuestionType.LISTENING_FILL_IN: self._fill_listening_fill_in,
            QuestionType.MATCHING: self._fill_matching,
            QuestionType.PARAGRAPH_MATCH: self._fill_paragraph_match,
            QuestionType.RECORD: self._answer_record,
            QuestionType.ORAL_ALOUD: self._answer_oral_aloud,
        }

        executor = executors.get(question.q_type, self._fill_unknown)
        return executor(question, answer)

    def _fill_single_choice(self, q: Question, answer: str) -> bool:
        valid_letters = [opt.letter or chr(65 + idx) for idx, opt in enumerate(q.options)]
        letters = self._parse_choice_letters(answer, valid_letters)
        if not letters:
            letters = self._letters_from_option_text(answer, q)
            if letters:
                print(f"\t答案不是字母，已按选项原文反查: {letters}")
        if len(letters) != 1:
            print(f"\t单选题需要一个有效选项，收到: {answer}")
            return False
        answer_letter = letters[0]

        print(f"\t寻找选项: {answer_letter}")
        print(f"\t可用选项: {[opt.letter for opt in q.options]}")

        for opt in q.options:
            if opt.letter.upper() == answer_letter.upper():
                print(f"\t点击选项 {opt.letter}: {opt.text[:30]}...")
                return WebDriverHelper.safe_click(self.driver, opt.element)

        try:
            idx = ord(answer_letter.upper()) - ord('A')
            if 0 <= idx < len(q.options):
                opt = q.options[idx]
                print(f"\t通过索引匹配选项 {opt.letter}: {opt.text[:30]}...")
                return WebDriverHelper.safe_click(self.driver, opt.element)
        except:
            pass

        return False

    def _fill_multiple_choice(self, q: Question, answer: str) -> bool:
        letters = self._parse_choice_letters(answer, [opt.letter for opt in q.options])
        if not letters:
            letters = self._letters_from_option_text(answer, q)
            if letters:
                print(f"\t答案不是字母，已按选项原文反查: {letters}")
        if not letters:
            print(f"\t多选题答案没有有效的选项组合: {answer}")
            return False
        for opt in q.options:
            should_select = opt.letter.upper() in letters
            if opt.is_selected != should_select:
                if not WebDriverHelper.safe_click(self.driver, opt.element):
                    return False
                opt.is_selected = should_select
        return True

    def _fill_sorting(self, q: Question, answer: str) -> bool:
        order = self._parse_sorting_order(answer, [opt.letter for opt in q.options])
        if not order:
            return False

        print(f"\t排序答案: {' '.join(order)}")
        if self._apply_sorting_by_react(q, order):
            return True

        if self._apply_sorting_by_drag(q, order):
            return True

        if self._apply_sorting_by_js(q, order):
            return True

        return False

    def _parse_sorting_order(self, answer: str, valid_letters: List[str]) -> List[str]:
        valid = [letter.upper() for letter in valid_letters if letter]
        valid_set = set(valid)
        if not valid:
            return []

        single_answer = re.fullmatch(r'\s*\d+\s*[.、:：)\]]\s*([A-Z][A-Z\s,，、;；>→-]*)\s*', answer.upper())
        if single_answer:
            answer = single_answer.group(1)
        numbered = re.findall(r'\d+\s*[.、)\]]\s*([A-Z])\b', answer.upper())
        candidates = numbered if numbered else re.findall(r'\b([A-Z])\b', answer.upper())

        if len(candidates) < len(valid):
            compact = re.findall(r'[A-Z]+', answer.upper())
            for chunk in compact:
                letters = [ch for ch in chunk if ch in valid_set]
                if len(letters) >= len(valid):
                    candidates = letters
                    break

        order = []
        for letter in candidates:
            letter = letter.upper()
            if letter in valid_set and letter not in order:
                order.append(letter)

        return order if len(order) == len(valid) else []

    def _get_sorting_items(self, q: Question) -> List[Dict[str, Any]]:
        items = []
        elems = q.element.find_elements(By.CSS_SELECTOR, '.sequence-reply-view-item-text')
        for elem in elems:
            text = elem.text.strip()
            letter = ""
            try:
                spans = elem.find_elements(By.TAG_NAME, 'span')
                if spans:
                    letter = spans[0].text.strip().replace('.', '').replace(')', '').upper()
            except:
                pass
            if not letter:
                match = re.match(r'^([A-Z])[\s.、)]*', text)
                if match:
                    letter = match.group(1).upper()
            if letter:
                items.append({'letter': letter, 'element': elem})
        return items

    def _current_sorting_order(self, q: Question) -> List[str]:
        return [item['letter'] for item in self._get_sorting_items(q)]

    def _apply_sorting_by_react(self, q: Question, order: List[str]) -> bool:
        js = """
        const root = arguments[0];
        const order = arguments[1];
        const wrapper = root.matches('.sortable-list-wrapper, .sequence-view')
            ? root
            : root.querySelector('.sortable-list-wrapper') || root.querySelector('.sequence-view');
        if (!wrapper) return false;
        const fiberKey = Object.keys(wrapper).find(k => k.startsWith('__reactFiber$'));
        if (!fiberKey) return false;

        let fiber = wrapper[fiberKey];
        let sortable = null;
        let component = null;
        for (let depth = 0; depth < 12 && fiber; depth++, fiber = fiber.return) {
            const props = fiber.memoizedProps;
            const state = fiber.memoizedState;
            if (!sortable && props && typeof props.onSortEnd === 'function') sortable = fiber;
            if (!component && state && Array.isArray(state.options)) component = fiber;
            if (sortable && component) break;
        }
        if (!sortable || !component) return false;

        const currentOptions = () => (
            (component.stateNode && component.stateNode.state && component.stateNode.state.options)
            || component.memoizedState.options
            || []
        );
        let options = currentOptions();
        if (!order.every(letter => options.some(opt => opt.value === letter))) return false;

        for (let i = 0; i < order.length; i++) {
            options = currentOptions();
            const from = options.findIndex(opt => opt.value === order[i]);
            if (from >= 0 && from !== i) sortable.memoizedProps.onSortEnd({oldIndex: from, newIndex: i});
        }
        return true;
        """
        try:
            if not self.driver.execute_script(js, q.element, order):
                return False
            time.sleep(0.4)
            return self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\tReact 排序失败: {str(e)[:60]}")
            return False

    def _apply_sorting_by_drag(self, q: Question, order: List[str]) -> bool:
        try:
            for target_index, target_letter in enumerate(order):
                items = self._get_sorting_items(q)
                current_order = [item['letter'] for item in items]
                if current_order == order:
                    return True
                if target_index >= len(items) or current_order[target_index] == target_letter:
                    continue

                source = next((item['element'] for item in items if item['letter'] == target_letter), None)
                target = items[target_index]['element']
                if not source or not target:
                    return False

                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", source)
                time.sleep(0.2)
                ActionChains(self.driver).click_and_hold(source).pause(0.2).move_to_element(target).pause(0.2).release().perform()
                time.sleep(0.5)

            return self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\t拖拽排序失败: {str(e)[:60]}")
            return False

    def _apply_sorting_by_js(self, q: Question, order: List[str]) -> bool:
        js = """
        const root = arguments[0];
        const order = arguments[1];
        const wrapper = root.querySelector('.sortable-list-wrapper') || root.querySelector('.sequence-view');
        if (!wrapper) return false;

        const children = Array.from(wrapper.children);
        const pairs = [];
        for (let i = 0; i < children.length; i++) {
            const node = children[i];
            if (!node.classList.contains('sequence-reply-view-item-text')) continue;
            const letterText = (node.querySelector('span')?.textContent || node.textContent || '').trim();
            const letter = (letterText.match(/[A-Z]/) || [''])[0];
            const numberNode = i > 0 && children[i - 1].classList.contains('sortable-list-question-no')
                ? children[i - 1]
                : null;
            if (letter) pairs.push({ letter, numberNode, itemNode: node });
        }

        const byLetter = new Map(pairs.map(pair => [pair.letter, pair]));
        if (!order.every(letter => byLetter.has(letter))) return false;

        order.forEach((letter, index) => {
            const pair = byLetter.get(letter);
            if (pair.numberNode) {
                const strong = pair.numberNode.querySelector('strong');
                if (strong) strong.textContent = String(index + 1);
                wrapper.appendChild(pair.numberNode);
            }
            wrapper.appendChild(pair.itemNode);
        });

        ['input', 'change', 'mouseup', 'drop', 'dragend', 'sortupdate'].forEach(type => {
            wrapper.dispatchEvent(new Event(type, { bubbles: true }));
        });

        const reactKey = Object.keys(wrapper).find(key => key.startsWith('__reactProps$'));
        if (reactKey) {
            const props = wrapper[reactKey];
            if (props && typeof props.onChange === 'function') props.onChange(order);
            if (props && typeof props.onSortEnd === 'function') props.onSortEnd({ oldIndex: 0, newIndex: 0 });
        }

        return Array.from(wrapper.querySelectorAll('.sequence-reply-view-item-text'))
            .map(node => ((node.querySelector('span')?.textContent || node.textContent || '').match(/[A-Z]/) || [''])[0])
            .join('') === order.join('');
        """
        try:
            result = self.driver.execute_script(js, q.element, order)
            time.sleep(0.5)
            return bool(result) and self._current_sorting_order(q) == order
        except Exception as e:
            print(f"\tJS排序失败: {str(e)[:60]}")
            return False

    def _fill_banked_cloze(self, q: Question, answer: str) -> bool:
        words = self._parse_banked_answer(answer, len(q.banked_blanks))
        is_phrase_mode = q.is_phrase_mode

        print(f"\t解析答案: {words}")
        print(f"\t填空数量: {len(q.banked_blanks)}")
        print(f"\t模式: {'短语' if is_phrase_mode else '单词'}")

        success_count = 0

        for i, (blank, word) in enumerate(zip(q.banked_blanks, words)):
            if blank['input'] and word:
                try:
                    clean_word = word.strip()
                    clean_word = self._strip_given_prefix(blank.get('hint', ''), clean_word)
                    if not getattr(self, '_exact_official_mode', False):
                        matched = self._match_to_option(
                            clean_word, q.banked_options, is_phrase_mode,
                            allow_rewrite=not is_expression_bank_question(q))
                        if matched:
                            clean_word = matched
                            print(f"        匹配到选项: {clean_word}")

                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'});",
                        blank['input']
                    )
                    time.sleep(0.05)

                    if not self._fill_text_input_verified(blank['input'], clean_word):
                        print(f"        空{i + 1}: 写入后校验失败，跳过该空")
                        continue

                    print(f"        空{i + 1}: {clean_word}")
                    success_count += 1

                except Exception as e:
                    error_msg = str(e)
                    print(f"      填空 {i + 1} 失败:{error_msg[:50]} ")
                    logger.error(f"详细错误: {error_msg}", exc_info=True)

        return bool(q.banked_blanks) and success_count == len(q.banked_blanks)

    def _match_to_option(self, answer: str, options: List[str], is_phrase_mode: bool,
                         allow_rewrite: bool = True) -> Optional[str]:
        if not answer or not options:
            return None

        answer_lower = answer.lower().strip()

        for opt in options:
            if opt.lower() == answer_lower:
                return answer if not allow_rewrite else opt

        if is_phrase_mode:
            for opt in options:
                opt_lower = opt.lower()
                if '...' in opt_lower or '…' in opt_lower or '_' in opt_lower:
                    parts = [p.strip() for p in re.split(r'\.\.\.|…|_', opt_lower) if p.strip()]
                    for part in parts:
                        if len(part) >= 2 and (answer_lower.startswith(part[:3]) or part.startswith(answer_lower[:3])):
                            return answer
                    continue

                if answer_lower in opt_lower and len(answer_lower) >= 4:
                    if not allow_rewrite:
                        continue
                    return opt
                if opt_lower in answer_lower:
                    return answer
        else:
            for opt in options:
                opt_lower = opt.lower()
                if answer_lower.startswith(opt_lower[:3]):
                    if (answer_lower == opt_lower + 's' or
                            answer_lower == opt_lower + 'es' or
                            answer_lower == opt_lower + 'd' or
                            answer_lower == opt_lower + 'ed' or
                            answer_lower == opt_lower + 'ing' or
                            answer_lower == opt_lower[:-1] + 'ies' or
                            answer_lower == opt_lower[:-1] + 'ied' or
                            answer_lower == opt_lower[:-1] + 'ing' or
                            answer_lower == opt_lower + opt_lower[-1] + 'ed' or
                            answer_lower == opt_lower + opt_lower[-1] + 'ing'):
                        return answer

                if opt_lower.startswith(answer_lower[:3]):
                    if (opt_lower == answer_lower + 's' or
                            opt_lower == answer_lower + 'es' or
                            opt_lower == answer_lower + 'd' or
                            opt_lower == answer_lower + 'ed' or
                            opt_lower == answer_lower + 'ing'):
                        return opt

        return None

    def _fill_fill_in(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.inputs))
        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, inp in enumerate(q.inputs):
            ans = answers[i] if i < len(answers) else ""
            if ans:
                hint = ""
                if i < len(q.banked_blanks):
                    hint = q.banked_blanks[i].get('hint') or ''
                stripped = self._strip_given_prefix(hint, ans)
                if stripped != ans:
                    print(f"\t空{i + 1}: 去掉已给出的「{hint}」-> {stripped}")
                    ans = stripped
                print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")
            else:
                print(f"\t空{i + 1}: (空)")

        if q.inputs and success_count < len(q.inputs):
            print(f"\t只填写 {success_count}/{len(q.inputs)} 个空，不提交")
        return bool(q.inputs) and success_count == len(q.inputs)

    @staticmethod
    def _strip_given_prefix(hint: str, answer: str) -> str:
        text = (answer or "").strip()
        prefix = (hint or "").strip()
        if not text or not prefix:
            return text
        if len(text) > len(prefix) and text[:len(prefix)].lower() == prefix.lower():
            return text[len(prefix):].lstrip()
        return text

    @classmethod
    def _extract_answer_by_number(cls, answer: str, question_number: int) -> str:
        answer = cls._normalize_answer_labels(answer)
        label_prefix = rf'(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?'
        number_prefix = cls._number_prefix_pattern(question_number)
        next_prefix = rf'(?<![$\w])(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{cls.NUMBER_PREFIX_PATTERN})'
        pattern = rf'(?<![$\w]){label_prefix}{number_prefix}\s*(.+?)(?=\s*{next_prefix}\s*|$)'
        match = re.search(pattern, answer, re.DOTALL)
        if match:
            return cls._clean_extracted_answer(match.group(1))

        lines = [l.strip() for l in answer.split('\n') if l.strip()]
        for line in lines:
            clean = re.sub(
                rf'^(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{cls.NUMBER_PREFIX_PATTERN})\s*',
                '',
                line,
                flags=re.I
            ).strip()
            if clean and not re.match(r'^\d', clean):
                if re.match(rf'^(?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?{number_prefix}', line, re.I):
                    return cls._clean_extracted_answer(clean)

        return ""

    @classmethod
    def _normalize_answer_labels(cls, answer: str) -> str:
        return re.sub(
            rf'(?<!^)(?<![$\w])\s+((?:{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{cls.NUMBER_PREFIX_PATTERN}))',
            r'\n\1',
            answer.strip(),
            flags=re.I
        )

    @classmethod
    def _clean_extracted_answer(cls, answer: str) -> str:
        if not answer:
            return ""
        answer = re.sub(rf'^{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(rf'^(?:{cls.NUMBER_PREFIX_PATTERN})\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(rf'\s*{cls.ANSWER_LABEL_PATTERN}\s*[：:]\s*$', '', answer.strip(), flags=re.I)
        return answer.strip()

    @staticmethod
    def _number_prefix_pattern(number: int) -> str:
        return rf'(?:(?:空\s*)?{number}\s*[.、:：\)\]]|Blank\s*{number}\s*[.、:：\)\]])'

    RECORD_LEAD_PAD = 0.8
    RECORD_RETRY_PAD = 1.3
    RECORD_MIN_SCORE = 60
    RECORD_MAX_SECONDS = 105.0
    RECORD_AUDIO_CACHE: Dict[str, bytes] = {}

    def _answer_record(self, q: Question, answer: str = "") -> bool:
        items = list(q.record_items or [])
        if not items:
            print("\t没有找到录音条目")
            return False
        if items[0].get('role_play'):
            return self._answer_role_play(q)
        if items[0].get('vocab'):
            return self._answer_vocabulary_read_aloud(q)

        self._dismiss_record_popup()
        probe = self.driver.execute_async_script(RECORD_MIC_JS, "", self.RECORD_LEAD_PAD, 0)
        if not probe or not probe.get("ok"):
            print(f"\t录音注入初始化失败: {probe}")
            return False

        texts = self._record_speech_texts(q, answer)
        if not any((item.get('url') or '').strip() for item in items):
            print("\t录音题没有原声，用 TTS 朗读页面原文")

        for idx in range(len(items)):
            try:
                self._record_one_item(q, idx, texts)
            except Exception as exc:
                print(f"\t第 {idx + 1} 条录音失败: {str(exc)[:80]}")
                logger.error("录音题执行失败", exc_info=True)

        missing = [i for i in range(len(items)) if not self._row_done(q, i)]
        if missing:
            print(f"\t有 {len(missing)} 条没有录制结果，补录: {[i + 1 for i in missing]}")
            for idx in missing:
                try:
                    self._record_one_item(q, idx, texts)
                except Exception:
                    logger.error("录音题补录失败", exc_info=True)

        done = sum(1 for i in range(len(items)) if self._row_done(q, i))
        print(f"\t录音题完成 {done}/{len(items)} 条")
        return done == len(items)

    ORAL_ALOUD_COUNTDOWN = 130
    ORAL_ALOUD_TIMEOUT = 420

    def _answer_oral_aloud(self, q: Question, answer: str = "") -> bool:
        if self.stop_requested.is_set():
            return False
        item = (q.record_items or [{}])[0]
        text = (item.get('text') or '').strip() or self._record_reading_text(q)
        if not text:
            print("\t朗读题没有读到原文，跳过")
            return False
        if not self._tts().enabled:
            print("\t朗读题需要 TTS，TTS 已关闭，跳过")
            return False
        state = self.driver.execute_script(ORAL_ALOUD_STATE_JS) or {}
        if (state.get('score') or '').strip():
            print(f"\t朗读题已有得分 {state.get('score')}，跳过")
            return True
        data = self._speech_audio(text, self.RECORD_MAX_SECONDS)
        if not data:
            return False
        payload = base64.b64encode(data).decode('ascii')
        probe = self.driver.execute_async_script(
            RECORD_MIC_JS, payload, self.RECORD_LEAD_PAD, self.RECORD_MAX_SECONDS)
        if not probe or not probe.get('ok'):
            print(f"\t朗读音频注入失败: {probe}")
            return False
        self._oral_stop_after = float(probe.get('dur') or 0) + 1.0
        print("\t朗读题：开始强制流程（约 4 分钟）")
        if not self._oral_aloud_start(q):
            return False
        score = self._wait_oral_aloud(q)
        if score is None:
            print("\t朗读题等待超时，没有拿到分数")
            return False
        print(f"\t朗读题得分 {score}")
        return True

    def _oral_aloud_start(self, q: Question) -> bool:
        state = self.driver.execute_script(ORAL_ALOUD_STATE_JS) or {}
        label = (state.get('btn') or '').strip()
        if state.get('recording') or (label and label != 'start'):
            return True
        try:
            button = q.element.find_element(By.CSS_SELECTOR, '.p-oral-aloud-button .button-start')
        except Exception:
            try:
                button = self.driver.find_element(
                    By.CSS_SELECTOR, '.p-oral-aloud-button .button-start')
            except Exception:
                print("\t找不到朗读题的 start 按钮")
                return False
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'})", button)
            time.sleep(0.3)
            button.click()
        except Exception as exc:
            print(f"\t点击 start 失败: {str(exc)[:60]}")
            return False
        return True

    def _wait_oral_aloud(self, q: Question) -> Optional[float]:
        start = time.time()
        active_start = None
        stopped = False
        while time.time() - start < self.ORAL_ALOUD_TIMEOUT:
            if self.stop_requested.is_set():
                return None
            try:
                state = self.driver.execute_script(ORAL_ALOUD_STATE_JS) or {}
            except Exception:
                state = {}
            raw = (state.get('score') or '').strip()
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    return 0.0
            label = (state.get('timer') or state.get('btn') or '').strip()
            match = re.search(r'(\d{1,2}):(\d{2})', label)
            active = bool(match and (int(match.group(1)) > 0 or int(match.group(2)) > 0))
            if active:
                if active_start is None:
                    active_start = time.time()
                stop_after = getattr(self, '_oral_stop_after', 0.0)
                if not stopped and stop_after and time.time() - active_start >= stop_after:
                    stopped = True
                    try:
                        btn = q.element.find_element(By.CSS_SELECTOR, '.record-icon.button-record')
                        self.driver.execute_script('arguments[0].click()', btn)
                        print("\t朗读录音结束，点击停止")
                    except Exception:
                        try:
                            btn = self.driver.find_element(By.CSS_SELECTOR, '.record-icon.button-record')
                            self.driver.execute_script('arguments[0].click()', btn)
                            print("\t朗读录音结束，点击停止")
                        except Exception:
                            pass
            time.sleep(2)
        return None

    def _answer_vocabulary_read_aloud(self, q: Question) -> bool:
        if self.stop_requested.is_set():
            return False
        first = (q.record_items or [{}])[0]
        total = int(first.get('vocab_total') or 0) or 0
        if total <= 0:
            print("\t未读到词汇卡片总数，停止以免遗漏卡片")
            return False
        print(f"\t词汇跟读：共 {total} 张卡片，使用页面原声逐张跟读（不调用 AI）")

        self._dismiss_record_popup()
        probe = self.driver.execute_async_script(
            RECORD_MIC_JS, "", self.RECORD_LEAD_PAD, 0)
        if not probe or not probe.get("ok"):
            print(f"\t录音注入初始化失败: {probe}")
            return False

        try:
            current = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
            if int(current.get('index') or 0) != 1:
                if not self.driver.execute_script(VOCAB_NEXT_JS, q.element, True):
                    print("\t无法返回第一张词汇卡片，停止")
                    return False
        except Exception:
            return False

        done = 0
        for index in range(1, total + 1):
            info = self._wait_vocab_card(q, index)
            if not info:
                print(f"\t第 {index} 张卡片尚未就绪，停止")
                break
            word = (info.get('word') or '').strip()
            ok = self._record_vocab_card(q, info)
            print(f"\t词汇跟读 [{index}/{total}] {word} -> {'OK' if ok else '失败'}")
            if not ok or self.stop_requested.is_set():
                break
            done += 1
            if index == total:
                break

            try:
                moved = self.driver.execute_script(VOCAB_NEXT_JS, q.element)
            except Exception:
                moved = ''
            if not moved:
                print("\t找不到下一张卡片的切换方式，停止")
                break

        print(f"\t词汇跟读完成 {done}/{total} 张")
        return done == total and not self.stop_requested.is_set()

    def _wait_vocab_card(self, q: Question, index: int) -> Optional[Dict]:
        for _ in range(24):
            if self.stop_requested.is_set():
                return None
            try:
                info = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
                if (int(info.get('index') or 0) == index
                        and (info.get('word') or '').strip() and info.get('hasButton')
                        and self._vocab_record_buttons(q)):
                    return info
            except Exception:
                pass
            if self.stop_requested.wait(0.25):
                return None
        return None

    def _vocab_record_buttons(self, q: Question) -> List[Any]:
        try:
            slide = q.element.find_element(By.CSS_SELECTOR, '.swiper-slide-active')
            return find_record_buttons(self.driver, slide)
        except Exception:
            return []

    def _wait_vocab_upload(self, q: Question, baseline: Dict,
                           timeout: float = 30.0) -> bool:
        previous = set(baseline.get('recordings') or [])
        for _ in range(max(1, int(timeout * 4))):
            if self.stop_requested.is_set():
                return False
            try:
                info = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
                if info and (info.get('index') != baseline.get('index')
                             or info.get('word') != baseline.get('word')):
                    return False
                uploaded = any(src.startswith(('http://', 'https://')) and src not in previous
                               for src in info.get('recordings') or [])
                if uploaded and not info.get('recording'):
                    return True
            except Exception:
                pass
            if self.stop_requested.wait(0.25):
                return False
        return False

    def _record_vocab_card(self, q: Question, info: Dict) -> bool:
        if self.stop_requested.is_set():
            return False
        word = (info.get('word') or '').strip()
        raw = info.get('audio') or ''
        url = raw.split('#')[0].strip()
        duration = float(info.get('duration') or 0) or 1.2
        data = (self._download_record_audio(url) if url
                else self._speech_audio(word, 0))
        if not data:
            print(f"\t{word}: {'原声下载失败' if url else 'TTS 生成失败'}")
            return False

        payload = base64.b64encode(data).decode()
        state = self.driver.execute_async_script(
            RECORD_MIC_JS, payload, self.RECORD_LEAD_PAD, 0)
        if not state or not state.get('ok'):
            print(f"\t{word}: 音频注入失败 {state}")
            return False
        clip = float(state.get('dur') or 0) or (duration + self.RECORD_LEAD_PAD)

        try:
            baseline = self.driver.execute_script(VOCAB_CARD_JS, q.element) or {}
        except Exception:
            return False
        if baseline.get('index') != info.get('index') or baseline.get('word') != word:
            return False
        buttons = self._vocab_record_buttons(q)
        if not buttons:
            print(f"\t{word}: 没找到录音按钮")
            return False
        button = buttons[0]
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", button)
        except Exception:
            pass
        if self.stop_requested.wait(0.2):
            return False
        if not WebDriverHelper.safe_click(self.driver, button):
            print(f"\t{word}: 点击录音失败")
            return False
        interrupted = self.stop_requested.wait(clip + 0.8)
        buttons = self._vocab_record_buttons(q)
        if not buttons or not WebDriverHelper.safe_click(self.driver, buttons[0]):
            print(f"\t{word}: 点击停止失败")
            return False
        if interrupted or self.stop_requested.is_set():
            return False
        if not self._wait_vocab_upload(q, baseline):
            print(f"\t{word}: 未确认本次录音上传完成")
            return False
        return True

    def _record_row(self, item: Dict):
        row_index = (item or {}).get('row_index')
        if not isinstance(row_index, int) or row_index < 0:
            return None
        try:
            rows = self.driver.find_elements(By.CSS_SELECTOR, '.oral-study-sentence')
            if not rows:
                rows = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    '.question-video-dub-pc .question-common-abs-reply > .item')
        except Exception:
            return None
        if row_index >= len(rows):
            return None
        return rows[row_index]

    def _record_button(self, q: Question, idx: int = 0):
        items = q.record_items or []
        buttons = find_record_buttons(self.driver, q.element)
        if len(buttons) == len(items) and idx < len(buttons):
            return buttons[idx]
        row = self._record_row(items[idx] if idx < len(items) else {})
        if row is not None:
            row_buttons = find_record_buttons(self.driver, row)
            if row_buttons:
                return row_buttons[0]
        return buttons[idx] if idx < len(buttons) else None

    def _row_state(self, q: Question, idx: int) -> Optional[dict]:
        try:
            return self.driver.execute_script(
                RECORD_ROW_STATE_JS, q.element, idx, list(record_button_selectors()))
        except Exception:
            items = q.record_items or []
            row = self._record_row(items[idx]) if idx < len(items) else None
            if row is None:
                return None
            try:
                return self.driver.execute_script(
                    RECORD_ROW_STATE_JS, row, 0, list(record_button_selectors()))
            except Exception:
                return None

    @staticmethod
    def _row_recorded(state: dict, baseline: Optional[dict] = None) -> bool:
        if not state:
            return False
        if state.get('score') is not None:
            return True
        if state.get('recorded'):
            return True
        if baseline:
            return (state.get('blob') or 0) > (baseline.get('blob') or 0) or \
                   (state.get('uploaded') or 0) > (baseline.get('uploaded') or 0)
        return False

    def _row_done(self, q: Question, idx: int) -> bool:
        return self._row_recorded(self._row_state(q, idx) or {})

    def _record_reading_text(self, q: Question) -> str:
        parts = [(item.get('text') or '').strip() for item in (q.record_items or [])]
        joined = ' '.join(part for part in parts if part).strip()
        if joined:
            return joined[:600]
        lines = [(line or '').strip() for line in (q.text or '').splitlines()]
        english = [line for line in lines
                   if re.search(r'[A-Za-z]{3}', line)
                   and not CJK_CHAR_PATTERN.search(line)]
        return ' '.join(english)[:600]

    def _record_speech_texts(self, q: Question, answer: str) -> List[str]:
        count = len(q.record_items or [])
        if count <= 0:
            return []
        text = (answer or '').strip() or self._record_reading_text(q)
        texts = self._split_speech(text, count) if text and count > 1 else [text] * count
        return [(item.get('text') or '').strip() or texts[idx]
                for idx, item in enumerate(q.record_items)]

    @staticmethod
    def _split_speech(text: str, count: int) -> List[str]:
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text or '') if s.strip()]
        if len(sentences) <= 1:
            return [text] + [''] * (count - 1)
        size = max(1, -(-len(sentences) // count))
        chunks = [' '.join(sentences[i:i + size]) for i in range(0, len(sentences), size)]
        chunks = [chunk for chunk in chunks if chunk]
        while len(chunks) < count:
            chunks.append('')
        return chunks[:count]

    def _tts(self) -> TTSEngine:
        if getattr(self, 'tts', None) is None:
            self.tts = TTSEngine()
        return self.tts

    def _speech_audio(self, text: str, idx: int) -> Optional[bytes]:
        text = (text or '').strip()
        if not text:
            print(f"\t第 {idx + 1} 条: 没有可朗读的稿件")
            return None
        engine = self._tts()
        fmt, data = engine.synthesize(text, self.RECORD_MAX_SECONDS)
        if not data:
            print(f"\t第 {idx + 1} 条: TTS 合成失败（{engine.last_error or '未知原因'}）")
            return None
        print(f"\t第 {idx + 1} 条: TTS 生成 {fmt} 音频 {len(data)} 字节")
        return data

    def _record_one_item(self, q: Question, idx: int, texts: Optional[List[str]] = None) -> bool:
        items = q.record_items or []
        if idx >= len(items):
            return False
        item = items[idx]
        url = (item.get('url') or '').strip()
        max_dur = 0.0
        if url:
            data = self._download_record_audio(url)
            if not data:
                print(f"\t第 {idx + 1} 条: 参考音频下载失败")
                return False
        else:
            text = texts[idx] if texts and idx < len(texts) else ''
            data = self._speech_audio(text, idx)
            if not data:
                return False
            max_dur = self.RECORD_MAX_SECONDS

        payload = base64.b64encode(data).decode()
        baseline = self._row_state(q, idx) or {}
        last_state = None
        pads = (self.RECORD_LEAD_PAD, self.RECORD_RETRY_PAD) if url else (self.RECORD_LEAD_PAD,)
        for pad in pads:
            state = self.driver.execute_async_script(RECORD_MIC_JS, payload, pad, max_dur)
            if not state or not state.get("ok"):
                print(f"\t第 {idx + 1} 条: 音频注入失败 {state}")
                return False
            clip = float(state.get("dur") or 0) or (float(item.get('duration') or 0) + pad)

            button = self._record_button(q, idx)
            if button is None:
                print(f"\t第 {idx + 1} 条: 录音按钮已消失")
                return False
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", button)
            time.sleep(0.3)
            if not WebDriverHelper.safe_click(self.driver, button):
                print(f"\t第 {idx + 1} 条: 点击录音失败")
                return False
            time.sleep(clip + 1.0)
            stop_button = self._record_button(q, idx)
            if stop_button is None or not WebDriverHelper.safe_click(
                    self.driver, stop_button):
                print(f"\t第 {idx + 1} 条: 点击停止失败")

            timeout = max(30.0, min(120.0, clip * 1.2))
            last_state = self._wait_record_finish(q, idx, timeout=timeout, baseline=baseline)
            score = (last_state or {}).get('score')
            if score is None:
                if last_state and self._row_recorded(last_state, baseline):
                    print(f"\t第 {idx + 1} 条: 已录制（{clip:.1f}s，未返回分数）")
                    return True
                print(f"\t第 {idx + 1} 条: 未拿到录制结果（可能录音未被识别）")
            else:
                print(f"\t第 {idx + 1} 条: 得分 {score}")
                if int(score) >= self.RECORD_MIN_SCORE:
                    return True
            time.sleep(0.6)
            if last_state is None:
                break
        return bool(last_state) and self._row_recorded(last_state, baseline)

    def _wait_record_score(self, q: Question, idx: int, timeout: float = 20.0) -> Optional[int]:
        state = self._wait_record_finish(q, idx, timeout=timeout)
        return state.get('score') if state else None

    def _wait_record_finish(self, q: Question, idx: int, timeout: float = 25.0,
                            baseline: Optional[dict] = None) -> Optional[dict]:
        deadline = time.time() + timeout
        extra_stops = 0
        while time.time() < deadline:
            state = self._row_state(q, idx)
            if state and (state.get('score') is not None
                          or self._row_recorded(state, baseline)):
                return state
            if state and state.get('stop') and extra_stops < 2:
                extra_stops += 1
                try:
                    button = self._record_button(q, idx)
                    if button is not None:
                        WebDriverHelper.safe_click(self.driver, button)
                        print(f"\t第 {idx + 1} 条: 检测到仍在录音，补一次停止点击")
                except Exception:
                    pass
                time.sleep(1.5)
                continue
            time.sleep(0.5)
        return None

    def _answer_role_play(self, q: Question) -> bool:
        self._dismiss_record_popup()
        roles = [e for e in q.element.find_elements(By.CSS_SELECTOR, '.role-list label.label')
                 if e.is_displayed()]
        if roles and not WebDriverHelper.safe_click(self.driver, roles[0]):
            return False
        try:
            WebDriverWait(self.driver, 10).until(
                lambda d: any(e.is_displayed() for e in q.element.find_elements(
                    By.CSS_SELECTOR, '.role-reply .record-seat')))
            info = self.driver.execute_script("""
                const row = arguments[0].querySelector('.role-reply .list-item-review');
                if (!row) return null;
                let fiber = row[Object.keys(row).find(k => k.startsWith('__reactFiber'))];
                while (fiber && !(fiber.memoizedProps || {}).question) fiber = fiber.return;
                const question = fiber && fiber.memoizedProps.question;
                const selected = arguments[0].querySelector('.role-reply .score:not(.hide)');
                const role = selected && selected.closest('.list-item-review').querySelector('.role');
                if (!question || !role) return null;
                const children = question.children || [];
                return {lines: children.filter(x => x.role && x.role.name === role.textContent.trim())
                    .map(x => ({url: (x.contents || []).find(c => c.type === 1 && c.path)?.path || ''})),
                    seconds: children.reduce((s, x) => s + ((x.config || {}).recordTime || 0) / 1000, 0)};
            """, q.element)
            if not info or not info.get('lines'):
                print('\t无法读取角色台词')
                return False
            clips = []
            for line in info['lines']:
                data = self._download_record_audio((line.get('url') or '').split('#')[0])
                if not data:
                    print('\t角色台词缺少参考音频')
                    return False
                clips.append(base64.b64encode(data).decode())
            prepared = self.driver.execute_async_script("""
                const done = arguments[arguments.length - 1], api = window.__ucRec;
                if (!api) { done(false); return; }
                (async () => {
                    const queue = [];
                    for (const b64 of arguments[0]) {
                        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                        queue.push(await api.ctxOf().decodeAudioData(bytes.buffer));
                    }
                    api.rolePlayStream = api.stream;
                    api.rolePlayQueue = queue;
                    api.stream = function () {
                        api.buf = api.rolePlayQueue.shift() || null;
                        return api.rolePlayStream();
                    };
                    done(true);
                })().catch(() => done(false));
            """, clips)
            if not prepared:
                print('\t角色台词音频初始化失败')
                return False
            started = False
            try:
                seat = q.element.find_element(By.CSS_SELECTOR, '.role-reply .record-seat')
                if not WebDriverHelper.safe_click(self.driver, seat):
                    return False
                started = True
                deadline = time.time() + max(90, min(600, info.get('seconds', 0) * 2 + 60))
                while time.time() < deadline and not self.stop_requested.is_set():
                    state = self.driver.execute_script("""
                        const root = arguments[0], api = window.__ucRec;
                        const rows = [...root.querySelectorAll('.role-play-quiz .list-item-review')];
                        const scores = [...root.querySelectorAll('.role-play-quiz .score:not(.hide)')];
                        const audio = root.querySelector('.role-reply audio');
                        return {left: api.rolePlayQueue.length,
                            scores: scores.map(x => x.textContent.trim()),
                            finished: rows.length && rows[rows.length - 1].classList.contains('active') &&
                                (!audio || audio.paused || audio.ended)};
                    """, q.element)
                    if (state['left'] == 0 and len(state['scores']) == len(clips)
                            and all(state['scores']) and state['finished']):
                        print(f"\t角色扮演录音完成，得分: {', '.join(state['scores'])}")
                        started = False
                        return True
                    self.stop_requested.wait(0.5)
                print('\t角色扮演录音未完成')
                return False
            finally:
                if started:
                    buttons = q.element.find_elements(By.CSS_SELECTOR, '.role-reply .pre-page-btn')
                    if buttons:
                        WebDriverHelper.safe_click(self.driver, buttons[0])
                self.driver.execute_script("""
                    const api = window.__ucRec;
                    if (api && api.rolePlayStream) {
                        api.stream = api.rolePlayStream;
                        delete api.rolePlayStream;
                        delete api.rolePlayQueue;
                        api.buf = null;
                    }
                """)
        except Exception as exc:
            print(f'\t角色扮演处理失败: {str(exc)[:100]}')
            return False

    def _download_record_audio(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        cached = self.RECORD_AUDIO_CACHE.get(url)
        if cached:
            return cached
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                               'AppleWebKit/537.36 (KHTML, like Gecko) '
                               'Chrome/120.0 Safari/537.36'),
                'Referer': 'https://ucontent.unipus.cn/',
            })
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
            if data:
                if len(self.RECORD_AUDIO_CACHE) >= 64:
                    self.RECORD_AUDIO_CACHE.clear()
                self.RECORD_AUDIO_CACHE[url] = data
                return data
        except Exception as exc:
            print(f"\t参考音频下载失败: {str(exc)[:80]}")
        return None

    def _dismiss_record_popup(self):
        try:
            self.driver.execute_script("""
                const selectors = ['.system-info-cloud-ok-button',
                                   '.know-box .iKnow',
                                   '.ant-modal-close',
                                   '.ant-modal-confirm-btns .ant-btn-primary'];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) { btn.click(); }
                }
            """)
            time.sleep(0.4)
        except Exception:
            pass

    def _fill_text(self, q: Question, answer: str) -> bool:
        if not q.inputs:
            return False

        expected_count = len(q.inputs)

        if expected_count == 1:
            ans = self._extract_answer_by_number(answer, q.number)
            if not ans and q.q_type == QuestionType.TEXT:
                direct = self._clean_direct_text_answer(answer)
                if len(direct) > len(ans):
                    ans = direct
            if not ans:
                answers = self._parse_banked_answer(answer, expected_count)
                ans = answers[0] if answers else ""
        else:
            answers = self._parse_banked_answer(answer, expected_count)
            success_count = 0
            for idx, (inp, ans) in enumerate(zip(q.inputs, answers), 1):
                if not ans:
                    print(f"\t题{idx}: (空)")
                    continue

                print(f"\t题{idx}: {ans[:60]}...")
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'});",
                    inp
                )
                time.sleep(0.05)
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t题{idx}: 写入后校验失败")

            return len(answers) == expected_count and success_count == expected_count

        if ans and q.q_type == QuestionType.MY_VOICE_TEXT:
            ans = self._limit_text_answer(ans, 500)

        print(f"\t题{q.number}: {ans[:60]}..." if ans else f"\t题{q.number}: (空)")

        if ans:
            inp = q.inputs[0]
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'});",
                inp
            )
            time.sleep(0.05)
            if not self._fill_text_input_verified(inp, ans):
                return False
            if q.q_type == QuestionType.MY_VOICE_TEXT:
                if not self._upload_my_voice_answer_file(q, ans):
                    return False
            return True

        return False

    def _fill_text_input_verified(self, inp, ans: str) -> bool:
        WebDriverHelper.simulate_typing(self.driver, inp, ans)
        time.sleep(0.15)
        if self._input_value_matches(inp, ans):
            return True

        if len(ans) > WebDriverHelper.LONG_TEXT_JS_THRESHOLD:
            try:
                WebDriverHelper.ensure_focused(self.driver, inp, retries=2)
                WebDriverHelper.clear_input_verified(self.driver, inp)
                inp.send_keys(ans)
                self.driver.execute_script("""
                    arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
                    arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                    arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));
                """, inp)
                time.sleep(0.2)
                if self._input_value_matches(inp, ans):
                    return True
            except Exception:
                pass
            print("\t长文本输入未匹配，重试 JS 同步值")
            try:
                WebDriverHelper.set_value_via_js(self.driver, inp, ans, dispatch_blur=True)
                time.sleep(0.25)
                if self._input_value_matches(inp, ans):
                    return True
            except Exception:
                pass
        else:
            print("\t输入框为受控组件，使用 JS 同步值")
            try:
                WebDriverHelper.set_value_via_js(self.driver, inp, ans, dispatch_blur=True)
                time.sleep(0.25)
                if self._input_value_matches(inp, ans):
                    return True
            except Exception:
                pass

        try:
            current = inp.get_attribute('value') or ''
        except Exception:
            current = ''
        try:
            maxlength = inp.get_attribute('maxlength')
        except Exception:
            maxlength = None
        focused = WebDriverHelper._is_element_focused(self.driver, inp)
        print(
            f"\t文本写入校验失败: 当前长度={len(current)}, 期望长度={len(ans)}, "
            f"maxlength={maxlength or '无'}, 已聚焦={focused}"
        )
        return False

    @staticmethod
    def _input_value_matches(inp, ans: str) -> bool:
        try:
            current = (inp.get_attribute('value') or '').replace('\r\n', '\n')
            expected = (ans or '').replace('\r\n', '\n')
            return current.strip() == expected.strip()
        except:
            return False

    def _upload_my_voice_answer_file(self, q: Question, answer: str) -> bool:
        try:
            if self._my_voice_has_uploaded_file(q):
                print("\tMy voice 已存在上传文件，跳过自动上传")
                return True

            file_input = q.element.find_element(By.CSS_SELECTOR, '.question-multi-file-upload input[type="file"]')
            file_path = self._create_my_voice_pdf(answer)
            self.driver.execute_script("""
                arguments[0].style.display = 'block';
                arguments[0].style.visibility = 'visible';
                arguments[0].style.opacity = 1;
                arguments[0].style.width = '1px';
                arguments[0].style.height = '1px';
            """, file_input)
            file_input.send_keys(file_path)
            print(f"\tMy voice 已上传附件: {os.path.basename(file_path)}")
            return self._wait_my_voice_upload_ready(q)
        except Exception as e:
            print(f"\tMy voice 附件上传失败: {str(e)[:80]}")
            return False

    def _my_voice_has_uploaded_file(self, q: Question) -> bool:
        try:
            media_list = q.element.find_element(By.CSS_SELECTOR, '.question-multi-file-upload .media-list')
            return bool(media_list.find_elements(By.XPATH, './*')) or bool(media_list.text.strip())
        except:
            return False

    def _my_voice_upload_status(self, q: Question) -> Dict[str, Any]:
        try:
            res = self.driver.execute_script("""
                const container = arguments[0];
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const vis = el => !!el && !!el.getClientRects().length;
                const root = (container && container.querySelector('.question-multi-file-upload')) || container;
                const mediaList = root && root.querySelector('.media-list');
                const items = mediaList ? [...mediaList.children].filter(el => vis(el) || clean(el.innerText)) : [];
                const mediaText = clean(mediaList && mediaList.innerText);
                const hasItem = items.length > 0 || !!mediaText;
                const doneItems = mediaList ? mediaList.querySelectorAll('.media-status-done').length : 0;
                const uploadingEls = mediaList ? [...mediaList.querySelectorAll(
                    '.media-status-uploading, .ant-progress, .ant-spin-spinning, [class*="progress"]'
                )].filter(vis) : [];
                const uploading = uploadingEls.length > 0 || (
                    doneItems === 0 && /上传中|uploading|\\b\\d{1,3}%/i.test(mediaText)
                );
                const toasts = [...document.querySelectorAll('.ant-message-notice')].filter(vis).map(el => clean(el.innerText)).join(' ');
                const modals = [...document.querySelectorAll('[role="dialog"], .ant-modal')].filter(vis).map(el => clean(el.innerText)).join(' ');
                const feedback = (toasts + ' ' + modals).trim();
                return {
                    has_item: hasItem,
                    uploading: uploading,
                    toast_success: /上传成功/.test(toasts) || (doneItems > 0 && !uploading),
                    toast_fail: /上传失败/.test(feedback),
                    fallback: false
                };
            """, q.element)
            if isinstance(res, dict):
                return res
        except Exception:
            pass
        return {
            'has_item': self._my_voice_has_uploaded_file(q),
            'uploading': False,
            'toast_success': False,
            'toast_fail': False,
            'fallback': True,
        }

    def _wait_my_voice_upload_ready(self, q: Question, timeout: int = 20) -> bool:
        start = time.monotonic()
        ready_since = None
        last_status: Dict[str, Any] = {}
        while time.monotonic() - start < timeout:
            status = self._my_voice_upload_status(q)
            last_status = status
            if status.get('toast_fail'):
                print("\tMy voice 附件上传失败")
                return False
            if status.get('has_item') and not status.get('uploading'):
                if status.get('toast_success') or status.get('fallback'):
                    time.sleep(0.4)
                    return True
                now = time.monotonic()
                if ready_since is None:
                    ready_since = now
                elif now - ready_since >= 1.5:
                    return True
            else:
                ready_since = None
            time.sleep(0.4)
        print(f"\tMy voice 附件上传超时（{timeout}s，has_item={last_status.get('has_item')}，"
              f"uploading={last_status.get('uploading')}），停止提交以避免漏传")
        return False

    def _create_my_voice_pdf(self, answer: str) -> str:
        filename = f"unipus_my_voice_{int(time.time())}.pdf"
        path = os.path.join(tempfile.gettempdir(), filename)
        lines = self._wrap_pdf_text(answer, 82)[:24]
        if not lines:
            lines = ["My voice answer"]

        text_ops = ["BT", "/F1 12 Tf", "72 760 Td", "16 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            text_ops.append(f"({escaped}) Tj")
            text_ops.append("T*")
        text_ops.append("ET")
        stream = "\n".join(text_ops).encode("latin-1", errors="replace")

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        ]

        content = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for idx, obj in enumerate(objects, 1):
            offsets.append(len(content))
            content.extend(f"{idx} 0 obj\n".encode("ascii"))
            content.extend(obj)
            content.extend(b"\nendobj\n")

        xref_offset = len(content)
        content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        content.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        content.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
        )

        with open(path, "wb") as f:
            f.write(content)
        return path

    @staticmethod
    def _wrap_pdf_text(text: str, width: int) -> List[str]:
        words = re.sub(r'\s+', ' ', text).strip().split(' ')
        lines = []
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _clean_direct_text_answer(answer: str) -> str:
        answer = re.sub(r'^(My voice文字作答|简答题|答案|Answer)[：:]\s*', '', answer.strip(), flags=re.I)
        answer = re.sub(r'^\d+\s*[.、)\]]\s*', '', answer).strip()
        return answer

    @staticmethod
    def _limit_text_answer(answer: str, max_chars: int) -> str:
        answer = re.sub(r'\s+', ' ', answer).strip()
        if len(answer) <= max_chars:
            return answer

        clipped = answer[:max_chars].rstrip()
        sentence_end = max(clipped.rfind('.'), clipped.rfind('!'), clipped.rfind('?'))
        if sentence_end >= max_chars * 0.6:
            return clipped[:sentence_end + 1]
        return clipped

    def _fill_matching(self, q: Question, answer: str) -> bool:
        total = len(q.inputs) or len(q.banked_blanks)
        letters_pool = [opt.letter.upper() for opt in q.options] or list("ABCDEF")
        letters = self._parse_matching_letters(answer, total, letters_pool)
        if not letters:
            print(f"\t匹配题没有解析出字母: {answer[:80]}")
            return False

        print(f"\t匹配答案: {letters}")
        success = 0
        for idx, inp in enumerate(q.inputs):
            letter = letters[idx] if idx < len(letters) else ""
            if not letter:
                continue
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", inp)
                time.sleep(0.1)
            except Exception:
                pass
            if self._fill_text_input_verified(inp, letter):
                success += 1
            else:
                print(f"\t空{idx + 1}: 写入 {letter} 失败")
        return success == total or (success > 0 and success == len(q.inputs))

    def _fill_paragraph_match(self, q: Question, answer: str) -> bool:
        total = len(q.inputs) or len(q.banked_blanks)
        pool = [str(x).upper() for x in (q.banked_options or [])] or list("ABCDEFGHIJKLMNO")
        letters = self._parse_paragraph_letters(answer, total, pool)
        if not any(letters):
            print(f"\t段落匹配没有解析出字母: {answer[:80]}")
            return False
        print(f"\t段落匹配答案: {letters}")
        success = 0
        for idx, inp in enumerate(q.inputs):
            letter = letters[idx] if idx < len(letters) else ""
            if not letter:
                continue
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", inp)
                time.sleep(0.1)
            except Exception:
                pass
            if self._fill_text_input_verified(inp, letter):
                success += 1
            else:
                print(f"\t空{idx + 1}: 写入 {letter} 失败")
        return success == total

    @staticmethod
    def _parse_paragraph_letters(answer: str, total: int, pool: List[str]) -> List[str]:
        text = (answer or "").upper()
        valid = set(pool)
        found = {}
        for num, letter in re.findall(
                r'(?:^|[\s,;，；])(\d{1,2})\s*[.、:：)）=\-]?\s*([A-Z])(?![A-Z])', text):
            idx = int(num) - 1
            if 0 <= idx < total and letter in valid:
                found.setdefault(idx, letter)
        if found:
            return [found.get(i, "") for i in range(total)]
        seq = [c for c in re.findall(r'(?<![A-Z])([A-Z])(?![A-Z])', text)
               if c in valid]
        return (seq + [""] * total)[:total]

    @staticmethod
    def _parse_matching_letters(answer: str, total: int, pool: List[str]) -> List[str]:
        text = (answer or "").upper()
        valid = set(pool) if pool else set("ABCDEFGH")

        numbered = {}
        for num, letter in re.findall(r'(?:^|[\s,;，；])(\d{1,2})\s*[.、:：)）=\-]?\s*([A-H])(?![A-Z])', text):
            idx = int(num) - 1
            if 0 <= idx < total and letter in valid:
                numbered.setdefault(idx, letter)
        if numbered:
            return [numbered.get(i, "") for i in range(total)]

        seq = [ch for ch in re.findall(r'[A-H]', text) if ch in valid]
        if len(seq) >= total:
            seq = seq[:total]
        return seq or []

    def _fill_unknown(self, q: Question, answer: str) -> bool:
        return False

    @staticmethod
    def _button_text(element) -> str:
        text = element.text or element.get_attribute('aria-label') or ''
        return re.sub(r'\s+', '', text).lower()

    @classmethod
    def _is_navigation_button(cls, element) -> bool:
        text = cls._button_text(element)
        return any(k in text for k in ['上一题', '下一题', '上一页', '下一页', 'prev', 'previous', 'next', 'nextquestion'])

    @classmethod
    def _is_submit_button(cls, element) -> bool:
        text = cls._button_text(element)
        if cls._is_navigation_button(element):
            return False
        if any(k in text for k in ['已提交', '已完成', '提交成功', '保存成功', 'submitted']):
            return False
        if any(k in text for k in ['提交', '确认提交', '完成', 'submit', 'done', 'finish']):
            return True
        tag_name = (element.tag_name or '').lower()
        element_type = (element.get_attribute('type') or '').lower()
        return tag_name == 'button' and element_type == 'submit'

    def submit(self) -> bool:
        priority_selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button[type="submit"]',
            'button.submit-btn',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
        ]
        for selector in priority_selectors:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled() and self._is_submit_button(btn):
                        return WebDriverHelper.safe_click(self.driver, btn)
            except:
                continue

        for selector in Selectors.SUBMIT_BUTTON:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled() and self._is_submit_button(btn):
                        return WebDriverHelper.safe_click(self.driver, btn)
            except:
                continue

        try:
            fallback = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, '');
                const cands = [...document.querySelectorAll('button, a, div, span')];
                for (const el of cands) {
                  if (!el.getClientRects().length) continue;
                  const text = clean(el.innerText);
                  if (text !== '提交' && text !== '交卷' && text !== '确认提交') continue;
                  if (el.querySelector('button, a, div')) continue;
                  return el;
                }
                return null;
            """)
            if fallback is not None:
                print("\t通过文本兜底点击提交按钮")
                return WebDriverHelper.safe_click(self.driver, fallback)
        except Exception:
            pass

        return False

    @classmethod
    def _parse_choice_letters(cls, answer: str, valid_letters: List[str]) -> List[str]:
        answer = re.sub(r'^\s*(?:单选题|多选题)\s*[：:]\s*', '', answer)
        answer = cls._clean_extracted_answer(answer).rstrip('.。').upper()
        if not re.fullmatch(r'[A-Z][A-Z\s,，、/;&+]*', answer):
            return []
        letters = list(dict.fromkeys(re.findall(r'[A-Z]', answer)))
        valid = {letter.upper() for letter in valid_letters}
        return letters if set(letters) <= valid else []

    @staticmethod
    def _letters_from_option_text(answer: str, question: Question) -> List[str]:
        text = (answer or '').strip()
        if not text:
            return []
        lowered = text.lower()
        hits = []
        for opt in question.options:
            body = (opt.text or '').strip().lower()
            if len(body) < 6:
                continue
            body = re.sub(r'\s+', ' ', body)
            if body and body[:40] in re.sub(r'\s+', ' ', lowered):
                hits.append(opt.letter.upper())
        return list(dict.fromkeys(hits))

    @staticmethod
    def _split_numbered_answers(answer: str, expected_count: int) -> List[str]:
        text = re.sub(
            rf'^{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I
        )
        token_re = re.compile(
            r'(?<![\d.])(?:(?:空|Blank)\s*)?(\d{1,2})\s*(?:\.(?!\d)|\.(?=\d+\.\d)|[、:：\)\]])', re.I
        )
        tokens = [(int(m.group(1)), m.start(), m.end()) for m in token_re.finditer(text)]
        if not tokens:
            return []

        chosen = {}
        cursor = 0
        for number in range(1, expected_count + 1):
            candidates = [t for t in tokens if t[0] == number and t[1] >= cursor]
            if not candidates:
                return []
            chosen[number] = candidates[0]
            cursor = candidates[0][2]

        result = []
        for number in range(1, expected_count + 1):
            start = chosen[number][2]
            end = chosen[number + 1][1] if number < expected_count else len(text)
            value = re.sub(r'[\s,，;；]+$', '', text[start:end].strip())
            if not value:
                return []
            result.append(value)
        return result

    @staticmethod
    def _parse_banked_answer(answer: str, expected_count: int) -> List[str]:
        result = [''] * expected_count
        answer = AnswerExecutor._normalize_answer_labels(answer)
        answer = re.sub(rf'^{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I)
        numbered = AnswerExecutor._split_numbered_answers(answer, expected_count)
        if numbered:
            return numbered
        print(f"    [调试] 清理后答案前200字: {answer[:200]}...")

        matched_any = False
        label_prefix = rf'(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?'
        next_prefix = rf'(?<![$\w])(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{AnswerExecutor.NUMBER_PREFIX_PATTERN})'

        for i in range(1, expected_count + 1):
            number_prefix = AnswerExecutor._number_prefix_pattern(i)
            pattern = rf'{label_prefix}{number_prefix}\s*(.*?)(?=\s*{next_prefix}\s*|$)'
            match = re.search(pattern, answer, re.DOTALL)

            if match:
                clean_ans = AnswerExecutor._clean_extracted_answer(match.group(1)).replace('\n', ' ')
                result[i - 1] = clean_ans
                matched_any = True
                print(f"    [调试] 成功提取空 {i}: '{clean_ans}'")
            else:
                print(f"    [调试] 题号 {i} 匹配失败或为空")

        if matched_any:
            return result

        print(f"    [调试] 题号匹配完全失效，启动降级切分模式")
        lines = [line.strip() for line in answer.split('\n') if line.strip()]
        content_lines = []

        for line in lines:
            clean = re.sub(
                rf'^(?:{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*)?(?:{AnswerExecutor.NUMBER_PREFIX_PATTERN})\s*',
                '',
                line,
                flags=re.I
            ).strip()
            if clean and not re.match(r'^\d+$', clean):
                content_lines.append(AnswerExecutor._clean_extracted_answer(clean))

        for i, content in enumerate(content_lines[:expected_count]):
            result[i] = content

        return result

    def _fill_dropdown_select(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.banked_blanks))
        if (not q.banked_blanks or len(answers) != len(q.banked_blanks)
                or not all(answer.strip() for answer in answers)):
            return False
        print(f"      解析答案: {answers}")
        print(f"      填空数量: {len(q.banked_blanks)}")

        success_count = 0

        for i, (blank, ans) in enumerate(zip(q.banked_blanks, answers)):
            if not ans:
                continue

            try:
                print(f"      空{i + 1}: '{ans}'")
                select_wrapper = blank['element']

                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    select_wrapper
                )
                time.sleep(0.5)

                try:
                    trigger = select_wrapper.find_element(
                        By.CSS_SELECTOR,
                        '.ant-dropdown-trigger, .ant-select-selector, [role="combobox"]')
                except NoSuchElementException:
                    trigger = select_wrapper

                actions = ActionChains(self.driver)
                actions.move_to_element(trigger).click().perform()
                print(f"         点击触发器打开下拉")
                time.sleep(0.8)

                dropdown_menu = None
                for attempt in range(5):
                    try:
                        dropdown_menu = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((
                                By.CSS_SELECTOR,
                                '.ant-dropdown:not(.ant-dropdown-hidden) .ant-dropdown-menu, '
                                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item, '
                                '[role="listbox"], [class*="dropdown"] li'
                            ))
                        )
                        if dropdown_menu.is_displayed():
                            break
                    except:
                        pass
                    time.sleep(0.3)

                if not dropdown_menu:
                    print(f"         下拉菜单未出现，尝试备选方案")
                    if self._force_select_by_js(select_wrapper, ans):
                        success_count += 1
                    continue

                option_selectors = [
                    f'.ant-dropdown-menu-item:contains("{ans}")',
                    f'.ant-select-item-option:contains("{ans}")',
                    f'.ant-dropdown-menu-item[title="{ans}"]',
                    '//li[contains(@class,"ant-dropdown-menu-item") and contains(text(),"{}")]'.format(ans),
                    '//div[contains(@class,"ant-select-item-option-content") and contains(text(),"{}")]'.format(ans)
                ]

                option_clicked = False

                for selector in option_selectors[:3]:
                    try:
                        options = self.driver.find_elements(By.CSS_SELECTOR,
                                                            selector.replace(f':contains("{ans}")', ''))
                        for opt in options:
                            if ans.lower() in opt.text.lower() and opt.is_displayed():
                                ActionChains(self.driver).move_to_element(opt).click().perform()
                                print(f"         点击选项: {opt.text[:20]}")
                                option_clicked = True
                                break
                        if option_clicked:
                            break
                    except Exception as e:
                        continue

                if not option_clicked:
                    try:
                        for opt in self.driver.find_elements(
                                By.CSS_SELECTOR,
                                '[role="option"], .ant-select-item-option, '
                                '.ant-dropdown-menu-item, li[class*="option"]'):
                            if not opt.is_displayed():
                                continue
                            label = (opt.text or '').strip()
                            if label and (label.lower() == ans.lower()
                                          or ans.lower() in label.lower()):
                                ActionChains(self.driver).move_to_element(opt).click().perform()
                                print(f"         通用候选项点击: {label[:20]}")
                                option_clicked = True
                                break
                    except Exception:
                        pass

                if not option_clicked:
                    for xpath in option_selectors[3:]:
                        try:
                            option = self.driver.find_element(By.XPATH, xpath)
                            if option.is_displayed():
                                ActionChains(self.driver).move_to_element(option).click().perform()
                                print(f"         XPath点击选项")
                                option_clicked = True
                                break
                        except:
                            continue

                if option_clicked:
                    time.sleep(0.5)

                    try:
                        answer_text_elem = select_wrapper.find_element(
                            By.CSS_SELECTOR, '.user-answer-text, .ant-select-selection-item')
                        displayed_text = answer_text_elem.text.strip()

                        if ans.lower() in displayed_text.lower():
                            print(f"         验证成功，显示文本: {displayed_text[:20]}")
                            success_count += 1
                        else:
                            print(f"         视觉反馈异常，文本: {displayed_text[:20]}")
                            self._sync_react_state(select_wrapper, ans)

                    except Exception as e:
                        print(f"         验证失败: {str(e)[:50]}")

                else:
                    print(f"         未找到选项 '{ans}'")
                    if self._force_select_by_js(select_wrapper, ans):
                        success_count += 1

            except Exception as e:
                print(f"        处理空{i + 1}失败: {str(e)[:50]}")
                logger.error(f"详细错误: {str(e)}", exc_info=True)
                continue

        return success_count == len(q.banked_blanks)

    def _fill_listening_fill_in(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.inputs))
        if not q.inputs or len(q.banked_blanks) != len(q.inputs) or len(answers) != len(q.inputs):
            return False

        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, (blank_info, ans) in enumerate(zip(q.banked_blanks, answers)):
            if ans and blank_info['input']:
                original_ans = ans
                if getattr(self, '_exact_official_mode', False):
                    ans = re.sub(r'\s+', ' ', ans).strip()
                    hint = blank_info.get('hint') or ''
                    if hint:
                        ans = self._strip_given_prefix(hint, ans)
                else:
                    ans = self._normalize_listening_blank_answer(blank_info, ans)
                if ans != original_ans:
                    print(f"\t空{i + 1}: {original_ans} -> {ans}")
                else:
                    print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(blank_info['input'], ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")

        return success_count == len(q.inputs)

    def _normalize_listening_blank_answer(self, blank_info: Dict[str, Any], answer: str) -> str:
        ans = self._clean_extracted_answer(answer)
        ans = re.sub(r'\s+', ' ', ans).strip()
        left = (blank_info.get('left_context') or '').lower()
        right = (blank_info.get('right_context') or '').lower()

        if re.search(r"\b(to|for|at|in|on)\s*$", left):
            prep = re.search(r"\b(to|for|at|in|on)\s*$", left).group(1)
            ans = re.sub(rf"^{prep}\s+", "", ans, flags=re.I).strip()

        if right.startswith("at ") or right.startswith("at the "):
            ans = re.sub(r"\s+at\s+.+$", "", ans, flags=re.I).strip()
        if right.startswith("in ") or right.startswith("in the "):
            ans = re.sub(r"\s+in\s+.+$", "", ans, flags=re.I).strip()

        if len(ans.split()) > 8:
            for connector in [' at ', ' in ', ' to ', ' and ']:
                if connector in ans.lower():
                    ans = re.split(connector, ans, flags=re.I)[0].strip()
                    break

        if self._listening_left_expects_ing(left):
            ans = self._to_present_participle_phrase(ans)

        return ans

    @staticmethod
    def _listening_left_expects_ing(left_context: str) -> bool:
        left = left_context.strip().lower()
        return bool(re.search(r"\b(?:i'm|we're|you're|they're|he's|she's|is|are|am)\s*$", left))

    @staticmethod
    def _to_present_participle_phrase(answer: str) -> str:
        replacements = {
            'go': 'going',
            'have': 'having',
            'get': 'getting',
            'meet': 'meeting',
            'visit': 'visiting',
            'watch': 'watching',
            'see': 'seeing',
            'start': 'starting',
        }
        match = re.match(r"^([A-Za-z]+)\b(.*)$", answer.strip())
        if not match:
            return answer

        verb = match.group(1)
        rest = match.group(2)
        lower = verb.lower()
        if lower.endswith('ing'):
            return answer
        if lower in replacements:
            replacement = replacements[lower]
            if verb[:1].isupper():
                replacement = replacement[:1].upper() + replacement[1:]
            return replacement + rest
        return answer

    def _force_select_by_js(self, select_wrapper, value: str) -> bool:
        try:
            js = """
            var wrapper = arguments[0];
            var value = arguments[1];

            var trigger = wrapper.querySelector('.ant-dropdown-trigger, .ant-select-selector, [role="combobox"]') || wrapper;
            if (!trigger) return false;
            var events = ['mousedown', 'focus', 'click', 'input', 'change', 'blur'];

            events.forEach(function(eventType) {
                var event = new Event(eventType, { bubbles: true, cancelable: true });
                trigger.dispatchEvent(event);
            });

            var textElem = wrapper.querySelector('.user-answer-text');
            if (textElem) {
                textElem.innerHTML = '<p>' + value + '</p>';
                textElem.textContent = value;
            }

            trigger.classList.remove('empty');
            trigger.classList.add('selected');

            var reactKey = Object.keys(trigger).find(k => k.startsWith('__react'));
            if (reactKey) {
                var fiber = trigger[reactKey];
                while (fiber) {
                   if (fiber.memoizedProps && fiber.memoizedProps.onChange) {
                    fiber.memoizedProps.onChange(value);
                    return 'react_onChange_triggered';
                }
                fiber = fiber.return || fiber._debugOwner;
            }
        }

        return false;
        """

            result = self.driver.execute_script(js, select_wrapper, value)
            print(f"        JS强制设置结果: {result}")

            time.sleep(0.3)
            text_elem = select_wrapper.find_element(By.CSS_SELECTOR, '.user-answer-text')
            return result == 'react_onChange_triggered' and value.lower() in text_elem.text.lower()

        except Exception as e:
            print(f"        JS强制设置失败: {str(e)[:50]}")
            return False

    def _sync_react_state(self, select_wrapper, value: str) -> bool:
        try:
            js = """
            var wrapper = arguments[0];
            var value = arguments[1];

            wrapper.setAttribute('data-selected-value', value);

            var hiddenInput = wrapper.querySelector('input[type="hidden"]');
            if (hiddenInput) {
                hiddenInput.value = value;
                hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
            }

            if (!window.__formData) window.__formData = {};
            var scoopIndex = wrapper.closest('[data-scoop-index]')?.getAttribute('data-scoop-index');
            if (scoopIndex) {
                window.__formData[scoopIndex] = value;
            }

            return true;
            """
            return self.driver.execute_script(js, select_wrapper, value)
        except:
            return False




class PublishPlayer:

    FRAME_CSS = (
        'iframe#ipublish-pc-book-easy-iframe',
        'iframe[id*="ipublish"][id*="iframe"]',
        'iframe[src*="ipub.unipus.cn"]',
    )
    SHELL_HOST_HINT = 'ucontent.unipus.cn'
    SHELL_PATH_HINT = 'publishpc_default'

    GROUP_CSS = '[data-ipublish-question-group-type]'
    QUESTION_CSS = '[data-question-id]'
    SUBMIT_CSS = '[data-ipublish-event-type="question-submit"]'

    SUBMITTED_KEYWORDS = ('重新作答', '已提交', '查看答案', '已批改')

    @staticmethod
    def reset(driver) -> None:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    @classmethod
    def find_frame(cls, driver):
        cls.reset(driver)
        for css in cls.FRAME_CSS:
            try:
                frames = driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                continue
            if frames:
                return frames[0]
        return None

    @classmethod
    def has_content_frame(cls, driver) -> bool:
        return cls.find_frame(driver) is not None

    @classmethod
    def is_publish_page(cls, driver) -> bool:
        try:
            url = driver.current_url or ''
        except Exception:
            url = ''
        if cls.SHELL_HOST_HINT in url and cls.SHELL_PATH_HINT in url:
            return True

        frame = cls.find_frame(driver)
        if frame is None:
            return False
        try:
            driver.switch_to.frame(frame)
            try:
                return bool(driver.find_elements(By.CSS_SELECTOR, cls.GROUP_CSS)
                            or driver.find_elements(By.CSS_SELECTOR, cls.SUBMIT_CSS))
            finally:
                cls.reset(driver)
        except Exception:
            cls.reset(driver)
            return False

    @staticmethod
    def button_text(element) -> str:
        text = ''
        try:
            text = element.text or ''
        except Exception:
            text = ''
        if not text:
            try:
                text = element.get_attribute('innerText') or ''
            except Exception:
                text = ''
        if not text:
            try:
                text = element.get_attribute('textContent') or ''
            except Exception:
                text = ''
        return re.sub(r'\s+', '', text)


class PublishFrameContext:

    def __init__(self, driver):
        self.driver = driver
        self.entered = False

    def __enter__(self):
        self.entered = self.enter()
        return self

    def __exit__(self, *_exc):
        PublishPlayer.reset(self.driver)
        return False

    def enter(self) -> bool:
        for _ in range(3):
            frame = PublishPlayer.find_frame(self.driver)
            if frame is None:
                return False
            try:
                self.driver.switch_to.frame(frame)
                self.entered = True
                return True
            except Exception:
                time.sleep(0.5)
        return False


@dataclass(frozen=True)
class PublishTaskScope:

    document_url: str
    start_anchor: str = ''
    end_anchor: str = ''
    title: str = ''
    block_only: bool = False

    @classmethod
    def capture_visible(cls, driver):
        state = driver.execute_script("""
            const selector = '[data-lexical-node-type="question-block"]';
            const blocks = [...document.querySelectorAll(selector + ', [data-ipublish-question-group-type]')]
                .filter(el => el.matches(selector) || !el.closest(selector));
            const candidates = blocks.map(el => {
                const r = el.getBoundingClientRect();
                let top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
                let left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
                for (let p = el.parentElement; p; p = p.parentElement) {
                    const style = getComputedStyle(p), box = p.getBoundingClientRect();
                    if (/auto|scroll|hidden|clip/.test(style.overflowY)) {
                        top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom);
                    }
                    if (/auto|scroll|hidden|clip/.test(style.overflowX)) {
                        left = Math.max(left, box.left); right = Math.min(right, box.right);
                    }
                }
                const anchor = el.dataset.lexicalNodeAnchor || el.id;
                const visible = el.getClientRects().length && right > left && bottom > top;
                return {el, anchor, height: visible ? bottom - top : 0};
            }).filter(item => item.anchor && item.height > 0)
              .sort((a, b) => b.height - a.height);
            if (!candidates.length) return {error: '当前视口没有题目，请先滚动到需要处理的题块'};
            const {el, anchor} = candidates[0];
            const root = el.closest('.editor-content') || document.body;
            const headings = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(head =>
                !head.closest('[data-ipublish-question-group-type]') &&
                (head.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
            const heading = headings[headings.length - 1];
            return {document_url: location.href.split('#')[0], start_anchor: anchor,
                    title: heading ? (heading.innerText || heading.textContent || '').trim() : '当前题块',
                    block_only: true};
        """)
        if not state or state.get('error'):
            raise ValueError((state or {}).get('error', '无法定位当前可见题块'))
        return cls(**state)

    @classmethod
    def capture(cls, driver, anchor: str = ''):
        state = driver.execute_script("""
            const anchor = arguments[0] || decodeURIComponent(location.hash.slice(1));
            const url = location.href.split('#')[0];
            const find = id => document.getElementById(id) ||
                [...document.querySelectorAll('[data-lexical-node-anchor]')]
                    .find(el => el.dataset.lexicalNodeAnchor === id);
            if (!anchor) {
                if (document.querySelector('[data-lexical-node-type="heading"], [data-marker-id*="#"]'))
                    return {error: '没有当前任务锚点'};
                return {document_url: url};
            }
            const start = find(anchor);
            if (!start) return {error: '找不到当前任务正文锚点: ' + anchor};
            const title = (start.innerText || start.textContent || '').trim();
            if (!/^H[1-6]$/.test(start.tagName)) {
                if (start.matches('[data-lexical-node-type="question-block"], ' +
                                  '[data-ipublish-question-group-type]'))
                    return {document_url: url, start_anchor: anchor, title, block_only: true};
                return {error: '当前任务锚点不是可识别的章节标题或题块'};
            }
            const root = start.closest('.editor-content') || document.body;
            const rank = Number(start.tagName.slice(1));
            const end = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].find(el =>
                !el.closest('[data-ipublish-question-group-type]') &&
                Number(el.tagName.slice(1)) <= rank &&
                (start.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
            const endAnchor = end && (end.dataset.lexicalNodeAnchor || end.id);
            if (end && !endAnchor) return {error: '下一任务标题缺少定位锚点'};
            return {document_url: url, start_anchor: anchor, end_anchor: endAnchor || '', title};
        """, anchor)
        if not state or state.get('error'):
            raise ValueError((state or {}).get('error', '无法读取当前任务范围'))
        return cls(**state)

    def select(self, driver, groups: List[Dict]) -> List[Dict]:
        result = driver.execute_script("""
            const [url, startId, endId, blockOnly, groups] = arguments;
            const find = id => document.getElementById(id) ||
                [...document.querySelectorAll('[data-lexical-node-anchor]')]
                    .find(el => el.dataset.lexicalNodeAnchor === id);
            if (location.href.split('#')[0] !== url) return null;
            const start = startId ? find(startId) : null, end = endId ? find(endId) : null;
            if ((startId && !start) || (endId && !end)) return null;
            if (end && !(start.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING))
                return null;
            return groups.map(group => {
                if (!group.isConnected) return false;
                if (!start) return true;
                if (blockOnly) return start === group || start.contains(group);
                return (start === group || start.contains(group) ||
                    !!(start.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING)) &&
                    (!end || !!(group.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING));
            });
        """, self.document_url, self.start_anchor, self.end_anchor, self.block_only,
             [state['element'] for state in groups])
        if result is None:
            raise ValueError('当前任务文档或边界已变化，停止作答以免越界')
        return [state for state, included in zip(groups, result) if included]


class PublishQuestionParser:

    GROUP_KINDS = {
        'single_choice_group': 'choice',
        'multiple_choice_group': 'choice',
        'multi_choice_group': 'choice',
        'evaluation_group': 'choice',
        'true_false_group': 'true_false',
        'fill_blanks_group': 'fill',
        'fill_blanks_choice_group': 'fill',
        'fill_blanks_dropdown_group': 'dropdown',
        'short_answer_group': 'text',
        'translation_group': 'text',
        'writing_group': 'text',
        'multi_media_upload_group': 'upload',
        'ai_oral_practice_group': 'ai_oral',
        'rich_text_read_group': 'skip',
        'discussion_group': 'skip',
        'text_match_group': 'link_match',
        'oral_personal_state_group': 'record',
        'oral_sentence_scoop_group': 'record',
        'vocabulary_group': 'record',
        'sequence_group': 'sorting',
    }

    QUESTION_TYPE_KINDS = {
        'single_choice': 'choice',
        'multiple_choice': 'choice',
        'evaluation': 'choice',
        'true_false': 'true_false',
        'fill_blanks': 'fill',
        'fill_blanks_choice': 'fill',
        'fill_blanks_dropdown': 'dropdown',
        'short_answer': 'text',
        'translation': 'text',
        'writing': 'text',
        'oral_personal_state': 'record',
        'oral_sentence_scoop': 'record',
    }
    CHOICE_HEADER_SKIP = 'strongListView'
    EXPLANATION_PLACEHOLDERS = ('请输入',)
    TITLE_NOISE_WORDS = ('自动判题', 'AI判题', 'AI 判题', '计分', '已提交', '重新作答', '提交')
    TRUE_FALSE_LABELS = {'T': ('T', 'TRUE', '正确'), 'F': ('F', 'FALSE', '错误')}

    def __init__(self, driver):
        self.driver = driver
        self.unsupported: List[str] = []
        self.passages: List[str] = []

    @classmethod
    def classify_group(cls, group_type: str) -> str:
        return cls.GROUP_KINDS.get((group_type or '').strip(), 'unknown')

    def collect_groups(self) -> List[Dict[str, Any]]:
        groups = []
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, PublishPlayer.GROUP_CSS)
        except Exception:
            return groups
        for element in elements:
            group_type = (element.get_attribute('data-ipublish-question-group-type') or '').strip()
            if not group_type:
                continue
            try:
                visible = element.is_displayed()
            except Exception:
                visible = False
            groups.append({
                'element': element,
                'type': group_type,
                'kind': self.classify_group(group_type),
                'visible': visible,
            })
        return groups

    def submit_button(self, group):
        try:
            buttons = group.find_elements(By.CSS_SELECTOR, PublishPlayer.SUBMIT_CSS)
        except Exception:
            return None
        for button in buttons:
            try:
                if button.is_displayed():
                    return button
            except Exception:
                continue
        return buttons[0] if buttons else None

    def group_submitted(self, group) -> bool:
        button = self.submit_button(group)
        if button is None:
            return False
        text = PublishPlayer.button_text(button)
        return any(keyword in text for keyword in PublishPlayer.SUBMITTED_KEYWORDS)

    def group_key(self, group) -> str:
        group_id = group.get_attribute('id') or ''
        group_type = group.get_attribute('data-ipublish-question-group-type') or ''
        question_ids = []
        for node in self._question_nodes(group):
            try:
                question_ids.append(node.get_attribute('data-question-id') or '')
            except Exception:
                continue
        if not group_id and not question_ids:
            return f'{group_type}:{self.element_text(group, 120)}'
        return f'{group_type}:{group_id}:{",".join(question_ids)}'

    @staticmethod
    def clean_text(text: str, limit: Optional[int] = 4000) -> str:
        text = re.sub(r'[ \t]+', ' ', text or '')
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()[:limit]

    def element_text(self, element, limit: Optional[int] = 4000) -> str:
        text = ''
        try:
            text = element.text or ''
        except Exception:
            text = ''
        if not text.strip():
            try:
                text = self.driver.execute_script(
                    'return arguments[0] && arguments[0].innerText ? arguments[0].innerText : "";',
                    element) or ''
            except Exception:
                text = ''
        return self.clean_text(text, limit)

    def _stem_text(self, node, skip_class: str = '') -> str:
        try:
            contents = node.find_elements(By.CSS_SELECTOR, '.editor-content')
        except Exception:
            contents = []
        parts = []
        for content in contents:
            try:
                if content.find_elements(By.XPATH, 'ancestor::*[contains(@class, "q-answer-and-analysis")]'):
                    continue
            except Exception:
                pass
            if skip_class:
                try:
                    ancestor = content.find_elements(
                        By.XPATH, f'ancestor::*[contains(@class, "{skip_class}")]')
                    if ancestor:
                        continue
                except Exception:
                    pass
            text = self.element_text(content, limit=None)
            if text and text not in parts:
                parts.append(text)
        return self.clean_text('\n\n'.join(parts), limit=None) if parts else self._fallback_text(node)

    def _fallback_text(self, node) -> str:
        return self._dom_text(node, limit=None)

    def _dom_text(self, node, numbered: bool = False, limit: Optional[int] = 4000) -> str:
        script = """
        var node = arguments[0];
        if (!node) { return ""; }
        var clone = node.cloneNode(true);
        if (arguments[1]) {
            clone.querySelectorAll('span[data-dig-empty-id]').forEach((blank, i) => {
                blank.replaceWith(document.createTextNode('(' + (i + 1) + ')'));
            });
        }
        var drop = '[class*="options"], [class*="ListView"], [class*="shortAnswerWrap"], ' +
                   'textarea, input, button, [data-ipublish-event-type], ' +
                   '[class*="answerPreview"], [class*="upload"], [class*="title-content"], ' +
                   '[data-option-value], [class*="answerContent"], audio, video, ' +
                   'script, style, [aria-hidden="true"]';
        clone.querySelectorAll(drop).forEach(function (el) { el.remove(); });
        function text(el) {
            if (el.nodeType === Node.TEXT_NODE) return el.textContent;
            if (el.nodeType !== Node.ELEMENT_NODE) return '';
            if (el.tagName === 'BR') return '\\n';
            const children = Array.from(el.childNodes).map(text).join('');
            if (/^(TD|TH)$/.test(el.tagName)) return children.trim() + '\\t';
            if (/^(P|DIV|H[1-6]|LI|TR|TABLE|UL|OL|BLOCKQUOTE)$/.test(el.tagName))
                return '\\n' + children + '\\n';
            return children;
        }
        return text(clone);
        """
        try:
            text = self.driver.execute_script(script, node, numbered) or ''
        except Exception:
            text = ''
        text = re.sub(r'\t[ \t]*\n', '\n', text).replace('\t', ' | ')
        return self.clean_text(text, limit)

    def group_direction(self, group) -> str:
        text = ''
        try:
            titles = group.find_elements(
                By.CSS_SELECTOR, '[class*="title-content"], [class*="titleContent"]')
        except Exception:
            titles = []
        for title in titles:
            text = self.element_text(title, 300)
            if text:
                break
        for word in self.TITLE_NOISE_WORDS:
            text = text.replace(word, '')
        text = re.sub(r'得分[：:]\s*\d+(?:\.\d+)?\s*分', '', text)
        text = re.sub(r'小题\s*\d+', '', text)
        text = re.sub(r'^\s*\d+\s*分\s*$', '', text)
        title = re.sub(r'\s+', ' ', text).strip()
        try:
            introductions = self.driver.execute_script("""
                const group = arguments[0];
                return Array.from(group.querySelectorAll('.editor-content'))
                    .filter(el => !el.closest('[data-question-id]') &&
                        el.closest('[data-ipublish-question-group-type]') === group)
                    .map(el => el.innerText || '').filter(text => text.trim());
            """, group) or []
        except Exception:
            introductions = []
        preceding = self._preceding_direction(group)
        parts = list(dict.fromkeys([part for part in [preceding, title, *introductions] if part]))
        return self.clean_text('\n'.join(parts), 12000)

    def _preceding_direction(self, group) -> str:
        try:
            blocks = self.driver.execute_script("""
                const block = arguments[0].closest('[data-lexical-node-type="question-block"]');
                const parts = [];
                for (let el = block && block.previousElementSibling; el && parts.length < 12;
                     el = el.previousElementSibling) {
                    const type = el.getAttribute('data-lexical-node-type') || '';
                    if (!/^(base-paragraph|paragraph|highlight-block|table|list)$/.test(type) &&
                        !/^(P|TABLE|UL|OL|BLOCKQUOTE)$/.test(el.tagName)) break;
                    if (el.querySelector('[data-ipublish-question-group-type]')) break;
                    parts.unshift(el);
                }
                return parts;
            """, group) or []
            return '\n\n'.join(filter(None, (self._dom_text(block, limit=12000) for block in blocks)))
        except Exception:
            return ''

    def _question_nodes(self, group) -> List[Any]:
        nodes = []
        try:
            candidates = group.find_elements(By.CSS_SELECTOR, PublishPlayer.QUESTION_CSS)
        except Exception:
            return nodes
        for node in candidates:
            try:
                if node.get_attribute('data-option-value'):
                    continue
                if node.find_elements(
                        By.XPATH, 'ancestor::*[@data-question-id][not(@data-option-value)]'):
                    continue
            except Exception:
                continue
            nodes.append(node)
        return nodes

    @staticmethod
    def _question_type(node) -> str:
        try:
            tagged = node.find_elements(By.CSS_SELECTOR, '[data-ipublish-question-type]')
        except Exception:
            tagged = []
        for element in tagged:
            value = (element.get_attribute('data-ipublish-question-type') or '').strip()
            if value:
                return value
        try:
            return (node.get_attribute('data-ipublish-question-type') or '').strip()
        except Exception:
            return ''

    def parse_group(self, group, group_type: str, start_number: int = 1) -> List[Question]:
        kind = self.classify_group(group_type)
        if kind == 'unknown':
            kind = self._kind_from_questions(group)
        if kind == 'choice':
            return self._parse_choices(group, group_type, start_number)
        if kind == 'true_false':
            return self._parse_true_false(group, start_number)
        if kind == 'fill':
            return self._parse_fill_blanks(group, start_number, dropdown=False)
        if kind == 'dropdown':
            return self._parse_fill_blanks(group, start_number, dropdown=True)
        if kind == 'text':
            return self._parse_text(group, start_number)
        if kind == 'link_match':
            return self._parse_link_match(group, start_number)
        if kind == 'record':
            return self._parse_record(group, start_number, group_type)
        if kind == 'sorting':
            return self._parse_sequence(group, start_number)
        if kind != 'skip':
            self.unsupported.append(group_type)
        return []

    def _kind_from_questions(self, group) -> str:
        for node in self._question_nodes(group):
            kind = self.QUESTION_TYPE_KINDS.get(self._question_type(node))
            if kind:
                return kind
        return 'unknown'

    def collect_passages(self, groups: List[Dict[str, Any]]) -> List[str]:
        passages = []
        for state in groups:
            if state['type'] != 'rich_text_read_group':
                continue
            text = self._dom_text(state['element'], limit=None)
            if len(text) >= 80:
                passages.append(text)
        self.passages = passages
        return passages

    def _parse_choices(self, group, group_type: str, start_number: int) -> List[Question]:
        questions = []
        multiple = 'multiple' in group_type or 'multi_choice' in group_type
        nodes = self._question_nodes(group)
        for node in nodes:
            text = self._stem_text(node, skip_class=self.CHOICE_HEADER_SKIP)
            if not text and len(nodes) == 1:
                text = self.group_direction(group)
            options = self._choice_options(node)
            if not text or not options:
                continue
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.MULTIPLE_CHOICE if multiple else QuestionType.SINGLE_CHOICE,
                element=node,
                options=options,
                group=group,
                is_evaluation=(group_type == 'evaluation_group'
                               or self._question_type(node) == 'evaluation'),
            ))
        return questions

    def _parse_record(self, group, start_number: int, group_type: str = '') -> List[Question]:
        questions = []
        nodes = self._question_nodes(group)
        for node in nodes:
            buttons = node.find_elements(
                By.CSS_SELECTOR,
                '[class*="i-record_record-wave-wrapper"], [class*="result_re-record-wrapper"], '
                '[class*="i-oral-reply_oral-base-re-record-wrapper"]')
            text = self._stem_text(node)
            if group_type == 'vocabulary_group':
                words = node.find_elements(By.CSS_SELECTOR, '[class*="q-oral-vocabulary-content_word-text"]')
                text = self.element_text(words[0]) if words else ''
            if not text and len(nodes) == 1:
                text = self.group_direction(group)
            if not buttons or not text:
                continue
            samples = node.find_elements(By.CSS_SELECTOR, '[class*="answer_sample-audio-wrapper"] audio')
            if group_type == 'vocabulary_group':
                samples = node.find_elements(By.CSS_SELECTOR, '[class*="q-oral-vocabulary-content_media"] audio')
            url = samples[0].get_attribute('src') if samples else ''
            questions.append(Question(
                number=start_number + len(questions), text=text,
                q_type=QuestionType.RECORD, element=node, group=group,
                record_items=[{'url': url or '', 'element': buttons[0],
                               'text': '' if group_type == 'oral_sentence_scoop_group' and not url else text,
                               'sample': samples[0] if samples else None,
                               'group_type': group_type}],
            ))
        return questions

    def _parse_sequence(self, group, start_number: int) -> List[Question]:
        questions = []
        for node in self._question_nodes(group):
            rows = node.find_elements(By.CSS_SELECTOR, '[data-optionid][data-index]')
            if not rows:
                continue
            questions.append(Question(number=start_number + len(questions),
                text=self.group_direction(group), q_type=QuestionType.SORTING,
                element=node, group=group, options=[Option(letter=chr(65 + index),
                    text=self._stem_text(row), element=row) for index, row in enumerate(rows)]))
        return questions

    def _choice_options(self, node) -> List[Option]:
        options = []
        if self._question_type(node) == 'evaluation':
            radios = node.find_elements(By.CSS_SELECTOR, '[role="radio"][aria-posinset]')
            labels = node.find_elements(By.CSS_SELECTOR, '[class*="q-rates_title__"]')
            for index, radio in enumerate(radios):
                value = radio.get_attribute('aria-posinset')
                if len(labels) == 2 and index in (0, len(radios) - 1):
                    value += f' ({self.element_text(labels[0 if index == 0 else 1])})'
                options.append(Option(
                    letter=chr(65 + index), text=value, element=radio,
                    is_selected=radio.get_attribute('aria-checked') == 'true',
                ))
            return options
        try:
            rows = node.find_elements(By.CSS_SELECTOR, '[class*="strongListView"]')
        except Exception:
            rows = []
        for row in rows:
            try:
                letter_element = row.find_element(By.CSS_SELECTOR, '[class*="strongItem"]')
            except Exception:
                continue
            letter = self.element_text(letter_element, 8).strip().strip('.、):：')
            option_text = ''
            for content in row.find_elements(By.CSS_SELECTOR, '.editor-content'):
                option_text = self.element_text(content)
                if option_text:
                    break
            if not letter:
                letter = chr(65 + len(options))
            try:
                classes = ((letter_element.get_attribute('class') or '') + ' ' +
                           (row.get_attribute('class') or '')).lower()
            except Exception:
                classes = ''
            options.append(Option(
                letter=letter.upper(),
                text=option_text,
                element=letter_element,
                is_selected='selected' in classes or 'sele' in classes,
            ))
        return options

    def _parse_true_false(self, group, start_number: int) -> List[Question]:
        questions = []
        for node in self._question_nodes(group):
            text = self._stem_text(node, skip_class='answer_shortAnswerWrap')
            options = self._true_false_options(node)
            if not text or not options:
                continue
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.TRUE_FALSE,
                element=node,
                options=options,
                group=group,
            ))
        return questions

    def _true_false_options(self, node) -> List[Option]:
        options = []
        try:
            rows = node.find_elements(By.CSS_SELECTOR, 'div[class*="answer_option"]')
        except Exception:
            rows = []
        for row in rows:
            label = self.element_text(row, 12).strip().upper()
            letter = ''
            for candidate, labels in self.TRUE_FALSE_LABELS.items():
                if label in labels:
                    letter = candidate
                    break
            if not letter:
                continue
            if any(option.letter == letter for option in options):
                continue
            try:
                classes = (row.get_attribute('class') or '').lower()
            except Exception:
                classes = ''
            options.append(Option(
                letter=letter,
                text='True' if letter == 'T' else 'False',
                element=row,
                is_selected='select' in classes or 'active' in classes,
            ))
        return options

    def _parse_fill_blanks(self, group, start_number: int,
                           dropdown: bool = False) -> List[Question]:
        nodes = self._question_nodes(group) or [group]
        questions = []
        for node in nodes:
            text = self._numbered_stem_text(node) or self._stem_text(node)
            if not text:
                continue
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.DROPDOWN_SELECT if dropdown else QuestionType.FILL_IN,
                element=node,
                inputs=self._blank_inputs(node),
                banked_options=self._banked_options(node),
                group=group,
                blank_count=len(node.find_elements(By.CSS_SELECTOR, 'span[data-dig-empty-id]')),
            ))
        return questions

    def _numbered_stem_text(self, node) -> str:
        return self._dom_text(node, numbered=True, limit=None)

    LINK_NODE_WAIT_SECONDS = 12.0

    LINK_NODES_JS = """
    const group = arguments[0];
    const nodes = [...group.querySelectorAll('.react-flow__node')];
    return nodes.map((node, index) => {
      const handle = node.querySelector('.react-flow__handle');
      return {
        i: index,
        id: node.getAttribute('data-id') || '',
        side: handle ? (handle.getAttribute('data-handlepos') || '') : '',
        text: (node.innerText || '').replace(/\\s+/g, ' ').trim(),
      };
    });
    """

    def _link_nodes(self, group) -> List[Dict[str, Any]]:
        deadline = time.time() + self.LINK_NODE_WAIT_SECONDS
        nodes: List[Dict[str, Any]] = []
        while True:
            try:
                raw = self.driver.execute_script(self.LINK_NODES_JS, group)
            except Exception:
                raw = None
            nodes = [item for item in (raw or []) if isinstance(item, dict)]
            if nodes and all((item.get('text') or '').strip() for item in nodes):
                return nodes
            if time.time() >= deadline:
                return nodes
            time.sleep(0.8)

    def _parse_link_match(self, group, start_number: int) -> List[Question]:
        nodes = self._link_nodes(group)
        prompts = [item for item in nodes if item.get('side') == 'right']
        options = [item for item in nodes if item.get('side') == 'left']
        if not prompts or not options:
            self.unsupported.append('text_match_group')
            return []
        prompt_lines = [f"{index + 1}. {self._link_label(item.get('text', ''), number=True)}"
                        for index, item in enumerate(prompts)]
        return [Question(
            number=start_number,
            text='\n'.join(prompt_lines),
            q_type=QuestionType.LINK_MATCH,
            element=group,
            options=[Option(letter=chr(65 + index),
                            text=self._link_label(item.get('text', ''), number=False),
                            element=None)
                     for index, item in enumerate(options)],
            group=group,
            directions=self.group_direction(group),
            link_items=nodes,
        )]

    @staticmethod
    def _link_label(text: str, number: bool = True) -> str:
        pattern = r'^\s*\d{1,2}\s*[.、:：)）]\s+' if number else r'^\s*[A-Za-z]\s*[.、:：)）]\s+'
        return re.sub(pattern, '', text or '').strip()

    def _parse_text(self, group, start_number: int) -> List[Question]:
        questions = []
        for node in self._question_nodes(group):
            text = self._fallback_text(node) or self._stem_text(node)
            areas = self._answer_textareas(node)
            if not text or not areas:
                continue
            questions.append(Question(
                number=start_number + len(questions),
                text=text,
                q_type=QuestionType.TEXT,
                element=node,
                inputs=areas,
                group=group,
            ))
        return questions

    def _blank_inputs(self, node) -> List[Any]:
        inputs = []
        try:
            blanks = node.find_elements(By.CSS_SELECTOR, 'span[data-dig-empty-id]')
        except Exception:
            blanks = []
        for blank in blanks:
            try:
                inputs.extend(blank.find_elements(By.CSS_SELECTOR, 'input'))
            except Exception:
                continue
        if inputs and len(inputs) >= len(blanks):
            return inputs
        try:
            all_inputs = node.find_elements(
                By.CSS_SELECTOR, 'input[class*="answer_answerInput"]')
        except Exception:
            all_inputs = []
        return all_inputs or inputs

    def _banked_options(self, node) -> List[str]:
        options = []
        try:
            chips = node.find_elements(By.CSS_SELECTOR, '[data-option-value]')
        except Exception:
            chips = []
        for chip in chips:
            try:
                value = (chip.get_attribute('data-option-value') or '').strip()
            except Exception:
                value = ''
            if value and value not in options:
                options.append(value)
        return options

    def _answer_textareas(self, node) -> List[Any]:
        try:
            areas = node.find_elements(By.CSS_SELECTOR, 'textarea')
        except Exception:
            return []
        answer_areas = []
        for area in areas:
            try:
                placeholder = (area.get_attribute('placeholder') or '').strip()
            except Exception:
                placeholder = ''
            if placeholder in self.EXPLANATION_PLACEHOLDERS:
                continue
            answer_areas.append(area)
        return answer_areas or areas


class PublishAnswerExecutor(AnswerExecutor):

    RULE_POPUP_TEXTS = ('我知道了', '知道了', '确定')
    last_rejection = ''

    def __init__(self, driver, tts=None, stop_requested=None):
        super().__init__(driver, tts=tts, stop_requested=stop_requested)

    def _fill_single_choice(self, q: Question, answer: str) -> bool:
        if q.options and q.element.find_elements(By.CSS_SELECTOR, '[data-ipublish-question-type="evaluation"]'):
            letters = self._parse_choice_letters(answer, [opt.letter for opt in q.options])
            if len(letters) != 1:
                return False
            index = next(i for i, opt in enumerate(q.options) if opt.letter == letters[0])
            selected = [opt.element.get_attribute('aria-checked') == 'true' for opt in q.options]
            if selected[index] and (index == len(selected) - 1 or not selected[index + 1]):
                return True
            if not WebDriverHelper.safe_click(self.driver, q.options[index].element):
                return False
            selected = [opt.element.get_attribute('aria-checked') == 'true' for opt in q.options]
            return selected[index] and (index == len(selected) - 1 or not selected[index + 1])
        return super()._fill_single_choice(q, answer)

    def play_material(self, media, timeout: float = 20.0) -> bool:
        try:
            start = self.driver.execute_script("""
                const media = arguments[0];
                media.currentTime = 0;
                const start = media.currentTime || 0;
                media.muted = true;
                const playing = media.play();
                if (playing && playing.catch) playing.catch(() => {});
                return start;
            """, media)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                state = self.driver.execute_script("""
                    const media = arguments[0];
                    return {time: media.currentTime || 0, ended: media.ended,
                            error: !!media.error};
                """, media) or {}
                elapsed = float(state.get('time') or 0) - float(start or 0)
                if elapsed >= 3 or (state.get('ended') and elapsed > 0):
                    return True
                if state.get('error') or self.stop_requested.wait(0.25):
                    break
        except Exception as exc:
            print(f"    材料播放失败: {str(exc)[:80]}")
        finally:
            try:
                self.driver.execute_script('arguments[0].pause();', media)
            except Exception:
                pass
        return False

    def _answer_record(self, q: Question, answer: str = "") -> bool:
        item = q.record_items[0]
        url = (item.get('url') or '').strip()
        if url and item.get('sample') and not self.play_material(item['sample']):
            print("\t样音播放失败")
            return False
        if url:
            data = self._download_record_audio(url)
        else:
            text = (item.get('text') or '').strip() or answer or self._record_reading_text(q)
            data = self._speech_audio(text, 0)
        if not data or self.stop_requested.is_set():
            return False
        started = False
        try:
            prep = self.driver.execute_script(r"""
                const boxes = arguments[0].querySelectorAll('[class*="i-record-count-down_title-wrapper"]');
                for (const box of boxes) {
                    const text = (box.innerText || '').replace(/\s/g, '');
                    const match = text.match(/准备时长(\d{2}):(\d{2})/);
                    if (match) return Number(match[1]) * 60 + Number(match[2]);
                }
                return 3;
            """, q.element)
            prep = max(0.0, min(float(prep), 30.0))
            state = self.driver.execute_async_script(
                RECORD_MIC_JS, base64.b64encode(data).decode(),
                prep + self.RECORD_LEAD_PAD, self.RECORD_MAX_SECONDS)
            if not state or not state.get('ok') or not state.get('dur'):
                print("\t录音注入初始化失败")
                return False
            before = self._publish_record_state(q)
            if not self._click_publish_record(q):
                return False
            started = True
            deadline = time.monotonic() + prep + 15
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                current = self._publish_record_state(q)
                if current.get('calls', 0) > before.get('calls', 0):
                    break
                if self.stop_requested.wait(0.25):
                    break
            else:
                current = {}
            if not current or current.get('calls', 0) <= before.get('calls', 0):
                print("\t录音器未启动，停止本题")
                return False
            if self.stop_requested.wait(float(state['dur']) + 1.0):
                return False
            if not self._click_publish_record(q):
                return False
            started = False
            deadline = time.monotonic() + max(30.0, min(120.0, float(state['dur'])))
            previous = set(before.get('uploaded') or [])
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                current = self._publish_record_state(q)
                if set(current.get('uploaded') or []) - previous:
                    print("\t录音已上传")
                    return True
                if self.stop_requested.wait(0.5):
                    break
            print("\t未能确认录音上传完成，不提交")
        except Exception as exc:
            print(f"\t录音失败: {str(exc)[:80]}")
        finally:
            if started:
                try:
                    self._click_publish_record(q)
                except Exception:
                    pass
        return False

    def _click_publish_record(self, q: Question) -> bool:
        buttons = q.element.find_elements(
            By.CSS_SELECTOR,
            '[class*="i-record_record-wave-wrapper"], [class*="result_re-record-wrapper"], '
            '[class*="i-oral-reply_oral-base-re-record-wrapper"]')
        return bool(buttons and WebDriverHelper.safe_click(self.driver, buttons[0]))

    def _publish_record_state(self, q: Question) -> Dict[str, Any]:
        return self.driver.execute_script("""
            const root = arguments[0];
            return {
                calls: (window.__ucRec && window.__ucRec.calls) || 0,
                uploaded: Array.from(root.querySelectorAll('audio[src]'))
                    .filter(audio => !audio.closest('[class*="answer_sample-audio-wrapper"], ' +
                        '[class*="sample-audio"], [class*="q-oral-vocabulary-content_media"], ' +
                        '[data-lexical-node-type="insert-audio"]'))
                    .map(audio => audio.getAttribute('src') || '')
                    .filter(src => /^https?:/i.test(src))
            };
        """, q.element) or {}

    def publish_record_uploaded(self, q: Question) -> bool:
        return bool(self._publish_record_state(q).get('uploaded'))

    def reveal_group(self, group, stop_requested: Optional[threading.Event] = None) -> bool:
        try:
            contents = self.driver.execute_script("""
                const contents = [];
                for (let el = arguments[0].parentElement; el; el = el.parentElement) {
                    if (el.matches('[data-lexical-node-type="collapsible-content"]'))
                        contents.unshift(el);
                }
                return contents;
            """, group) or []
            for content in contents:
                if stop_requested is not None and stop_requested.is_set():
                    return False
                head = self.driver.execute_script("""
                    const content = arguments[0];
                    const style = getComputedStyle(content);
                    if (!content.hasAttribute('hidden') && style.display !== 'none' &&
                        style.contentVisibility !== 'hidden') return null;
                    return Array.from(content.parentElement.children).find(el =>
                        el.matches('[data-lexical-node-type="collapsible-head"], summary')) || null;
                """, content)
                if head is None:
                    continue
                if not WebDriverHelper.safe_click(self.driver, head):
                    return False
                WebDriverWait(self.driver, 2).until(lambda driver: driver.execute_script("""
                    const el = arguments[0], s = getComputedStyle(el);
                    return !el.hasAttribute('hidden') && s.display !== 'none' &&
                        s.contentVisibility !== 'hidden';
                """, content))
            return True
        except Exception:
            return False

    def execute(self, question: Question, answer: str) -> bool:
        if question.q_type == QuestionType.TRUE_FALSE:
            return self._fill_true_false(question, answer)
        if question.q_type == QuestionType.DROPDOWN_SELECT:
            return self._fill_publish_dropdown(question, answer)
        if question.q_type == QuestionType.LINK_MATCH:
            return self._fill_link_match(question, answer)
        return super().execute(question, answer)

    def _fill_sorting(self, q: Question, answer: str) -> bool:
        selector = '[data-optionid][data-index]'
        if not q.element.find_elements(By.CSS_SELECTOR, selector):
            return super()._fill_sorting(q, answer)
        order = self._parse_sorting_order(answer, [option.letter for option in q.options])
        if not order:
            return False
        ids = {option.letter: option.element.get_attribute('data-optionid') for option in q.options}
        expected = [ids[letter] for letter in order]
        for target, option_id in enumerate(expected):
            if self.stop_requested.is_set():
                return False
            rows = q.element.find_elements(By.CSS_SELECTOR, selector)
            current = [row.get_attribute('data-optionid') for row in rows]
            if current == expected:
                return True
            if option_id not in current:
                return False
            position = current.index(option_id)
            if position == target:
                continue
            row = rows[position]
            self.driver.execute_script('arguments[0].scrollIntoView({block: "center"});', q.element)
            self.driver.execute_async_script("""
                const row = arguments[0], target = arguments[1], done = arguments[2];
                const handle = row.querySelector('[class*="q-drag_dragBox"]');
                if (!handle) { done(false); return; }
                const a = handle.getBoundingClientRect(), b = target.getBoundingClientRect();
                const x = a.left + a.width / 2, y = a.top + a.height / 2;
                const end = b.top + b.height / 2;
                const fire = (node, type, yy, buttons = 1) => node.dispatchEvent(
                    new MouseEvent(type, {view: window, bubbles: true, cancelable: true,
                        clientX: x, clientY: yy, button: 0, buttons}));
                fire(handle, 'mousedown', y);
                setTimeout(() => {
                    fire(document, 'mousemove', y - 12);
                    setTimeout(() => {
                        fire(document, 'mousemove', end);
                        setTimeout(() => {
                            fire(document, 'mouseup', end, 0);
                            done(true);
                        }, 250);
                    }, 150);
                }, 150);
            """, row, rows[target])
            self.stop_requested.wait(0.2)
        return [row.get_attribute('data-optionid') for row in
                q.element.find_elements(By.CSS_SELECTOR, selector)] == expected

    def _fill_fill_in(self, q: Question, answer: str) -> bool:
        if q.blank_count and q.blank_count != len(q.inputs):
            print("\t空位与输入框数量不一致，不填写该题")
            return False
        return self._fill_required_inputs(q.inputs, self._parse_banked_answer(answer, len(q.inputs)))

    def _fill_text(self, q: Question, answer: str) -> bool:
        if len(q.inputs) <= 1:
            return super()._fill_text(q, answer)
        return self._fill_required_inputs(q.inputs, self._parse_banked_answer(answer, len(q.inputs)))

    LINK_STUB_JS = """
    if (!window.__unipusLinkStub) {
      window.__unipusLinkStub = true;
      const noop = function () { return false; };
      ['setPointerCapture', 'releasePointerCapture'].forEach((name) => {
        try { Element.prototype[name] = noop; } catch (e) {}
      });
      try { Element.prototype.hasPointerCapture = function () { return true; }; } catch (e) {}
    }
    return true;
    """

    LINK_ONCE_JS = """
    const group = arguments[0];
    const sourceId = arguments[1];
    const targetId = arguments[2];
    const steps = 12;
    const nodeOf = (id) => group.querySelector('.react-flow__node[data-id="' + id + '"]');
    const sourceNode = nodeOf(sourceId);
    const targetNode = nodeOf(targetId);
    if (!sourceNode || !targetNode) return 'no-node';
    const sourceHandle = sourceNode.querySelector('.react-flow__handle');
    const targetHandle = targetNode.querySelector('.react-flow__handle');
    if (!sourceHandle || !targetHandle) return 'no-handle';
    const centerOf = (el) => {
      const box = el.getBoundingClientRect();
      return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    };
    const from = centerOf(sourceHandle);
    const to = centerOf(targetHandle);
    const fire = (el, type, point, buttons) => {
      el.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, composed: true,
        clientX: point.x, clientY: point.y,
        button: 0, buttons: buttons, view: window,
      }));
    };
    fire(sourceHandle, 'mousedown', from, 1);
    for (let step = 1; step <= steps; step++) {
      fire(sourceHandle, 'mousemove', {
        x: from.x + (to.x - from.x) * step / steps,
        y: from.y + (to.y - from.y) * step / steps,
      }, 1);
    }
    fire(targetHandle, 'mousemove', to, 1);
    fire(targetHandle, 'mouseup', to, 0);
    return 'ok';
    """

    LINK_EDGES_JS = "return arguments[0].querySelectorAll('.react-flow__edge').length;"

    def _link_stub(self) -> None:
        try:
            self.driver.execute_script(self.LINK_STUB_JS)
        except Exception:
            pass

    def _link_edge_count(self, group) -> int:
        try:
            return int(self.driver.execute_script(self.LINK_EDGES_JS, group) or 0)
        except Exception:
            return 0

    @staticmethod
    def _parse_link_answer(answer: str, prompt_count: int,
                           option_count: int) -> List[Tuple[int, str]]:
        pairs: List[Tuple[int, str]] = []
        seen = set()
        for number, letter in re.findall(r'(\d+)\s*[.\-:：)）]?\s*([A-Za-z])', answer or ''):
            index = int(number) - 1
            position = ord(letter.upper()) - 65
            if not (0 <= index < prompt_count and 0 <= position < option_count):
                continue
            if index in seen:
                continue
            seen.add(index)
            pairs.append((index, letter.upper()))
        return pairs

    def _fill_link_match(self, q: Question, answer: str) -> bool:
        group = q.group or q.element
        prompts = [item for item in q.link_items if item.get('side') == 'right']
        options = [item for item in q.link_items if item.get('side') == 'left']
        if not prompts or not options:
            print("\t连线匹配：画布节点不完整，跳过")
            return False
        letter_to_item = {chr(65 + index): item for index, item in enumerate(options)}
        pairs = self._parse_link_answer(answer, len(prompts), len(options))
        if not pairs:
            print("\t连线匹配：未能从回答里解析出配对，跳过")
            return False
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", group)
        except Exception:
            pass
        self._link_stub()
        before = self._link_edge_count(group)
        linked = 0
        for index, letter in pairs:
            source = prompts[index]
            target = letter_to_item.get(letter)
            if not target:
                continue
            try:
                result = self.driver.execute_script(
                    self.LINK_ONCE_JS, group, source.get('id'), target.get('id'))
            except Exception as exc:
                print(f"\t连线匹配：{index + 1}→{letter} 异常 {type(exc).__name__}")
                continue
            time.sleep(0.35)
            now = self._link_edge_count(group)
            if result == 'ok' and now > before:
                linked += 1
                before = now
            else:
                print(f"\t连线匹配：{index + 1}→{letter} 未生成连线（{result}）")
        after = self._link_edge_count(group)
        print(f"\t连线匹配：连上 {linked} 组，画布共 {after} 条连线（提示 {len(prompts)} 条）")
        return after >= len(prompts)

    def _fill_required_inputs(self, inputs: List[Any], answers: List[str]) -> bool:
        if not inputs or len(answers) != len(inputs) or not all(answers):
            print("\t答案不完整，不填写该题")
            return False
        for index, (inp, answer) in enumerate(zip(inputs, answers), 1):
            print(f"\t空{index}: {answer[:80]}")
            if not self._fill_text_input_verified(inp, answer):
                print(f"\t空{index}: 写入后校验失败")
                return False
        return True

    def _fill_text_input_verified(self, inp, ans: str) -> bool:
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", inp)
            time.sleep(0.2)
        except Exception:
            pass

        for _ in range(2):
            if self._type_by_keyboard(inp, ans):
                return True
        print("\t真实键盘输入失败，不提交该题目组")
        return False

    def _type_by_keyboard(self, inp, ans: str) -> bool:
        try:
            self.driver.execute_script("arguments[0].focus();", inp)
            time.sleep(0.2)
            inp.send_keys(Keys.CONTROL, 'a')
            inp.send_keys(Keys.DELETE)
            current = inp.get_attribute('value') or ''
            for _ in range(len(current)):
                inp.send_keys(Keys.BACKSPACE)
            inp.send_keys(ans)
            inp.send_keys(Keys.TAB)
            time.sleep(0.2)
            return self._input_value_matches(inp, ans)
        except Exception:
            return False

    @staticmethod
    def normalize_true_false(answer: str) -> str:
        raw = (answer or '').strip()
        if '正确' in raw or raw in ('对', '是', '√'):
            return 'T'
        if '错误' in raw or raw in ('错', '否', '×', '不对'):
            return 'F'
        text = re.sub(r'^[\d\s.、):：\]]+', '', raw.upper())
        match = re.search(r'(?<![A-Z])(TRUE|FALSE|T|F)(?![A-Z])', text)
        if not match:
            return ''
        return 'T' if match.group(1) in ('TRUE', 'T') else 'F'

    def _fill_true_false(self, question: Question, answer: str) -> bool:
        letter = self.normalize_true_false(answer)
        if not letter:
            print(f"\t判断题答案无法识别: {str(answer)[:40]!r}")
            return False
        for option in question.options:
            if option.letter.upper() == letter:
                print(f"\t选择 {option.letter}（{option.text}）")
                return WebDriverHelper.safe_click(self.driver, option.element)
        return False

    def _fill_publish_dropdown(self, question: Question, answer: str) -> bool:
        blanks = self._dropdown_blanks(question)
        if not blanks:
            return self._fill_fill_in(question, answer)
        answers = self._parse_banked_answer(answer, len(blanks))
        if len(answers) != len(blanks) or not all(answers):
            print("\t下拉填空答案不完整，不填写该题")
            return False
        filled = 0
        for blank, value in zip(blanks, answers):
            if value and self._select_dropdown_value(blank, value):
                filled += 1
        if filled < len(blanks):
            print(f"\t下拉填空仅选中 {filled}/{len(blanks)} 个")
            return False
        return True

    def _dropdown_blanks(self, question: Question) -> List[Any]:
        try:
            return question.element.find_elements(
                By.CSS_SELECTOR, 'span[data-dig-empty-id]')
        except Exception:
            return []

    def _select_dropdown_value(self, blank, value: str) -> bool:
        trigger = self._dropdown_trigger(blank)
        if trigger is not None:
            if self._select_trigger_option(trigger, value):
                return True

        optional_inputs = []
        try:
            elements = blank.find_elements(By.CSS_SELECTOR, 'input, textarea, select')
        except Exception:
            elements = []
        for element in elements:
            try:
                tag = (element.tag_name or '').lower()
                readonly = (element.get_attribute('readonly') or '').strip().lower()
                disabled = (element.get_attribute('disabled') or '').strip().lower()
            except Exception:
                continue
            if tag == 'select':
                if self._select_native_option(element, value):
                    return True
                continue
            if readonly in ('true', 'readonly') or disabled == 'true':
                continue
            optional_inputs.append(element)

        for element in optional_inputs:
            if self._fill_text_input_verified(element, value):
                return True

        if not WebDriverHelper.safe_click(self.driver, trigger or blank):
            return False
        time.sleep(0.6)
        if self._click_dropdown_option(value):
            return True
        for element in optional_inputs:
            if self._fill_text_input_verified(element, value):
                return True
        try:
            ActionChains(self.driver).send_keys('\ue00c').perform()
        except Exception:
            pass
        return False

    @staticmethod
    def _dropdown_trigger(blank):
        selectors = (
            '[class*="selectTrigger"]',
            '[class*="digEmptySelectView"]',
            '[role="combobox"]',
            '[class*="select"]',
        )
        for css in selectors:
            try:
                candidates = blank.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                continue
            for element in candidates:
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
        return None

    DROPDOWN_POPUP_CSS = ('[class*="digEmptySelectView_dropdownContent"], '
                          '[class*="digEmptySelectView_dropdown"]')

    def _visible_dropdown_popup(self):
        try:
            candidates = self.driver.find_elements(By.CSS_SELECTOR,
                                                   self.DROPDOWN_POPUP_CSS)
        except Exception:
            return None
        for element in candidates:
            try:
                rect = element.rect
                if element.is_displayed() and rect.get('height', 0) > 10:
                    return element
            except Exception:
                continue
        return None

    def _nearest_dropdown_trigger(self, popup):
        try:
            popup_rect = popup.rect
            triggers = self.driver.find_elements(
                By.CSS_SELECTOR, '[class*="selectTrigger"], [role="combobox"]')
        except Exception:
            return None
        best = None
        best_distance = None
        for trigger in triggers:
            try:
                if not trigger.is_displayed():
                    continue
                rect = trigger.rect
                distance = (abs(rect['x'] - popup_rect['x'])
                            + abs(rect['y'] - popup_rect['y']))
            except Exception:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = trigger, distance
        return best

    def _wait_dropdown_popup(self, timeout: float = 1.6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            popup = self._visible_dropdown_popup()
            if popup is not None:
                return popup
            time.sleep(0.12)
        return None

    def _dropdown_chevron(self, trigger):
        try:
            return trigger.find_element(By.CSS_SELECTOR,
                                        '[class*="close-icon"], [class*="icon"]')
        except Exception:
            return None

    def _js_mouse_click(self, element) -> None:
        self.driver.execute_script("""
        const el = arguments[0];
        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (type) {
            el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
        });
        """, element)

    def _close_visible_dropdowns(self, max_rounds: int = 4) -> None:
        for round_index in range(max_rounds):
            popup = self._visible_dropdown_popup()
            if popup is None:
                return
            trigger = self._nearest_dropdown_trigger(popup)
            if trigger is None:
                return
            if DEBUG_MODE:
                try:
                    text = PublishPlayer.button_text(trigger)
                except Exception:
                    text = ''
                print(f" [dd] 关闭弹层 第{round_index + 1}轮: trigger={text!r} "
                      f"h={popup.rect.get('height')}")
            try:
                ActionChains(self.driver).move_to_element(trigger).pause(0.15).click().perform()
            except Exception:
                WebDriverHelper.safe_click(self.driver, trigger)
            time.sleep(0.55)
            if self._visible_dropdown_popup() is None:
                return
            try:
                self._js_mouse_click(trigger)
            except Exception:
                return
            time.sleep(0.55)

    def _open_dropdown(self, trigger) -> bool:
        self._close_visible_dropdowns()
        chevron = self._dropdown_chevron(trigger)
        targets = [trigger]
        if chevron is not None:
            targets.append(chevron)
        for target in targets:
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                    target)
                time.sleep(0.2)
            except Exception:
                pass
            strategies = (
                ('actionchains', lambda t=target: ActionChains(self.driver)
                    .move_to_element(t).pause(0.2).click().perform()),
                ('native', lambda t=target: t.click()),
                ('js-events', lambda t=target: self._js_mouse_click(t)),
            )
            for name, action in strategies:
                try:
                    action()
                except Exception as exc:
                    if DEBUG_MODE:
                        print(f" [dd] {name} 点击失败: {str(exc).splitlines()[0][:60]}")
                    continue
                if self._wait_dropdown_popup(1.3) is not None:
                    if DEBUG_MODE:
                        print(f" [dd] 弹层已打开（{name}）")
                    return True
        if DEBUG_MODE:
            print(" [dd] 所有打开方式都没能打开弹层")
        return False

    def _dropdown_options_from_popup(self) -> List[str]:
        popup = self._visible_dropdown_popup()
        if popup is None:
            return []
        options = []
        try:
            items = popup.find_elements(By.CSS_SELECTOR, '[class*="optionItem"]')
        except Exception:
            items = []
        for item in items:
            try:
                if not item.is_displayed():
                    continue
                text = re.sub(r'\s+', ' ', item.text or '').strip()
            except Exception:
                continue
            if text and 2 < len(text) <= 160 and text not in options:
                options.append(text)
        return options

    def collect_dropdown_options(self, blank) -> List[str]:
        trigger = self._dropdown_trigger(blank)
        if trigger is None:
            if DEBUG_MODE:
                print(" [dd] 没找到下拉触发器")
            return []
        popup = self._visible_dropdown_popup()
        if popup is None:
            if not self._open_dropdown(trigger):
                return []
            popup = self._visible_dropdown_popup()
        options = self._dropdown_options_from_popup() if popup is not None else []
        if DEBUG_MODE:
            print(f" [dd] 候选数量: {len(options)}")
        self._close_visible_dropdowns()
        return options[:20]

    def _select_trigger_option(self, trigger, value: str) -> bool:
        target = re.sub(r'\s+', '', value or '').lower()
        if not target:
            return False
        try:
            current = re.sub(r'\s+', '', PublishPlayer.button_text(trigger)).lower()
        except Exception:
            current = ''
        if current == target:
            return True

        if not self._open_dropdown(trigger):
            return False
        popup = self._visible_dropdown_popup()
        if popup is None:
            return False
        items = []
        try:
            items = popup.find_elements(By.CSS_SELECTOR, '[class*="optionItem"]')
        except Exception:
            items = []

        match = None
        for item in items:
            try:
                if not item.is_displayed():
                    continue
                text = re.sub(r'\s+', '', PublishPlayer.button_text(item)).lower()
            except Exception:
                continue
            if not text:
                continue
            if text == target:
                match = item
                break
        chosen = match
        if chosen is None:
            self._close_visible_dropdowns()
            return False

        clicked = False
        try:
            ActionChains(self.driver).move_to_element(chosen).pause(0.15).click().perform()
            clicked = True
        except Exception:
            clicked = WebDriverHelper.safe_click(self.driver, chosen)
        time.sleep(0.5)
        try:
            updated = re.sub(r'\s+', '', PublishPlayer.button_text(trigger)).lower()
        except Exception:
            updated = ''
        if not clicked or updated != target:
            try:
                self._js_mouse_click(chosen)
                clicked = True
            except Exception:
                pass
            time.sleep(0.5)
            try:
                updated = re.sub(r'\s+', '', PublishPlayer.button_text(trigger)).lower()
            except Exception:
                updated = ''
        self._close_visible_dropdowns()
        if not clicked:
            return False
        return updated == target

    @staticmethod
    def _select_native_option(select, value: str) -> bool:
        target = re.sub(r'\s+', '', value or '').lower()
        if not target:
            return False
        try:
            options = select.find_elements(By.TAG_NAME, 'option')
        except Exception:
            return False
        for option in options:
            text = re.sub(r'\s+', '', PublishPlayer.button_text(option)).lower()
            if not text:
                continue
            if text == target:
                try:
                    option.click()
                    return True
                except Exception:
                    continue
        return False

    def _click_dropdown_option(self, value: str) -> bool:
        target = re.sub(r'\s+', '', (value or '')).lower()
        if not target:
            return False
        selectors = (
            '[role="option"]',
            '.ant-select-item-option',
            '[class*="option"]',
            '[class*="OptionItem"]',
            'li',
        )
        for css in selectors:
            try:
                candidates = self.driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                continue
            for element in candidates[:300]:
                try:
                    if not element.is_displayed():
                        continue
                    text = re.sub(r'\s+', '', PublishPlayer.button_text(element)).lower()
                except Exception:
                    continue
                if text == target:
                    if WebDriverHelper.safe_click(self.driver, element):
                        return True
        return False

    def find_submit_button(self, group):
        if group is None:
            return None
        try:
            buttons = group.find_elements(By.CSS_SELECTOR, PublishPlayer.SUBMIT_CSS)
        except Exception:
            return None
        for button in buttons:
            try:
                if button.is_displayed():
                    return button
            except Exception:
                continue
        return buttons[0] if buttons else None

    @staticmethod
    def button_is_submitted(button) -> bool:
        text = PublishPlayer.button_text(button)
        return any(keyword in text for keyword in PublishPlayer.SUBMITTED_KEYWORDS)

    REJECTION_HINTS = ('正确率未达到', '正确率不足', '正确率未达', '未达到60%', '未达到 60%')

    def rejection_message(self) -> str:
        script = """
        const hints = arguments[0];
        const out = [];
        for (const el of document.querySelectorAll('body *')) {
            if (el.children.length > 2) continue;
            const t = (el.innerText || '').trim();
            if (!t || t.length > 120) continue;
            if (!hints.some(h => t.indexOf(h) >= 0)) continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            out.push(t.replace(/\\s+/g, ' ').slice(0, 120));
            if (out.length >= 2) break;
        }
        return out;
        """
        messages = []
        try:
            messages = self.driver.execute_script(script, list(self.REJECTION_HINTS)) or []
        except Exception:
            messages = []
        if not messages:
            in_content = False
            try:
                in_content = self.driver.execute_script('return window !== window.top;') is True
                PublishPlayer.reset(self.driver)
                messages = self.driver.execute_script(script, list(self.REJECTION_HINTS)) or []
            except Exception:
                messages = []
            finally:
                if in_content:
                    PublishFrameContext(self.driver).enter()
        return ' | '.join(messages)

    def submit(self, group=None, timeout: float = 20.0, resolver=None) -> bool:
        self.last_rejection = ''
        if group is None:
            return False
        button = self.find_submit_button(group)
        if button is None:
            print("\t未找到该题目组的提交按钮")
            return False
        if self.button_is_submitted(button):
            return True
        print("\t点击「提交」...")
        if not WebDriverHelper.safe_click(self.driver, button):
            print("\t点击提交按钮失败")
            return False
        return self.wait_graded(group, timeout, resolver)

    def wait_graded(self, group, timeout: float = 20.0, resolver=None) -> bool:
        deadline = time.time() + timeout
        current = group
        while time.time() < deadline:
            if resolver is not None:
                try:
                    PublishFrameContext(self.driver).enter()
                    fresh = resolver()
                    if fresh is not None:
                        current = fresh
                except Exception:
                    pass
            try:
                button = self.find_submit_button(current)
                if button is not None and self.button_is_submitted(button):
                    return True
                if current.find_elements(By.CSS_SELECTOR, '[class*="answerPreview"]'):
                    return True
            except StaleElementReferenceException:
                current = group
            except Exception:
                pass
            rejection = self.rejection_message()
            if rejection:
                self.last_rejection = rejection
                print(f"\t平台拒绝提交：{rejection}")
                return False
            time.sleep(0.5)
        print("\t提交后未检测到已批改状态，请人工确认")
        return False

    def dismiss_rules_popup(self, max_candidates: int = 400) -> bool:
        PublishPlayer.reset(self.driver)
        script = r"""
        const labels = arguments[0], limit = arguments[1];
        const visible = el => {
            const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
        };
        const textOf = el => (el.innerText || '').replace(/\s+/g, '');
        const selectors = ['.ipublish-modal', '[role="dialog"]', '[class*="modal"], [class*="dialog"]'];
        for (const selector of selectors) {
            for (const modal of [...document.querySelectorAll(selector)].slice(0, limit)) {
                if (!visible(modal) || !textOf(modal).includes('单元学习规则')) continue;
                if (arguments[2]) return true;
                for (const css of ['button, [role="button"], [class*="btn"]', 'a, span, div']) {
                    const buttons = [...modal.querySelectorAll(css)].slice(0, limit);
                    for (const button of buttons) {
                        if (visible(button) && !button.disabled && labels.includes(textOf(button))) {
                            return button;
                        }
                    }
                }
            }
        }
        return arguments[2] ? false : null;
        """
        try:
            button = self.driver.execute_script(script, list(self.RULE_POPUP_TEXTS),
                                                max_candidates, False)
            if button is None:
                return False
        except Exception:
            return False
        try:
            ActionChains(self.driver).move_to_element(button).click().perform()
        except Exception:
            if not WebDriverHelper.safe_click(self.driver, button):
                return False
        try:
            WebDriverWait(self.driver, 2, poll_frequency=0.1).until(
                lambda driver: not driver.execute_script(
                    script, list(self.RULE_POPUP_TEXTS), max_candidates, True))
        except Exception:
            return False
        print("    已关闭「本单元学习规则」弹窗")
        return True


class ContentHandler(ABC):

    @abstractmethod
    def can_handle(self, question: Question) -> bool:
        pass

    @abstractmethod
    def handle(self, question: Question) -> bool:
        pass


class DiscussionBoardHandler(ContentHandler):

    def __init__(self, driver, ai_client=None, stop_requested: Optional[threading.Event] = None):
        self.driver = driver
        self.ai_client = ai_client
        self.stop_requested = stop_requested or threading.Event()

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.DISCUSSION_BOARD

    def handle(self, question: Question) -> bool:
        textarea = self._find_textarea()
        if not textarea:
            print("     讨论板未找到输入框，跳过")
            return True

        topic = self._topic_text()
        print(f"     讨论板发帖：{topic[:60]}")

        body = self._compose(topic)
        if not body:
            print("     讨论板内容生成失败，跳过")
            return True

        try:
            textarea.click()
            textarea.clear()
        except Exception:
            pass
        filled = False
        try:
            self.driver.execute_script("""
                const el = arguments[0], value = arguments[1];
                el.focus();
                const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
                if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                return el.value;
            """, textarea, body)
            filled = True
        except Exception as exc:
            print(f"     讨论板写入失败: {str(exc)[:50]}")
        if not filled:
            textarea.send_keys(body)

        try:
            textarea.send_keys(Keys.SPACE)
            textarea.send_keys(Keys.BACKSPACE)
        except Exception:
            pass

        if self.stop_requested.wait(0.6):
            return False

        submit = self._find_submit()
        if not submit:
            print("     讨论板未找到发布按钮")
            return False
        clicked = WebDriverHelper.safe_click(self.driver, submit)
        if not clicked:
            try:
                self.driver.execute_script("""
                    const btn = arguments[0];
                    btn.scrollIntoView({block: 'center'});
                    btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    btn.click();
                """, submit)
                clicked = True
            except Exception:
                clicked = False
        if not clicked:
            return False

        self.stop_requested.wait(2.5)
        if self._posted_successfully():
            print("     讨论板已发布")
            return True
        print("     讨论板发布后未检测到评论，可能被限流或需要人机验证")
        return False

    def _posted_successfully(self) -> bool:
        try:
            return bool(self.driver.execute_script("""
                const area = document.querySelector('.ds-discussion-bottom textarea, textarea[placeholder*="评论"]');
                if (area && (area.value || '').trim().length === 0) return true;
                const text = document.body.innerText || '';
                return /全部评论（[1-9]/.test(text);
            """))
        except Exception:
            return False

    def _find_textarea(self):
        for selector in ('.ds-discussion-bottom textarea',
                         'textarea[placeholder*="评论"]',
                         '.ds-discussion-reply textarea'):
            try:
                for area in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if area.is_displayed():
                        return area
            except Exception:
                continue
        return None

    def _find_submit(self):
        for selector in ('button.submit-btn',
                         '.btns-submit button',
                         '.ds-discussion-bottom-btns button',
                         '.ds-discussion-bottom button',
                         'button.ant-btn-default'):
            try:
                for btn in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    text = re.sub(r'\s+', '', btn.text or '')
                    if btn.is_displayed() and ('发布' in text or 'submit' in text.lower()):
                        return btn
            except Exception:
                continue
        return None

    def _topic_text(self) -> str:
        parts = []
        try:
            parts = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const out = [];
                const dir = document.querySelector('.layout-direction-container');
                if (dir) out.push(clean(dir.innerText));

                for (const el of document.querySelectorAll('.component-htmlview, p, .question-common-abs-scoop')) {
                  if (el.closest('.ds-discussion-bottom, [class*="recordList"], [class*="comment"], .layout-sidebar')) continue;
                  const text = clean(el.innerText);
                  if (text.length < 15 || text.length > 400) continue;
                  if (text.includes('全部评论') || text.includes('我来评论')) continue;
                  if (out.includes(text)) continue;
                  out.push(text);
                  if (out.length >= 3) break;
                }
                return out;
            """) or []
        except Exception:
            parts = []
        text = "\n".join([p for p in parts if p])[:1200]
        if len(text) > 10:
            return text
        return ""

    def _compose(self, topic: str) -> str:
        if self.ai_client and topic:
            prompt = (f"【讨论题】{topic}\n\n"
                      "请用英文写一段 60-90 词、观点明确的讨论回复（3 个要点，可举例），"
                      "直接输出正文，不要标题、不要编号、不要解释。")
            try:
                answer = self.ai_client.ask(prompt, retry_count=2, stop_requested=self.stop_requested)
                if answer:
                    return re.sub(r'^[\s"“”]+|[\s"“”]+$', '', answer).strip()[:1200]
            except Exception as exc:
                print(f"     讨论板生成失败: {str(exc)[:50]}")
        return ""


class AudioHandler(ContentHandler):

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.AUDIO

    def handle(self, question: Question) -> bool:
        audios = question.element.find_elements(By.TAG_NAME, 'audio')
        if not audios:
            return False
        for index, audio in enumerate(audios, 1):
            if self.stop_requested.is_set():
                return False
            print(f"     播放第 {index}/{len(audios)} 段音频，等待完整播放...")
            try:
                self.driver.execute_script("""
                    const audio = arguments[0];
                    audio.loop = false;
                    audio.playbackRate = 1;
                    audio.currentTime = 0;
                    audio.__ucampusPlayError = '';
                    const playing = audio.play();
                    if (playing && playing.catch) playing.catch(error => {
                        audio.__ucampusPlayError = String(error);
                    });
                """, audio)
                last_time = 0.0
                progress_at = time.monotonic()
                while not self.stop_requested.is_set():
                    state = self.driver.execute_script("""
                        const audio = arguments[0];
                        return {time: audio.currentTime || 0, ended: audio.ended,
                                error: !!audio.error || !!audio.__ucampusPlayError};
                    """, audio) or {}
                    current = float(state.get('time') or 0)
                    if state.get('error'):
                        print("     音频播放失败，保留任务以便重试")
                        return False
                    if state.get('ended') and current > 0:
                        print(f"     第 {index} 段音频已完整播放（{current:.1f}s）")
                        break
                    if current > last_time:
                        last_time, progress_at = current, time.monotonic()
                    if time.monotonic() - progress_at > 30:
                        print("     音频加载或播放停滞，保留任务以便重试")
                        return False
                    if self.stop_requested.wait(0.25):
                        return False
                else:
                    return False
            except Exception as exc:
                print(f"     音频播放失败: {str(exc)[:80]}")
                return False
            finally:
                try:
                    self.driver.execute_script('arguments[0].pause();', audio)
                except Exception:
                    pass
        return not self.stop_requested.is_set()


class SelfCheckHandler(ContentHandler):

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.SELF_CHECK

    def handle(self, question: Question) -> bool:
        print("     处理 Self-check 词汇勾选表...")
        clicked = 0

        row_count = len(question.element.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)'))
        for row_index in range(row_count):
            if self.stop_requested.is_set():
                return False
            try:
                rows = question.element.find_elements(By.CSS_SELECTOR, 'tbody tr.ant-table-row:not(.category-name)')
                if row_index >= len(rows):
                    break
                row = rows[row_index]

                word = ""
                try:
                    word = row.find_element(By.CSS_SELECTOR, '.content-text').text.strip()
                except:
                    pass

                got_it_cell = row.find_element(By.CSS_SELECTOR, 'td:nth-child(2)')
                icon = got_it_cell.find_element(By.CSS_SELECTOR, '.anticon')
                icon_class = icon.get_attribute('class') or ''
                aria_label = (icon.get_attribute('aria-label') or '').lower()

                if 'anticon-border' not in icon_class and aria_label != 'border':
                    continue

                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", icon)
                if self.stop_requested.wait(0.1):
                    return False

                if WebDriverHelper.safe_click(self.driver, icon):
                    clicked += 1
                    print(f"      勾选: {word or clicked}")
                else:
                    self.driver.execute_script("""
                        arguments[0].click();
                        arguments[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    """, icon)
                    clicked += 1
                    print(f"      JS勾选: {word or clicked}")

                if self.stop_requested.wait(0.1):
                    return False
            except Exception as e:
                print(f"      勾选失败: {str(e)[:50]}")
                continue

        print(f"     Self-check 完成，共勾选 {clicked} 项")
        return True


class VideoHandler(ContentHandler):

    def __init__(self, driver, config: Config, stop_requested: threading.Event):
        self.driver = driver
        self.config = config
        self.stop_requested = stop_requested
        self.popup_monitor_thread = None
        self.stop_monitoring = threading.Event()

        self.transcriber = AudioTranscriber()

        ai_ready = bool(
            (getattr(config, 'api_key', '') or '').strip()
            and (getattr(config, 'base_url', '') or '').strip()
            and (getattr(config, 'model', '') or '').strip()
        )
        self.analyzer_client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url
        ) if ai_ready else None

        self.video_transcript = ""
        self.current_video_url = ""
        self.skip_transcription = False
        self._transcript_lock = threading.Lock()
        self._handling_popup = threading.Event()
        self._preprocessed_video_marker: Optional[Dict[str, Any]] = None
        self._active_tab_key: str = ""

    @staticmethod
    def _normalize_video_url(url: str) -> str:
        return (url or '').strip().split('#')[0]

    def set_active_tab(self, tab_key: str) -> None:
        self._active_tab_key = (tab_key or '').strip()

    def clear_preprocessed_played(self) -> None:
        self._preprocessed_video_marker = None

    def mark_preprocessed_played(
        self,
        video_key: str = "",
        video_url: str = "",
        tab_key: str = "",
        video_element=None,
    ) -> None:
        self._preprocessed_video_marker = {
            'video_key': (video_key or '').strip(),
            'video_url': self._normalize_video_url(video_url),
            'tab_key': (tab_key or '').strip(),
            'video_element': video_element,
        }

    def _consume_preprocessed_played(self) -> bool:
        marker = getattr(self, '_preprocessed_video_marker', None)
        if not marker:
            return False
        self._preprocessed_video_marker = None

        active_tab_key = (getattr(self, '_active_tab_key', '') or '').strip()
        marker_tab_key = (marker.get('tab_key') or '').strip()
        if active_tab_key and marker_tab_key and active_tab_key != marker_tab_key:
            return False

        try:
            info = self._get_video_info() or {}
        except Exception:
            info = {}

        current_url = self._normalize_video_url(info.get('url', ''))
        marker_url = (marker.get('video_url') or '').strip()
        if marker_url and current_url and marker_url == current_url:
            return True

        marker_el = marker.get('video_element')
        current_el = info.get('element')
        if marker_el is not None and current_el is not None and marker_el == current_el:
            return True

        if active_tab_key and marker_tab_key and active_tab_key == marker_tab_key:
            return True

        return False

    def _play_video_and_handle_popups(self, on_transcript: Optional[Callable[[str], None]] = None):
        if self.stop_requested.is_set():
            return False
        video_info = self._get_video_info()
        if not video_info:
            print("      未找到视频元素")
            return False

        video_url = video_info.get('url', '')
        duration = video_info.get('duration', 0)

        if video_url != self.current_video_url:
            self.current_video_url = video_url
            self.video_transcript = ""
        if self.video_transcript:
            print(f"      使用已缓存的视频转录（{len(self.video_transcript)}字符）")
        elif not self.skip_transcription:
            self.video_transcript = self._transcribe_video(video_url, duration)
        if self.stop_requested.is_set():
            return False
        if on_transcript:
            try:
                on_transcript(self.video_transcript)
            except Exception as exc:
                print(f"      预取答案启动失败: {str(exc)[:60]}")
        self.stop_monitoring.clear()
        handling_popup = getattr(self, '_handling_popup', None)
        if handling_popup is not None:
            handling_popup.clear()
        self.popup_monitor_thread = threading.Thread(
            target=self._monitor_popup_questions,
            daemon=True
        )
        self.popup_monitor_thread.start()

        try:
            completed = self._play_video(duration)
            if completed and not self.stop_requested.is_set():
                print("      视频播放完成")
                return True
            return False
        finally:
            self.stop_monitoring.set()
            if self.popup_monitor_thread.is_alive():
                self.popup_monitor_thread.join(timeout=1)

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VIDEO

    def handle(self, question: Question) -> bool:
        if self._consume_preprocessed_played():
            print("     视频已在预处理阶段播放完成，跳过重复播放")
            return True

        if self._check_video_completed():
            print("     视频已标记为完成，跳过")
            return True

        print("     视频页面，开始处理...")
        return self._play_video_and_handle_popups()

    def _get_video_info(self) -> Optional[Dict]:
        try:
            video = self.driver.find_element(By.TAG_NAME, 'video')
            url = video.get_attribute('src') or ''

            if not url:
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    url = source.get_attribute('src')
                    if url:
                        break

            duration = self.driver.execute_script("return arguments[0].duration;", video)

            return {
                'url': url,
                'duration': duration or 0,
                'element': video
            }
        except:
            return None

    def _transcribe_video(self, video_url: str, duration: float) -> str:
        if not video_url:
            return ""

        print(f"     开始识别视频音频（时长: {int(duration)}秒）...")

        try:
            transcript = self.transcriber.transcribe(video_url, language="en")

            if transcript:
                preview = transcript[:200] + "..." if len(transcript) > 200 else transcript
                print(f"     识别成功: {preview}")
                return transcript
            else:
                print("     未能识别音频内容")
                return ""

        except Exception as e:
            print(f"     音频识别失败: {str(e)[:50]}")
            return ""

    def ensure_transcript(self, video_url: str = "") -> str:
        url = video_url or self.current_video_url
        if not url:
            info = self._get_video_info() or {}
            url = info.get('url', '')
        url = (url or '').split('#')[0]
        if not url:
            return ""
        with self._transcript_lock:
            if url != self.current_video_url.split('#')[0]:
                self.video_transcript = ""
            if self.video_transcript:
                return self.video_transcript
            self.current_video_url = url
            print("     补做视频转写...")
            self.video_transcript = self._transcribe_video(url, 0)
            return self.video_transcript

    def _play_video(self, duration: float):
        video = None
        try:
            video = self.driver.find_element(By.TAG_NAME, 'video')

            if duration > 0:
                print(f"      【播放】开始播放视频（{int(duration)} 秒，2 倍速）...")
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)

                return self._wait_for_video_complete(video, duration)
            else:
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)
                return self._wait_for_video_complete(video, duration)

        except Exception as e:
            print(f"       视频播放失败: {str(e)[:50]}")
            return False

        finally:
            if self.stop_requested.is_set() and video is not None:
                try:
                    self.driver.execute_script("arguments[0].pause();", video)
                except Exception:
                    pass

    def _monitor_popup_questions(self):
        print("      [监视器] 开始监视弹窗...")
        check_interval = 0.5
        processed_popups = set()

        while not self.stop_monitoring.is_set() and not self.stop_requested.is_set():
            try:
                popup = self._find_popup_question()

                if popup and popup.is_displayed():
                    popup_id = self._get_popup_id(popup)

                    if popup_id in processed_popups:
                        if self.stop_requested.wait(0.5):
                            break
                        continue

                    print("      [监视器]  检测到新弹窗题目！")
                    handling_popup = getattr(self, '_handling_popup', None)
                    if handling_popup is not None:
                        handling_popup.set()
                    try:
                        question_data = self._parse_popup_question(popup)

                        if not question_data:
                            print("      [监视器]  未能解析题目")
                            continue

                        if question_data['options'] and not self.video_transcript:
                            self.ensure_transcript()
                        if self.video_transcript and question_data['options']:
                            answer = self._intelligent_select_answer(question_data)
                        else:
                            answer = self._random_select(question_data)
                            print(f"      [监视器]  随机选择: {answer}")

                        if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                            break
                        success = self._click_option(popup, answer)

                        if success:
                            print(f"      [监视器]  已选择: {answer}")
                            processed_popups.add(popup_id)
                            if self.stop_requested.wait(0.5) or self.stop_monitoring.is_set():
                                break
                            self._click_submit_if_exists(popup)
                            self.stop_requested.wait(1.0)
                            if not self.stop_requested.is_set() and not self.stop_monitoring.is_set():
                                remaining = self._find_popup_question()
                                if not (remaining and remaining.is_displayed()):
                                    if self._resume_video_if_paused():
                                        print("      [监视器]  弹窗处理后已恢复视频播放")
                        else:
                            print(f"      [监视器]  点击失败: {answer}")
                    finally:
                        if handling_popup is not None:
                            handling_popup.clear()

            except Exception as e:
                pass

            self.stop_monitoring.wait(check_interval)

        print("      [监视器] 已停止")

    def _find_popup_question(self) -> Optional[Any]:
        selectors = [
            '.video-box .popupBox .questionReplyBox',
            '.popupBox .question-common-abs-choice',
            '.questionReplyBox .question-common-abs-choice',
            '.video-popup .question-common-abs-choice',
            '.popupBox:has(.option)',
        ]

        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elements:
                    if elem.is_displayed():
                        options = elem.find_elements(By.CSS_SELECTOR, '.option, .option-wrap .option')
                        if len(options) >= 2:
                            return elem
            except:
                continue
        return None

    def _get_popup_id(self, popup) -> str:
        try:
            text = popup.text
            return hashlib.md5(text[:200].encode()).hexdigest()[:16]
        except:
            return str(time.time())

    def _parse_popup_question(self, popup) -> Optional[Dict]:
        try:
            title_selectors = ['.ques-title', '.question-title', '.title', '.question-stem']
            title = ""
            for sel in title_selectors:
                try:
                    elem = popup.find_element(By.CSS_SELECTOR, sel)
                    title = elem.text.strip()
                    if title:
                        break
                except:
                    continue

            option_elems = popup.find_elements(By.CSS_SELECTOR,
                                               '.option.isNotReview, .option-wrap .option, .choice-option')

            options = []
            for i, opt_elem in enumerate(option_elems):
                try:
                    letter_selectors = ['.caption', '.index', '.option-label', '.choice-label']
                    letter = ""
                    for sel in letter_selectors:
                        try:
                            letter_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            letter = letter_elem.text.strip().replace('.', '').replace(')', '').upper()
                            if letter:
                                break
                        except:
                            continue

                    if not letter:
                        letter = chr(65 + i)

                    content_selectors = ['.content', '.option-content', '.text', '.choice-text']
                    content = ""
                    for sel in content_selectors:
                        try:
                            content_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            content = content_elem.text.strip()
                            if content:
                                break
                        except:
                            continue

                    if not content:
                        content = opt_elem.text.strip()

                    options.append({
                        'letter': letter,
                        'text': content,
                        'element': opt_elem
                    })

                except:
                    continue

            if not options:
                return None

            return {
                'question': title,
                'options': options
            }

        except Exception as e:
            print(f"      [监视器] 解析失败: {str(e)[:50]}")
            return None

    @staticmethod
    def _extract_popup_answer(answer_text: str, valid_letters: List[str]) -> Optional[str]:
        valid = [letter.upper() for letter in valid_letters if letter]
        if not valid:
            return None
        text = (answer_text or "").strip().upper()
        if not text:
            return None
        cleaned = re.sub(r'^[\s*`"\'()（）\[\]【】]+|[\s*`"\'()（）\[\]【】.。,:：!！]+$', '', text)
        if cleaned in valid:
            return cleaned
        for pattern in (
            r'(?:答案|选项|选择|CORRECT\s+ANSWER|FINAL\s+ANSWER|ANSWER)\s*(?:IS|为|是)?\s*[:：]?\s*[\(\[（【*]?\s*([A-H])\b',
            r'\b([A-H])\b',
        ):
            matches = [m for m in re.findall(pattern, text) if m in valid]
            if matches:
                return matches[-1]
        return None

    def _intelligent_select_answer(self, question_data: Dict) -> str:
        question = question_data['question']
        options = question_data['options']

        if self.analyzer_client is None:
            print("      [监视器] 未配置 AI 接口，使用关键词匹配")
            return self._keyword_match(question, options)

        print(f"      [监视器]  分析问题: {question[:50]}...")
        prompt = self._build_analysis_prompt(question, options)

        configured_model = getattr(self.config, 'model', '') or 'kimi-k2-turbo-preview'
        configured_temp = getattr(self.config, 'temperature', 0.1)
        try:
            configured_max = int(getattr(self.config, 'max_tokens', 2048) or 2048)
        except (TypeError, ValueError):
            configured_max = 2048
        popup_max_tokens = min(max(configured_max, 512), 2048) if configured_max > 0 else 1024

        try:
            response = self.analyzer_client.chat.completions.create(
                model=configured_model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是视频理解助手。根据视频内容选择最正确的答案，只返回选项字母，不要解释。"
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=configured_temp,
                max_tokens=popup_max_tokens
            )

            answer_text = (response.choices[0].message.content or "").strip().upper()

            valid_letters = [opt['letter'] for opt in options]
            letter = self._extract_popup_answer(answer_text, valid_letters)
            if letter:
                print(f"      [监视器]  AI选择: {letter}")
                return letter

            return self._keyword_match(question, options)

        except Exception as e:
            print(f"      [监视器]  AI分析失败: {str(e)[:50]}，使用关键词匹配")
            return self._keyword_match(question, options)

    def _build_analysis_prompt(self, question: str, options: List[Dict]) -> str:
        transcript = self.video_transcript[:2000] if len(self.video_transcript) > 2000 else self.video_transcript

        prompt = f"""【视频内容】
        {transcript}

        【问题】
        {question}

        【选项】
        """
        for opt in options:
            prompt += f"{opt['letter']}. {opt['text']}\n"

        prompt += """
        【任务】
        根据视频内容，选择最正确的答案。只返回选项字母（如：A 或 B），不要任何解释。

        答案："""

        return prompt

    def _keyword_match(self, question: str, options: List[Dict]) -> str:
        transcript_lower = self.video_transcript.lower()
        question_lower = question.lower()

        best_option = None
        best_score = -1

        for opt in options:
            opt_text = opt['text'].lower()
            score = 0
            score += transcript_lower.count(opt_text) * 2

            keywords = [w for w in opt_text.split() if len(w) > 3]
            for kw in keywords:
                if kw in transcript_lower:
                    score += 1

            if any(word in question_lower for word in opt_text.split()[:3]):
                score += 3

            if score > best_score:
                best_score = score
                best_option = opt

        if best_option:
            print(f"      [监视器]  关键词匹配: {best_option['letter']} (得分: {best_score})")
            return best_option['letter']

        return options[0]['letter'] if options else "A"

    def _random_select(self, question_data: Dict) -> str:
        options = question_data.get('options', [])
        if not options:
            return "C"
        choice = random.choice(options)
        return choice['letter']

    def _click_option(self, popup, answer: str) -> bool:
        try:
            option_elems = popup.find_elements(By.CSS_SELECTOR,
                                               '.option.isNotReview, .option-wrap .option')

            for opt_elem in option_elems:
                try:
                    letter_selectors = ['.caption', '.index', '.option-label']
                    letter = ""
                    for sel in letter_selectors:
                        try:
                            letter_elem = opt_elem.find_element(By.CSS_SELECTOR, sel)
                            letter = letter_elem.text.strip().replace('.', '').replace(')', '').upper()
                            if letter:
                                break
                        except:
                            continue

                    if letter == answer.upper():
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                            opt_elem
                        )
                        time.sleep(0.2)

                        try:
                            opt_elem.click()
                        except:
                            self.driver.execute_script("arguments[0].click();", opt_elem)

                        return True

                except:
                    continue

            try:
                idx = ord(answer.upper()) - ord('A')
                if 0 <= idx < len(option_elems):
                    opt_elem = option_elems[idx]
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                        opt_elem
                    )
                    time.sleep(0.2)
                    opt_elem.click()
                    return True
            except:
                pass

            return False

        except Exception as e:
            print(f"      [监视器] 点击失败: {str(e)[:50]}")
            return False

    def _click_submit_if_exists(self, popup):
        submit_selectors = [
            '.submit-btn', '.confirm-btn', '.ok-btn',
            'button[type="submit"]', '.popup-submit',
            '.questionReplyBox .submit'
        ]

        for selector in submit_selectors:
            try:
                btn = popup.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    btn.click()
                    print("      [监视器]  已提交")
                    return True
            except:
                continue
        return False

    def _resume_video_if_paused(self, video=None) -> bool:
        try:
            target = video if video is not None else self.driver.find_element(By.TAG_NAME, 'video')
            resumed = self.driver.execute_script("""
                const v = arguments[0];
                if (!v || v.ended || !v.paused) return false;
                v.playbackRate = 2.0;
                v.muted = true;
                const p = v.play();
                if (p && typeof p.catch === 'function') p.catch(() => {});
                return true;
            """, target)
            return bool(resumed)
        except Exception:
            return False

    def _wait_for_video_complete(self, video, duration: float):
        max_wait = (duration / 2 + 30) if duration and duration > 0 else 60
        start_time = time.time()
        last_progress = 0
        last_current = None
        stall_since = None
        popup_paused_total = 0.0
        max_popup_wait = 300.0
        popup_pause_start = None
        handling_popup = getattr(self, '_handling_popup', None)

        while True:
            now = time.time()
            if handling_popup is not None and handling_popup.is_set():
                if popup_pause_start is None:
                    popup_pause_start = now
                if popup_paused_total + (now - popup_pause_start) < max_popup_wait:
                    if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                        break
                    if self.stop_requested.wait(0.5):
                        break
                    continue
            elif popup_pause_start is not None:
                popup_paused_total += min(now - popup_pause_start, max(0.0, max_popup_wait - popup_paused_total))
                popup_pause_start = None
                stall_since = now

            if now - start_time - popup_paused_total >= max_wait:
                break

            try:
                if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                    break

                current = self.driver.execute_script("return arguments[0].currentTime;", video)
                ended = self.driver.execute_script("return arguments[0].ended;", video)

                if ended:
                    print(f"       视频播放完成")
                    return True

                if not duration or duration <= 0:
                    try:
                        detected = self.driver.execute_script("return arguments[0].duration;", video)
                        if isinstance(detected, (int, float)) and detected > 0:
                            duration = float(detected)
                            max_wait = duration / 2 + 30
                    except Exception:
                        pass

                if last_current is None or current != last_current:
                    last_current = current
                    stall_since = now
                elif stall_since is not None and now - stall_since >= 3:
                    if handling_popup is None or not handling_popup.is_set():
                        popup = None
                        try:
                            popup = self._find_popup_question()
                        except Exception:
                            popup = None
                        if not (popup and popup.is_displayed()):
                            if self._resume_video_if_paused(video):
                                print("      检测到视频已暂停，已尝试恢复播放")
                    stall_since = now

                elapsed = int(time.time() - start_time - popup_paused_total)
                if elapsed - last_progress >= 5:
                    print(f"      播放进度: {int(current)}/{int(duration)} 秒")
                    last_progress = elapsed

                if self.stop_requested.wait(0.5):
                    break

            except:
                break

        return False

    def _check_video_completed(self) -> bool:
        try:
            indicators = [
                '.video-completed', '.watched', '.finished',
                '[class*="completed"]', '[class*="finished"]'
            ]
            for indicator in indicators:
                if self.driver.find_elements(By.CSS_SELECTOR, indicator):
                    return True
            return False
        except:
            return False


class FlashcardHandler(ContentHandler):

    def __init__(self, driver, stop_requested: threading.Event):
        self.driver = driver
        self.stop_requested = stop_requested

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VOCABULARY_FLASHCARD

    def handle(self, question: Question) -> bool:
        print("     处理单词闪卡...")
        max_cards = 100
        clicked = 0
        if self.stop_requested.wait(2):
            return False
        for i in range(max_cards):
            if self.stop_requested.is_set():
                return False
            try:
                next_btn = self._find_next_button()
                if not next_btn:
                    print(f"      未找到下一个按钮，可能已完成（已点击{clicked}个）")
                    break
                if not next_btn.is_displayed() or not next_btn.is_enabled():
                    print(f"      按钮不可用，完成")
                    break
                try:
                    disabled_next_button = self.driver.find_element(By.CSS_SELECTOR, '.action.next.disabled')
                    if disabled_next_button:
                        break
                except:
                    pass
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    next_btn
                )
                if self.stop_requested.wait(0.5):
                    return False
                try:
                    next_btn.click()
                except:
                    self.driver.execute_script("arguments[0].click();", next_btn)
                clicked += 1
                if self.stop_requested.wait(0.5):
                    return False
                current_word = self.driver.find_element(By.XPATH,
                                                        '//*[@id="question-vocabulary-base-id"]/div/div[2]/div')
                print(f" 学习{current_word.text}")
            except Exception as e:
                error_msg = str(e)
                print(f"      处理闪卡失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                if self.stop_requested.wait(1):
                    return False
                continue

        print(f"     单词闪卡完成，共 {clicked} 个")
        return True

    def _find_next_button(self):
        selectors = [
            '.vocActions .next',
            '.action.next',
            '.next-btn',
            '.vocabulary-actions .next',
            'button.next',
            '.flashcard-next',
            '[class*="next"]:not([class*="disabled"])',
        ]

        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elems:
                    if elem.is_displayed():
                        return elem
            except:
                continue
        return None


PAGE_SCORE_JS = r"""
    const clean = t => (t || '').replace(/\s+/g, ' ').trim();
    const visible = e => !!(e && e.getClientRects().length);
    const body = clean(document.body.innerText);

    const summary = body.match(/(?:得分|成绩|分数|总分)\s*[:：]?\s*(\d{1,3}(?:\.\d+)?)/)
                 || body.match(/(\d{1,3}(?:\.\d+)?)\s*分/);
    if (summary) return parseFloat(summary[1]);
    const scores = [];
    for (const el of document.querySelectorAll('.score_layout')) {
        if (!visible(el)) continue;
        const hit = clean(el.innerText).match(/(\d{1,3}(?:\.\d+)?)/);
        if (hit) scores.push(parseFloat(hit[1]));
    }
    if (!scores.length) return null;
    if (scores.length === 1) return scores[0];

    return Math.round(scores.reduce((a, b) => a + b, 0) / scores.length * 10) / 10;
"""


class AnswerPrefetch:

    def __init__(self, client, prompt: str, screenshot: Optional[bytes],
                 stop_requested: threading.Event, require_image: bool = False):
        self.client = client
        self.materials = tuple((m['role'], m['content']) for m in client.material_messages)
        self.prompt = prompt
        self.screenshot = screenshot
        self.require_image = require_image
        self.stop_requested = stop_requested
        self.answer: Optional[str] = None
        self.error: Optional[Exception] = None
        self.started = time.monotonic()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self.answer = self.client.ask(
                self.prompt, stop_requested=self.stop_requested,
                screenshot=self.screenshot, remember=False,
                require_image=self.require_image)
        except Exception as exc:
            self.error = exc
        finally:
            self._done.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def wait(self, timeout: Optional[float] = None) -> Optional[str]:
        self._done.wait(timeout)
        return self.answer


def _format_webdriver_error(exc: Exception, driver=None) -> str:
    raw = str(exc or '').split('Stacktrace:')[0].strip()
    lines = []
    for line in raw.splitlines():
        cleaned = re.sub(r'^(?:Message:\s*)+', '', line.strip()).strip()
        if cleaned:
            lines.append(cleaned)
    detail = ' '.join(lines)
    base = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    if driver is None:
        return base
    state = describe_directory_page_state(driver)
    parts = [base]
    body_head = state.get('body_head') if isinstance(state, dict) else ''
    url = state.get('url') if isinstance(state, dict) else ''
    if isinstance(body_head, str) and body_head.strip():
        parts.append(f"页面={body_head.strip()[:50]}")
    if isinstance(url, str) and url.strip():
        parts.append(f"url={url.strip()[:100]}")
    return ' | '.join(parts)


def _wait_for_unit_container(driver, course_home_url: str, stop_requested=None,
                             max_attempts: int = 3, per_attempt_timeout: int = 18):
    last_summary = ''
    for attempt in range(1, max_attempts + 1):
        if stop_requested is not None and stop_requested.is_set():
            raise TimeoutException('单元切换已停止')
        if attempt > 1 and course_home_url:
            driver.get(course_home_url)
            if stop_requested is not None and stop_requested.wait(1.0):
                raise TimeoutException('单元切换已停止')
        try:
            container = WebDriverWait(driver, per_attempt_timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, '[class*="unitTabScrollContainer"]'))
            )
            children = container.find_elements(By.CSS_SELECTOR, ':scope > *')
            if isinstance(children, (list, tuple)) and len(children) == 0:
                for _ in range(10):
                    if stop_requested is not None and stop_requested.is_set():
                        raise TimeoutException('单元切换已停止')
                    if stop_requested is not None:
                        if stop_requested.wait(0.3):
                            raise TimeoutException('单元切换已停止')
                    else:
                        time.sleep(0.3)
                    children = container.find_elements(By.CSS_SELECTOR, ':scope > *')
                    if not isinstance(children, (list, tuple)) or len(children) > 0:
                        break
            if not isinstance(children, (list, tuple)) or len(children) > 0:
                return container
            raise TimeoutException('unitTabScrollContainer 已出现但 Unit 列表为空')
        except TimeoutException:
            if stop_requested is not None and stop_requested.is_set():
                raise
            state = describe_directory_page_state(driver)
            body_head = state.get('body_head') if isinstance(state, dict) else ''
            last_summary = (body_head or state.get('title') or state.get('url') or '未知状态')[:60]
            if attempt < max_attempts:
                print(f"    课程目录未加载完成（第 {attempt}/{max_attempts} 次，页面状态: {last_summary}），"
                      f"重新加载目录页...")
    raise TimeoutException(f"课程目录加载超时（已尝试 {max_attempts} 次，页面状态: {last_summary}）")


def _dismiss_known_info_overlays(driver) -> bool:
    if driver is None:
        return False
    try:
        clicked = driver.execute_script("""
            const vis = el => !!el && !!el.getClientRects().length;
            let hit = false;
            for (const sel of ['.system-info-cloud-ok-button', '.know-box .iKnow']) {
                for (const btn of document.querySelectorAll(sel)) {
                    if (vis(btn)) {
                        btn.click();
                        hit = true;
                    }
                }
            }
            return hit;
        """)
        return clicked is True
    except Exception:
        return False


def _install_submit_network_hook(driver) -> None:
    if driver is None:
        return
    try:
        driver.execute_script("""
            const isTarget = url => /\\/(?:submit|grade\\/content)(?:[/?#]|$)/i.test(String(url || ''));
            if (!window.__unipusSubmitMonitor) {
                const state = {
                    reqCount: 0,
                    inFlight: 0,
                    gradingPending: false,
                    submitSucceeded: false,
                    lastError: '',
                    reset() {
                        this.reqCount = 0;
                        this.inFlight = 0;
                        this.gradingPending = false;
                        this.submitSucceeded = false;
                        this.lastError = '';
                    }
                };
                const inspectResponse = (url, status, bodyText) => {
                    const u = String(url || '');
                    const txt = String(bodyText || '');
                    if (status >= 400) {
                        state.lastError = 'HTTP ' + status + ' ' + u.slice(-60);
                        state.gradingPending = false;
                        return;
                    }
                    if (/icorrectId/i.test(txt) || /\\\\?"GradeStatus\\\\?":\\s*0/.test(txt)) {
                        state.gradingPending = true;
                        return;
                    }
                    if (/\\\\?"GradeStatus\\\\?":\\s*1/.test(txt)) {
                        state.gradingPending = false;
                        return;
                    }
                    if (/\\/submit(?:[/?#]|$)/i.test(u) && /"code"\\s*:\\s*0/.test(txt)) {
                        state.gradingPending = false;
                        state.submitSucceeded = true;
                        return;
                    }
                    if (/\\/grade\\/content/i.test(u) && /"code"\\s*:\\s*10/.test(txt)) {
                        state.gradingPending = true;
                    }
                };
                if (typeof window.fetch === 'function') {
                    const origFetch = window.fetch;
                    window.fetch = function(...args) {
                        const raw = args[0];
                        const url = String((raw && raw.url) || raw || '');
                        if (!isTarget(url)) return origFetch.apply(this, args);
                        state.reqCount += 1;
                        state.inFlight += 1;
                        return origFetch.apply(this, args).then(res => {
                            try {
                                res.clone().text().then(txt => {
                                    inspectResponse(url, res.status, txt);
                                }).catch(() => {});
                            } catch (_) {}
                            state.inFlight = Math.max(0, state.inFlight - 1);
                            return res;
                        }, err => {
                            state.inFlight = Math.max(0, state.inFlight - 1);
                            state.lastError = String((err && err.message) || err || 'fetch error').slice(0, 80);
                            throw err;
                        });
                    };
                }
                const origOpen = XMLHttpRequest.prototype.open;
                const origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                    this.__unipusHookUrl = String(url || '');
                    return origOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function() {
                    const url = this.__unipusHookUrl || '';
                    if (isTarget(url)) {
                        state.reqCount += 1;
                        state.inFlight += 1;
                        this.addEventListener('loadend', () => {
                            state.inFlight = Math.max(0, state.inFlight - 1);
                            inspectResponse(url, this.status, this.responseText);
                        });
                    }
                    return origSend.apply(this, arguments);
                };
                window.__unipusSubmitMonitor = state;
            }
            window.__unipusSubmitMonitor.reset();
        """)
    except Exception:
        pass


def _read_submit_network_state(driver) -> Optional[Dict[str, Any]]:
    if driver is None:
        return None
    try:
        res = driver.execute_script("""
            const m = window.__unipusSubmitMonitor;
            if (!m) return null;
            return {
                reqCount: Number(m.reqCount || 0),
                inFlight: Number(m.inFlight || 0),
                gradingPending: !!m.gradingPending,
                submitSucceeded: !!m.submitSucceeded,
                lastError: String(m.lastError || '')
            };
        """)
        return res if isinstance(res, dict) else None
    except Exception:
        return None


def _read_submit_progress_toast(driver) -> str:
    if driver is None:
        return ''
    try:
        res = driver.execute_script("""
            const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
            const vis = el => !!el && !!el.getClientRects().length;
            return [...document.querySelectorAll('.ant-message-notice, .ant-spin-text')]
                .filter(vis)
                .map(el => clean(el.innerText))
                .filter(Boolean)
                .join(' | ');
        """)
        return res if isinstance(res, str) else ''
    except Exception:
        return ''


def _describe_submit_timeout_state(driver, net_state: Optional[Dict[str, Any]] = None) -> str:
    parts = []
    if isinstance(net_state, dict):
        parts.append(
            f"xhr(req={net_state.get('reqCount', 0)}, inFlight={net_state.get('inFlight', 0)}, "
            f"grading={net_state.get('gradingPending', False)})"
        )
        if net_state.get('lastError'):
            parts.append(f"err={net_state['lastError']}")
    if driver is None:
        return ' | '.join(parts)
    try:
        dom = driver.execute_script("""
            const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
            const vis = el => !!el && !!el.getClientRects().length;
            const btns = [...document.querySelectorAll('button, a.btn, .btn')].filter(vis).slice(0, 6).map(b =>
                clean(b.innerText).slice(0, 20) + '[' + String(b.className || '').slice(0, 30) + ']'
            );
            const modals = [...document.querySelectorAll('[role="dialog"], .ant-modal, .ant-message-notice')]
                .filter(vis).slice(0, 2).map(m => clean(m.innerText).slice(0, 60));
            return {btns, modals};
        """)
        if isinstance(dom, dict):
            btns = dom.get('btns') or []
            modals = dom.get('modals') or []
            if btns:
                parts.append(f"可见按钮={btns}")
            if modals:
                parts.append(f"可见弹窗={modals}")
    except Exception:
        pass
    return ' | '.join(parts)


class AISolver:

    def __init__(self, driver, config: Config):
        self.driver = driver
        self.config = config
        self.stop_requested = threading.Event()
        self.ai_client = OpenAICompatibleClient(self.config)
        self.parser = QuestionParser(driver)
        self.vocab_store = VocabularyStore()
        self.tts_engine = TTSEngine(
            getattr(config, 'tts_engine', 'auto') or 'auto',
            getattr(config, 'tts_voice', '') or '',
            getattr(config, 'tts_proxy', '') or '',
        )
        self.prompt_builder = PromptBuilder(self.ai_client, vocab_store=self.vocab_store)
        self.executor = AnswerExecutor(driver, tts=self.tts_engine,
                                       stop_requested=self.stop_requested)
        self.publish_parser = PublishQuestionParser(driver)
        self.publish_executor = PublishAnswerExecutor(driver, tts=self.tts_engine,
                                                      stop_requested=self.stop_requested)
        self.video_handler = VideoHandler(driver, self.config, self.stop_requested)
        self.content_handlers: List[ContentHandler] = [
            AudioHandler(driver, self.stop_requested),
            self.video_handler,
            FlashcardHandler(driver, self.stop_requested),
            SelfCheckHandler(driver, self.stop_requested),
            DiscussionBoardHandler(driver, self.ai_client, self.stop_requested),
        ]
        self.processed_hashes: set = set()
        self._processed_video_tabs: set = set()
        self._video_transcripts: Dict[str, str] = {}
        self._skipped_video_tabs: set = set()
        self._processed_audio_tabs: set = set()
        self._processed_audio_context = self.ai_client.material_messages
        self._publish_processed_groups: set = set()
        self._publish_scope: Optional[PublishTaskScope] = None
        self._publish_scope_url: str = ''
        self._retry_hint: str = ''
        self._last_question_types: set = set()
        self._last_tab_outcome: str = ''
        self.accuracy_controller = AccuracyController(
            getattr(config, 'target_accuracy_min', 100.0),
            getattr(config, 'target_accuracy_max', 100.0),
        )
        # 官方答案接口：命中则客观题直接用平台真值，未命中/失败一律回落 AI。
        self.answer_api = OfficialAnswerProvider(config, log=print)

    def _skip_record_mode(self) -> str:
        config = getattr(self, 'config', None)
        try:
            return normalize_skip_record_mode(getattr(config, 'skip_record_mode', 'off'))
        except ValueError:
            return 'off'

    def _accuracy_controller(self) -> AccuracyController:
        controller = getattr(self, 'accuracy_controller', None)
        if controller is None:
            config = getattr(self, 'config', None)
            if config is not None and hasattr(config, 'target_accuracy_min'):
                low = getattr(config, 'target_accuracy_min', 100.0)
                high = getattr(config, 'target_accuracy_max', 100.0)
                controller = AccuracyController(low, high)
            elif config is not None and hasattr(config, 'target_accuracy'):
                controller = AccuracyController.from_config_value(getattr(config, 'target_accuracy', 100))
            else:
                controller = AccuracyController(100.0, 100.0)
            self.accuracy_controller = controller
        return controller

    def _ai_available(self) -> bool:
        ai_client = getattr(self, 'ai_client', None)
        if ai_client is not None and hasattr(ai_client, 'enabled'):
            return bool(ai_client.enabled)
        config = getattr(self, 'config', None)
        if config is None:
            return True
        return bool(
            (getattr(config, 'api_key', '') or '').strip()
            and (getattr(config, 'base_url', '') or '').strip()
            and (getattr(config, 'model', '') or '').strip()
        )

    def _tab_failure_status(self) -> str:
        outcome = getattr(self, '_last_tab_outcome', '')
        if outcome == 'skipped_record':
            return 'skipped_record'
        if outcome == 'skipped_no_ai':
            return 'skipped_no_ai'
        if outcome == 'skipped':
            return 'skipped_no_answer'
        return 'error'

    def request_stop(self):
        self.stop_requested.set()

    def clear_stop(self):
        self.stop_requested.clear()

    def _should_stop(self) -> bool:
        return self.stop_requested.is_set()

    @staticmethod
    def _is_fatal_driver_error(exc: Exception) -> bool:
        if isinstance(exc, InvalidSessionIdException):
            return True
        if isinstance(exc, WebDriverException):
            msg = str(exc).lower()
            return any(token in msg for token in (
                'invalid session id', 'no such window', 'target window already closed',
                'disconnected', 'not connected to devtools', 'chrome not reachable',
                'unable to connect', 'connection refused', 'session deleted',
            ))
        return False

    def process_selected_tabs(self, selected_tabs: List[Dict], chapter_name: str = "",
                              on_task_done: Optional[Callable[[Dict], None]] = None,
                              on_task_skipped: Optional[Callable[[Dict], None]] = None):
        if not chapter_name:
            chapter_name = "selected_chapter"

        print(f"\n{'=' * 60}")
        print(f"开始处理 {len(selected_tabs)} 个选中任务")
        print(f"{'=' * 60}")

        self.ai_client.start_new_chapter(chapter_name)
        self.processed_hashes.clear()
        self._publish_processed_groups = set()
        self._accuracy_controller().reset()

        course_home_url = selected_tabs[0].get('_directory_url') if selected_tabs else ''
        course_home_url = course_home_url or self.driver.current_url

        done_count = 0
        skipped_tasks: List[str] = []
        failed_tasks: List[str] = []
        aborted = False

        for task_idx, tab in enumerate(selected_tabs):
            if self._should_stop():
                print("  已请求停止，批量处理提前结束")
                break

            task_label = tab.get('display') or tab.get('l1_title', '')
            print(f"\n  [{task_idx + 1}/{len(selected_tabs)}] {task_label}")
            try:
                status = self._process_selected_tab(tab, task_idx, len(selected_tabs), chapter_name, course_home_url)
            except Exception as exc:
                if AISolver._is_fatal_driver_error(exc):
                    print(f"  浏览器连接异常，已中断批量处理；剩余勾选保持原样: {str(exc)[:120]}")
                    logger.error(f"浏览器连接中断: {exc}", exc_info=True)
                    aborted = True
                    break
                print(f"  该任务处理异常，已保留勾选，继续处理剩余任务: {str(exc)[:120]}")
                logger.error(f"任务处理异常: {exc}", exc_info=True)
                status = 'error'

            if status == 'done':
                self._retry_hint = ''
                score = self._read_task_score()
                if score is None and getattr(self, '_last_question_types', set()) & {
                        QuestionType.RECORD, QuestionType.ORAL_ALOUD,
                        QuestionType.MY_VOICE_TEXT}:
                    for _ in range(15):
                        if self.stop_requested.wait(2):
                            break
                        score = self._read_task_score()
                        if score is not None:
                            print(f"  朗读评分已返回: {score:g}")
                            break
                retries = 0
                retry_status = status
                while score is not None and self._score_is_abnormal(score) and retries < 3:
                    retries += 1
                    hint = self._read_answer_analysis()
                    self._retry_hint = hint
                    print(f"  本次得分 {score:g} 偏低，"
                          f"{'使用答案解析' if hint else '用 AI 重做'}（第 {retries}/3 次）")
                    logger.warning("分数异常重试 %s: %.1f -> %s", tab.get('display'), score,
                                   '答案解析' if hint else 'AI 重做')
                    self.processed_hashes.clear()
                    if hasattr(self, '_publish_processed_groups'):
                        self._publish_processed_groups.clear()
                    try:
                        retry_status = self._process_selected_tab(
                            tab, task_idx, len(selected_tabs), chapter_name, course_home_url)
                    except Exception as exc:
                        logger.error(f"重做任务异常: {exc}", exc_info=True)
                        if AISolver._is_fatal_driver_error(exc):
                            aborted = True
                            retry_status = 'error'
                            break
                        retry_status = 'error'
                    if retry_status != 'done':
                        break
                    score = self._read_task_score()

                if retry_status != 'done':
                    print(f"  重做未完成（{retry_status}），保留任务状态，不标记完成")
                    logger.warning("分数异常重做未完成 %s: status=%s",
                                   tab.get('display'), retry_status)
                    if aborted:
                        print("  浏览器连接异常，已中断批量处理；剩余勾选保持原样")
                        break
                    if retry_status == 'stopped':
                        break
                    if retry_status == 'error':
                        self._retry_hint = ''
                        tab['status'] = 'error'
                        tab['status_text'] = '重做失败'
                        tab['score_abnormal'] = True
                        failed_tasks.append(task_label)
                        print("  重做未完成，已保留勾选，继续处理剩余任务")
                else:
                    done_count += 1
                    tab['status'] = 'done'
                    tab['status_text'] = '已完成'
                    tab['session_score'] = score
                    if score is not None:
                        tab['score'] = score
                        print(f"  本次得分 {score:g}")
                    if score is not None and self._score_is_abnormal(score):
                        tab['score_abnormal'] = True
                        logger.error("任务分数仍异常 %s: %.1f（已重试 %d 次）",
                                     tab.get('display'), score, retries)
                        print(f"  分数仍异常，标记红色并跳到下一题")
                    else:
                        tab['score_abnormal'] = False
                    if on_task_done:
                        try:
                            on_task_done(tab)
                        except Exception as exc:
                            print(f"  完成回调异常: {str(exc)[:60]}")
            elif status == 'skipped_no_answer':
                tab['skip_reason'] = 'no_answer'
                skipped_tasks.append(task_label)
                print("  已跳过无需作答页面并取消勾选，继续处理剩余任务")
                if on_task_skipped:
                    try:
                        on_task_skipped(tab)
                    except Exception as exc:
                        print(f"  跳过回调异常: {str(exc)[:60]}")
            elif status == 'skipped_record':
                tab['skip_reason'] = 'record'
                skipped_tasks.append(task_label)
                print("  已跳过跟读/录音任务并取消本次勾选（保留未完成状态），继续处理剩余任务")
                if on_task_skipped:
                    try:
                        on_task_skipped(tab)
                    except Exception as exc:
                        print(f"  跳过回调异常: {str(exc)[:60]}")
            elif status == 'skipped_no_ai':
                tab['skip_reason'] = 'no_ai'
                skipped_tasks.append(task_label)
                print("  未配置 AI 接口，已跳过需 AI 作答的任务并取消本次勾选（保留未完成状态），继续处理剩余任务")
                if on_task_skipped:
                    try:
                        on_task_skipped(tab)
                    except Exception as exc:
                        print(f"  跳过回调异常: {str(exc)[:60]}")
            elif status == 'skipped':
                skipped_tasks.append(task_label)
                print("  已跳过当前任务，继续处理剩余任务")
            elif status == 'stopped':
                break
            elif status == 'error':
                failed_tasks.append(task_label)
                print("  该任务未确认完成，已保留勾选，继续处理剩余任务")

            if self.stop_requested.wait(0.5):
                break

        self._release_transcriber()

        print(f"\n{'=' * 60}")
        if self._should_stop():
            print("批量处理已停止")
        elif aborted:
            print("已中断批量处理；剩余勾选保持原样，可直接从这里继续")
        else:
            print(f"全部 {len(selected_tabs)} 个任务处理完毕"
                  f"（完成 {done_count} / 跳过 {len(skipped_tasks)} / 未确认 {len(failed_tasks)}）")
        if skipped_tasks:
            print(f"  已跳过 {len(skipped_tasks)} 项: {'；'.join(skipped_tasks[:8])}"
                  f"{' …' if len(skipped_tasks) > 8 else ''}")
        if failed_tasks:
            print(f"  未确认完成 {len(failed_tasks)} 项（已保留勾选）: {'；'.join(failed_tasks[:8])}"
                  f"{' …' if len(failed_tasks) > 8 else ''}")
        print(f"{'=' * 60}")

    def _score_is_abnormal(self, score: float) -> bool:
        types = getattr(self, '_last_question_types', set())
        record_types = {QuestionType.RECORD, QuestionType.ORAL_ALOUD,
                        QuestionType.MY_VOICE_TEXT}
        controller = self._accuracy_controller()
        threshold = effective_score_threshold(
            self.config.score_threshold,
            controller.target_range,
            is_record_or_subjective=bool(types & record_types),
            record_score_threshold=self.config.record_score_threshold,
        )
        return score < threshold

    def _read_answer_analysis(self) -> str:
        js = """
        const nodes = Array.from(document.querySelectorAll(
            '[class*="analysis"], [class*="answer-detail"], [class*="correct-answer"], .answer-content'));
        const text = nodes.filter(n => n.offsetParent !== null).map(n => n.innerText || '').join('\\n');
        const match = text.match(/(正确答案|参考答案|答案解析)[\\s\\S]{0,1500}/);
        return match ? match[0].slice(0, 1500) : '';
        """
        try:
            value = self.driver.execute_script(js)
            return value.strip() if isinstance(value, str) else ''
        except Exception:
            return ''

    def _read_task_score(self) -> Optional[float]:
        try:
            value = self.driver.execute_script(PAGE_SCORE_JS)
        except Exception:
            return None
        if isinstance(value, (int, float)) and 0 <= value <= 100:
            return round(float(value), 1)
        return None

    def _process_selected_tab(self, tab: Dict, task_idx: int, total: int,
                              chapter_name: str, course_home_url: str) -> str:
        tab_name = tab.get('l1_title', 'unknown')

        if self._should_stop():
            return 'stopped'

        self._inline_learning_tried = False
        self._reading_ctx_tried = False
        self._pending_reading_context = None

        if '_element' in tab and tab['_element'] is not None:
            if task_idx > 0 or self.driver.current_url != course_home_url:
                self.driver.get(course_home_url)
                if self.stop_requested.wait(1.0):
                    return 'stopped'
                self.ai_client.force_reset(f"{chapter_name}_{tab_name}")

        section_text = ' '.join([
            str(tab.get('section') or ''),
            str(tab.get('l2_title') or ''),
            str(tab.get('display') or ''),
        ]).lower()
        if 'collocation' in section_text and tab.get('_unit_idx') is not None:
            # 延后到官方答案未命中、确实需要 AI 作答时再读取隔壁原文/Learning 页
            self._pending_reading_context = {
                'mode': 'directory',
                'directory_url': course_home_url,
                'unit_idx': tab.get('_unit_idx'),
                'section_filter': str(tab.get('section') or tab.get('l2_title') or ''),
            }

        if '_element' in tab and tab['_element'] is not None:
            if '_unit_idx' in tab:
                try:
                    unit_container = _wait_for_unit_container(
                        self.driver, course_home_url, self.stop_requested
                    )
                    unit_tabs = unit_container.find_elements(By.CSS_SELECTOR, ':scope > *')
                    if tab.get('_unit_id'):
                        unit_tabs = [unit for unit in unit_tabs
                                     if unit.get_attribute('id') == f"id-{tab['_unit_id']}-tab"]
                        unit_index = 0
                    else:
                        unit_index = tab['_unit_idx']
                    if unit_index < 0 or unit_index >= len(unit_tabs):
                        print("    目标Unit不存在，跳过")
                        return 'skipped'
                    if self._should_stop():
                        return 'stopped'
                    select_course_unit(self.driver, unit_tabs[unit_index], self.stop_requested)
                except Exception as e:
                    err_summary = _format_webdriver_error(e, self.driver)
                    print(f"    切换Unit失败: {err_summary}")
                    logger.warning(f"切换Unit失败 ({tab.get('display') or tab_name}): {err_summary}")
                    return 'skipped'

            chapter_clicked = False
            try:
                from unipus_helper.course_catalog import CourseCatalogMixin
                task_rows = self.driver.execute_script(CourseCatalogMixin.READ_TASKS_JS) or []
                expected_section = tab.get('_section_title', tab.get('section', ''))
                matching_indices = {row['index'] for row in task_rows
                                    if (row.get('name') or '').strip() == tab_name
                                    and (row.get('section') or '').strip() == expected_section}
                chapters = self.driver.find_elements(
                    By.CSS_SELECTOR, '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]'
                )
                name_occurrence = 0
                for dom_index, ch in enumerate(chapters):
                    if self._should_stop():
                        return 'stopped'
                    try:
                        if not ch.is_displayed():
                            continue
                        name_elem = ch.find_element(
                            By.CSS_SELECTOR, '[class*="taskTypeName"], [class*="nodeTypeName"]')
                        if name_elem.text.strip() == tab_name:
                            if name_occurrence == tab.get('_name_occurrence', 0):
                                if dom_index not in matching_indices:
                                    print("    任务所在章节与扫描结果不一致，跳过")
                                    return 'skipped'
                                self.driver.execute_script("arguments[0].click();", name_elem)
                                chapter_clicked = True
                                break
                            name_occurrence += 1
                    except Exception:
                        continue
            except Exception as e:
                print(f"    重新定位章节失败: {_format_webdriver_error(e, self.driver)}")

            if self._should_stop():
                return 'stopped'
            if not chapter_clicked:
                print("  点击章节失败，跳过")
                return 'skipped'
            if self.stop_requested.wait(3):
                return 'stopped'
            success = self._process_tab_with_accumulation(tab_name, task_idx, 0)
            if self._should_stop():
                return 'stopped'
            if success:
                return 'done'
            return self._tab_failure_status()

        l1_idx = tab.get('l1_idx', 0)
        l2_idx = tab.get('l2_idx', -1)

        level1_tabs = self._get_level1_tabs()
        if l1_idx >= len(level1_tabs):
            print(f"  一级Tab索引 {l1_idx} 越界，跳过")
            return 'skipped'

        l1_tab = level1_tabs[l1_idx]
        print(f"    一级Tab: {l1_tab['title']}")

        if self._should_stop():
            return 'stopped'
        if not WebDriverHelper.safe_click(self.driver, l1_tab['element']):
            print(f"  点击一级Tab失败，跳过")
            return 'skipped'
        if self.stop_requested.wait(1.5):
            return 'stopped'
        if l2_idx < 0:
            success = self._process_tab_with_accumulation(l1_tab['title'], l1_idx, 0)
            if self._should_stop():
                return 'stopped'
            if success:
                return 'done'
            return self._tab_failure_status()

        level2_tabs = self._get_level2_tabs()
        if l2_idx >= len(level2_tabs):
            print(f"  二级Tab索引 {l2_idx} 越界，跳过")
            return 'skipped'

        l2_tab = level2_tabs[l2_idx]
        print(f"    二级Tab: {l2_tab['title']}")

        if self._should_stop():
            return 'stopped'
        if not WebDriverHelper.safe_click(self.driver, l2_tab['element']):
            print(f"  点击二级Tab失败，跳过")
            return 'skipped'
        if self.stop_requested.wait(1.5):
            return 'stopped'
        combined_name = f"{l1_tab['title']}_{l2_tab['title']}"
        success = self._process_tab_with_accumulation(combined_name, l1_idx, l2_idx)
        if self._should_stop():
            return 'stopped'
        if success:
            return 'done'
        return self._tab_failure_status()

    def _answer_chunked_fill(self, q: Question, directions: str = "", chunk_size: int = 8) -> bool:
        total = len(q.inputs)
        if not total:
            return False
        filled = 0
        used_answers: List[str] = []
        print(f"    空位较多（{total} 个），拆成每 {chunk_size} 空一批作答")

        pending = [(start, min(start + chunk_size, total)) for start in range(0, total, chunk_size)]
        corpus = q.text or ""
        try:
            corpus += " " + " ".join(m.get('content', '') for m in self.ai_client.material_messages)
        except Exception:
            pass
        corpus_words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", corpus)
        while pending:
            if self._should_stop():
                return False
            start, end = pending.pop(0)
            sub_blanks = [dict(b) for b in q.banked_blanks[start:end]]
            for item in sub_blanks:
                hint = (item.get('hint') or '').strip()
                if not hint:
                    continue
                seen = []
                for word in corpus_words:
                    if word.lower().startswith(hint.lower()) and len(word) > len(hint):
                        if word.lower() not in [w.lower() for w in seen]:
                            seen.append(word)
                    if len(seen) >= 8:
                        break
                item['candidates'] = seen
            sub = Question(
                number=q.number,
                text=q.text,
                q_type=q.q_type,
                element=q.element,
                inputs=q.inputs[start:end],
                banked_blanks=sub_blanks,
                banked_options=q.banked_options,
                directions=q.directions,
            )
            prompt = self.prompt_builder.build([sub], directions)
            if not prompt:
                continue
            prompt += (f"\n本批只包含第 {start + 1}-{end} 空，请按这些空号作答，"
                       f"格式：{start + 1}.答案 {start + 2}.答案 ...")
            if used_answers and re.search(r'\b(?:only\s+once|once\s+only)\b', q.directions or '', re.I):
                prompt += ("\n【已用答案】前面批次已填入：" + "；".join(used_answers) +
                           "。词库里的每个表达只能用一次，本批不得重复使用，"
                           "请优先选取尚未使用的表达。")
            response = self.ai_client.ask(prompt, stop_requested=self.stop_requested,
                                          screenshot=self._question_screenshot([q]))
            if not response:
                if end - start > 2:
                    mid = (start + end) // 2
                    print(f"    第 {start + 1}-{end} 空无答案，拆成两批重试")
                    pending[0:0] = [(start, mid), (mid, end)]
                else:
                    retry_prompt = (f"【填空】请只回答第 {start + 1}-{end} 空，"
                                    f"每空一个答案（可以是多词短语），格式：{start + 1}.答案\n"
                                    f"{q.text[:1200]}")
                    for item in sub_blanks:
                        retry_prompt += f"\n空{item['index'] + 1}: 提示 {item.get('hint', '') or '无'}；前文 {item.get('context', '')[-80:]}"
                    response = self.ai_client.ask(retry_prompt, retry_count=1,
                                                  stop_requested=self.stop_requested)
                if not response:
                    print(f"    第 {start + 1}-{end} 空未拿到答案，跳过")
                    continue

            mapping = self._parse_numbered_map(response)
            chunk_answers = []
            chunk_hints = []
            for offset in range(len(sub.inputs)):
                global_no = start + offset + 1
                ans = mapping.get(global_no) or mapping.get(offset + 1) or ""
                hint = sub_blanks[offset].get('hint', '') if offset < len(sub_blanks) else ''
                chunk_answers.append(ans)
                chunk_hints.append(hint)
            original_answers = list(chunk_answers)
            adjusted_answers, _ = self._accuracy_controller().apply_to_blank_list(
                chunk_answers,
                hints=chunk_hints,
                banked_options=q.banked_options,
                enforce_submit_floor=False,
            )
            for offset, inp in enumerate(sub.inputs):
                global_no = start + offset + 1
                ans = adjusted_answers[offset]
                orig_ans = original_answers[offset]
                if not ans:
                    continue
                hint = chunk_hints[offset]
                ans = self.executor._strip_given_prefix(hint, ans)
                orig_clean = self.executor._strip_given_prefix(hint, orig_ans) if orig_ans else ans
                try:
                    if self.executor._fill_text_input_verified(inp, ans):
                        filled += 1
                        used_answers.append(orig_clean)
                except Exception as exc:
                    print(f"    空{global_no} 写入失败: {str(exc)[:50]}")
            print(f"    第 {start + 1}-{end} 空已处理（累计 {filled}/{total}）")

        return filled == total

    @staticmethod
    def _parse_numbered_map(text: str) -> Dict[int, str]:
        result: Dict[int, str] = {}
        if not (text or '').strip():
            return result
        marker = re.compile(r'(?:^|[\s,;，；])(?:空\s*)?(\d{1,2})\s*[.、:：)）]')
        matches = list(marker.finditer(text))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            value = text[match.end():end].strip().strip(' .,;，。；')
            if value:
                result.setdefault(int(match.group(1)), value)
        if not result:
            for line in (text or '').splitlines():
                cleaned = re.sub(r'^\s*(?:空\s*)?\d{1,2}\s*[.、:：)）]\s*', '', line)
                cleaned = cleaned.strip().strip(' .,;，。；')
                if cleaned:
                    result[len(result) + 1] = cleaned
        return result

    def _load_reading_context_for_current_page(self) -> bool:
        if getattr(self, '_reading_ctx_tried', False):
            return False
        self._reading_ctx_tried = True

        if self._load_learning_inline():
            return True

        url = self.driver.current_url or ""
        resource_id = ""
        try:
            query = parse_qs(urlsplit(url).query)
            resource_id = (query.get('courseResourceId') or [""])[0]
        except Exception:
            resource_id = ""
        if not resource_id:
            print("    搭配学习页：当前页没有 courseResourceId，跳过")
            return False

        unit_label = self._unit_label_from_page()
        if not unit_label:
            print("    搭配学习页：认不出当前单元，跳过")
            return False

        directory_url = f"https://ucloud.unipus.cn/app/cmgt/resource-detail/{resource_id}"
        print(f"    搭配学习页：当前单元 {unit_label}，准备读取同单元 Learning 页")
        try:
            self._context_reference_text = self.driver.find_element(By.TAG_NAME, 'body').text or ""
        except Exception:
            self._context_reference_text = ""
        return self._load_reading_context(directory_url, unit_label=unit_label)

    def _unit_label_from_page(self) -> str:
        try:
            body = self.driver.find_element(By.TAG_NAME, 'body').text or ""
        except Exception:
            return ""
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        head = body[:800] if body else ""
        breadcrumb = [part.strip() for part in head.split('>') if part.strip()]
        if not first_line or len(breadcrumb) < 2:
            breadcrumb = [part.strip() for part in first_line.split('>') if part.strip()]
        unit_title = breadcrumb[1] if len(breadcrumb) >= 2 else ""

        items = []
        try:
            items = self.driver.execute_script("""
                const out = [];
                for (const el of document.querySelectorAll('div, span, li, a')) {
                  const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (text.length > 80) continue;
                  if (!/^Unit\\s*\\d+\\b/i.test(text)) continue;
                  if (out.includes(text)) continue;
                  out.push(text);
                }
                return out;
            """) or []
        except Exception:
            items = []

        if unit_title:
            probe = re.sub(r'\W+', '', unit_title).lower()
            for item in items:
                match = re.match(r'(Unit\s*\d+)\s*(.*)', item, re.I)
                if not match:
                    continue
                title_part = re.sub(r'\W+', '', match.group(2) or '').lower()
                if probe and title_part and (title_part.startswith(probe[:10])
                                             or probe.startswith(title_part[:10])):
                    return match.group(1)
        if items:
            print(f"    搭配学习页：侧栏单元标题没对上（{unit_title or '无面包屑'}），候选: {items[:6]}")
        return ""

    def _ensure_reading_context_for_ai(self) -> bool:
        """按需加载隔壁 Reading / Learning 上下文；仅在未命中官方答案、准备走 AI 时触发。

        返回 True 表示在当前页内切换过 Learning/Practicing 标签，调用方需重新解析题目元素。
        """
        pending = getattr(self, '_pending_reading_context', None)
        if not pending:
            return False
        self._pending_reading_context = None
        self._inline_learning_switched = False
        mode = pending.get('mode')
        if mode == 'current_page':
            self._load_reading_context_for_current_page()
        elif mode == 'directory':
            try:
                self._context_reference_text = self.driver.find_element(By.TAG_NAME, 'body').text or ""
            except Exception:
                self._context_reference_text = ""
            self._load_reading_context(
                pending.get('directory_url') or '',
                pending.get('unit_idx'),
                section_filter=pending.get('section_filter') or '',
            )
        return bool(getattr(self, '_inline_learning_switched', False))

    def _load_learning_inline(self) -> bool:
        if getattr(self, '_inline_learning_tried', False):
            return False
        self._inline_learning_tried = True
        try:
            tabs = [t for t in self.driver.find_elements(By.CSS_SELECTOR, '[class*="pc-header-tab"]')
                    if t.is_displayed()]
        except Exception:
            return False

        learning = next((t for t in tabs if (t.text or '').strip().lower() == 'learning'), None)
        practicing = next((t for t in tabs if (t.text or '').strip().lower() == 'practicing'), None)
        if learning is None:
            return False

        try:
            learning.click()
        except Exception:
            return False
        self._inline_learning_switched = True
        time.sleep(5)

        text = ""
        try:
            text = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                let text = clean(document.body.innerText);
                const cut = text.indexOf('目录笔记');
                if (cut > 200) text = text.slice(0, cut);
                return text.slice(0, 7000);
            """) or ""
        except Exception:
            text = ""

        if practicing is not None:
            try:
                practicing.click()
            except Exception:
                pass
            time.sleep(4)

        if len(text) < 120:
            print("    搭配学习页：Learning 页没抓到内容，跳过")
            return False

        self._inline_learning_chars = len(text)
        self.ai_client.add_passage_if_new(text)
        print(f"    搭配学习页：当前页切到 Learning 抓到 {len(text)} 字符，已加入上下文")
        return True

    def _load_reading_context(self, directory_url: str, unit_idx: Optional[int] = None,
                              unit_label: str = "", section_filter: str = "") -> bool:
        if self._load_learning_inline():
            if getattr(self, '_inline_learning_chars', 0) >= 400:
                return True
            print("    搭配学习页：Learning 内容太短，改用同单元 Reading the text 兜底")
        if not directory_url or (unit_idx is None and not unit_label):
            return False
        if (getattr(self, '_loaded_reading_units', None) is None
                or getattr(self, '_loaded_reading_context', None) is not self.ai_client.material_messages):
            self._loaded_reading_units = set()
            self._loaded_reading_context = self.ai_client.material_messages
        cache_key = (directory_url, unit_idx, (unit_label or '').lower(), section_filter.lower())
        if cache_key in self._loaded_reading_units:
            return True

        try:
            original = self.driver.current_window_handle
        except Exception:
            original = None

        try:
            self.driver.switch_to.new_window('tab')

            def open_unit():
                self.driver.get(directory_url)
                _wait_for_unit_container(
                    self.driver, directory_url, self.stop_requested,
                    max_attempts=2, per_attempt_timeout=18
                )
                time.sleep(1.0)
                tabs = self.driver.find_elements(By.CSS_SELECTOR, '[class*="unitTabScrollContainer"] > *')
                target = None
                if unit_label:
                    wanted = re.sub(r'\s+', '', unit_label).lower()
                    for tab in tabs:
                        if wanted and wanted in re.sub(r'\s+', '', (tab.text or '')).lower():
                            target = tab
                            break
                elif unit_idx is not None and unit_idx < len(tabs):
                    target = tabs[unit_idx]
                if target is None:
                    return False
                select_course_unit(self.driver, target, self.stop_requested)
                return True

            if not open_unit():
                print(f"    搭配学习页：目录里找不到 {unit_label or unit_idx}，跳过")
                return False

            candidates = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const all = [...document.querySelectorAll('[class*="taskItemInnerLayout"]')];
                const out = [];
                all.forEach((el, index) => {
                  if (!el.getClientRects().length) return;
                  const nameEl = el.querySelector('[class*="taskTypeName"]');
                  const name = clean(nameEl && nameEl.innerText).toLowerCase();
                  if (!name.includes('learning')) return;
                  const path = [];
                  let p = el;
                  while ((p = p.parentElement)) {
                    const head = p.querySelector(':scope > .ant-collapse-header');
                    if (head) path.unshift(clean(head.innerText));
                  }
                  if (!path.join(' / ').toLowerCase().includes('collocation')) return;
                  out.push({index, path: path.join(' / '), name});
                });
                return out;
            """) or []

            if section_filter:
                wanted = re.sub(r'\s+', '', section_filter).lower()
                filtered = [c for c in candidates
                            if wanted and wanted in re.sub(r'\s+', '', c.get('path', '')).lower()]
                if filtered:
                    candidates = filtered
                else:
                    print(f"    搭配学习页：没有 section 为「{section_filter}」的 Learning，改用全部候选")

            if not candidates:
                print("    搭配学习页：本单元没有 Collocation / Learning，跳过")
                return False
            print(f"    搭配学习页：本单元有 {len(candidates)} 个候选 Learning，按内容匹配挑一个")

            reference = self._context_reference_tokens()
            best_text, best_score, best_path = "", -1.0, ""
            for order, cand in enumerate(candidates[:3]):
                if order > 0 and not open_unit():
                    break
                clicked = self.driver.execute_script("""
                    const index = arguments[0];
                    const all = [...document.querySelectorAll('[class*="taskItemInnerLayout"]')];
                    const el = all[index];
                    if (!el) return false;
                    const nameEl = el.querySelector('[class*="taskTypeName"]');
                    let target = nameEl || el;
                    for (let i = 0; i < 3 && target; i++) { target.click(); target = target.parentElement; }
                    return true;
                """, cand['index'])
                if not clicked:
                    continue
                time.sleep(11)
                text = self.driver.execute_script("""
                    const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                    let text = clean(document.body.innerText);
                    const cut = text.indexOf('目录笔记');
                    if (cut > 200) text = text.slice(0, cut);
                    return text.slice(0, 7000);
                """) or ""
                if len(text) < 120:
                    continue
                score = self._text_overlap(reference, text)
                print(f"      候选 {cand['path']} → 重合度 {score:.2f}（{len(text)} 字符）")
                if score > best_score:
                    best_text, best_score, best_path = text, score, cand['path']
                if score >= 0.35:
                    break

            if not best_text:
                print("    搭配学习页：没抓到有效正文，跳过")
                return False

            self.ai_client.add_passage_if_new(best_text)
            print(f"    搭配学习页：采用 {best_path}（重合度 {best_score:.2f}，{len(best_text)} 字符）")
            self._loaded_reading_units.add(cache_key)
            return True
        except Exception as exc:
            print(f"    搭配学习页读取失败（不影响作答）: {str(exc)[:80]}")
            return False
        finally:
            try:
                if original:
                    self.driver.close()
                    self.driver.switch_to.window(original)
            except Exception:
                pass

    def _context_reference_tokens(self) -> set:
        text = getattr(self, '_context_reference_text', "") or ""
        if not text:
            try:
                text = self.driver.find_element(By.TAG_NAME, 'body').text or ""
            except Exception:
                text = ""
        return {w for w in re.findall(r"[A-Za-z]{5,}", text.lower())}

    @staticmethod
    def _text_overlap(reference: set, text: str) -> float:
        if not reference:
            return 0.0
        tokens = {w for w in re.findall(r"[A-Za-z]{5,}", (text or '').lower())}
        if not tokens:
            return 0.0
        return len(reference & tokens) / max(1, len(reference))

    def _vocab(self) -> VocabularyStore:
        store = getattr(self, 'vocab_store', None)
        if store is None:
            store = VocabularyStore()
            self.vocab_store = store
        return store

    def _tts(self) -> TTSEngine:
        engine = getattr(self, 'tts_engine', None)
        if engine is None:
            engine = TTSEngine()
            self.tts_engine = engine
        return engine

    def _needs_vocabulary(self, questions: List[Question]) -> bool:
        for q in questions or []:
            if getattr(q, 'needs_speech_script', False):
                return True
            if getattr(q, 'q_type', None) in (QuestionType.FILL_IN, QuestionType.BANKED_CLOZE,
                                              QuestionType.TEXT) and blank_cjk_hints(q):
                return True
        return False

    def _harvest_from_vocabulary_tab(self) -> Optional[bool]:
        if self._should_stop():
            return False
        try:
            tabs = self._get_level2_tabs()
        except Exception:
            return False
        if len(tabs) < 2:
            return False

        def title_of(tab: Dict) -> str:
            return str(tab.get('title') or '').strip().lower()

        vocab_tab = next((tab for tab in tabs if title_of(tab) == 'vocabulary'), None)
        if vocab_tab is None:
            return False
        current = next((tab for tab in tabs if tab.get('is_active')), None)
        if current is None or title_of(current) == 'vocabulary':
            return False

        print("    缓存里没有 Vocabulary 对照表，先切过去抓一遍...")
        if not WebDriverHelper.safe_click(self.driver, vocab_tab['element']):
            print("    切换 Vocabulary 失败，继续用模型作答")
            return False
        if self.stop_requested.wait(2.5):
            return None
        self._harvest_vocabulary()
        if not WebDriverHelper.safe_click(self.driver, current['element']):
            print("    切回原标签失败，请留意页面是否停在 Vocabulary")
            return None
        if self.stop_requested.wait(2.0):
            return None
        return True

    def _harvest_vocabulary(self) -> int:
        if self._should_stop():
            return 0
        try:
            rows = self.driver.execute_script(VOCAB_PAIRS_JS) or []
        except Exception as exc:
            logger.debug(f"读取词汇对照失败: {str(exc)[:80]}")
            return 0
        if not isinstance(rows, (list, tuple)):
            return 0
        added = self._vocab().add_rows(rows)
        if added:
            print(f"    词汇表更新 {added} 条（累计 {len(self._vocab())} 条）")
        return added

    def _speaking_script_for(self, q: Question, directions: str = "") -> str:
        if self._should_stop():
            return ""
        images = q.element.find_elements(
            By.XPATH,
            "ancestor::div[contains(@class,'p-oral-personal-state')][1]"
            "//div[contains(@class,'oral-personal-state-sentence-container')]//img")
        screenshot = None
        if images:
            if not self.config.screenshot:
                print("    口语题需要识别图片，请在配置中启用截图并使用支持图片的模型")
                return ""
            try:
                screenshot = images[0].screenshot_as_png
            except Exception:
                print("    口语题图片截图失败，停止作答")
                return ""
            if not screenshot:
                print("    口语题图片截图为空，停止作答")
                return ""
        prompt = self.prompt_builder.build_speaking_script(
            q, directions, target_seconds=self.executor.RECORD_MAX_SECONDS * 0.85)
        response = self.ai_client.ask(prompt, stop_requested=self.stop_requested,
                                      screenshot=screenshot, require_image=bool(images))
        if not response or self._should_stop():
            print(f"    题目 {q.number}: 没有拿到口语稿")
            return ""
        script = self._clean_speech_script(response)
        if not script:
            print(f"    题目 {q.number}: 口语稿清洗后为空")
        else:
            print(f"    题目 {q.number}: 口语稿 {len(script.split())} 词")
        return script

    @staticmethod
    def _clean_speech_script(text: str) -> str:
        value = (text or '').strip()
        value = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', value).strip()
        value = re.sub(r'^(口语稿|参考答案|Answer|Script)\s*[:：]\s*', '', value, flags=re.I)
        value = re.sub(r'[*_`#>]+', ' ', value)
        return re.sub(r'\s+', ' ', value).strip()

    def _repair_questions(self, questions: List[Question], directions: str = "") -> int:
        if not questions or self._should_stop():
            return 0
        prompt = self.prompt_builder.build(questions, directions)
        prompt += ("\n\n【修正要求】上一轮这些题没有填好：答案格式不符合题型或没有写进输入框。"
                   "请严格按题型作答——选项/匹配题只输出字母（如 1.B），填空题每空一个答案，"
                   "文本题给完整段落，不要解释、不要重复题号之外的文字。")
        response = self.ai_client.ask(
            prompt, stop_requested=self.stop_requested,
            screenshot=self._question_screenshot(questions),
            require_image=self._requires_image_context(questions))
        if not response or self._should_stop():
            return 0

        repaired = 0
        for q in questions:
            if self._should_stop():
                break
            if q.q_type in [QuestionType.SINGLE_CHOICE, QuestionType.LISTENING_CHOICE,
                            QuestionType.VIDEO_CHOICE, QuestionType.MULTIPLE_CHOICE,
                            QuestionType.MATCHING, QuestionType.VOCABULARY_TEST]:
                ans = self._extract_single_answer(response, q.number)
                if not ans:
                    ans = response
            elif q.q_type == QuestionType.SORTING and len(questions) > 1:
                ans = self._extract_single_answer(response, q.number)
            else:
                ans = response
            try:
                if ans and self.executor.execute(q, ans):
                    repaired += 1
            except Exception as exc:
                print(f"    修正题目 {q.number} 失败: {str(exc)[:60]}")
        return repaired

    def _is_skipped_record_only_page(self) -> bool:
        mode = self._skip_record_mode()
        if mode == 'off':
            return False
        try:
            questions, _ = self.parser.parse_all()
        except Exception:
            return False
        if not questions or self.parser.incomplete:
            return False
        return all(should_skip_record_question(q, mode) for q in questions)

    def _process_tab_with_accumulation(self, tab_name: str, l1_idx: int, l2_idx: int) -> bool:

        if self._should_stop():
            return False
        tab_key = f"{tab_name}|{l1_idx}|{l2_idx}"
        video_handler = getattr(self, 'video_handler', None)
        if video_handler is not None:
            if hasattr(video_handler, 'clear_preprocessed_played'):
                video_handler.clear_preprocessed_played()
            if hasattr(video_handler, 'set_active_tab'):
                video_handler.set_active_tab(tab_key)
        if self._is_publish_page():
            print("    检测到新版 ipublish 播放器，跳过旧版音视频预处理")
            return self._process_current_tab_content(
                self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx
            )
        if self._is_skipped_record_only_page():
            return self._process_current_tab_content(
                self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx
            )

        # 客观题优先尝试官方答案接口：命中或未配置 AI 时跳过音频转写、视频转写与 AI 后台预取
        self._answer_api_reset_tab()
        official_covers = self._answer_api_covers_current_page()
        if official_covers or not self._ai_available():
            if official_covers and (self._has_video_on_page() or self._has_audio_on_page()):
                print("    本页客观题已命中官方答案，跳过音视频转写与 AI 预取")
            if self._has_video_on_page():
                if not self._preprocess_video_if_needed(
                        tab_name, l1_idx, l2_idx, skip_transcription=True):
                    return False
                if self._should_stop():
                    return False
            return self._process_current_tab_content(
                self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx,
                api_prepared=True,
            )

        skip_asr = self._skip_transcription(tab_name)
        prefetch_holder = {}
        prefetch_signature = ""
        on_transcript = None
        prepared = self._prepare_media_prefetch(skip_asr)
        if prepared:
            all_questions, candidates, directions, prefetch_signature = prepared
        def on_transcript(transcript):
            nonlocal prepared, candidates, directions, prefetch_signature
            if not transcript or self._should_stop():
                return
            if not prepared:
                prepared = self._prepare_media_prefetch(False)
                if not prepared:
                    return
                _all, candidates, directions, prefetch_signature = prepared
            prefetch_holder['answer'] = self._start_answer_prefetch(candidates, directions)
        if not self._preprocess_video_if_needed(
                tab_name, l1_idx, l2_idx, skip_transcription=skip_asr,
                on_transcript=on_transcript):
            return False
        if self._should_stop():
            return False
        if not self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx,
                                               skip_transcription=skip_asr,
                                               on_transcript=on_transcript):
            return False
        if self._should_stop():
            return False

        current_passage = self._extract_passage()
        if self._should_stop():
            return False
        if current_passage:
            self.ai_client.add_passage_if_new(current_passage)

        return self._process_current_tab_content(
            self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx,
            prefetch=prefetch_holder.get('answer'),
            prefetch_signature=prefetch_signature,
            api_prepared=True,
        )

    def _prepare_media_prefetch(self, skip_asr: bool = False):
        if not (self._has_video_on_page() or self._has_audio_on_page()):
            return None
        try:
            questions, directions = self.parser.parse_all()
            if self.parser.incomplete:
                return None
        except Exception as exc:
            print(f"    预取解析题目失败: {str(exc)[:60]}")
            return None
        excluded = {
            QuestionType.VIDEO, QuestionType.AUDIO,
            QuestionType.VOCABULARY_FLASHCARD, QuestionType.SELF_CHECK,
            QuestionType.DISCUSSION_BOARD, QuestionType.RECORD,
            QuestionType.ORAL_ALOUD, QuestionType.MY_VOICE_TEXT,
        }
        if any(q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD,
                            QuestionType.MY_VOICE_TEXT) for q in questions):
            return None
        candidates = [q for q in questions if q.q_type not in excluded]
        if not candidates:
            return None
        large_fill = any(
            q.q_type in (QuestionType.FILL_IN, QuestionType.BANKED_CLOZE)
            and len(q.inputs) > 8 and not is_expression_bank_question(q)
            for q in candidates)
        if large_fill:
            return None
        signature = self._generate_questions_signature(questions)
        return questions, candidates, directions, signature

    def _start_answer_prefetch(self, questions, directions) -> Optional[AnswerPrefetch]:
        prompt = self.prompt_builder.build(questions, directions)
        if not prompt:
            return None
        shot = self._question_screenshot(questions)
        print(f"    后台预取答案（{len(questions)} 题，与视频播放并行）...")
        return AnswerPrefetch(self.ai_client, prompt, shot, self.stop_requested,
                              require_image=self._requires_image_context(questions))

    def _material_corpus(self) -> str:
        parts = []
        for msg in getattr(self.ai_client, 'material_messages', []) or []:
            text = msg.get('content', '') if isinstance(msg, dict) else ''
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        value = (text or '').lower().replace('’', "'").replace('‘', "'")
        value = re.sub(r'[^a-z0-9%$.\s-]', ' ', value)
        return re.sub(r'\s+', ' ', value).strip()

    def _reorder_sorting_by_transcript(self, q: Question, order: List[str]) -> List[str]:
        if not order:
            return order
        corpus = self._normalize_for_match(self._material_corpus())
        if not corpus:
            return order
        positions = {}
        for letter in order:
            opt = next((o for o in q.options if o.letter == letter), None)
            text = self._normalize_for_match(opt.text if opt else '')
            if not text:
                return order
            pos = corpus.find(text)
            if pos < 0 and len(text.split()) > 8:
                pos = corpus.find(' '.join(text.split()[:8]))
            if pos >= 0:
                positions[letter] = pos
        if len(positions) != len(order):
            positions = self._keyword_positions(corpus, q, order)
        if len(positions) != len(order) or len(set(positions.values())) != len(positions):
            return order
        corrected = sorted(order, key=lambda letter: positions[letter])
        if corrected != order:
            print(f"    按原文顺序校正排序: {order} -> {corrected}")
        return corrected

    def _keyword_positions(self, corpus: str, q: Question, order: List[str]) -> Dict[str, int]:
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', corpus) if s.strip()]
        if not sentences:
            return {}
        stop = {'the', 'and', 'that', 'with', 'from', 'this', 'have', 'were', 'their',
                'they', 'into', 'such', 'when', 'what', 'which', 'been', 'also', 'more',
                'some', 'many', 'other', 'after', 'before', 'over', 'than', 'them'}
        result = {}
        for letter in order:
            opt = next((o for o in q.options if o.letter == letter), None)
            words = {w for w in self._normalize_for_match(opt.text if opt else '').split()
                     if len(w) > 2 and w not in stop}
            if len(words) < 2:
                return {}
            best_idx, best_score = -1, 0.0
            for idx, sentence in enumerate(sentences):
                overlap = len(words & set(sentence.split()))
                score = overlap / len(words)
                if score > best_score:
                    best_idx, best_score = idx, score
            if best_idx < 0 or best_score < 0.34:
                return {}
            result[letter] = best_idx
        return result

    # ------------------------------------------------------------------ #
    # 官方答案接口（客观题真值）
    # ------------------------------------------------------------------ #
    ANSWER_API_SINGLE_CHOICE_TYPES = frozenset({
        QuestionType.SINGLE_CHOICE, QuestionType.LISTENING_CHOICE,
        QuestionType.VIDEO_CHOICE, QuestionType.VOCABULARY_TEST,
    })
    ANSWER_API_FILL_TYPES = frozenset({
        QuestionType.FILL_IN, QuestionType.BANKED_CLOZE,
        QuestionType.LISTENING_FILL_IN, QuestionType.DROPDOWN_SELECT,
    })
    ANSWER_API_SUPPORTED_TYPES = (ANSWER_API_SINGLE_CHOICE_TYPES
                                  | ANSWER_API_FILL_TYPES
                                  | frozenset({QuestionType.MULTIPLE_CHOICE,
                                               QuestionType.TRUE_FALSE,
                                               QuestionType.SORTING,
                                               QuestionType.LINK_MATCH,
                                               QuestionType.MATCHING,
                                               QuestionType.PARAGRAPH_MATCH,
                                               QuestionType.TEXT}))
    ANSWER_API_ITEM_KINDS = frozenset({'choice', 'true_false', 'fill', 'dropdown',
                                       'sorting', 'link_match', 'text'})

    def _answer_api_provider(self):
        """返回可用的答案接口门面；未启用/未命中/已停用时返回 None。"""
        provider = getattr(self, 'answer_api', None)
        if provider is None:
            return None
        if not getattr(provider, 'enabled', False) or not getattr(provider, 'active', False):
            return None
        if getattr(provider, 'abandoned', False):
            return None
        return provider

    def _answer_api_url_candidates(self) -> List[str]:
        """候选地址：当前窗口 + ipublish 内容 iframe（新版外壳地址常缺 taskId）。"""
        urls: List[str] = []
        try:
            urls.append(self.driver.current_url or '')
        except Exception:
            pass
        if self._is_publish_page():
            try:
                frame = PublishPlayer.find_frame(self.driver)
                if frame is not None:
                    urls.append(frame.get_attribute('src') or '')
            except Exception:
                pass
        return [url for url in urls if url]

    def _answer_api_prepare(self, task_label: str = '') -> bool:
        """按当前播放器地址准备答案；失败只记原因，绝不抛异常打断任务。"""
        provider = getattr(self, 'answer_api', None)
        if provider is None or not getattr(provider, 'enabled', False):
            return False
        stats = getattr(provider, 'stats', None)
        if isinstance(stats, dict):
            stats['used'] = 0
            stats['fallback'] = 0
        try:
            ready = provider.prepare(driver=self.driver,
                                     candidates=self._answer_api_url_candidates())
        except Exception as exc:
            print(f"    官方答案接口准备失败：{str(exc)[:80]}，继续使用 AI")
            return False
        if ready:
            return True
        reason = str(getattr(provider, 'reason', '') or '')
        if task_label and reason and '未能从当前地址解析任务' not in reason:
            print(f"    {task_label}：官方答案接口未命中（{reason}），继续使用 AI")
        return False

    def _answer_api_choice_letter(self, question: Question, value: str) -> str:
        valid = [option.letter.upper() for option in question.options if option.letter]
        if not valid:
            valid = [chr(65 + index) for index in range(len(question.options or []))]
        text = (value or '').strip()
        compact = re.sub(r'^[\d\s.、):：\]]+', '', text).upper()
        if compact and compact in valid:
            return compact
        match = re.search(r'(?<![A-Z])([A-Z])(?![A-Z])', compact)
        if match and match.group(1) in valid:
            return match.group(1)
        for option in question.options:
            if (option.text or '').strip().casefold() == text.casefold():
                return (option.letter or '').upper()
        return ''

    def _answer_api_choice_letters(self, question: Question, values: List[str]) -> List[str]:
        valid = [option.letter.upper() for option in question.options if option.letter]
        if not valid:
            valid = [chr(65 + index) for index in range(len(question.options or []))]
        letters: List[str] = []
        for value in values:
            text = (value or '').strip().upper()
            if text in valid:
                if text not in letters:
                    letters.append(text)
                continue
            for char in re.findall(r'[A-Z]', text):
                if char in valid and char not in letters:
                    letters.append(char)
        return letters if letters and all(letter in valid for letter in letters) else []

    def _answer_api_fill_answer(self, question: Question, values: List[str]) -> str:
        count = question.answer_count or 1
        hints = [str(blank.get('hint') or '') for blank in (question.banked_blanks or [])]
        if count == 1:
            answer = (values[0] if values else '').strip()
            if hints and hints[0]:
                answer = self.executor._strip_given_prefix(hints[0], answer)
            return answer
        if len(values) != count:
            return ''
        parts: List[str] = []
        for index, value in enumerate(values):
            hint = hints[index] if index < len(hints) else ''
            value = self.executor._strip_given_prefix(hint, value) if hint else value
            value = (value or '').strip()
            if not value:
                return ''
            parts.append(f'{index + 1}.{value}')
        return '\n'.join(parts)

    def _answer_api_link_answer(self, values: List[str]) -> str:
        pairs: List[str] = []
        for position, value in enumerate(values, 1):
            match = re.fullmatch(r'\s*\d*\s*[.\-:：)）]?\s*([A-Za-z])\s*', value or '')
            if not match:
                return ''
            pairs.append(f'{position}.{match.group(1).upper()}')
        return ' '.join(pairs)

    def _answer_api_letter_sequence_answer(self, question: Question,
                                           values: List[str]) -> str:
        """人物匹配 / 段落匹配：按空位顺序产出 ``1.A 2.B`` 形式的字母序列。"""
        count = question.answer_count or len(values)
        if not count or len(values) != count:
            return ''
        letters: List[str] = []
        for value in values:
            match = re.fullmatch(r'\s*\d*\s*[.\-:：)）]?\s*([A-Za-z])\s*', value or '')
            if not match:
                return ''
            letters.append(match.group(1).upper())
        if count == 1:
            return letters[0]
        return ' '.join(f'{index}.{letter}' for index, letter in enumerate(letters, 1))

    def _answer_api_question_answer(self, question: Question, values: List[str]) -> str:
        cleaned = [str(value).strip() for value in (values or []) if str(value).strip()]
        if not cleaned:
            return ''
        q_type = question.q_type
        if q_type in self.ANSWER_API_SINGLE_CHOICE_TYPES:
            return self._answer_api_choice_letter(question, cleaned[0])
        if q_type == QuestionType.MULTIPLE_CHOICE:
            return ' '.join(self._answer_api_choice_letters(question, cleaned))
        if q_type == QuestionType.TRUE_FALSE:
            for value in cleaned:
                letter = PublishAnswerExecutor.normalize_true_false(value)
                if letter:
                    return letter
            return ''
        if q_type == QuestionType.SORTING:
            letters = [value.strip().upper() for value in cleaned]
            valid = [option.letter.upper() for option in question.options if option.letter]
            if not valid or len(letters) != len(valid):
                return ''
            if any(letter not in valid for letter in letters):
                return ''
            return ' '.join(letters)
        if q_type == QuestionType.LINK_MATCH:
            return self._answer_api_link_answer(cleaned)
        if q_type in (QuestionType.MATCHING, QuestionType.PARAGRAPH_MATCH):
            return self._answer_api_letter_sequence_answer(question, cleaned)
        if q_type in self.ANSWER_API_FILL_TYPES or q_type == QuestionType.TEXT:
            return self._answer_api_fill_answer(question, cleaned)
        return ''

    @staticmethod
    def _answer_api_question_slots(question: Question) -> int:
        """计算一道 Question 在官方答案接口 children 中对应的子项数。"""
        q_type = question.q_type
        if q_type == QuestionType.SORTING:
            return len(getattr(question, 'options', None) or []) or 1
        if q_type == QuestionType.LINK_MATCH:
            link_items = getattr(question, 'link_items', None) or []
            prompts = [item for item in link_items if isinstance(item, dict) and item.get('side') == 'right']
            if prompts:
                return len(prompts)
        if q_type in (QuestionType.FILL_IN, QuestionType.BANKED_CLOZE,
                      QuestionType.LISTENING_FILL_IN, QuestionType.DROPDOWN_SELECT,
                      QuestionType.MATCHING, QuestionType.PARAGRAPH_MATCH, QuestionType.TEXT):
            count = (getattr(question, 'answer_count', 0)
                     or len(getattr(question, 'inputs', None) or [])
                     or len(getattr(question, 'banked_blanks', None) or []))
            if count > 0:
                return count
        return 1

    def _answer_api_convert(self, questions: List[Question],
                            values: List[List[str]]) -> Optional[List[str]]:
        """把接口答案转成本项目 executor 认识的答案格式；有一题不合法就整组放弃。"""
        if not questions or not values:
            return None
        slot_counts = [self._answer_api_question_slots(q) for q in questions]
        if len(values) != len(questions):
            # 选词填空 / 多空填空 / 排序 / 匹配等题型，页面合并成 1（或少量）道 Question，
            # 而接口按空/子项逐条返回 children；按每题子项数切分并取每空首个可接受答案。
            if sum(slot_counts) == len(values):
                grouped: List[List[str]] = []
                offset = 0
                for slots in slot_counts:
                    chunk = values[offset:offset + slots]
                    offset += slots
                    if any(not item for item in chunk):
                        return None
                    grouped.append([item[0] for item in chunk])
                values = grouped
            else:
                return None
        elif len(questions) == 1 and slot_counts[0] > 1 and len(values[0]) != slot_counts[0]:
            return None
        converted: List[str] = []
        for question, raw in zip(questions, values):
            answer = self._answer_api_question_answer(question, list(raw or []))
            if not answer:
                return None
            converted.append(answer)
        return converted

    def _answer_api_align(self, provider: Any, questions: List[Question],
                          group_index: Optional[int] = None,
                          ) -> Optional[Tuple[int, List[str]]]:
        """在接口大题列表中对齐并转换答案：优先按总空/子项数对齐，再回退按题数对齐。"""
        if provider is None or not questions:
            return None
        q_count = len(questions)
        slot_count = sum(self._answer_api_question_slots(q) for q in questions)
        counts = [slot_count] if slot_count == q_count else [slot_count, q_count]
        seen = set()
        for count in counts:
            if count <= 0 or count in seen:
                continue
            seen.add(count)
            aligned = (provider.answers_for(group_index, count)
                       if group_index is not None
                       else provider.peek_aligned(count))
            if not aligned:
                continue
            index, values = aligned
            converted = self._answer_api_convert(questions, values)
            if converted is not None:
                return index, converted
        return None

    def _answer_api_reset_tab(self) -> None:
        api_provider = getattr(self, 'answer_api', None)
        if api_provider is not None and getattr(api_provider, 'enabled', False):
            api_provider.reset()
        self._answer_api_tab_prepared = False

    def _answer_api_ensure_prepared(self) -> Optional[Any]:
        provider = getattr(self, 'answer_api', None)
        if provider is None or not getattr(provider, 'enabled', False):
            return None
        if not getattr(provider, 'active', False):
            if not getattr(self, '_answer_api_tab_prepared', False):
                self._answer_api_tab_prepared = True
                self._answer_api_prepare('')
        return self._answer_api_provider()

    def _answer_api_covers_current_page(self) -> bool:
        """只读预判当前页客观题是否可直接由官方答案接口覆盖（不推进游标、不放弃）。"""
        provider = getattr(self, 'answer_api', None)
        if provider is None or not getattr(provider, 'enabled', False) \
                or getattr(provider, 'abandoned', False):
            return False
        try:
            questions, _ = self.parser.parse_all()
        except Exception:
            return False
        if not questions or getattr(self.parser, 'incomplete', False):
            return False
        special = {
            QuestionType.VOCABULARY_FLASHCARD,
            QuestionType.VIDEO,
            QuestionType.AUDIO,
            QuestionType.SELF_CHECK,
            QuestionType.DISCUSSION_BOARD,
        }
        normal = [q for q in questions if q.q_type not in special]
        skip_mode = self._skip_record_mode()
        active_records = [
            q for q in normal
            if q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD)
            and not should_skip_record_question(q, skip_mode)
        ]
        if active_records:
            return False
        normal = [q for q in normal
                  if q.q_type not in (QuestionType.RECORD, QuestionType.ORAL_ALOUD)]
        if not normal:
            return False
        if any(q.q_type not in self.ANSWER_API_SUPPORTED_TYPES for q in normal):
            return False
        provider = self._answer_api_ensure_prepared()
        if provider is None:
            return False
        return self._answer_api_align(provider, normal) is not None

    def _answer_api_page_answers(self, questions: List[Question]) -> Optional[List[str]]:
        """老播放器：按「本页空位/小题数」在接口答案里对齐取用。

        只有本页真的出现题目时才去请求答案（每个任务最多尝试一次），
        避免纯音视频 / 阅读任务白跑一次接口。
        """
        provider = getattr(self, 'answer_api', None)
        if provider is None or not getattr(provider, 'enabled', False) or not questions:
            return None
        provider = self._answer_api_ensure_prepared()
        if provider is None:
            return None
        if any(question.q_type not in self.ANSWER_API_SUPPORTED_TYPES for question in questions):
            provider.abandon('本页含接口未覆盖的题型')
            return None
        matched = self._answer_api_align(provider, questions)
        if matched is None:
            provider.mark_fallback()
            provider.abandon('本页题目无可用官方答案（主观题或结构不匹配）')
            return None
        index, answers = matched
        provider.consume(index)
        return answers

    def _answer_api_publish_answers(self, questions: List[Question],
                                    group_index: Optional[int] = None,
                                    ) -> Optional[List[str]]:
        """新版播放器：优先按题目组序号取答案，序号对不上再按空位/小题数对齐。"""
        provider = self._answer_api_provider()
        if provider is None or not questions:
            return None
        if any(question.q_type not in self.ANSWER_API_SUPPORTED_TYPES for question in questions):
            provider.mark_fallback()
            return None
        matched = self._answer_api_align(provider, questions, group_index=group_index)
        if matched is None:
            provider.mark_fallback()
            return None
        index, answers = matched
        provider.consume(index)
        return answers

    def _answer_api_report(self) -> None:
        provider = getattr(self, 'answer_api', None)
        stats = getattr(provider, 'stats', None)
        if not isinstance(stats, dict):
            return
        used = int(stats.get('used', 0) or 0)
        fallback = int(stats.get('fallback', 0) or 0)
        if used or fallback:
            print(f"    官方答案接口：直接作答 {used} 个大题，回落 AI {fallback} 次")

    def _process_current_tab_content(self, chapter_name: str, tab_name: str, l1_idx: int, l2_idx: int,
                                     prefetch: Optional[AnswerPrefetch] = None,
                                     prefetch_signature: str = "",
                                     api_prepared: bool = False) -> bool:
        self._last_tab_outcome = ''
        if self._should_stop():
            return False
        direction_part = self._generate_content_hash_from_direction()
        if direction_part == "empty":
            direction_part = "no_direction"

        content_hash = f"{chapter_name}|{tab_name}|{l1_idx}|{l2_idx}|{direction_part}"

        print(f"    内容标识: {hashlib.md5(content_hash.encode()).hexdigest()[:16]}...")

        if content_hash in self.processed_hashes:
            print(f"   【跳过】已处理过，跳过")
            return False

        if self._is_publish_page():
            success = self._process_publish_content(tab_name)
            if success:
                self.processed_hashes.add(content_hash)
            return success

        # 老播放器：切到新任务后清空上一题的答案状态（若外层已预查则复用）
        if not api_prepared:
            self._answer_api_reset_tab()

        page_num = 1
        total_answered = 0
        total_record_skipped = 0
        last_questions_signature = ""

        while True:
            if self._should_stop():
                print("    已请求停止，当前任务提前结束")
                return False

            if not self._is_skipped_record_only_page():
                official_covers = self._answer_api_covers_current_page()
                if official_covers or not self._ai_available():
                    if self._has_video_on_page():
                        if not self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx,
                                                               skip_transcription=True):
                            return False
                        if self._should_stop():
                            return False
                else:
                    skip_asr = self._skip_transcription(tab_name)
                    if not self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx,
                                                           skip_transcription=skip_asr):
                        return False
                    if self._should_stop():
                        return False
                    if not self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx,
                                                           skip_transcription=skip_asr):
                        return False
                    if self._should_stop():
                        return False
            questions, directions = self.parser.parse_all()
            if self.parser.incomplete:
                print("    本页有交互题未能解析，停止当前任务")
                return False
            if self._should_stop():
                return False
            self._harvest_vocabulary()
            self._last_question_types = {q.q_type for q in questions}
            print(f"\n    处理第 {page_num} 页题目...")
            print(f"    找到 {len(questions)} 个可见题目")

            current_signature = self._generate_questions_signature(questions)

            if current_signature == last_questions_signature and page_num > 1:
                print(f"    题目内容与上次相同，无法确认翻页完成")
                return False

            last_questions_signature = current_signature

            special_handled = False
            self_check_handled = False
            for q in questions:
                if self._should_stop():
                    return False
                for handler in self.content_handlers:
                    if self._should_stop():
                        return False
                    if handler.can_handle(q):
                        if q.q_type == QuestionType.DISCUSSION_BOARD and not self._ai_available():
                            print("    讨论区发帖需要 AI 作答，但未配置 AI 接口，跳过该任务")
                            self._last_tab_outcome = 'skipped_no_ai'
                            return False
                        if q.q_type == QuestionType.VIDEO and hasattr(handler, 'set_active_tab'):
                            handler.set_active_tab(f"{tab_name}|{l1_idx}|{l2_idx}")
                        print(f"     使用 {handler.__class__.__name__} 处理")
                        handled = handler.handle(q)
                        if not handled or self._should_stop():
                            return False
                        if q.q_type == QuestionType.AUDIO:
                            special_handled = True
                            break
                        if self._should_stop():
                            return False
                        special_handled = True
                        if q.q_type == QuestionType.SELF_CHECK:
                            self_check_handled = True
                        if q.q_type in [QuestionType.VOCABULARY_FLASHCARD, QuestionType.VIDEO]:
                            print(f"    特殊内容处理完成")
                            self.processed_hashes.add(content_hash)
                            return True
                        if q.q_type == QuestionType.DISCUSSION_BOARD:
                            print(f"    讨论区已处理")
                            self.processed_hashes.add(content_hash)
                            return True
                        break

            normal_questions = [q for q in questions if q.q_type not in [
                QuestionType.VOCABULARY_FLASHCARD,
                QuestionType.VIDEO,
                QuestionType.AUDIO,
                QuestionType.SELF_CHECK,
                QuestionType.DISCUSSION_BOARD
            ]]

            skip_mode = self._skip_record_mode()
            record_questions = [q for q in normal_questions
                                if q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD)]
            skipped_records = [q for q in record_questions
                               if should_skip_record_question(q, skip_mode)]
            active_records = [q for q in record_questions if q not in skipped_records]
            if skipped_records:
                total_record_skipped += len(skipped_records)
                print(f"    本页 {len(skipped_records)} 道跟读/录音题，已按配置（{skip_mode}）自动跳过")
                normal_questions = [q for q in normal_questions if q not in skipped_records]

            total_questions = len(normal_questions)

            all_self_submitting = (
                bool(active_records)
                and len(active_records) == total_questions
                and all(q.is_self_submitting_record for q in active_records)
            )
            record_answered = 0
            if active_records:
                if page_num == 1 and not len(self._vocab()) and self._needs_vocabulary(active_records):
                    switched = self._harvest_from_vocabulary_tab()
                    if switched is None:
                        return False
                    if switched:
                        questions, directions = self.parser.parse_all()
                        if self.parser.incomplete or self._should_stop():
                            return False
                        normal_questions = [q for q in questions if q.q_type not in [
                            QuestionType.VOCABULARY_FLASHCARD,
                            QuestionType.VIDEO,
                            QuestionType.AUDIO,
                            QuestionType.SELF_CHECK,
                            QuestionType.DISCUSSION_BOARD
                        ]]
                        record_questions = [q for q in normal_questions
                                            if q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD)]
                        skipped_records = [q for q in record_questions
                                           if should_skip_record_question(q, skip_mode)]
                        active_records = [q for q in record_questions if q not in skipped_records]
                        normal_questions = [q for q in normal_questions if q not in skipped_records]
                        total_questions = len(normal_questions)
                        prefetch = None
                print(f"    本页 {len(active_records)} 道录音题，开始逐条录音...")
                for q in active_records:
                    if self._should_stop():
                        return False
                    needs_tts = q.needs_speech_script or q.q_type == QuestionType.ORAL_ALOUD
                    if needs_tts and not self._tts().enabled:
                        print("    录音题没有原声，TTS 已关闭，跳过该题")
                        continue
                    script = ""
                    if q.q_type == QuestionType.RECORD and q.needs_speech_script:
                        if not self._ai_available():
                            print("    主观口语题需要 AI 写稿，但未配置 AI 接口，跳过该任务")
                            self._last_tab_outcome = 'skipped_no_ai'
                            return False
                        script = self._speaking_script_for(q, directions)
                        if not script:
                            continue
                    if self.executor.execute(q, script):
                        record_answered += 1
                print(f"    录音题完成 {record_answered}/{len(active_records)}")
                if record_answered < len(active_records):
                    print("    录音题未完成，停止当前页")
                    return False
                normal_questions = [q for q in normal_questions
                                    if q.q_type not in (QuestionType.RECORD,
                                                        QuestionType.ORAL_ALOUD)]
                if not normal_questions:
                    total_answered += record_answered
                    print(f"    本页成功填写 {record_answered}/{total_questions} 题")

            if normal_questions:
                print(f"    共 {total_questions} 道题目需要回答")
                success_count = record_answered

                # 官方答案接口命中时跳过 AI，并接入目标正确率控分（以官方答案为基准）
                official_answers = None
                api_provider = getattr(self, 'answer_api', None)
                if api_provider is not None and getattr(api_provider, 'enabled', False) \
                        and not getattr(api_provider, 'abandoned', False):
                    official_answers = self._answer_api_page_answers(normal_questions)
                if official_answers is not None:
                    adjusted_official, _ = self._accuracy_controller().apply_to_items(
                        list(zip(normal_questions, official_answers)),
                        enforce_submit_floor=False,
                    )
                    filled_official = 0
                    self.executor._exact_official_mode = True
                    try:
                        for question, answer in zip(normal_questions, adjusted_official):
                            if self._should_stop():
                                return False
                            if answer and self.executor.execute(question, answer):
                                filled_official += 1
                    finally:
                        self.executor._exact_official_mode = False
                    if filled_official == len(normal_questions):
                        print(f"    本页 {filled_official} 题已使用官方答案填写，跳过 AI")
                        success_count += filled_official
                        normal_questions = []
                        prefetch = None
                    else:
                        print(f"    官方答案仅写入 {filled_official}/{len(normal_questions)} 题，"
                              f"本页改回 AI 流程")

                # 仅在未命中官方答案、确实要走 AI 流程时，才补抓隔壁原文 / 词汇表 / 音视频转写
                if normal_questions:
                    if not self._ai_available():
                        print("    本页未能通过官方答案完成且未配置 AI 接口，跳过该任务")
                        self._last_tab_outcome = 'skipped_no_ai'
                        return False
                    dom_refreshed = False
                    if self._ensure_reading_context_for_ai():
                        dom_refreshed = True
                    if self._should_stop():
                        return False
                    if page_num == 1 and not len(self._vocab()) and self._needs_vocabulary(normal_questions):
                        switched = self._harvest_from_vocabulary_tab()
                        if switched is None:
                            return False
                        if switched:
                            dom_refreshed = True
                    if dom_refreshed:
                        questions, directions = self.parser.parse_all()
                        if self.parser.incomplete:
                            print("    本页有交互题未能解析，停止当前任务")
                            return False
                        if self._should_stop():
                            return False
                        normal_questions = [q for q in questions if q.q_type not in [
                            QuestionType.VOCABULARY_FLASHCARD,
                            QuestionType.VIDEO,
                            QuestionType.AUDIO,
                            QuestionType.SELF_CHECK,
                            QuestionType.DISCUSSION_BOARD,
                            QuestionType.RECORD,
                            QuestionType.ORAL_ALOUD,
                        ]]
                        if prefetch is not None:
                            print("    上下文页面切换后，放弃预取答案")
                            prefetch = None
                    if self._has_video_on_page():
                        if not self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx,
                                                               skip_transcription=False):
                            return False
                        if self._should_stop():
                            return False
                    if self._has_audio_on_page():
                        if not self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx,
                                                               skip_transcription=False):
                            return False
                        if self._should_stop():
                            return False
                    fallback_passage = self._extract_passage()
                    if fallback_passage:
                        self.ai_client.add_passage_if_new(fallback_passage)

                large_fills = [q for q in normal_questions
                               if q.q_type in (QuestionType.FILL_IN, QuestionType.BANKED_CLOZE)
                               and len(q.inputs) > 8
                               and not is_expression_bank_question(q)]
                for q in large_fills:
                    if self._should_stop():
                        return False
                    if self._answer_chunked_fill(q, directions):
                        success_count += 1
                normal_questions = [q for q in normal_questions if q not in large_fills]

                ai_response = None
                used_prefetch = (prefetch is not None and page_num == 1
                                 and current_signature == prefetch_signature
                                 and not self._retry_hint
                                 and prefetch.materials == tuple(
                                     (m['role'], m['content']) for m in self.ai_client.material_messages))
                if used_prefetch:
                    print(f"    等待播放期间的 AI 预取答案（已运行 {prefetch.elapsed:.1f}s）...")
                    ai_response = prefetch.wait(timeout=180)
                    if ai_response:
                        print(f"    预取答案已就绪（总耗时 {prefetch.elapsed:.1f}s）")
                    else:
                        print("    预取未返回，改为同步请求")
                if not ai_response:
                    prompt = self.prompt_builder.build(normal_questions, directions) if normal_questions else ""
                    if prompt and self._retry_hint:
                        prompt += "\n\n【平台答案解析】\n" + self._retry_hint[:2000]
                    if self._should_stop():
                        return False
                    shot = self._question_screenshot(normal_questions)
                    ai_response = self.ai_client.ask(
                        prompt, stop_requested=self.stop_requested, screenshot=shot,
                        require_image=self._requires_image_context(normal_questions)
                    ) if normal_questions else None
                if self._should_stop():
                    return False
                if ai_response:
                    failed_questions = []
                    prepared_items = []

                    for q in normal_questions:
                        if self._should_stop():
                            return False
                        if q.q_type in [QuestionType.SINGLE_CHOICE, QuestionType.LISTENING_CHOICE,
                                        QuestionType.VIDEO_CHOICE, QuestionType.MULTIPLE_CHOICE,
                                        QuestionType.VOCABULARY_TEST]:
                            ans = self._extract_single_answer(ai_response, q.number)
                        elif q.q_type in [QuestionType.BANKED_CLOZE, QuestionType.FILL_IN,
                                          QuestionType.DROPDOWN_SELECT]:
                            ans = ai_response
                        elif q.q_type == QuestionType.SORTING and len(normal_questions) > 1:
                            ans = self._extract_single_answer(ai_response, q.number)
                        else:
                            ans = ai_response

                        if ans and q.q_type == QuestionType.SORTING:
                            letters = [opt.letter for opt in q.options]
                            order = self.executor._parse_sorting_order(ans, letters)
                            corrected = self._reorder_sorting_by_transcript(q, order)
                            if corrected and corrected != order:
                                ans = ' '.join(corrected)
                        prepared_items.append((q, ans or ""))

                    adjusted_answers, _ = self._accuracy_controller().apply_to_items(
                        prepared_items, enforce_submit_floor=False
                    )

                    for (q, _), ans in zip(prepared_items, adjusted_answers):
                        if self._should_stop():
                            return False
                        if ans:
                            if self.executor.execute(q, ans):
                                success_count += 1
                            else:
                                failed_questions.append(q)
                        else:
                            print(f"    题目 {q.number} 无答案")
                            failed_questions.append(q)

                    if failed_questions:
                        repaired = self._repair_questions(failed_questions, directions)
                        success_count += repaired
                        print(f"    二次修正成功 {repaired}/{len(failed_questions)} 题")

                    total_answered += success_count
                    print(f"    本页成功填写 {success_count}/{total_questions} 题")
                    if success_count != total_questions:
                        print("    本页未完整填写，停止当前任务，请检查答案和选项状态")
                        return False
                elif normal_questions:
                    print("    AI 未返回答案，停止当前任务")
                    return False
                else:
                    total_answered += success_count
                    print(f"    本页成功填写 {success_count}/{total_questions} 题")
                    if success_count != total_questions:
                        print("    本页未完整填写，停止当前任务，请检查答案和选项状态")
                        return False

            if self._should_stop():
                return False
            if self_check_handled and not normal_questions:
                _dismiss_known_info_overlays(self.driver)
                _install_submit_network_hook(self.driver)
                if not self.executor.submit() or not self._wait_for_submit_complete():
                    return False
                if self._should_stop():
                    return False
            next_btn = self._find_next_question_button()
            if next_btn:
                print(f"    点击下一题...")
                pre_click_signature = current_signature

                if self._should_stop():
                    return False
                if not WebDriverHelper.safe_click(self.driver, next_btn):
                    print(f"    点击下一题失败")
                    return False

                if not self._wait_for_content_change(pre_click_signature, timeout=30):
                    if self._should_stop():
                        return False
                    print(f"    内容未变化，未确认翻页完成")
                    return False

                page_num += 1

                if page_num > 50:
                    print(f"    达到最大页数限制，停止")
                    return False
                continue

            if self._should_stop():
                return False
            if total_questions:
                _dismiss_known_info_overlays(self.driver)
                _install_submit_network_hook(self.driver)
            if total_questions and self.executor.submit():
                if not self._wait_for_submit_complete():
                    self._last_tab_outcome = 'error'
                    return False
                if self._should_stop():
                    return False
                print(f"    已自动提交")
            elif total_questions and all_self_submitting:
                print(f"    本页录音已逐条上传/评分，无需点击页面提交按钮")
            elif total_questions:
                print(f"    没找到可点击的提交按钮，请手动提交")
                self._last_tab_outcome = 'error'
                return False
            elif not special_handled:
                if total_record_skipped > 0 and total_answered == 0:
                    print("    本任务为跟读/录音任务，已按配置自动跳过")
                    self._last_tab_outcome = 'skipped_record'
                    return False
                if total_record_skipped > 0 and total_answered > 0:
                    print("    本页跟读/录音题已按配置跳过，其余页面已完成")
                    break
                non_answer = getattr(self.parser, 'non_answer_containers', 0)
                if (isinstance(non_answer, int) and non_answer > 0
                        and not self.parser.incomplete and not self._page_needs_answer()):
                    print("    该页无需作答（未检测到作答控件），跳过并继续")
                    self._last_tab_outcome = 'skipped'
                    return False
                print("    未确认有可处理内容，保留任务为未完成")
                self._last_tab_outcome = 'error'
                return False
            print(f"    没有更多题目了")
            break
        print(f"    总共回答 {total_answered} 题")
        self._answer_api_report()
        self.processed_hashes.add(content_hash)
        self._last_tab_outcome = 'done'
        return True

    def _is_publish_page(self) -> bool:
        driver = getattr(self, 'driver', None)
        if driver is None:
            return False
        try:
            return PublishPlayer.is_publish_page(driver)
        except Exception:
            return False

    def _process_publish_content(self, task_name: str = '') -> bool:
        print("    检测到新版 ipublish 播放器（course-v3），启用专用适配流程")
        self.publish_executor.dismiss_rules_popup()
        if not self._publish_prepare_scope(task_name):
            return False
        # 新版播放器：优先请求官方答案接口；仅当本任务需要且已配置 AI 时才读取隔壁原文
        api_ready = self._answer_api_prepare(task_name)
        if getattr(self, '_pending_reading_context', None):
            if not self._ai_available():
                self._pending_reading_context = None
            else:
                from unipus_helper.answer_api import child_answers, is_objective_answers
                provider = self._answer_api_provider() if api_ready else None
                all_objective = bool(
                    provider and provider.items
                    and all(is_objective_answers(child_answers(it)) for it in provider.items)
                )
                if not all_objective:
                    self._ensure_reading_context_for_ai()
                    if self._should_stop():
                        return False
                else:
                    self._pending_reading_context = None

        quiz_result = self._publish_process_quiz()
        if quiz_result is not None:
            return quiz_result
        videos = self._publish_play_task_media()
        if videos is None:
            return False
        reading = self._publish_read_task()
        if reading is None:
            return False

        handled = 0
        skipped = 0
        record_skipped = 0
        ai_skipped = 0
        rejected = 0
        idle_reveals = 0
        while not self._should_stop():
            result = self._publish_process_next_group()
            if result == 'error':
                return False
            if result == 'answered':
                handled += 1
                idle_reveals = 0
                if handled >= 80:
                    print("    已处理 80 个题目组，停止当前任务")
                    break
                continue
            if result == 'skipped_record':
                record_skipped += 1
                idle_reveals = 0
                continue
            if result == 'skipped_no_ai':
                ai_skipped += 1
                idle_reveals = 0
                continue
            if result == 'skipped':
                skipped += 1
                idle_reveals = 0
                continue
            if result == 'rejected':
                rejected += 1
                idle_reveals = 0
                continue

            self.publish_executor.dismiss_rules_popup()
            if idle_reveals < 25 and self._publish_reveal_more_groups():
                idle_reveals += 1
                continue
            break

        if handled:
            print(f"    新版播放器共提交 {handled} 个题目组")
        if record_skipped:
            print(f"    另有 {record_skipped} 个跟读/录音题目组已按配置自动跳过")
        if ai_skipped:
            print(f"    另有 {ai_skipped} 个题目组需 AI 作答但未配置 AI 接口，已跳过")
        if rejected:
            print(f"    另有 {rejected} 个题目组因正确率不足 60% 被平台拒绝提交")
        if skipped:
            print(f"    另有 {skipped} 个题目组题型暂不支持或未渲染，已跳过")
        self._answer_api_report()
        if ai_skipped and not skipped and not rejected:
            self._last_tab_outcome = 'skipped_no_ai'
            return False
        if record_skipped and not skipped and not rejected:
            self._last_tab_outcome = 'skipped_record'
            return False
        if not handled and not skipped and not rejected and not record_skipped and not ai_skipped:
            if not videos and not reading:
                print("    当前任务没有可处理的题目或视频，请检查课程目录中的实际进度")
                return False
            print("    本任务正文已浏览，请以课程目录显示的进度为准")
        return not self._should_stop() and not skipped and not rejected and not record_skipped and not ai_skipped

    def _publish_read_task(self):
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return None
            nodes = self.driver.find_elements(
                By.CSS_SELECTOR, '.editor-content > [data-lexical-node-type="base-paragraph"], '
                                 '.editor-content > [data-lexical-node-type="highlight-block"]')
            scoped = self._publish_scoped_groups([{'element': node} for node in nodes])
            count = 0
            for state in scoped:
                node = state['element']
                if not (node.get_attribute('textContent') or '').strip():
                    continue
                if self.driver.execute_script('return !!arguments[0].closest("[data-ipublish-question-group-type]")', node):
                    continue
                self.driver.execute_script('arguments[0].scrollIntoView({block:"center"});', node)
                if self.stop_requested.wait(3):
                    return None
                count += 1
            return count

    def _publish_play_task_media(self):
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return None
            wrappers = self.driver.find_elements(By.CSS_SELECTOR,
                '[data-lexical-node-type="insert-video"], [data-lexical-node-type="insert-audio"]')
            scoped = self._publish_scoped_groups([{'element': wrapper} for wrapper in wrappers])
            played = 0
            for state in scoped:
                wrapper = state['element']
                if self.driver.execute_script('return !!arguments[0].closest("[data-ipublish-question-group-type]")', wrapper):
                    group = self.driver.execute_script('return arguments[0].closest("[data-ipublish-question-group-type]")', wrapper)
                    if not self.publish_parser.group_submitted(group):
                        continue
                video = None
                try:
                    self.driver.execute_script('arguments[0].scrollIntoView({block:"center"});', wrapper)
                    tag = 'audio' if wrapper.get_attribute('data-lexical-node-type') == 'insert-audio' else 'video'
                    video = WebDriverWait(self.driver, 15).until(
                        lambda d: wrapper.find_elements(By.TAG_NAME, tag) or False)[0]
                    self.driver.execute_script('arguments[0].muted=true; arguments[0].playbackRate=2; arguments[0].play();', video)
                    duration = WebDriverWait(self.driver, 15).until(
                        lambda d: d.execute_script('return Number.isFinite(arguments[0].duration) && arguments[0].duration || 0', video))
                    print(f'    播放当前任务音视频（{int(duration)} 秒）')
                    deadline = time.monotonic() + duration + 60
                    while not self._should_stop() and time.monotonic() < deadline:
                        progress = self.driver.execute_script(
                            'return {ended:arguments[0].ended, error:!!arguments[0].error}', video)
                        if progress['ended']:
                            break
                        if progress['error'] or self.stop_requested.wait(0.5):
                            return None
                    else:
                        print('    视频未播放完成')
                        return None
                    played += 1
                except Exception as exc:
                    print(f'    视频播放失败: {str(exc)[:100]}')
                    return None
                finally:
                    try:
                        if video is not None:
                            self.driver.execute_script('arguments[0].pause()', video)
                    except Exception:
                        pass
        return None if self._should_stop() else played

    QUIZ_SUPPORTED_KINDS = {
        'choice', 'true_false', 'fill', 'dropdown', 'text', 'sorting', 'link_match'
    }
    QUIZ_DROPDOWN_PLACEHOLDERS = frozenset({
        '请选择', '选择', 'select', 'choose', 'pleaseselect', 'pleasechoose',
        'selectone', 'selectanoption', 'chooseone', 'chooseanoption',
    })

    @classmethod
    def _normalize_dropdown_placeholder(cls, text: str) -> str:
        cleaned = re.sub(r'\s+', '', (text or '').strip().lower())
        return re.sub(r'^[\-—–_•·:：.…▼▾⌄<>]+|[\-—–_•·:：.…▼▾⌄<>]+$', '', cleaned)

    def _publish_quiz_has_existing_answer(self, questions: List[Question]) -> bool:
        for question in questions:
            if any(opt.is_selected for opt in question.options):
                return True
            if question.q_type == QuestionType.LINK_MATCH:
                try:
                    if self.publish_executor._link_edge_count(question.group or question.element) > 0:
                        return True
                except Exception:
                    pass
            for inp in question.inputs:
                try:
                    if (inp.get_attribute('value') or '').strip():
                        return True
                except Exception:
                    continue
            if question.q_type == QuestionType.DROPDOWN_SELECT:
                for blank in self.publish_executor._dropdown_blanks(question):
                    try:
                        for inp in blank.find_elements(By.CSS_SELECTOR, 'input, textarea'):
                            if (inp.get_attribute('value') or '').strip():
                                return True
                        for sel in blank.find_elements(By.CSS_SELECTOR, 'select'):
                            val = self._normalize_dropdown_placeholder(sel.get_attribute('value') or '')
                            if val and val not in self.QUIZ_DROPDOWN_PLACEHOLDERS:
                                return True
                        trigger = self.publish_executor._dropdown_trigger(blank)
                        if trigger is not None:
                            text = self._normalize_dropdown_placeholder(PublishPlayer.button_text(trigger))
                            if text and text not in self.QUIZ_DROPDOWN_PLACEHOLDERS:
                                return True
                    except Exception:
                        continue
        return False

    def _publish_process_quiz(self):
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return False
            links = self.driver.find_elements(By.CSS_SELECTOR, '[data-lexical-node-type="quiz-paper-link"]')
            scoped = self._publish_scoped_groups([{'element': link} for link in links])
            if not scoped:
                return None
            if len(scoped) != 1:
                print('    当前任务包含多个测试，请手动处理')
                return False
            if not self._ai_available():
                print('    单元测试需要 AI 作答，但未配置 AI 接口，跳过该任务')
                self._last_tab_outcome = 'skipped_no_ai'
                return False
            covers = scoped[0]['element'].find_elements(By.CSS_SELECTOR, '[class*="coverContainer"]')
            if not covers or not WebDriverHelper.safe_click(self.driver, covers[0]):
                print('    无法打开单元测试')
                return False
            try:
                WebDriverWait(self.driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, '[class*="acknowledge_btn__"]') or d.find_elements(By.CSS_SELECTOR, 'iframe[src*="/learner/testing/"]'))
                start = self.driver.find_elements(By.CSS_SELECTOR, '[class*="acknowledge_btn__"]')
                if start and not WebDriverHelper.safe_click(self.driver, start[0]):
                    return False
                frame = WebDriverWait(self.driver, 15).until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'iframe[src*="/learner/testing/"]')))
                self.driver.switch_to.frame(frame)
                WebDriverWait(self.driver, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, '[data-ipublish-question-group-type]'))
                text = self.driver.find_element(By.TAG_NAME, 'body').text
                if '最终得分' in text or '再次作答' in text:
                    print('    单元测试已有成绩，保留现有结果')
                    return True
                groups = self.publish_parser.collect_groups()
                parsed = []
                active_passages: List[str] = []
                seen_question_since_passage = False
                for state in groups:
                    kind = state['kind']
                    if kind == 'unknown':
                        kind = self.publish_parser._kind_from_questions(state['element'])
                    if state['type'] == 'rich_text_read_group':
                        chunk = self.publish_parser.collect_passages([state])
                        if chunk:
                            if seen_question_since_passage:
                                active_passages = []
                                seen_question_since_passage = False
                            active_passages.extend(chunk)
                        continue
                    if kind == 'skip':
                        continue
                    if kind not in self.QUIZ_SUPPORTED_KINDS:
                        print(f"    单元测试暂不支持题型: {state['type']}")
                        return False
                    questions = self.publish_parser.parse_group(state['element'], state['type'], 1)
                    nodes = self.publish_parser._question_nodes(state['element'])
                    mismatch_fill = any(
                        q.q_type == QuestionType.FILL_IN and q.blank_count and q.blank_count != len(q.inputs)
                        for q in questions
                    )
                    if (not questions
                            or (kind != 'link_match' and nodes and len(questions) != len(nodes))
                            or mismatch_fill
                            or self._publish_quiz_has_existing_answer(questions)):
                        print('    测试已有作答或题目解析失败，保留当前进度')
                        return False
                    group_passages = [] if state['type'] == 'translation_group' else list(active_passages)
                    seen_question_since_passage = True
                    parsed.append((state, kind, questions, group_passages))
                if not parsed:
                    return False
                for state, kind, questions, group_passages in parsed:
                    self.ai_client.force_reset('单元测试题组')
                    for passage in group_passages:
                        self.ai_client.add_passage_if_new(passage)
                    if not self._publish_prepare_media(state['element']):
                        return False
                    direction = self.publish_parser.group_direction(state['element'])
                    prompt = self._publish_build_group_prompt(
                        questions, direction, group_type=state['type'], kind=kind
                    )
                    response = self.ai_client.ask(
                        prompt,
                        stop_requested=self.stop_requested,
                        screenshot=self._question_screenshot(questions),
                        require_image=self._requires_image_context(questions),
                    )
                    if not response or self._should_stop():
                        return False
                    prepared_items = self._publish_prepare_answers(questions, response)
                    if prepared_items is None:
                        return False
                    adjusted_answers, _ = self._accuracy_controller().apply_to_items(
                        prepared_items, enforce_submit_floor=False
                    )
                    for (question, _), answer in zip(prepared_items, adjusted_answers):
                        if not answer or not self.publish_executor.execute(question, answer):
                            print('    测试未完整填写，保留当前进度')
                            return False
                text = self.driver.find_element(By.TAG_NAME, 'body').text
                progress = re.search(r'答题卡共计\s*(\d+)\s*题\s*(\d+)\s*/\s*(\d+)', text)
                if not progress or int(progress.group(1)) == 0 or len(set(progress.groups())) != 1:
                    print('    无法确认测试全部答完，不提交')
                    return False
                submit = self.driver.find_elements(By.XPATH, "//span[normalize-space(.)='提交']")
                if not submit or not WebDriverHelper.safe_click(self.driver, submit[-1]):
                    return False
                self.driver.switch_to.parent_frame()
                confirm = WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable(
                    (By.XPATH, "//button[normalize-space(.)='确 定' or normalize-space(.)='确定']")))
                if not WebDriverHelper.safe_click(self.driver, confirm):
                    return False
                self.driver.switch_to.frame(frame)
                WebDriverWait(self.driver, 30).until(lambda d: '最终得分' in d.find_element(By.TAG_NAME, 'body').text)
                print('    单元测试已提交并生成成绩')
                return True
            except Exception as exc:
                print(f'    单元测试处理失败: {str(exc)[:100]}')
                return False

    def _publish_prepare_scope(self, task_name: str = '', timeout: float = 30.0) -> bool:
        self._publish_scope = None
        deadline = time.monotonic() + timeout
        reason = '未能进入任务内容'
        while not self._should_stop() and time.monotonic() < deadline:
            try:
                route = self._publish_current_url()
                frame = PublishPlayer.find_frame(self.driver)
                if frame is None:
                    raise ValueError('无法定位内容 iframe')
                anchor = unquote(urlsplit(frame.get_attribute('src') or '').fragment)
                with PublishFrameContext(self.driver) as context:
                    if not context.entered:
                        raise ValueError('未能进入内容 iframe')
                    if task_name.startswith('quick_current_page_'):
                        scope = PublishTaskScope.capture_visible(self.driver)
                        if not scope.select(self.driver, self.publish_parser.collect_groups()):
                            raise ValueError('当前可见题块尚未加载出题目组')
                    else:
                        scope = PublishTaskScope.capture(self.driver, anchor)
                    if '/course-v3:' in route and not scope.start_anchor:
                        raise ValueError('新版任务没有明确的正文锚点')
                    if task_name and not task_name.startswith('quick_current_page_') and scope.title:
                        normalize = lambda text: re.sub(r'\s+', ' ', text).strip().casefold()
                        if normalize(task_name) != normalize(scope.title):
                            raise ValueError(f'打开的任务“{scope.title}”与勾选的“{task_name}”不一致')
                    if self._publish_current_url() != route:
                        raise ValueError('任务仍在切换中')
                    self._publish_scope = scope
                    self._publish_scope_url = route
                    print(f"    作答范围：{scope.title or '当前独立题目页'}，到本任务末尾即停止")
                    return True
            except Exception as exc:
                reason = str(exc)
            if self.stop_requested.wait(0.2):
                return False
        print(f"    无法确认当前任务范围：{reason}，停止当前任务")
        return False

    def _publish_scoped_groups(self, groups: List[Dict]) -> List[Dict]:
        scope = getattr(self, '_publish_scope', None)
        if scope is None:
            return groups
        return scope.select(self.driver, groups)

    def _publish_group_in_scope(self, group) -> bool:
        try:
            return bool(self._publish_scoped_groups([{'element': group}]))
        except Exception as exc:
            print(f"    题组范围校验失败：{exc}")
            return False

    def _publish_reveal_more_groups(self) -> bool:
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return False
            try:
                groups = self._publish_scoped_groups(self.publish_parser.collect_groups())
                before = len(groups)
                if not groups:
                    return False
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'end', inline: 'nearest'});",
                    groups[-1]['element'])
                if self.stop_requested.wait(1.2):
                    return False
                after = len(self._publish_scoped_groups(self.publish_parser.collect_groups()))
            except Exception:
                return False
            if after > before:
                print(f"    滚动加载出更多题目组: {before} -> {after}")
                return True
            return False

    def _publish_process_next_group(self) -> str:
        self.publish_executor.dismiss_rules_popup()
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                print("    未能进入 ipublish 内容 iframe")
                return 'error'

            parser = self.publish_parser
            groups = parser.collect_groups()
            if not groups:
                print("    内容层没有找到题目组（可能仍在加载）")
                return 'done'

            try:
                scoped_groups = self._publish_scoped_groups(groups)
            except ValueError as exc:
                print(f"    {exc}")
                return 'error'

            for passage in parser.collect_passages(groups):
                self.ai_client.add_passage_if_new(passage)

            pending = None
            answer_index = -1
            for state in scoped_groups:
                if state['kind'] in self.ANSWER_API_ITEM_KINDS:
                    answer_index += 1
                if state['kind'] == 'skip':
                    continue
                if parser.group_submitted(state['element']):
                    continue
                key = parser.group_key(state['element'])
                if key in self._publish_processed_groups:
                    continue
                if state['kind'] == 'upload':
                    print(f"    需要手动上传附件的题目组: {state['type']}")
                    self._publish_processed_groups.add(key)
                    return 'skipped'
                if state['kind'] == 'ai_oral':
                    print('    AI 口语对话需要在平台内完成')
                    self._publish_processed_groups.add(key)
                    return 'skipped'
                if state['kind'] == 'record':
                    fast_skip = should_skip_publish_group_fast(state['type'], self._skip_record_mode())
                    if fast_skip is True:
                        print(f"    已按配置（{self._skip_record_mode()}）跳过跟读/录音题目组: {state['type']}")
                        self._publish_processed_groups.add(key)
                        return 'skipped_record'
                pending = (state, key, answer_index)
                break

            if pending is None:
                return 'done'

            state, key, answer_index = pending
            outcome = self._publish_answer_group(state['element'], state['type'], key,
                                                 answer_index=answer_index)
            if outcome == 'submitted':
                return 'answered'
            if outcome == 'rejected':
                return 'rejected'
            if outcome == 'skipped_record':
                return 'skipped_record'
            if outcome == 'skipped_no_ai':
                return 'skipped_no_ai'
            if outcome == 'skipped':
                return 'skipped'
            return 'error'

    def _publish_answer_group(self, group, group_type: str, key: str,
                              answer_index: Optional[int] = None):
        parser = self.publish_parser
        if not self._publish_group_in_scope(group):
            print("    题目组不在当前勾选任务内，不作答")
            return False
        if not self.publish_executor.reveal_group(group, self.stop_requested):
            print("    未能展开题目所在的折叠面板，停止当前任务")
            return False
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", group)
        except Exception:
            pass
        time.sleep(0.4)

        if not self._publish_wait_group_rendered(group):
            print("    题目组滚动后仍未渲染，跳过该组（下次再处理）")
            self._publish_processed_groups.add(key)
            return 'skipped'

        direction = parser.group_direction(group)
        unsupported_before = len(parser.unsupported)
        questions = parser.parse_group(group, group_type, 1)
        if len(parser.unsupported) > unsupported_before:
            print(f"    暂不支持自动作答的题型: {parser.unsupported[-1]}")

        print(f"\n    题目组 {group_type}：{len(questions)} 道题")
        if not questions:
            print("    该题目组没有可解析的题目，跳过")
            self._publish_processed_groups.add(key)
            return 'skipped'
        nodes = parser._question_nodes(group)
        if nodes and len(questions) != len(nodes):
            print("    本组有小题未能解析，不提交，请检查页面结构")
            return False

        skip_mode = self._skip_record_mode()
        if questions and all(should_skip_record_question(question, skip_mode)
                             for question in questions):
            print(f"    题目组 {group_type} 为跟读/录音题，已按配置（{skip_mode}）跳过")
            self._publish_processed_groups.add(key)
            return 'skipped_record'

        filled = 0
        if all(question.q_type == QuestionType.RECORD for question in questions):
            if any(question.needs_speech_script for question in questions) and not self._ai_available():
                print(f"    题目组 {group_type} 含主观口语题但未配置 AI 接口，跳过该组")
                self._publish_processed_groups.add(key)
                return 'skipped_no_ai'
            if not self._publish_prepare_media(group):
                return False
            for question in questions:
                if self._should_stop() or not self._publish_group_in_scope(group):
                    return False
                if not self._publish_select_record_question(group, question):
                    print("    无法切换到口语小题，不提交")
                    return False
                if self.publish_executor.publish_record_uploaded(question):
                    print("    本题已有上传录音，保留现有结果")
                    filled += 1
                    continue
                answer = ''
                if question.needs_speech_script:
                    if group_type == 'oral_sentence_scoop_group':
                        prompt = ('将题目中的中文括注翻译成英文，填入相应空位，返回完整英文句子。'
                                  '去掉题号、空号、括号和中文提示，只返回可朗读的英文：\n' + question.text)
                    else:
                        prompt = self.prompt_builder.build_speaking_script(question, direction)
                    answer = self.ai_client.ask(
                        prompt, stop_requested=self.stop_requested,
                        screenshot=self._question_screenshot([question]),
                        require_image=self._requires_image_context([question]))
                    if not answer or self._should_stop():
                        print("    AI 未返回口语稿")
                        return False
                if self.publish_executor.execute(question, answer):
                    filled += 1
        else:
            official_answers = None
            api_provider = getattr(self, 'answer_api', None)
            if api_provider is not None and getattr(api_provider, 'active', False) \
                    and not getattr(api_provider, 'abandoned', False):
                official_answers = self._answer_api_publish_answers(questions, answer_index)
            if official_answers is not None:
                if not self._publish_prepare_media(group, skip_transcription=True):
                    return False
                adjusted_official, _ = self._accuracy_controller().apply_to_items(
                    list(zip(questions, official_answers)),
                    enforce_submit_floor=True,
                )
                print(f"    本组 {len(adjusted_official)} 题已使用官方答案填写，跳过 AI")
                self.publish_executor._exact_official_mode = True
                try:
                    for question, answer in zip(questions, adjusted_official):
                        if self._should_stop() or not self._publish_group_in_scope(group):
                            return False
                        if answer and self.publish_executor.execute(question, answer):
                            filled += 1
                finally:
                    self.publish_executor._exact_official_mode = False
            else:
                if not self._ai_available():
                    print(f"    题目组 {group_type} 未能通过官方答案完成且未配置 AI 接口，跳过该组")
                    self._publish_processed_groups.add(key)
                    return 'skipped_no_ai'
                if not self._publish_prepare_media(group, skip_transcription=False):
                    return False
                prompt = self._publish_build_group_prompt(
                    questions, direction, group_type=group_type
                )
                ai_response = self.ai_client.ask(
                    prompt, stop_requested=self.stop_requested,
                    screenshot=self._question_screenshot(questions),
                    require_image=self._requires_image_context(questions))
                if self._should_stop():
                    return False
                if not ai_response:
                    print("    AI 未返回答案")
                    return False
                prepared_items = self._publish_prepare_answers(
                    questions, ai_response, group=group
                )
                if prepared_items is None:
                    return False

                adjusted_answers, _ = self._accuracy_controller().apply_to_items(
                    prepared_items, enforce_submit_floor=True
                )
                for (question, _), answer in zip(prepared_items, adjusted_answers):
                    if self._should_stop() or not self._publish_group_in_scope(group):
                        return False
                    if answer and self.publish_executor.execute(question, answer):
                        filled += 1
        print(f"    本组填写 {filled}/{len(questions)} 题")

        if filled != len(questions):
            print("    本组未完整填写，不提交，停止当前任务以免留下半成品")
            return False

        if self._should_stop():
            return False

        PublishPlayer.reset(self.driver)
        popup_closed = self.publish_executor.dismiss_rules_popup()
        if popup_closed:
            print("    提交前已关闭「本单元学习规则」弹窗")

        with PublishFrameContext(self.driver) as submit_context:
            if not submit_context.entered:
                print("    提交前未能重新进入内容 iframe")
                return False
            submit_group = group
            if popup_closed:
                relocated = self._publish_find_group_by_key(key)
                if relocated is not None:
                    submit_group = relocated
            if not self._publish_wait_group_rendered(submit_group, timeout=6):
                print("    提交前题目组未渲染，跳过该组")
                self._publish_processed_groups.add(key)
                return 'skipped'
            if not self._publish_group_in_scope(submit_group):
                print("    提交前任务边界校验失败，不提交")
                return False
            button = self.publish_executor.find_submit_button(submit_group)
            print(f"    提交按钮状态: text={PublishPlayer.button_text(button)!r} "
                  f"displayed={button.is_displayed() if button is not None else None}")
            submitted_before = self._publish_submitted_count()
            group_index = self._publish_group_index(key)
            if self._should_stop():
                return False
            submitted_ok = self.publish_executor.submit(
                submit_group,
                timeout=30.0,
                resolver=lambda: (self._publish_find_group_by_key(key)
                                  or self._publish_group_at(group_index)))
            if not submitted_ok:
                rejection = getattr(self.publish_executor, 'last_rejection', '')
                if rejection:
                    print(f"    平台拒绝提交（{rejection}），该组保持未提交状态并继续后面题目")
                    self._publish_processed_groups.add(key)
                    return 'rejected'
                if self._publish_submitted_count() > submitted_before:
                    print("    已批改题目组数量增加，判定提交成功")
                    submitted_ok = True
            if not submitted_ok:
                submitted_ok = self._publish_confirm_graded_after_reload(group_index, key)
                if submitted_ok:
                    print("    重新进入播放器后确认已批改")
            if not submitted_ok:
                print("    提交失败或未进入已批改状态")
                return False

        self._publish_processed_groups.add(key)
        print("    本组已提交")
        return 'submitted'

    def _publish_confirm_graded_after_reload(self, index: int, key: str) -> bool:
        PublishPlayer.reset(self.driver)
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return False
            fresh = self._publish_find_group_by_key(key) or self._publish_group_at(index)
            if fresh is None or not self._publish_group_in_scope(fresh):
                return False
            if self.publish_parser.group_submitted(fresh):
                return True
            try:
                text = fresh.text or ''
            except Exception:
                text = ''
            return '得分' in text and '重新作答' in text

    def _publish_find_group_by_key(self, key: str):
        parser = self.publish_parser
        for state in self._publish_scoped_groups(parser.collect_groups()):
            if parser.group_key(state['element']) == key:
                return state['element']
        return None

    def _publish_group_index(self, key: str) -> int:
        parser = self.publish_parser
        try:
            for index, state in enumerate(parser.collect_groups()):
                if parser.group_key(state['element']) == key:
                    return index
        except Exception:
            pass
        return -1

    def _publish_group_at(self, index: int):
        if index < 0:
            return None
        try:
            groups = self.publish_parser.collect_groups()
        except Exception:
            return None
        if 0 <= index < len(groups):
            return groups[index]['element']
        return None

    def _publish_submitted_count(self) -> int:
        parser = self.publish_parser
        try:
            groups = self._publish_scoped_groups(parser.collect_groups())
        except Exception:
            return 0
        count = 0
        for state in groups:
            try:
                if parser.group_submitted(state['element']):
                    count += 1
            except Exception:
                continue
        return count

    def _publish_select_record_question(self, group, question: Question) -> bool:
        nodes = self.publish_parser._question_nodes(group)
        target = question.element.get_attribute('data-question-id')
        for _ in range(len(nodes) + 1):
            if self._should_stop():
                return False
            visible = [i for i, node in enumerate(nodes) if node.is_displayed()]
            desired = next((i for i, node in enumerate(nodes)
                            if node.get_attribute('data-question-id') == target), -1)
            if desired < 0:
                return False
            if desired in visible:
                return True
            if len(visible) != 1:
                return False
            label = '下一个' if visible[0] < desired else '上一个'
            buttons = group.find_elements(By.CSS_SELECTOR, '[class*="q-oral-header_handle-btn"]')
            button = next((item for item in buttons
                           if label in (item.text or '') and
                           'disabled' not in (item.get_attribute('class') or '').lower()), None)
            if not button or not WebDriverHelper.safe_click(self.driver, button):
                return False
            if self.stop_requested.wait(0.3):
                return False
        return False

    def _publish_prepare_media(self, group, *, skip_transcription: bool = False) -> bool:
        if self._should_stop():
            return False
        wrappers = group.find_elements(
            By.CSS_SELECTOR,
            '[data-lexical-node-type="insert-audio"], [data-lexical-node-type="insert-video"]')
        for wrapper in wrappers:
            kind = wrapper.get_attribute('data-lexical-node-type') or ''
            is_video = kind == 'insert-video'
            media = wrapper.find_elements(By.TAG_NAME, 'video' if is_video else 'audio')
            if not media:
                print("    音视频播放器未加载，不提交")
                return False
            if not self.publish_executor.play_material(media[0]):
                print("    未能确认音视频播放，不提交")
                return False
            if skip_transcription:
                continue
            url = (wrapper.get_attribute('data-video-url') if is_video
                   else media[0].get_attribute('src')) or ''
            if urlsplit(url).scheme not in ('http', 'https'):
                print("    未找到可下载的音视频材料，不提交")
                return False
            transcript = self.video_handler.transcriber.transcribe(url.split('#')[0], language='en')
            if not (transcript or '').strip() or self._should_stop():
                print("    音视频转写失败，不提交")
                return False
            if is_video:
                self.ai_client.add_video_transcript_if_new(transcript)
            else:
                self.ai_client.add_audio_transcript_if_new(transcript)
        return not self._should_stop()

    def _publish_dropdown_options_prompt(self, questions: List[Question]) -> str:
        if not questions:
            return ''
        lines = []
        offset = 0
        for question in questions:
            blanks = self.publish_executor._dropdown_blanks(question)
            collected_blanks = []
            for local_index, blank in enumerate(blanks):
                index = offset + local_index + 1
                try:
                    options = self.publish_executor.collect_dropdown_options(blank)
                except Exception:
                    options = []
                collected_blanks.append({'index': local_index, 'options': list(options)})
                print(f"    空{index} 候选抓取: {len(options)} 个")
                if options:
                    lines.append(f"   空{index} 候选: {' / '.join(options)}")
            if collected_blanks:
                question.banked_blanks = collected_blanks
                all_opts = list(dict.fromkeys(
                    opt for b in collected_blanks for opt in b['options'] if opt
                ))
                if all_opts and not question.banked_options:
                    question.banked_options = all_opts
            offset += question.answer_count or len(blanks) or 1
        if not lines:
            return ''
        return ("\n【空位候选】每个空只能从下列候选中选择，尽量按原样（含大小写）作答：\n"
                + "\n".join(lines) + "\n")

    def _publish_fill_format_hint(self, questions: List[Question]) -> str:
        fill_questions = [q for q in questions
                          if q.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT)]
        total_blanks = sum(q.answer_count or 1 for q in fill_questions)
        if len(fill_questions) < 2 or total_blanks < 2:
            return ''
        ranges = []
        offset = 0
        for question in fill_questions:
            count = question.answer_count or 1
            ranges.append(f"小题{question.number} 的局部空号 1..{count} 对应整组空号 "
                          f"{offset + 1}..{offset + count}")
            offset += count
        return (
            "\n【本题组格式】\n"
            f"本题目组共 {len(fill_questions)} 道小题、{total_blanks} 个空，"
            f"请按空号 1..{total_blanks} 连续编号作答，"
            "不要按小题再分组，也不要在答案里加括号或额外题号，"
            "例如：1.answer1 2.answer2 3.answer3\n"
            + '\n'.join(ranges) + '\n'
        )

    def _publish_build_group_prompt(self, questions: List[Question], direction: str,
                                    group_type: str = '', kind: str = '') -> str:
        prompt = self.prompt_builder.build(questions, direction)
        prompt += self._publish_fill_format_hint(questions)
        if group_type == 'fill_blanks_dropdown_group' or kind == 'dropdown':
            prompt += self._publish_dropdown_options_prompt(questions)
        return prompt

    def _publish_prepare_answers(self, questions: List[Question], ai_response: str,
                                 *, group=None) -> Optional[List[Tuple[Question, str]]]:
        total_blanks = sum(
            question.answer_count or 1
            for question in questions
            if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT)
        )
        blank_offset = 0
        prepared_items: List[Tuple[Question, str]] = []
        for question in questions:
            if self._should_stop() or (group is not None and not self._publish_group_in_scope(group)):
                return None
            answer = self._publish_answer_text(
                question, ai_response, blank_offset, total_blanks=total_blanks
            )
            if question.q_type == QuestionType.FILL_IN:
                answer = self._normalize_fill_answer_capitalization(question, answer)
            prepared_items.append((question, answer or ''))
            if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT):
                blank_offset += question.answer_count or 1
        return prepared_items

    def _publish_wait_group_rendered(self, group, timeout: float = 12.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._should_stop():
                return False
            try:
                length = self.driver.execute_script(
                    "return arguments[0] ? (arguments[0].innerText || '').length : 0;", group)
            except Exception:
                length = 0
            if length:
                return True
            if self.stop_requested.wait(0.5):
                return False
        return False

    def _publish_answer_text(self, question: Question, ai_response: str,
                             blank_offset: int = 0, total_blanks: int = 0) -> str:
        if question.q_type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
                               QuestionType.TRUE_FALSE, QuestionType.SORTING):
            return self._extract_single_answer(ai_response, question.number)
        if question.q_type == QuestionType.LINK_MATCH:
            pairs = re.findall(r'(?<![\d.])(\d{1,2})\s*[.\-:：)）]\s*([A-Za-z])(?![A-Za-z])',
                               ai_response or '')
            return ' '.join(f'{n}.{letter}' for n, letter in pairs)
        answer = ai_response
        if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT):
            answer = self._sanitize_fill_answer(answer)
            count = question.answer_count or 1
            if blank_offset or total_blanks > count:
                return self._shift_blank_answer(answer, blank_offset, count, total_blanks)
        return answer

    @staticmethod
    def _sanitize_fill_answer(answer: str) -> str:
        cleaned = re.sub(r'(?<=\d\.)\s*\(\s*\d+\s*\)', ' ', answer or '')
        return re.sub(r'[ \t]+', ' ', cleaned).strip()

    def _normalize_fill_answer_capitalization(self, question: Question, answer: str) -> str:
        count = question.answer_count or 1
        values = AnswerExecutor._parse_banked_answer(answer, count)
        if not values or not any(values):
            return answer
        values = self._capitalize_sentence_initial_answers(question.text, values)
        return ' '.join(f'{index + 1}.{value}'
                        for index, value in enumerate(values) if value)

    @staticmethod
    def _capitalize_sentence_initial_answers(text: str, values: List[str]) -> List[str]:
        result = []
        for index, value in enumerate(values, 1):
            marker = f'({index})'
            position = (text or '').find(marker)
            needs_capital = position == 0
            if position < 0:
                result.append(value)
                continue
            if not needs_capital:
                prefix = text[:position].rstrip()
                needs_capital = (not prefix) or prefix[-1] in '.!?;:。！？；：'
            if needs_capital and value and value[0].isalpha():
                value = value[0].upper() + value[1:]
            result.append(value)
        return result

    @staticmethod
    def _shift_blank_answer(answer: str, offset: int, count: int,
                            total_blanks: int = 0) -> str:
        parse_count = max(offset + count, total_blanks)
        parsed = AnswerExecutor._parse_banked_answer(answer, parse_count)
        values = parsed[offset:offset + count]
        return ' '.join(f'{index + 1}.{value}'
                        for index, value in enumerate(values) if value)

    def _publish_advance_page(self) -> bool:
        before_url = self._publish_current_url()
        before_text = self._publish_content_length()
        if not self._publish_click_next():
            return False

        for _ in range(20):
            if self._should_stop():
                return False
            if self._publish_current_url() != before_url:
                return True
            after_text = self._publish_content_length()
            if after_text and after_text != before_text:
                return True
            time.sleep(0.5)
        print("    点击下一节后内容未变化")
        return False

    def _publish_current_url(self) -> str:
        try:
            return self.driver.current_url or ''
        except Exception:
            return ''

    def _publish_content_length(self) -> int:
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return 0
            try:
                return self.driver.execute_script(
                    'return document.body ? document.body.innerText.length : 0;') or 0
            except Exception:
                return 0

    def _publish_click_next(self) -> bool:
        with PublishFrameContext(self.driver) as context:
            if context.entered:
                button = self._publish_find_next_button()
                if button is not None:
                    print("    尝试进入下一节（内容层）...")
                    if WebDriverHelper.safe_click(self.driver, button):
                        time.sleep(2.5)
                        return True

        PublishPlayer.reset(self.driver)
        button = self._publish_find_next_button()
        if button is None:
            return False
        print("    尝试进入下一节（外壳层）...")
        if not WebDriverHelper.safe_click(self.driver, button):
            return False
        time.sleep(2.5)
        return True

    def _publish_find_next_button(self):
        keywords = ('下一节', '下一课', '下一题', '下一页', '下一个', 'next')
        blocked = PublishPlayer.SUBMITTED_KEYWORDS + ('提交', '上一')
        try:
            candidates = self.driver.find_elements(
                By.CSS_SELECTOR,
                'button, a, [role="button"], div[class*="btn"], div[class*="next"]')
        except Exception:
            return None
        for element in candidates[:500]:
            try:
                if not element.is_displayed() or not element.is_enabled():
                    continue
                haystack = (PublishPlayer.button_text(element)
                            + (element.get_attribute('aria-label') or '')
                            + (element.get_attribute('title') or '')).lower()
            except Exception:
                continue
            if any(word in haystack for word in blocked):
                continue
            if any(word in haystack for word in keywords):
                return element
        return None

    def _extract_single_answer(self, ai_response: str, question_number: int) -> str:
        answer = AnswerExecutor._extract_answer_by_number(ai_response, question_number)
        if answer:
            return answer
        if re.search(AnswerExecutor.NUMBER_PREFIX_PATTERN, ai_response, re.IGNORECASE):
            return ""
        lines = [l.strip() for l in ai_response.split('\n') if l.strip()]
        if 1 <= question_number <= len(lines):
            return lines[question_number - 1]
        return ""

    def _generate_questions_signature(self, questions: List[Question]) -> str:
        if not questions:
            return "empty"

        parts = []
        for q in questions:
            q_type = q.q_type.name if q.q_type else "UNKNOWN"
            text_preview = re.sub(r'\s+', ' ', q.text or '').strip()
            if q.q_type in (QuestionType.RECORD, QuestionType.ORAL_ALOUD):
                stable = [(item.get('prompt') or item.get('text') or '').strip()
                          for item in (q.record_items or [])]
                stable = [value for value in stable if value]
                text_preview = '|'.join(stable)
            option_preview = "|".join(opt.text[:30] for opt in q.options[:4])
            page_progress = ""
            if q.q_type == QuestionType.RECORD:
                try:
                    page_progress = self.driver.execute_script("""
                        const progress = document.querySelector('.comp-question-progress .percent');
                        return progress ? progress.style.width : '';
                    """) or ""
                except Exception:
                    pass
            parts.append(f"{q.number}:{q_type}:{text_preview}:{option_preview}:{page_progress}")

        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

    def solve_current_page(self, chapter_name: str = "unknown") -> bool:
        print("\n" + "=" * 60)
        print(" 开始处理当前停留的页面")
        print("=" * 60)

        self._publish_processed_groups = set()
        self._inline_learning_tried = False
        self._reading_ctx_tried = False
        self._pending_reading_context = None
        self._accuracy_controller().reset()

        quick_tab_name = f"quick_current_page_{int(time.time())}"
        self.ai_client.force_reset(f"{chapter_name}_{quick_tab_name}")

        state_key = self._generate_content_hash()

        if not state_key or state_key == "empty":
            state_key = f"{chapter_name}_{int(time.time())}"

        print(f"    内容标识: {state_key[:50]}...")

        if state_key in self.processed_hashes:
            print(f"   【继续】该页面的哈希已被记录，正在执行作答...")

        self.processed_hashes.add(state_key)

        try:
            page_text = (self.driver.find_element(By.TAG_NAME, 'body').text or "").lower()
        except Exception:
            page_text = ""
        if 'collocation' in page_text or 'from the texts' in page_text:
            self._pending_reading_context = {'mode': 'current_page'}

        success = self._process_current_tab_content(
            chapter_name,
            quick_tab_name,
            0,
            0
        )

        print(f"\n{'=' * 60}")
        print(" 当前页面处理已停止" if self._should_stop() else
              (" 当前页面处理完毕" if success else " 当前页面未处理完成"))
        print(f"{'=' * 60}")
        return success

    def _find_next_question_button(self) -> Optional[Any]:
        selectors = [
            '.next-question-btn:not(.disabled)',
            '.btn-next:not([disabled])',
            'button.next:not(.disabled)',
            '.pagination-next:not(.disabled)',
            '.question-next:not(.disabled)',
            '.action.next:not(.disabled)',
            '.submit-bar-pc--btn-next:not(.disabled)',
        '.next-btn:not(.disabled)',
            '[class*="next"]:not(.disabled)',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
            'a[class*="btn"]',
        ]

        for selector in selectors:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled():
                        text = btn.text.lower()
                        aria_label = (btn.get_attribute('aria-label') or '').lower()
                        if any(k in text or k in aria_label for k in ['下一题', 'next', '下一页', 'next question']):
                            return btn
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue

        try:
            all_btns = self.driver.find_elements(By.CSS_SELECTOR, 'button, a')
            for btn in all_btns:
                if not btn.is_displayed():
                    continue
                text = btn.text.lower()
                if any(k in text for k in ['下一题', 'next question', '下一页', 'next']):
                    if 'submit' not in text and '提交' not in text:
                        return btn
        except:
            pass

        return None

    def _generate_content_hash_from_direction(self) -> str:
        if self._is_publish_page():
            return hashlib.md5(self._publish_current_url().encode()).hexdigest()[:16]
        try:
            direction_elem = WebDriverHelper.safe_find_element(
                self.driver,
                ['.abs-direction', '.layout-direction-container', '.direction-container']
            )
            if direction_elem:
                text = direction_elem.text.strip()
                if text:
                    return hashlib.md5(text.encode()).hexdigest()[:16]
        except:
            pass
        return "empty"

    def _generate_content_hash(self) -> str:
        if self._is_publish_page():
            return hashlib.md5(self._publish_current_url().encode()).hexdigest()[:16]
        try:
            direction_elem = WebDriverHelper.safe_find_element(
                self.driver,
                ['.abs-direction', '.layout-direction-container', '.discussion-title']
            )
            if direction_elem:
                text = direction_elem.text.strip()
                if text:
                    return hashlib.md5(text.encode()).hexdigest()[:16]

            questions, _ = self.parser.parse_all()
            if questions:
                content = "|".join([f"{q.number}:{q.text[:30]}" for q in questions[:3]])
                return hashlib.md5(content.encode()).hexdigest()[:16]

            body = self.driver.find_element(By.TAG_NAME, 'body').text[:300]
            return hashlib.md5(body.encode()).hexdigest()[:16]

        except Exception as e:
            error_msg = str(e)
            print(f"    生成哈希失败:{error_msg[:50]} ")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return "empty"

    def _get_level1_tabs(self) -> List[Dict]:
        tabs = []
        elements = WebDriverHelper.safe_find_elements(self.driver, Selectors.LEVEL1_TABS)
        seen = set()
        for elem in elements:
            try:
                title = elem.get_attribute('title') or elem.text.strip().split('\n')[0]
                if title and title not in seen and len(title) < 50:
                    seen.add(title)
                    tabs.append({
                        'element': elem,
                        'title': title,
                        'is_active': 'activity' in (elem.get_attribute('class') or '').lower()
                    })
            except:
                continue
        return tabs

    def _get_level2_tabs(self) -> List[Dict]:
        tabs = []
        container = WebDriverHelper.safe_find_element(
            self.driver,
            ['.pc-header-tasks-container', '.pc-header-tasks-layout']
        )
        if not container:
            return tabs
        elements = WebDriverHelper.safe_find_elements(
            self.driver,
            Selectors.LEVEL2_TABS,
            parent=container
        )
        seen = set()
        for elem in elements:
            try:
                title = elem.text.strip().split('\n')[0]
                if title and title not in seen and len(title) < 50:
                    seen.add(title)
                    tabs.append({
                        'element': elem,
                        'title': title,
                        'is_active': 'activity' in (elem.get_attribute('class') or '').lower()
                    })
            except:
                continue
        return tabs

    def _extract_passage(self) -> str:
        selectors = [
            '.question-common-abs-material',
            '.text-material-wrapper',
            '.reading-passage',
            '.passage-content',
        ]

        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elems:
                    if elem.find_elements(By.TAG_NAME, 'video'):
                        continue
                    text = elem.text.strip()
                    if len(text) > 200:
                        return text
            except:
                continue

        return ""

    def _handle_confirm_dialog(self):
        self._last_dialog_kind = None
        self._last_dialog_text = ''
        if self._should_stop():
            return False
        try:
            res = self.driver.execute_script("""
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const vis = el => !!el && !!el.getClientRects().length;
                const containers = [...document.querySelectorAll(
                    '[role="dialog"], .ant-modal, .ant-modal-confirm, [class*="modal"], [class*="dialog"]'
                )].filter(el => vis(el) && !el.classList.contains('ant-modal-mask'));
                if (!containers.length) return null;

                for (let i = containers.length - 1; i >= 0; i--) {
                    const modal = containers[i];
                    const modalText = clean(modal.innerText);
                    if (!modalText) continue;

                    const isValidationError = /还有.*没有作答|请完成后提交|请先作答|字数不足|不能少于|请先上传/.test(modalText);
                    const isInfoOverlay = /本单元学习时间|学习截止时间已过|是否必修/.test(modalText) ||
                        !!modal.querySelector('.system-info-cloud-ok-button');

                    const btns = [...modal.querySelectorAll('button, a.btn, .btn, a')].filter(vis);
                    let target = null;
                    if (isValidationError || isInfoOverlay) {
                        target = btns.find(b => b.classList && b.classList.contains('system-info-cloud-ok-button')) ||
                            btns.find(b => /我知道了|确定|确认|ok/i.test(clean(b.innerText))) ||
                            btns[0];
                        if (target) {
                            target.click();
                            return {
                                kind: isValidationError ? 'validation_error' : 'info_overlay',
                                text: modalText.slice(0, 120)
                            };
                        }
                    } else {
                        for (const b of btns) {
                            const txt = clean(b.innerText).replace(/\\s+/g, '');
                            if (!txt || /取消|cancel|关闭|close|返回/i.test(txt)) continue;
                            if (/确认提交|确认|确定|提交|我知道了|继续|ok|submit/i.test(txt)) {
                                target = b;
                                break;
                            }
                        }
                        if (target) {
                            target.click();
                            return {kind: 'confirm', text: modalText.slice(0, 120)};
                        }
                    }
                }
                return null;
            """)
            if isinstance(res, dict) and res.get('kind'):
                self._last_dialog_kind = res['kind']
                self._last_dialog_text = res.get('text', '')
                if self.stop_requested.wait(1):
                    return False
                return True
        except Exception:
            pass
        return False

    def _page_needs_answer(self) -> bool:
        driver = getattr(self, 'driver', None)
        if driver is None:
            return True
        for selector in ANSWER_WIDGET_SELECTORS:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            if not isinstance(elements, (list, tuple)):
                continue
            for element in elements:
                try:
                    if element.is_displayed():
                        return True
                except Exception:
                    continue
        return False

    def _release_transcriber(self):
        handler = getattr(self, 'video_handler', None)
        transcriber = getattr(handler, 'transcriber', None)
        if transcriber is None:
            return
        try:
            transcriber.release_local_model()
        except Exception as exc:
            print(f"   释放 Whisper 模型失败: {str(exc)[:60]}")

    def _skip_transcription(self, tab_name: str = "") -> bool:
        title = (tab_name or "").lower()
        retry = 0.6 if any(word in title for word in SKIP_ASR_TITLE_KEYWORDS) else 0.0
        for attempt in range(3):
            if self._page_needs_answer():
                return False
            if not retry or attempt == 2:
                break
            time.sleep(retry)
        return True

    def _requires_image_context(self, questions) -> bool:
        choice_types = {
            QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
            QuestionType.LISTENING_CHOICE, QuestionType.VIDEO_CHOICE,
        }
        for question in questions:
            if question.q_type not in choice_types:
                continue
            elements = [getattr(question, 'element', None)]
            elements.extend(option.element for option in question.options)
            for element in elements:
                if element is None:
                    continue
                try:
                    if element.find_elements(By.CSS_SELECTOR, 'img[src], img[data-src]'):
                        return True
                except Exception:
                    continue
        return False

    def _question_screenshot(self, questions) -> Optional[bytes]:
        if (not getattr(self.config, 'screenshot', False)
                and not self._requires_image_context(questions)):
            return None
        elements = []
        for question in questions:
            element = getattr(question, 'element', None)
            if element is not None:
                elements.append(element)
            elements.extend(option.element for option in question.options
                            if getattr(option, 'element', None) is not None)
        elements = [element for element in elements if element is not None]
        if not elements:
            return None
        try:
            size = self.driver.execute_script("return [window.innerWidth, window.innerHeight];")
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': max(1400, int(size[0])), 'height': max(1200, int(size[1])),
                'deviceScaleFactor': 1, 'mobile': False})
            try:
                time.sleep(0.5)
                self.driver.execute_script("""
                    const btn = [...document.querySelectorAll('button, span, div')].find(e => {
                        if (e.children.length || !e.getClientRects().length) return false;
                        const t = (e.innerText || '').trim();
                        return /^(我知道了|知道了)$/.test(t);
                    });
                    if (!btn) return false;
                    const opts = {bubbles: true, cancelable: true, view: window};
                    ['mouseover', 'mousedown', 'mouseup', 'click'].forEach(ev =>
                        btn.dispatchEvent(new MouseEvent(ev, opts)));
                    return true;
                """)
                time.sleep(0.6)
                rect = self.driver.execute_script("""
                    const els = arguments[0];
                    if (!els.length) return null;
                    els[0].scrollIntoView({block: 'start'});
                    const sx = window.scrollX, sy = window.scrollY;
                    let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
                    for (const el of els) {
                        for (const target of [el, ...el.querySelectorAll('table')]) {
                            const r = target.getBoundingClientRect();
                            if (!r.width || !r.height) continue;
                            left = Math.min(left, r.left + sx);
                            top = Math.min(top, r.top + sy);
                            right = Math.max(right, r.right + sx);
                            bottom = Math.max(bottom, r.bottom + sy);
                        }
                    }
                    if (!isFinite(left) || !isFinite(top)) return null;
                    return {x: Math.max(0, left), y: Math.max(0, top),
                            w: right - left, h: Math.min(bottom - top, 6000)};
                """, elements)
                if not rect:
                    return None
                width, height = int(rect.get('w') or 0), int(rect.get('h') or 0)
                if width < 40 or height < 20:
                    return None
                scale = 1.5 if width <= 900 else 1.0
                shot = self.driver.execute_cdp_cmd('Page.captureScreenshot', {
                    'format': 'png',
                    'captureBeyondViewport': True,
                    'clip': {'x': rect['x'], 'y': rect['y'], 'width': width,
                             'height': height, 'scale': scale},
                })
                return base64.b64decode(shot.get('data') or '')
            finally:
                self.driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
                time.sleep(0.3)
        except Exception as exc:
            print(f"    题目截图失败: {str(exc)[:60]}")
            return None

    def _preprocess_audio_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int,
                                    skip_transcription: Optional[bool] = None,
                                    on_transcript: Optional[Callable[[str], None]] = None):
        if self._should_stop():
            return False
        if self._has_video_on_page():
            return True

        if skip_transcription is None:
            skip_transcription = self._skip_transcription(tab_name)
        if skip_transcription:
            print("   该页不需要作答，跳过音频转写")
            return True

        if find_record_buttons(self.driver) or AudioStrategy.is_audio_only_page(self.driver):
            return True

        if not self._has_audio_on_page():
            return True

        try:
            audio_urls = self._extract_audio_urls_from_page()
            if not audio_urls:
                print("   未找到有效音频URL，停止当前任务")
                return False

            if self._processed_audio_context is not self.ai_client.material_messages:
                self._processed_audio_tabs.clear()
                self._processed_audio_context = self.ai_client.material_messages
            pending = [url for url in audio_urls
                       if url.split('#')[0] not in self._processed_audio_tabs]
            if not pending:
                return True

            for index, audio_url in enumerate(pending, 1):
                if self._should_stop():
                    return False
                audio_key = audio_url.split('#')[0]
                print(f"   音频转写 {index}/{len(pending)}...")
                transcript = self.video_handler.transcriber.transcribe(
                    audio_url, language="en")

                if self._should_stop():
                    return False
                if transcript and transcript.strip():
                    self.ai_client.add_audio_transcript_if_new(transcript)
                    print(f"   音频转写完成（{len(transcript)}字符）")
                else:
                    print("   音频转写为空")
                    return False

                self._processed_audio_tabs.add(audio_key)

            if on_transcript:
                try:
                    on_transcript("audio")
                except Exception as exc:
                    print(f"   预取答案启动失败: {str(exc)[:60]}")
            return True

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"音频预处理异常: {e}", exc_info=True)
            return False

    def _has_audio_on_page(self) -> bool:
        try:
            audios = self.driver.find_elements(By.TAG_NAME, 'audio')
            if any(a.is_displayed() or a.get_attribute('src') for a in audios):
                return True

            audio_containers = self.driver.find_elements(By.CSS_SELECTOR, '.audio-material-wrapper, .question-audio')
            if audio_containers:
                return True

            try:
                direction = self.driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
                text = direction.text.lower()
                if any(kw in text for kw in ['listen', 'audio', 'hear', 'talk', 'conversation']):
                    if self._extract_audio_url_from_page():
                        return True
            except:
                pass

            return False
        except:
            return False

    def _extract_audio_urls_from_page(self) -> List[str]:
        urls: List[str] = []
        try:
            elems = self.driver.find_elements(By.CSS_SELECTOR, 'audio, audio source')
        except Exception:
            return urls
        for elem in elems:
            try:
                src = (elem.get_attribute('src') or '').strip()
            except Exception:
                continue
            if not src:
                continue
            key = src.split('#')[0]
            if key and key not in urls:
                urls.append(key)
        return urls

    def _extract_audio_url_from_page(self) -> Optional[str]:
        urls = self._extract_audio_urls_from_page()
        return urls[0] if urls else None

    def _preprocess_video_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int,
                                    skip_transcription: Optional[bool] = None,
                                    on_transcript: Optional[Callable[[str], None]] = None):
        if self._should_stop():
            return False
        if not self._has_video_on_page():
            return True

        if skip_transcription is None:
            skip_transcription = self._skip_transcription(tab_name)
        skip_transcription = bool(skip_transcription)
        self.video_handler.skip_transcription = skip_transcription

        try:
            video_info = self.video_handler._get_video_info()
            if not video_info:
                print("   未找到可播放的视频，停止当前任务")
                return False
            tab_key = f"{tab_name}|{l1_idx}|{l2_idx}"
            video_url = (video_info or {}).get('url', '')
            video_key = video_url.split('#')[0] if video_url else tab_key
            if video_key in self._processed_video_tabs:
                transcript = self._video_transcripts[video_key]
                self.ai_client.add_video_transcript_if_new(transcript)
                if on_transcript:
                    on_transcript(transcript)
                return True

            if video_key in self._skipped_video_tabs:
                if skip_transcription:
                    return True
                print("   补做视频转写...")
                self.video_handler.skip_transcription = False
                transcript = self.video_handler.ensure_transcript(video_url)
                if transcript and transcript.strip():
                    self.ai_client.add_video_transcript_if_new(transcript)
                    print(f"   视频转写完成（{len(transcript)}字符）")
                    if on_transcript:
                        on_transcript(transcript)
                    self._processed_video_tabs.add(video_key)
                    self._video_transcripts[video_key] = transcript
                    self._skipped_video_tabs.discard(video_key)
                    return True
                else:
                    print("   视频转写失败")
                    return False

            if skip_transcription:
                print("   只播放，不转写")
            else:
                print("   播放并转写视频...")
            injected = {'done': False}

            def _inject_transcript_then_notify(transcript):
                if transcript and transcript.strip():
                    self.ai_client.add_video_transcript_if_new(transcript)
                    injected['done'] = True
                if injected['done'] and on_transcript:
                    on_transcript(transcript)

            completed = self.video_handler._play_video_and_handle_popups(
                on_transcript=_inject_transcript_then_notify)

            if self._should_stop() or not completed:
                return False
            transcript = self.video_handler.video_transcript
            if transcript and transcript.strip():
                if not injected['done']:
                    self.ai_client.add_video_transcript_if_new(transcript)
                print(f"   视频转写完成（{len(transcript)}字符）")
                self._processed_video_tabs.add(video_key)
                self._video_transcripts[video_key] = transcript
                if hasattr(self.video_handler, 'mark_preprocessed_played'):
                    self.video_handler.mark_preprocessed_played(
                        video_key=video_key,
                        video_url=video_url,
                        tab_key=tab_key,
                        video_element=video_info.get('element'),
                    )
                return True
            elif skip_transcription:
                self._skipped_video_tabs.add(video_key)
                if hasattr(self.video_handler, 'mark_preprocessed_played'):
                    self.video_handler.mark_preprocessed_played(
                        video_key=video_key,
                        video_url=video_url,
                        tab_key=tab_key,
                        video_element=video_info.get('element'),
                    )
                return True
            else:
                print("   视频转写失败")
                self._skipped_video_tabs.add(video_key)
                return False

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"视频预处理异常: {e}", exc_info=True)
            return False

    def _has_video_on_page(self) -> bool:
        try:
            videos = self.driver.find_elements(By.TAG_NAME, 'video')
            if not videos:
                videos = self.driver.find_elements(
                    By.CSS_SELECTOR, '.video-js, .video-box, .question-video-player')
            return any(v.is_displayed() for v in videos)
        except Exception:
            return False

    def _wait_for_submit_complete(self, timeout: int = 18, grading_timeout: int = 60) -> bool:
        if self.stop_requested.wait(1.5):
            return False
        start = time.time()
        effective_timeout = timeout
        resubmit_count = 0
        last_click_elapsed = 0.0
        grading_notified = False

        while not self._should_stop():
            elapsed = time.time() - start
            if elapsed >= effective_timeout:
                break
            try:
                self._last_dialog_kind = None
                self._last_dialog_text = ''
                if self._handle_confirm_dialog():
                    dialog_kind = getattr(self, '_last_dialog_kind', None)
                    if dialog_kind == 'validation_error':
                        dialog_text = str(getattr(self, '_last_dialog_text', '') or '')[:80]
                        print(f"   提交被页面拦截（{dialog_text}），保留任务为未完成")
                        return False
                    if dialog_kind == 'info_overlay':
                        net_state = _read_submit_network_state(self.driver)
                        no_req = net_state is None or (
                            net_state.get('reqCount', 0) == 0 and net_state.get('inFlight', 0) == 0
                        )
                        if no_req and resubmit_count < 2:
                            print("   已关闭提示遮罩，重新点击提交")
                            if self.executor.submit():
                                resubmit_count += 1
                                last_click_elapsed = elapsed
                    continue
                submit_btn = self._find_visible_submit_button()
                if submit_btn is None:
                    print(f"   提交按钮已消失，提交完成")
                    return True

                body_text = (self.driver.find_element(By.TAG_NAME, 'body').text or '').lower()
                if re.search(r'(?<!未)(?:提交成功|保存成功|已提交)|'
                             r'\b(?:successfully\s+(?:submitted|saved)|'
                             r'(?:submitted|saved)\s+successfully)\b', body_text):
                    print(f"   检测到成功提示")
                    return True

                net_state = _read_submit_network_state(self.driver)
                toast_text = _read_submit_progress_toast(self.driver)
                grading_active = (
                    (isinstance(net_state, dict)
                     and (net_state.get('gradingPending') or net_state.get('inFlight', 0) > 0))
                    or any(kw in toast_text for kw in ('正在提交', '评测中', '评分中'))
                )
                if grading_active:
                    if effective_timeout < grading_timeout:
                        effective_timeout = max(timeout, grading_timeout)
                    if not grading_notified:
                        print(f"   检测到平台提交/智能评测进行中，延长等待上限至 {effective_timeout}s...")
                        grading_notified = True
                elif (isinstance(net_state, dict)
                      and net_state.get('reqCount', 0) == 0
                      and net_state.get('inFlight', 0) == 0
                      and not net_state.get('gradingPending')
                      and resubmit_count < 2
                      and (elapsed - last_click_elapsed) >= 4.0):
                    print(f"   尚未检测到提交请求，尝试重新点击提交按钮（第 {resubmit_count + 1}/2 次）")
                    if self.executor.submit():
                        resubmit_count += 1
                        last_click_elapsed = elapsed

            except Exception:
                pass

            if self.stop_requested.wait(0.5):
                return False

        print(f"   等待提交确认超时（{effective_timeout}s），保留任务为未完成")
        diag = _describe_submit_timeout_state(self.driver, _read_submit_network_state(self.driver))
        if diag:
            print(f"   超时现场诊断: {diag}")
        return False

    def _find_visible_submit_button(self) -> Optional[Any]:
        selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button.submit-btn',
            'button[type="submit"]',
            '.question-common-course-page a.btn',
            '.question-common-course-page .btn',
            'a.btn',
            '.btn',
        ]

        for selector in selectors:
            try:
                elems = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elems:
                    if elem.is_displayed() and AnswerExecutor._is_submit_button(elem):
                        return elem
            except:
                continue

        return None

    def _wait_for_content_change(self, previous_signature: str, timeout: int = 10) -> bool:
        start_time = time.time()
        check_interval = 0.5

        while time.time() - start_time < timeout and not self._should_stop():
            try:
                questions, _ = self.parser.parse_all()
                current_signature = self._generate_questions_signature(questions)

                if current_signature != previous_signature and current_signature != "empty":
                    print(f"         内容已变化: {previous_signature[:8]}... -> {current_signature[:8]}...")
                    return True

            except Exception as e:
                logger.debug(f"等待内容变化时出错: {e}")

            if self.stop_requested.wait(check_interval):
                return False

        print(f"         等待内容变化超时")
        return False


class UCampusBot:

    def __init__(self, config_path: str = 'config.json', skip_check: bool = False):
        self.config = Config.from_json(config_path)
        self.browser = get_spec(self.config.browser)
        self._shutdown_event = threading.Event()
        self._background_threads = []
        self.driver = None

        if not skip_check:
            self._ensure_environment()

        self.driver = self._create_driver()
        self.popup_watcher = PopupWatcher(self.driver)

    def _ensure_environment(self):
        checker = EnvironmentChecker(self.browser.key)

        if not checker.check_all():
            while True:
                choice = checker.show_fix_guide()

                if choice == '1':
                    checker.auto_install_browser()
                    sys.exit(0)

                elif choice == '2':
                    if checker.auto_install_ffmpeg():
                        sys.exit(0)

                elif choice == '3':
                    if checker.add_ffmpeg_to_path():
                        sys.exit(0)

                elif choice == '4':
                    ffmpeg_path = checker.manual_specify_path()
                    if ffmpeg_path:
                        bin_dir = os.path.dirname(ffmpeg_path)
                        checker._add_to_system_path(bin_dir)
                        print(f" FFmpeg 已添加到 PATH: {bin_dir}")
                        print("请重新运行程序")
                        input("按回车键退出...")
                        sys.exit(0)

                elif choice == '5':
                    self._show_detailed_help()
                    input("\n按回车键退出...")
                    sys.exit(1)

                elif choice == 'Q':
                    sys.exit(1)

                else:
                    print("无效选项，请重新选择")

    def _show_detailed_help(self):
        name = self.browser.display_name
        hints = " / ".join(self.browser.manual_install_hint)
        if self.browser.supports_auto_install:
            browser_fix = f"       解决：选择 [1] 自动安装，或手动访问 {hints}"
        else:
            browser_fix = f"       解决：这个浏览器不提供自动安装，请手动下载：{hints}"

        print(f"""
    【问题诊断】

    1. {name} 浏览器问题
       原因：{name} 未安装，或安装在非默认位置
{browser_fix}
       如需改用另一种浏览器：在 config.json 里修改 "browser"（edge / chrome）后重启

    2. FFmpeg 问题（语音识别必需）
       原因：未安装 FFmpeg 或未添加到系统 PATH
       解决：
          - 方法A（推荐）：选择 [2] 自动下载安装（约130MB）
          - 方法B：选择 [3] 将已安装的 FFmpeg 添加到 PATH
          - 方法C：手动下载 https://ffmpeg.org/download.html
            解压后将 bin 目录添加到系统环境变量 PATH

    3. 验证 FFmpeg 安装
       打开 CMD 输入: ffmpeg -version
       应显示版本信息，如 "ffmpeg version 6.0"

    【手动安装 FFmpeg 步骤】

    1. 访问 https://ffmpeg.org/download.html
    2. 点击 Windows 图标，选择 "Windows builds from gyan.dev"
    3. 下载 "ffmpeg-release-essentials.zip"
    4. 解压到 C:\ffmpeg
    5. 将 C:\ffmpeg\bin 添加到系统环境变量 PATH
    6. 重启终端，输入 ffmpeg -version 验证
    """)

    COMPAT_BROWSER_ARGS = ('--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                           '--disable-extensions')

    MIC_BROWSER_ARGS = ('--use-fake-ui-for-media-stream',
                        '--autoplay-policy=no-user-gesture-required')

    def _start_browser(self, temp_dir: str, extra_args=()):
        options_cls, webdriver_cls, service_cls = driver_api(self.browser)
        options = options_cls()

        install_path = self.browser.detect_install_path()
        if install_path:
            options.binary_location = install_path

        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--window-size=1600,1000')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        for argument in self.MIC_BROWSER_ARGS:
            options.add_argument(argument)
        for argument in extra_args:
            options.add_argument(argument)

        try:
            user_data_dir = tempfile.mkdtemp(prefix='ucampus_', dir=temp_dir or None)
        except Exception:
            user_data_dir = tempfile.mkdtemp(prefix='ucampus_')
        options.add_argument(f'--user-data-dir={user_data_dir}')
        if temp_dir:
            cache_dir = os.path.join(temp_dir, self.browser.profile_dir_name)
            options.add_argument(f'--disk-cache-dir={cache_dir}')
            options.add_argument(f'--crash-dumps-dir={temp_dir}')

        service_options = {}
        if sys.platform == 'win32':
            service_options['popen_kw'] = {'creation_flags': subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            try:
                driver = webdriver_cls(options=options, service=service_cls(**service_options))
            except NoSuchDriverException:
                if not install_path:
                    raise
                print(f" 按 {install_path} 取不到 driver，改由自动解析重试...")
                options.binary_location = ""
                driver = webdriver_cls(options=options, service=service_cls(**service_options))
        except BaseException:
            shutil.rmtree(user_data_dir, ignore_errors=True)
            raise
        driver._unipus_profile_dir = user_data_dir
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
            })
        except BaseException:
            try:
                quit_driver(driver)
            except Exception:
                pass
            raise
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": RECORD_MIC_BOOTSTRAP_JS
            })
        except Exception as exc:
            logger.warning(f"录音注入脚本安装失败: {str(exc)[:100]}")
        return driver

    def _create_driver(self):
        temp_dir = ensure_writable_temp_dir()

        try:
            selenium_cache = os.path.join(BASE_DIR, 'logs', 'selenium_cache')
            os.makedirs(selenium_cache, exist_ok=True)
            os.environ.setdefault('SE_CACHE_PATH', selenium_cache)
        except Exception:
            pass

        try:
            return self._start_browser(temp_dir)
        except NoSuchDriverException as exc:
            print(f"\n 找不到 {self.browser.display_name} 的 driver：{str(exc)[:120]}")
            print(f"   请确认已安装 {self.browser.display_name}，或在 config.json 里改 \"browser\"（edge / chrome）")
            for hint in self.browser.manual_install_hint:
                print(f"   下载：{hint}")
            raise
        except (SessionNotCreatedException, WebDriverException) as exc:
            print(f" 启动 {self.browser.display_name} 失败（{str(exc)[:120]}），尝试兼容模式重试...")
            return self._start_browser(temp_dir, extra_args=self.COMPAT_BROWSER_ARGS)
        except Exception as exc:
            print(f" 启动 {self.browser.display_name} 超时（{str(exc)[:120]}），尝试兼容模式重试...")
            return self._start_browser(temp_dir, extra_args=self.COMPAT_BROWSER_ARGS)

    def start(self):
        solver = AISolver(self.driver, self.config)

        from unipus_helper.native_ui import NativeModernGUI
        self.gui = NativeModernGUI(self.driver, solver, self, globals())

        self._background_threads = [
            threading.Thread(target=self.popup_watcher.run, daemon=True, name="popup-watcher"),
            threading.Thread(target=self._background_login_flow, daemon=True, name="ucampus-login"),
        ]
        for worker in self._background_threads:
            worker.start()

        try:
            self.gui.mainloop()
        finally:
            self.request_shutdown()

        return True

    def request_shutdown(self):
        self._shutdown_event.set()
        self.popup_watcher.stop()

    def restart_browser(self):
        old, self.driver = self.driver, None
        self.popup_watcher.stop()
        if old is not None:
            def quit_old():
                try:
                    quit_driver(old)
                except Exception:
                    pass
            worker = threading.Thread(target=quit_old, daemon=True, name="browser-quit")
            worker.start()
            worker.join(timeout=6)
            if worker.is_alive():
                service = getattr(old, "service", None)
                process = getattr(service, "process", None)
                if process and process.poll() is None:
                    try:
                        if sys.platform == 'win32':
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           capture_output=True, timeout=3,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                        else:
                            process.terminate()
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        self.driver = self._create_driver()
        self.popup_watcher = PopupWatcher(self.driver)
        threading.Thread(target=self.popup_watcher.run, daemon=True, name="popup-watcher").start()
        return self.driver

    def _background_login_flow(self):
        gui_log_queue.put(" 正在与 U校园 建立连接，请稍候...")
        try:
            success = self._login()
        except Exception as exc:
            if not self._shutdown_event.is_set():
                logger.error(f"登录线程异常: {exc}", exc_info=True)
            return
        if success and not self._shutdown_event.is_set():
            self.gui.after(0, self.gui.enable_scan_button)

    def _login(self) -> bool:
        try:
            login_url = self.config.url or DEFAULT_LOGIN_URL
            self.driver.get(login_url)
            try:
                username = WebDriverWait(self.driver, 8).until(
                    EC.element_to_be_clickable((By.ID, 'username')))
            except TimeoutException:
                authenticated = False
                if self.driver.current_url.startswith("https://ucloud.unipus.cn/"):
                    try:
                        WebDriverWait(self.driver, 45).until(self._has_live_auth)
                        authenticated = True
                    except TimeoutException:
                        pass

                if authenticated:
                    username = None
                    gui_log_queue.put(" 检测到已有 U校园 登录会话，继续使用当前会话。")
                elif login_url != DEFAULT_LOGIN_URL:
                    gui_log_queue.put(" 登录地址未显示账号表单，正在使用兼容登录入口...")
                    self.driver.get(DEFAULT_LOGIN_URL)
                    username = WebDriverWait(self.driver, 120).until(
                        EC.element_to_be_clickable((By.ID, 'username')))
                else:
                    raise

            if username is not None:
                password = WebDriverWait(self.driver, 20).until(
                    EC.element_to_be_clickable((By.ID, 'password')))
                agreement_check = self.driver.find_element(By.ID, 'agreement')
                self._fill_login_input(username, self.config.username, '账号')
                self._fill_login_input(password, self.config.password, '密码')
                if not agreement_check.is_selected():
                    agreement_control = WebDriverWait(self.driver, 20).until(
                        EC.element_to_be_clickable((
                            By.CSS_SELECTOR,
                            'label.usso-agreements-checkbox .usso-checkbox'
                        )))
                    agreement_control.click()
                    try:
                        for agree_button in self.driver.find_elements(
                                By.CSS_SELECTOR, '.usso-modal-agreement .usso-btn-primary'):
                            if agree_button.is_displayed():
                                agree_button.click()
                                gui_log_queue.put(" 已确认服务协议与隐私政策")
                                break
                    except Exception:
                        pass

                login_btn = WebDriverWait(self.driver, 20).until(
                    EC.element_to_be_clickable((
                        By.CSS_SELECTOR,
                        '[id^="rc-tabs-"][id$="-panel-1"] form button'
                    )))
                login_btn.click()

                gui_log_queue.put(" 如果遇到验证码，请在弹出的浏览器中手动进行人机验证。")
                gui_log_queue.put("【等待】正在智能轮询登录状态...")
                WebDriverWait(self.driver, 120).until(
                    lambda driver: driver.current_url.startswith("https://ucloud.unipus.cn/"))

            self.anti_anti_cheat()

            try:
                zhidaole_button = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.XPATH,
                                                                                                      '/html/body/div[3]/div/div[2]/div/div[2]/div/div/div/div[4]/button')))
                zhidaole_button.click()
            except Exception:
                pass

            try:
                anti_cheat_announce_button = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH,
                                                    '/html/body/div[4]/div/div/div/div[2]/div/div/div[4]/div[5]')))
                anti_cheat_announce_button.click()
            except Exception:
                pass

            gui_log_queue.put(" 登录验证握手成功！")
            return True

        except Exception as e:
            if self._shutdown_event.is_set():
                return False
            error_msg = str(e)
            gui_log_queue.put(f" 登录执行流阻断: {error_msg[:50]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return False

    def _sync_auth_state(self, legacy_token: Optional[str], configured_jwt: Optional[str]) -> Dict[str, Any]:
        cookie = self.driver.get_cookie("jwt") or {}
        cookie_jwt = cookie.get("value") or ""
        return self.driver.execute_script("""
            const configuredLegacy = arguments[0];
            const configuredJwt = arguments[1];
            const cookieJwt = arguments[2] || '';
            let legacy = null;
            const legacyRaw = localStorage.getItem('__token');
            if (legacyRaw) {
                try { legacy = JSON.parse(legacyRaw); }
                catch (_) { localStorage.removeItem('__token'); }
            }

            let portal = {};
            try { portal = JSON.parse(localStorage.getItem('PORTAL_STATE_PERSISTENT') || '{}') || {}; }
            catch (_) {}

            const liveJwt = legacy && legacy.jwt;
            const portalJwt = portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt);
            const existingJwt = liveJwt || portalJwt || cookieJwt || '';
            let jwt = existingJwt || configuredJwt || '';
            let source = liveJwt ? 'legacy' : portalJwt ? 'portal' : cookieJwt ? 'cookie' : configuredJwt ? 'configured' : 'none';

            if (!existingJwt && configuredLegacy) {
                try {
                    legacy = JSON.parse(configuredLegacy);
                    jwt = legacy.jwt || jwt;
                    source = 'configured-legacy';
                    localStorage.setItem('__token', JSON.stringify(legacy));
                } catch (_) {}
            }

            if (jwt) {
                if (!portalJwt) {
                    portal.Authorization = jwt;
                    portal.tokenInfo = Object.assign({}, portal.tokenInfo || {}, {jwt});
                    localStorage.setItem('PORTAL_STATE_PERSISTENT', JSON.stringify(portal));
                }
                if (!cookieJwt) {
                    document.cookie = 'jwt=' + jwt + '; domain=.unipus.cn; path=/; Secure; SameSite=None';
                }
            }

            return {
                source,
                hasLegacy: !!(legacy && legacy.jwt),
                hasPortal: !!(portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt))
            };
        """, legacy_token, configured_jwt, cookie_jwt)

    @staticmethod
    def _fill_login_input(element, value: str, label: str = '输入框') -> bool:
        script = """
            const el = arguments[0], value = arguments[1];
            el.focus();
            const proto = Object.getPrototypeOf(el);
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return el.value;
        """
        try:
            written = element.parent.execute_script(script, element, value)
        except Exception as exc:
            print(f"  {label}写入异常: {type(exc).__name__}")
            written = None
        if written == value:
            return True

        try:
            element.click()
            element.send_keys(Keys.CONTROL, 'a')
            element.send_keys(Keys.DELETE)
            element.send_keys(value)
            written = element.get_attribute('value') or ''
        except Exception as exc:
            print(f"  {label}键盘写入异常: {type(exc).__name__}")
            return False
        if written != value:
            print(f"  {label}写入校验失败")
            return False
        return True

    @staticmethod
    def _has_live_auth(driver) -> bool:
        cookie = driver.get_cookie("jwt") or {}
        if cookie.get("value"):
            return True
        try:
            return bool(driver.execute_script("""
                let legacy = null;
                let portal = null;
                try { legacy = JSON.parse(localStorage.getItem('__token') || 'null'); } catch (_) {}
                try { portal = JSON.parse(localStorage.getItem('PORTAL_STATE_PERSISTENT') || 'null'); } catch (_) {}
                return !!((legacy && legacy.jwt) ||
                    (portal && (portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt))));
            """))
        except Exception:
            return False

    @staticmethod
    def _home_ready(driver) -> bool:
        try:
            cookie = driver.get_cookie("jwt") or {}
            return bool(driver.execute_script("""
                if (location.hostname !== 'ucloud.unipus.cn') return false;
                let legacy = null;
                let portal = null;
                try { legacy = JSON.parse(localStorage.getItem('__token') || 'null'); } catch (_) {}
                try { portal = JSON.parse(localStorage.getItem('PORTAL_STATE_PERSISTENT') || 'null'); } catch (_) {}
                const loading = document.querySelector('#root-loading');
                const loadingVisible = loading && getComputedStyle(loading).display !== 'none';
                const root = document.querySelector('#root');
                const rendered = !!(root && (root.innerText || '').trim().length > 20);
                const authenticated = !!((legacy && legacy.jwt) ||
                    (portal && (portal.Authorization || (portal.tokenInfo && portal.tokenInfo.jwt))) || arguments[0]);
                return authenticated && rendered && !loadingVisible;
            """, cookie.get("value") or ""))
        except Exception:
            return False

    def anti_anti_cheat(self):
        legacy_token, configured_jwt = _parse_auth_token(self.config.token_full)
        if self.config.token_full and not configured_jwt:
            print(" 配置的 token_full 格式无效，改用本次登录会话")

        try:
            WebDriverWait(self.driver, 45).until(self._has_live_auth)
        except TimeoutException:
            if not configured_jwt:
                raise
            print(" 未检测到实时登录凭据，使用配置中的 token")

        state = self._sync_auth_state(legacy_token, configured_jwt)
        print(f" 登录认证来源: {state.get('source', 'unknown')}")

        self.driver.get(UCLOUD_HOME_URL)
        WebDriverWait(self.driver, 45).until(self._home_ready)
        self._sync_auth_state(legacy_token, configured_jwt)



class PopupWatcher:

    def __init__(self, driver):
        self.driver = driver
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._click_known_buttons()
            except WebDriverException as e:
                if not self._stop_event.is_set():
                    logger.warning(f"弹窗监控已停止，浏览器会话不可用: {str(e)[:120]}")
                break
            except Exception as e:
                if self._stop_event.is_set():
                    break
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.warning(f"弹窗监控操作失败: {error_msg[:120]}")
            self._stop_event.wait(0.5)

    def _click_known_buttons(self):
        js = """
        function findBtn(w) {
            const selectors = [
                '.know-box .iKnow',
                '.ant-modal-confirm-btns .ant-btn-primary',
                '.system-info-cloud-ok-button'
            ];
            for (let sel of selectors) {
                const b = w.document.querySelector(sel);
                if (b) return b;
            }
            return null;
        }
        function clickBtn(btn) {
            ['mouseover','mousedown','mouseup','click'].forEach(ev => {
                btn.dispatchEvent(new MouseEvent(ev, {bubbles:true}));
            });
            btn.click();
        }
        let btn = findBtn(window);
        if (btn) { clickBtn(btn); return true; }
        for (let i=0; i<window.frames.length; i++) {
            try {
                btn = findBtn(window.frames[i]);
                if (btn) { clickBtn(btn); return true; }
            } catch(e) {}
        }
        return false;
        """
        self.driver.execute_script(js)

if __name__ == '__main__':
    print('*' * 25 + "Unipus-Helper" + '*' * 25)

    ensure_writable_temp_dir()

    skip_check = '--skip-check' in sys.argv

    if not skip_check:
        print("\n 提示：")
        print("   - 首次运行需要检查环境")
        print("   - 语音识别需要 FFmpeg（约130MB，可自动安装）")
        print("   - 如检查通过但无法启动，使用 --skip-check 跳过")

    logger = setup_logging()

    bot = None
    exit_code = 0
    try:
        if not os.path.exists(os.path.join(BASE_DIR, 'config.json')):
            from unipus_helper.native_ui import setup_first_run
            qt_app, configured = setup_first_run()
            if not configured:
                sys.exit(0)
        bot = UCampusBot(os.path.join(BASE_DIR, 'config.json'), skip_check=skip_check)
        bot.start()
    except KeyboardInterrupt:
        print("\n 正在关闭工作台与课程浏览器...")
    except Exception as e:
        error_msg = str(e)
        print(f"\n 程序运行失败: {error_msg[:100]}")
        logger.error(f"程序异常: {error_msg}", exc_info=True)
        exit_code = 1
    finally:
        if bot:
            bot.request_shutdown()
            gui = getattr(bot, 'gui', None)
            if gui and hasattr(gui, 'close'):
                gui.close()
            elif bot.driver:
                try:
                    quit_driver(bot.driver)
                except Exception:
                    pass
    sys.exit(exit_code)





