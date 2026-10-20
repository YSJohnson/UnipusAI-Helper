from AudioRecognizer import AudioTranscriber
from EnvironmentChecker import EnvironmentChecker
import hashlib, json, logging, os, sys, random, re, tempfile, threading, time, winsound
import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from glob import glob
from typing import List, Optional, Dict, Any, Tuple, Callable
from urllib.parse import unquote, urlsplit

from openai import OpenAI
from selenium import webdriver
from selenium.common.exceptions import (NoSuchElementException, TimeoutException,
                                        StaleElementReferenceException,
                                        SessionNotCreatedException, WebDriverException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from fluent_ui import FluentModernGUI

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_writable_temp_dir() -> str:
    """确保 TEMP/TMP 指向可写目录。

    Windows 上系统临时目录可能不可写（受限用户、策略重定向、沙箱环境等），
    此时 Chromium/Edge 启动会报
    "cannot create temp dir for unpacking extensions"。
    这里按「系统 TEMP/TMP → tempfile 默认 → 程序目录 logs/tmp」顺序挑第一个可写的，
    并同步写回 os.environ，供后续启动的浏览器进程继承。
    """
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

gui_log_queue = queue.Queue()

DEBUG_MODE = False

APP_VERSION = "3.6.0"

DEFAULT_LOGIN_URL = "https://uai.unipus.cn/sso/index.html?service=https%3A%2F%2Fucloud.unipus.cn%2Fhome"
UCLOUD_HOME_URL = "https://ucloud.unipus.cn/home"


def setup_logging():
    """配置日志系统：控制台简洁输出 + 文件详细记录 + UI队列同步"""

    def clean_all_logs(log_dir):
        """清空所有旧日志（激进模式）"""
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


@dataclass(frozen=True)
class Config:
    """不可变配置类"""
    url: str
    username: str
    password: str
    api_key: str
    token_full: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int

    @classmethod
    def from_json(cls, path: str = "config.json") -> "Config":
        with open(path, "r", encoding="UTF-8") as f:
            data = json.load(f)
        global DEBUG_MODE
        DEBUG_MODE = data.get("debug_mode", False)
        return cls(
            url=data.get("url") or DEFAULT_LOGIN_URL,
            username=data.get("username"),
            password=data.get("password"),
            token_full=data.get("token_full") or "",
            api_key=data.get("api_key"),
            base_url=data.get("base_url", "https://api.moonshot.cn/v1"),
            model=data.get("model", "kimi-k2-turbo-preview"),
            temperature=data.get("temperature", 0.3),
            max_tokens=data.get("max_tokens", 2000),
        )


def _parse_auth_token(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return (legacy __token JSON, JWT) for either supported config format."""
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
    """题目类型"""
    SINGLE_CHOICE = auto()
    MULTIPLE_CHOICE = auto()
    FILL_IN = auto()
    TEXT = auto()
    SORTING = auto()
    VOCABULARY_FLASHCARD = auto()
    BANKED_CLOZE = auto()
    VOCABULARY_TEST = auto()  # 词汇测试（英汉互译）
    VIDEO = auto()  # 纯视频页面
    DISCUSSION_BOARD = auto()
    SELF_CHECK = auto()
    MY_VOICE_TEXT = auto()
    DROPDOWN_SELECT = auto()
    LISTENING_FILL_IN = auto()
    LISTENING_CHOICE = auto()
    VIDEO_CHOICE = auto()
    TRUE_FALSE = auto()  # 判断题（course-v3 播放器 T/F）


@dataclass
class Option:
    """选项"""
    letter: str
    text: str
    element: Any
    is_selected: bool = False


@dataclass
class Question:
    """题目"""
    number: int
    text: str
    q_type: QuestionType
    element: Any
    options: List[Option] = field(default_factory=list)
    inputs: List[Any] = field(default_factory=list)
    banked_options: List[str] = field(default_factory=list)
    banked_blanks: List[Dict] = field(default_factory=list)
    directions: str = ""
    group: Any = None  # course-v3 题目组容器（提交单位）
    needs_upload: bool = False  # course-v3 需要上传附件的题型
    blank_count: int = 0  # course-v3 下拉空位可能没有 input

    @property
    def answer_count(self) -> int:
        return self.blank_count or len(self.inputs)

    def is_interactive(self) -> bool:
        """是否有交互元素"""
        if self.q_type in [QuestionType.VIDEO,
                           QuestionType.VOCABULARY_FLASHCARD,  # 闪卡需要交互
                           QuestionType.DISCUSSION_BOARD,
                           QuestionType.SELF_CHECK]:
            return True
        return bool(self.options or self.inputs or self.banked_blanks)

    @property
    def is_phrase_mode(self) -> bool:
        """检测是短语填空还是单词填空"""
        if not self.banked_options:
            return False
        phrase_count = sum(1 for opt in self.banked_options if ' ' in opt.strip() or len(opt) > 15)
        return phrase_count / len(self.banked_options) > 0.3

class Selectors:
    """CSS选择器仓库"""
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
    """WebDriver辅助工具类（静态方法）"""

    @staticmethod
    def safe_find_element(driver, selectors: List[str], parent=None) -> Optional[Any]:
        """安全查找单个元素"""
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
        """安全查找多个元素"""
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
                print(f"操作失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件
                continue
        return []

    @staticmethod
    def human_like_delay(base_delay: float = 0.1) -> None:
        """随机延迟"""
        delay = base_delay * (0.8 + random.random() * 0.4)
        time.sleep(delay)

    @staticmethod
    def simulate_typing(driver, element, text: str) -> None:
        """模拟人类打字"""
        actions = ActionChains(driver)
        actions.move_to_element(element).click().perform()
        WebDriverHelper.human_like_delay(0.1)
        element.clear()
        WebDriverHelper.human_like_delay(0.1)
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
        """安全点击元素"""
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
                print(f"操作失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件
        return False


class OpenAICompatibleClient:
    """OpenAI兼容 API客户端 - 职责：仅处理API通信"""

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
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url)
        self.conversation_history: List[Dict] = []
        self.material_messages: List[Dict] = []  # 材料独立保留，不随问答窗口裁剪
        self.current_chapter_id: Optional[str] = None
        self.accumulated_passages: set = set()  # 已累积的原文哈希，防重复

    def start_new_chapter(self, chapter_id: str):
        """开始新章节，记录章节ID（不自动清空历史）"""
        self.current_chapter_id = chapter_id
        print(f" 记录章节: {chapter_id[:50]}")

    def force_reset(self, chapter_id: str):
        """强制清空所有历史，无论章节是否相同"""
        self.conversation_history = []
        self.material_messages = []
        self.current_chapter_id = chapter_id
        self.accumulated_passages = set()
        print(f" 强制重置章节: {chapter_id[:50]}")

    def add_passage_if_new(self, passage: str) -> bool:
        """添加原文（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(passage, "阅读材料", "材料")

    def add_audio_transcript_if_new(self, transcript: str) -> bool:
        """添加音频转写（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(transcript, "听力音频转写", "音频转写")

    def add_video_transcript_if_new(self, transcript: str) -> bool:
        """添加视频转写（如果是新的），返回是否添加成功"""
        return self._add_context_if_new(transcript, "视频转写", "视频转写")

    def _add_context_if_new(self, content: str, label: str, ack_label: str) -> bool:
        """添加可复用上下文，按内容哈希去重"""
        if not content or len(content) < 50:
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

    def ask(self, prompt: str, retry_count: int = 3, stop_requested: Optional[threading.Event] = None) -> Optional[str]:
        """发送问题并获取回答

        推理型模型（如 deepseek-flash）可能把 max_tokens 全部消耗在 reasoning 上，
        导致 content 为空且 finish_reason=length；这里自动放宽预算重试一次。
        """
        print(f"当前ai对话历史共{len(self.conversation_history)}条")
        max_tokens = self.config.max_tokens
        for attempt in range(retry_count):
            if stop_requested is not None and stop_requested.is_set():
                return None
            try:
                messages = [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    *getattr(self, 'material_messages', []),
                    *self.conversation_history,
                    {"role": "user", "content": prompt}
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

                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=max_tokens
                )

                finish_reason = response.choices[0].finish_reason
                answer = (response.choices[0].message.content or "").strip()

                if DEBUG_MODE:
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 响应详情")
                    print(f"   model:        {response.model}")
                    print(f"   finish_reason:{finish_reason}")
                    print(f"   usage:        {response.usage}")
                    print(f"   answer:       {answer[:500]}{'...' if len(answer) > 500 else ''}")
                    print("=" * 60 + "\n")

                if not answer:
                    if finish_reason == 'length' and max_tokens < 32768:
                        max_tokens = min(max_tokens * 2, 32768)
                        print(f"推理耗尽 token（finish_reason=length），放宽 max_tokens 到 "
                              f"{max_tokens} 重试")
                        continue
                    print(f"模型返回空内容（finish_reason={finish_reason}）")
                    return None

                self.conversation_history.append({"role": "user", "content": prompt})
                self.conversation_history.append({"role": "assistant", "content": answer})

                self.conversation_history = self.conversation_history[-20:]

                print(f"AI回答: {answer}")
                return answer

            except Exception as e:
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
                print(f"AI调用失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件

        return None


class QuestionParserStrategy(ABC):
    """题目解析策略基类"""

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
        return bool(re.search(
            r'多选|\b(?:choose|select|check|tick|mark)\s+(?:all|two|three|four|five|six|seven|eight|nine|ten|[2-9]|multiple|more than one)\b',
            instructions, re.IGNORECASE))

    @abstractmethod
    def can_parse(self, container, driver) -> bool:
        """是否能解析该容器"""
        pass

    @abstractmethod
    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        """解析题目"""
        pass


class QuestionParser:
    """题目解析器 - 使用策略模式"""

    def __init__(self, driver):
        self.driver = driver
        self.strategies: List[QuestionParserStrategy] = [
            VideoStrategy(),
            DiscussionBoardStrategy(),
            SelfCheckStrategy(),
            SortingStrategy(),
            VocabularyFlashcardStrategy(),
            VocabularyTestStrategy(),
            DropdownSelectStrategy(),
            BankedClozeStrategy(),
            ListeningFillInStrategy(),  # 听力填空（转录由AISolver预处理完成）
            StandardChoiceStrategy(),
            MyVoiceTextStrategy(),
            TextInputStrategy(),
            FillInStrategy(),
        ]

    def _find_reading_question_containers(self) -> List[Any]:
        """
        查找需要拆分的问答题容器（阅读问答题、翻译题）
        关键：多个reply，每个包含.question-inputbox，且direction表明是多题作答
        """
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
                'answer', 'question', 'according to',  # 阅读问答题
                'translate',  # 翻译题
            ])

            if not is_multi_question_type:
                return []

            body_selectors = [
                '.layoutBody-container.has-material.has-reply',
                '.layoutBody-container.has-reply',  # 翻译题
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
        """从页面统一提取 direction"""
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
        """解析所有可见题目"""
        if self._is_discussion_board_page():
            print("     检测到讨论板页面，跳过")
            return [], ""

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
                else:
                    print(f"      容器 {idx} 解析为None")

            except Exception as e:
                error_msg = str(e)
                print(f"       解析容器 {idx} 失败:{error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue

        return questions, directions

    def _is_discussion_board_page(self) -> bool:
        """检查当前页面是否是讨论板"""
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
        """查找题目容器"""
        if self._is_discussion_board_page():
            print("     检测到讨论板页面，跳过")
            return []

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

        return []

    def _is_really_visible(self, element) -> bool:
        """检查元素真正可见"""
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
                except:
                    break

            return True
        except:
            return False

    def _parse_single(self, container, number: int, directions: str = "") -> Optional[Question]:
        """使用策略解析单个容器"""
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
            except Exception as e:
                error_msg = str(e)
                print(f"      策略 {strategy.__class__.__name__} 失败: {error_msg[:50]} ")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                continue
        print(f"      没有匹配的策略")
        return None


class DiscussionBoardStrategy(QuestionParserStrategy):
    """讨论板策略"""

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
    """Self-check 词汇勾选表解析策略"""

    def can_parse(self, container, driver) -> bool:
        direction_text = ""
        try:
            direction_elem = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            direction_text = direction_elem.text.lower()
        except:
            pass

        has_table = bool(container.find_elements(By.CSS_SELECTOR, '.ticket-view table, .ant-table-tbody'))
        has_got_it = 'got it' in container.text.lower()
        has_check_instruction = 'check in the box' in direction_text or "you've learned" in direction_text
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
    """拖拽排序题解析策略"""

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
    """词汇测试题解析策略"""

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
        """提取选项"""
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
    """选词填空解析策略"""

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
                context = context_elem.text.strip()
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

        question_text = f"选词填空（{len(banked_blanks)}个空）"
        if banked_options:
            question_text += f"\n可选选项: {', '.join(banked_options[:5])}"
            if len(banked_options) > 5:
                question_text += f" 等共{len(banked_options)}个"

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
    """My voice 上传页的文字作答策略"""

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


class TextInputStrategy(QuestionParserStrategy):
    """文本输入题策略"""

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
                    full_text += f"【阅读材料】\n{material_text[:800]}...\n\n（文章较长，根据问题回答即可）\n\n"

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
    """音视频填空题解析策略（仅解析题目，转录由预处理完成）"""

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
            try:
                scoop = inp.find_element(By.XPATH, './ancestor::span[@class="fe-scoop"]')
                sentence = self._extract_blank_sentence(scoop)
            except:
                sentence = ""
            left_context, right_context = self._split_blank_context(sentence, i + 1)
            blank_contexts.append({
                'index': i,
                'sentence': sentence,
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
    """填空题解析策略"""

    FILL_INPUTS = [
        'input.fill-blank--bc-input-DelG1',
        '.fe-scoop input:not([type="hidden"])',
        '.comp-abs-input input',
        '.blankinput',
        'input[type="text"]',
    ]

    def can_parse(self, container, driver) -> bool:
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

        inputs = WebDriverHelper.safe_find_elements(driver, self.FILL_INPUTS, container)
        inputs.sort(key=lambda x: int(
            x.find_element(By.XPATH, './ancestor::span[@class="fe-scoop"]').get_attribute('data-scoop-index') or 0))

        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.FILL_IN,
            element=container,
            inputs=inputs,
            directions=directions,
        )


class VideoStrategy(QuestionParserStrategy):
    """纯视频页面检测策略"""

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
    """单词闪卡策略"""

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
    """下拉选择填空题解析策略"""

    def can_parse(self, container, driver) -> bool:
        selects = container.find_elements(By.CSS_SELECTOR, '.scoop-select-wrapper, select, .ant-dropdown-trigger')
        return bool(selects)

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        blanks = []
        select_elements = container.find_elements(By.CSS_SELECTOR, '.scoop-select-wrapper')

        for i, elem in enumerate(select_elements):
            context = ""
            try:
                context_elem = elem.find_element(By.XPATH, './ancestor::li')
                context = context_elem.text.strip()
            except:
                pass

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
                        if text and text not in ['', '点击选择']:
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
        return Question(
            number=question_number,
            text=text,
            q_type=QuestionType.DROPDOWN_SELECT,
            element=container,
            banked_blanks=blanks,
            banked_options=list(set(opt for b in blanks for opt in b['options']))
        )


class PromptBuilder:
    """Prompt构建器"""

    def __init__(self, ai_client):
        self.ai_client = ai_client

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

        type_counts = {}
        for q in questions:
            type_counts[q.q_type] = type_counts.get(q.q_type, 0) + 1

        if QuestionType.VOCABULARY_TEST in type_counts:
            lines.extend(self._vocabulary_test_hints())

        if QuestionType.BANKED_CLOZE in type_counts:
            lines.extend(self._banked_cloze_hints())

        for q in questions:
            builder_method = self._get_builder_method(q.q_type)
            lines.extend(builder_method(q))

        lines.extend(self._format_instructions(type_counts))

        return '\n'.join(lines)

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
            "【重要提示】这是选词填空题，请从给定的单词列表中选择最合适的填入空白处。\n",
            "【格式要求】只需返回答案本身，不要添加括号注释、不要解释、不要变形说明！"
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
        }
        return builders.get(q_type, self._build_unknown)

    def _build_sorting(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【排序题】{q.text}",
            "   请根据前面提供的音频/视频转写或材料内容，将以下选项按出现顺序排序。",
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
            "   请根据前面提供的音频转写内容选择最佳答案。",
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
            "2. 每空只填写缺失部分，不要重复空格左边或右边已经出现的词",
            "3. 如果左边已有 be going to / I'm going to / we're 等结构，答案要能直接拼进原句并保持语法通顺",
            "4. 不要填整句，不要串用其他空的答案",
            ""
        ]

        for blank in q.banked_blanks:
            lines.append(f"   空{blank['index'] + 1}: {blank['sentence']}")
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
            context = blank['context'][:250] + "..." if len(blank['context']) > 250 else blank['context']
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
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
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
        if q.banked_options:
            lines.append(f"   可选词: {', '.join(q.banked_options)}")
            lines.append("   （只能从可选词中选择；必要时改变词形：单复数/时态/词性/派生词）")
            lines.append("   （若空位位于句子开头，首字母必须大写，否则会扣分）")
        lines.append("")
        return lines

    def _build_text(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【简答题】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 小题）")
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
            lines.append("选词/选择填空: 1.word1 2.word2 ...")

        if QuestionType.FILL_IN in type_counts:
            lines.append("填空题: 1.答案1 2.答案2 ...")

        if QuestionType.TRUE_FALSE in type_counts:
            lines.append("判断题: 每题回答 T 或 F，如：1.T 2.F")

        if QuestionType.TEXT in type_counts:
            lines.append("简答题: 1.答案内容...")

        if QuestionType.MY_VOICE_TEXT in type_counts:
            lines.append("My voice文字作答: 1.英文介绍内容（500字符以内）")

        if QuestionType.SORTING in type_counts:
            lines.append("排序题: 直接返回排序后的字母，如：B E A D C F")

        lines.append("-" * 50)
        return lines

    def _build_dropdown_select(self, q: Question) -> List[str]:
        # course-v3 的下拉填空没有旧版的 banked_blanks（上下文按空位拆好），
        # 题面和空号都在 q.text 里，此时退回填空式提示词，否则模型看不到题目。
        if not q.banked_blanks:
            lines = [f"{q.number}. 【选择填空】{q.text}"]
            total = q.answer_count or len(re.findall(r'\(\d+\)', q.text)) or 1
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

        for i, blank in enumerate(q.banked_blanks, 1):
            context = blank['context'][:200] + "..." if len(blank['context']) > 200 else blank['context']
            context = re.sub(r'<[^>]+>', '', context)
            lines.append(f"   空{i}: {context}")

        lines.append("")
        lines.append("要求：按顺序给出答案，格式：1.do 2.make ...")
        lines.append("")
        return lines


class AnswerExecutor:
    """答案执行器 - 执行答案填写"""

    ANSWER_LABEL_PATTERN = r'(?:My voice文字作答|简答题|选词/选择填空|填空题|答案|选词填空|翻译|Answer)'
    NUMBER_PREFIX_PATTERN = r'(?:空\s*)?\d+\s*[.、:：\)\]]|Blank\s*\d+\s*[.、:：\)\]]'

    def __init__(self, driver):
        self.driver = driver

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
        }

        executor = executors.get(question.q_type, self._fill_unknown)
        return executor(question, answer)

    def _fill_single_choice(self, q: Question, answer: str) -> bool:
        valid_letters = [opt.letter or chr(65 + idx) for idx, opt in enumerate(q.options)]
        letters = self._parse_choice_letters(answer, valid_letters)
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
        const wrapper = root.querySelector('.sortable-list-wrapper');
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
                    matched = self._match_to_option(clean_word, q.banked_options, is_phrase_mode)
                    if matched:
                        clean_word = matched
                        print(f"        匹配到选项: {clean_word}")

                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                        blank['input']
                    )
                    time.sleep(0.3)

                    blank['input'].clear()
                    time.sleep(0.1)
                    blank['input'].send_keys(clean_word)

                    self.driver.execute_script("""
                        arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
                        arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                        arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));
                    """, blank['input'])

                    print(f"        空{i + 1}: {clean_word}")
                    success_count += 1

                except Exception as e:
                    error_msg = str(e)
                    print(f"      填空 {i + 1} 失败:{error_msg[:50]} ")
                    logger.error(f"详细错误: {error_msg}", exc_info=True)

        return success_count > 0

    def _match_to_option(self, answer: str, options: List[str], is_phrase_mode: bool) -> Optional[str]:
        if not answer or not options:
            return None

        answer_lower = answer.lower().strip()

        for opt in options:
            if opt.lower() == answer_lower:
                return opt

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
                print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")
            else:
                print(f"\t空{i + 1}: (空)")

        return success_count > 0

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

    def _fill_text(self, q: Question, answer: str) -> bool:
        if not q.inputs:
            return False

        expected_count = len(q.inputs)

        if expected_count == 1:
            ans = self._extract_answer_by_number(answer, q.number)
            if not ans:
                if q.q_type == QuestionType.MY_VOICE_TEXT:
                    ans = self._clean_direct_text_answer(answer)
                else:
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
                    "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                    inp
                )
                time.sleep(0.2)
                if self._fill_text_input_verified(inp, ans):
                    success_count += 1
                else:
                    print(f"\t题{idx}: 写入后校验失败")

            return success_count > 0

        if ans and q.q_type == QuestionType.MY_VOICE_TEXT:
            ans = self._limit_text_answer(ans, 500)

        print(f"\t题{q.number}: {ans[:60]}..." if ans else f"\t题{q.number}: (空)")

        if ans:
            inp = q.inputs[0]
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                inp
            )
            time.sleep(0.2)
            if not self._fill_text_input_verified(inp, ans):
                return False
            if q.q_type == QuestionType.MY_VOICE_TEXT:
                if not self._upload_my_voice_answer_file(q, ans):
                    return False
            return True

        return False

    def _fill_text_input_verified(self, inp, ans: str) -> bool:
        WebDriverHelper.simulate_typing(self.driver, inp, ans)
        time.sleep(0.2)
        if self._input_value_matches(inp, ans):
            return True

        print("\t常规输入未生效，尝试JS同步输入框状态")
        self.driver.execute_script("""
            const el = arguments[0];
            const value = arguments[1];
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: value
            }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        """, inp, ans)
        time.sleep(0.3)
        return self._input_value_matches(inp, ans)

    @staticmethod
    def _input_value_matches(inp, ans: str) -> bool:
        try:
            current = inp.get_attribute('value') or ''
            return current.strip() == ans.strip()
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

    def _wait_my_voice_upload_ready(self, q: Question, timeout: int = 12) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self._my_voice_has_uploaded_file(q):
                return True
            time.sleep(0.5)
        print("\tMy voice 上传后未检测到文件列表变化，继续尝试提交")
        return True

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

        return False

    @classmethod
    def _parse_choice_letters(cls, answer: str, valid_letters: List[str]) -> List[str]:
        answer = cls._clean_extracted_answer(answer).rstrip('.。').upper()
        if not re.fullmatch(r'[A-Z][A-Z\s,，、/;&+]*', answer):
            return []
        letters = list(dict.fromkeys(re.findall(r'[A-Z]', answer)))
        valid = {letter.upper() for letter in valid_letters}
        return letters if set(letters) <= valid else []

    @staticmethod
    def _parse_banked_answer(answer: str, expected_count: int) -> List[str]:
        result = [''] * expected_count
        answer = AnswerExecutor._normalize_answer_labels(answer)
        answer = re.sub(rf'^{AnswerExecutor.ANSWER_LABEL_PATTERN}\s*[：:]\s*', '', answer.strip(), flags=re.I)
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

                trigger = select_wrapper.find_element(By.CSS_SELECTOR, '.ant-dropdown-trigger')

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
                                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item'
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
                        answer_text_elem = select_wrapper.find_element(By.CSS_SELECTOR, '.user-answer-text')
                        displayed_text = answer_text_elem.text.strip()
                        trigger_class = trigger.get_attribute('class') or ''

                        if ans.lower() in displayed_text.lower() or 'empty' not in trigger_class:
                            print(f"         验证成功，显示文本: {displayed_text[:20]}")
                            success_count += 1
                        else:
                            print(f"         视觉反馈异常，文本: {displayed_text[:20]}")
                            self._sync_react_state(select_wrapper, ans)

                    except Exception as e:
                        print(f"         验证失败: {str(e)[:50]}")
                        success_count += 1

                else:
                    print(f"         未找到选项 '{ans}'")
                    if self._force_select_by_js(select_wrapper, ans):
                        success_count += 1

            except Exception as e:
                print(f"        处理空{i + 1}失败: {str(e)[:50]}")
                logger.error(f"详细错误: {str(e)}", exc_info=True)
                continue

        return success_count > 0

    def _fill_listening_fill_in(self, q: Question, answer: str) -> bool:
        answers = self._parse_banked_answer(answer, len(q.inputs))

        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, (blank_info, ans) in enumerate(zip(q.banked_blanks, answers)):
            if ans and blank_info['input']:
                original_ans = ans
                ans = self._normalize_listening_blank_answer(blank_info, ans)
                if ans != original_ans:
                    print(f"\t空{i + 1}: {original_ans} -> {ans}")
                else:
                    print(f"\t空{i + 1}: {ans}")
                if self._fill_text_input_verified(blank_info['input'], ans):
                    success_count += 1
                else:
                    print(f"\t空{i + 1}: 写入后校验失败")

        return success_count > 0

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

            var trigger = wrapper.querySelector('.ant-dropdown-trigger');
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

        var formEvent = new Event('submit', { bubbles: true });
        var form = trigger.closest('form');
        if (form) form.dispatchEvent(formEvent);

        return 'dom_updated';
        """

            result = self.driver.execute_script(js, select_wrapper, value)
            print(f"        JS强制设置结果: {result}")

            time.sleep(0.3)
            text_elem = select_wrapper.find_element(By.CSS_SELECTOR, '.user-answer-text')
            return value.lower() in text_elem.text.lower()

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


# ============================================================================
# 新版 ipublish 播放器（course-v3 教材）适配
# ----------------------------------------------------------------------------
# 新农科大学英语等 course-v3 教材的题目不在顶层文档里：
#
#   ucontent.unipus.cn/_ipublishpc_default/pc.html      ← 播放器外壳（顶层文档）
#     └─ iframe#ipublish-pc-book-easy-iframe
#          └─ ipub.unipus.cn/learner/textbook/book/...  ← 题目 DOM（内容层）
#
# 旧版 .layoutBody-container / .question-common-abs-* 在顶层文档里全部失效，
# 内容层改用 data-ipublish-* 标记：
#
#   [data-ipublish-question-group-type="fill_blanks_group"]      题目组（提交单位）
#   [data-question-id][data-ipublish-question-type]              单题
#   span[data-dig-empty-id] > input[class*="answer_answerInput"] 填空空位
#   [data-option-value]                                          选词候选
#   div[class*="strongItem"]                                     单选选项字母
#   div[class*="answer_optionTF"]                                判断 T/F 选项
#   [data-ipublish-event-type="question-submit"]                 组内提交（已作答后变「重新作答」）
#
# CSS Modules 类名带 __hash 后缀（甚至含 +），只能按片段匹配，
# 例如 input[class*="answer_answerInput"]，不能写 input.answer_answerInput。
# ============================================================================


class PublishPlayer:
    """course-v3 ipublish 播放器：外壳/内容层切换与题目组状态判断。"""

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
        """回到顶层文档；没有 iframe 或已被清理时静默忽略。"""
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    @classmethod
    def find_frame(cls, driver):
        """在顶层文档定位内容 iframe，找不到返回 None。"""
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
        """当前页面是否为新版 ipublish 播放器（course-v3）。

        判定条件（满足其一）：
        1. 地址是播放器外壳（ucontent.unipus.cn + _ipublishpc_default）；
        2. 内容 iframe 里确实有 ipublish 的题目组/提交按钮标记。

        第 2 条要求真的看到标记，避免旧教材页面上恰好嵌了 ipub 域名的
        iframe 时被误判成 course-v3，把旧版解析流程顶掉。
        """
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
        """读取按钮/标签文本，兼容只有内部 span 有文字的实现。"""
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
                # 折叠面板中的已完成组没有 innerText，仍应识别「重新作答」。
                text = element.get_attribute('textContent') or ''
            except Exception:
                text = ''
        return re.sub(r'\s+', '', text)


class PublishFrameContext:
    """进入 ipublish 内容 iframe 的上下文管理器，退出时回到顶层文档。"""

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
    """同一 iframe 中当前目录任务的正文范围；保存锚点以兼容批改后的 DOM 重建。"""

    document_url: str
    start_anchor: str = ''
    end_anchor: str = ''
    title: str = ''
    block_only: bool = False

    @classmethod
    def capture(cls, driver, anchor: str = ''):
        """调用前进入内容层。无法确认任务边界时抛错，禁止退回整单元作答。"""
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
        """校验文档与边界后筛选组；忽略视口位置，折叠题也按正文所属任务判断。"""
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
    """course-v3 播放器题目解析：按 data-ipublish-* 标记读取题目组。"""

    GROUP_KINDS = {
        'single_choice_group': 'choice',
        'multiple_choice_group': 'choice',
        'multi_choice_group': 'choice',
        'true_false_group': 'true_false',
        'fill_blanks_group': 'fill',
        'fill_blanks_choice_group': 'fill',
        'fill_blanks_dropdown_group': 'dropdown',
        'short_answer_group': 'text',
        'translation_group': 'text',
        'writing_group': 'text',
        'multi_media_upload_group': 'upload',
        'rich_text_read_group': 'skip',
        'discussion_group': 'skip',
        'text_match_group': 'skip',
        'oral_personal_state_group': 'skip',
    }

    QUESTION_TYPE_KINDS = {
        'single_choice': 'choice',
        'multiple_choice': 'choice',
        'true_false': 'true_false',
        'fill_blanks': 'fill',
        'fill_blanks_choice': 'fill',
        'fill_blanks_dropdown': 'dropdown',
        'short_answer': 'text',
        'translation': 'text',
        'writing': 'text',
    }
    CHOICE_HEADER_SKIP = 'strongListView'
    EXPLANATION_PLACEHOLDERS = ('请输入',)
    TITLE_NOISE_WORDS = ('自动判题', 'AI判题', 'AI 判题', '计分', '已提交', '重新作答', '提交')
    TRUE_FALSE_LABELS = {'T': ('T', 'TRUE', '正确'), 'F': ('F', 'FALSE', '错误')}

    def __init__(self, driver):
        self.driver = driver
        self.unsupported: List[str] = []
        self.passages: List[str] = []

    # ---- 题目组 ---------------------------------------------------------
    @classmethod
    def classify_group(cls, group_type: str) -> str:
        return cls.GROUP_KINDS.get((group_type or '').strip(), 'unknown')

    def collect_groups(self) -> List[Dict[str, Any]]:
        """读取内容层全部题目组（调用前需已进入内容 iframe）。"""
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
        """已批改/已提交的题目组不再重复作答。"""
        button = self.submit_button(group)
        if button is None:
            return False
        text = PublishPlayer.button_text(button)
        return any(keyword in text for keyword in PublishPlayer.SUBMITTED_KEYWORDS)

    def group_key(self, group) -> str:
        """题目组指纹：组 id + 题目 id，用于去重。"""
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

    # ---- 文本工具 -------------------------------------------------------
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
        """题目正文：Lexical 只读编辑器里的文本块（跳过选项区/理由框）。"""
        try:
            contents = node.find_elements(By.CSS_SELECTOR, '.editor-content')
        except Exception:
            contents = []
        parts = []
        for content in contents:
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
        """剔除作答 UI，保留段落/表格边界；脱离文档的 clone.innerText 会粘连文本。"""
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
        # 表格的单元格使用分隔符，避免列标题和答案空位粘在一起。
        text = re.sub(r'\t[ \t]*\n', '\n', text).replace('\t', ' | ')
        return self.clean_text(text, limit)

    def group_direction(self, group) -> str:
        """收集组标题及小题外的公共题面，不混入各题选项或已答内容。"""
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
        """Lexical 题块前连续的正文/提示块也是题面，不能只读组内的作答占位语。"""
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

    # ---- 题目节点 -------------------------------------------------------
    def _question_nodes(self, group) -> List[Any]:
        """组内最外层题目节点（排除带 data-option-value 的候选项）。"""
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

    # ---- 解析入口 -------------------------------------------------------
    def parse_group(self, group, group_type: str, start_number: int = 1) -> List[Question]:
        """解析单个题目组；不支持的题型记入 unsupported 并返回空列表。"""
        kind = self.classify_group(group_type)
        if kind == 'unknown':
            # 播放器新增了未登记的组类型时，退回题目自身的 data-ipublish-question-type
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
        """收集阅读材料，作为 AI 上下文。"""
        passages = []
        for state in groups:
            if state['type'] != 'rich_text_read_group':
                continue
            text = self._dom_text(state['element'], limit=None)
            if len(text) >= 80:
                passages.append(text)
        self.passages = passages
        return passages

    # ---- 各题型 ---------------------------------------------------------
    def _parse_choices(self, group, group_type: str, start_number: int) -> List[Question]:
        questions = []
        multiple = 'multiple' in group_type or 'multi_choice' in group_type
        for node in self._question_nodes(group):
            text = self._stem_text(node, skip_class=self.CHOICE_HEADER_SKIP)
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
            ))
        return questions

    def _choice_options(self, node) -> List[Option]:
        options = []
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
        """填空/下拉题：把每个空位替换成 (N) 标记，保证 AI 能对上空号。"""
        return self._dom_text(node, numbered=True, limit=None)

    def _parse_text(self, group, start_number: int) -> List[Question]:
        questions = []
        for node in self._question_nodes(group):
            # TEXT 组的第一段 .editor-content 往往只是「请在此处作答」，
            # 真正题面在组内其他块里，所以优先取整组文本（已剔除按钮/输入框）。
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
        # 空位里的输入框没有被完整识别（结构被重排或空位里没有 input）时，
        # 按 DOM 顺序直接取所有填空框，避免少数空位漏填。
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
    """course-v3 作答执行器：复用基础填写，补充判断题、下拉填空与组内提交。"""

    RULE_POPUP_TEXTS = ('我知道了', '知道了', '确定')
    last_rejection = ''  # 最近一次提交被平台拒绝时的提示文案

    def reveal_group(self, group, stop_requested: Optional[threading.Event] = None) -> bool:
        """从外向内点击折叠标题，使隐藏题块真正可见，不修改页面的 hidden 状态。"""
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
        return super().execute(question, answer)

    def _fill_fill_in(self, q: Question, answer: str) -> bool:
        if q.blank_count and q.blank_count != len(q.inputs):
            print("\t空位与输入框数量不一致，不填写该题")
            return False
        return self._fill_required_inputs(q.inputs, self._parse_banked_answer(answer, len(q.inputs)))

    def _fill_text(self, q: Question, answer: str) -> bool:
        if len(q.inputs) <= 1:
            return super()._fill_text(q, answer)
        return self._fill_required_inputs(q.inputs, self._parse_banked_answer(answer, len(q.inputs)))

    def _fill_required_inputs(self, inputs: List[Any], answers: List[str]) -> bool:
        """整题的所有空位均有答案且实际写入成功，才能交给上层提交。"""
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
        """course-v3 填空必须用真实键盘输入才会计分。

        真机上直接 JS 赋值虽然能通过回读校验，但平台评分读到的是 0 分；
        所以键盘输入失败时重试一次，仍失败则阻止整组提交。
        """
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
        """聚焦输入框，用真实按键清空后逐字输入。"""
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

    # ---- 判断题 ---------------------------------------------------------
    @staticmethod
    def normalize_true_false(answer: str) -> str:
        """把 T/F、True/False、正确/错误 统一成 T 或 F。"""
        raw = (answer or '').strip()
        if '正确' in raw or raw in ('对', '是', '√'):
            return 'T'
        if '错误' in raw or raw in ('错', '否', '×', '不对'):
            return 'F'
        text = re.sub(r'^[\d\s.、):：\]]+', '', raw.upper())
        # 只认独立出现的 T/F，避免把 "IT IS CHEAP" 里的 T 当成答案
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

    # ---- 下拉填空 -------------------------------------------------------
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
        """一个空位：可输入就直接写入，否则点开下拉弹层按文本选择。

        course-v3 的下拉空位是 .digEmptySelectView_selectTrigger，
        选项要点开触发器后才会渲染到弹层里。
        """
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
            ActionChains(self.driver).send_keys('\ue00c').perform()  # ESC 收起弹层
        except Exception:
            pass
        return False

    @staticmethod
    def _dropdown_trigger(blank):
        """course-v3 下拉空位的可点击触发器。"""
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
        """当前可见的下拉弹层容器。"""
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
        """离弹层最近的下拉触发器（用于点开关闭弹层）。"""
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
        """轮询等待下拉弹层出现。"""
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
        """用 JS 派发完整鼠标事件序列（原生点击被遮挡时兜底）。"""
        self.driver.execute_script("""
        const el = arguments[0];
        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (type) {
            el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
        });
        """, element)

    def _close_visible_dropdowns(self, max_rounds: int = 4) -> None:
        """收起当前打开的下拉弹层。真机上 ESC 无效，只能再点一次触发器。"""
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
            # ActionChains 被遮挡时用 JS 事件再切换一次
            try:
                self._js_mouse_click(trigger)
            except Exception:
                return
            time.sleep(0.55)

    def _open_dropdown(self, trigger) -> bool:
        """点开下拉弹层：多种点击方式轮试，每次都验证弹层是否真的出现。"""
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
        """点开一个下拉空位，抓取弹层容器里的候选选项。

        真机确认：弹层是 body 下的 .digEmptySelectView_dropdownContent，
        每个空位给出的是同一组候选；ESC 收不起来，只能再点触发器。
        """
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
        """点开下拉触发器，在弹层里选择与答案文本匹配的选项，并校验回显。"""
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
            # 选项点击被遮挡时用 JS 事件兜底
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

    # ---- 组内提交 -------------------------------------------------------
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
        """提交后被平台拒绝时的顶部提示（如「客观题正确率未达到60%」）。

        注意：只认否定句（未达到/不足），不能把「本单元学习规则」弹窗里的
        「客观题正确率60%」过关条件误判成拒绝。
        """
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
            # 提示也可能挂在外壳层
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
        """提交一个 course-v3 题目组，并等待进入已批改状态。

        平台在客观题正确率不足 60% 时会拒绝提交并弹 toast，
        这种情况记录到 self.last_rejection，由上层决定是否继续。
        resolver: 可选的「按 key 重新定位题目组」回调；批改后 DOM 会重渲染，
        提交前的元素引用会失效，需要用新元素判断按钮是否变成「重新作答」。
        """
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
        """等待题目组进入已批改状态：按钮变「重新作答」或出现答案预览。"""
        deadline = time.time() + timeout
        current = group
        while time.time() < deadline:
            if resolver is not None:
                try:
                    # 批改可能重载 iframe；resolver 必须始终在内容层执行。
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

    # ---- 外壳层弹窗 -----------------------------------------------------
    def dismiss_rules_popup(self, max_candidates: int = 400) -> bool:
        """关闭「本单元学习规则」弹窗：挂在外壳层，且只认真实鼠标点击。"""
        PublishPlayer.reset(self.driver)
        # 一次浏览器端查询定位规则弹窗及按钮，避免全页逐元素往返 WebDriver。
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
    """内容处理器基类"""

    @abstractmethod
    def can_handle(self, question: Question) -> bool:
        pass

    @abstractmethod
    def handle(self, question: Question) -> bool:
        pass


class DiscussionBoardHandler(ContentHandler):
    """讨论板处理器"""

    def __init__(self, driver):
        self.driver = driver

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.DISCUSSION_BOARD

    def handle(self, question: Question) -> bool:
        print("     讨论板页面，无需作答")
        return True


class SelfCheckHandler(ContentHandler):
    """Self-check 词汇勾选处理器"""

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
    """视频处理器"""

    def __init__(self, driver, config: Config, stop_requested: threading.Event):
        self.driver = driver
        self.config = config
        self.stop_requested = stop_requested
        self.popup_monitor_thread = None
        self.stop_monitoring = threading.Event()

        self.transcriber = AudioTranscriber()

        self.analyzer_client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url
        )

        self.video_transcript = ""
        self.current_video_url = ""

    def _play_video_and_handle_popups(self):
        """播放视频并自动处理弹窗选择题（供外部预处理调用）"""
        if self.stop_requested.is_set():
            return
        video_info = self._get_video_info()
        if not video_info:
            print("      未找到视频元素")
            return

        video_url = video_info.get('url', '')
        duration = video_info.get('duration', 0)

        if video_url and video_url == self.current_video_url and self.video_transcript:
            print(f"      使用已缓存的视频转录（{len(self.video_transcript)}字符）")
        else:
            self.current_video_url = video_url
            self.video_transcript = self._transcribe_video(video_url, duration)
        if self.stop_requested.is_set():
            return
        self.stop_monitoring.clear()
        self.popup_monitor_thread = threading.Thread(
            target=self._monitor_popup_questions,
            daemon=True
        )
        self.popup_monitor_thread.start()

        try:
            self._play_video(duration)
            if not self.stop_requested.is_set():
                print("      视频播放完成")
        finally:
            self.stop_monitoring.set()
            if self.popup_monitor_thread.is_alive():
                self.popup_monitor_thread.join(timeout=1)

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VIDEO

    def handle(self, question: Question) -> bool:
        if self._check_video_completed():
            print("     视频已标记为完成，跳过")
            return True

        print("     视频页面，开始处理...")
        self._play_video_and_handle_popups()
        print("     视频处理完成")
        return True

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

    def _play_video(self, duration: float):
        video = None
        try:
            video = self.driver.find_element(By.TAG_NAME, 'video')

            if duration > 0:
                print(f"      ▶ 播放视频（{int(duration)}秒，2倍速）...")
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)

                self._wait_for_video_complete(video, duration)
            else:
                self.driver.execute_script("""
                    arguments[0].playbackRate = 2.0;
                    arguments[0].muted = true;
                    arguments[0].play();
                """, video)
                print(f"      ⏳ 等待 10 秒...")
                self.stop_requested.wait(10)

        except Exception as e:
            print(f"       视频播放失败: {str(e)[:50]}")

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
                    question_data = self._parse_popup_question(popup)

                    if not question_data:
                        print("      [监视器]  未能解析题目")
                        continue

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
                    else:
                        print(f"      [监视器]  点击失败: {answer}")

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

    def _intelligent_select_answer(self, question_data: Dict) -> str:
        question = question_data['question']
        options = question_data['options']

        print(f"      [监视器]  分析问题: {question[:50]}...")
        prompt = self._build_analysis_prompt(question, options)

        try:
            response = self.analyzer_client.chat.completions.create(
                model="kimi-k2-turbo-preview",
                messages=[
                    {
                        "role": "system",
                        "content": "你是视频理解助手。根据视频内容选择最正确的答案，只返回选项字母，不要解释。"
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=5
            )

            answer_text = response.choices[0].message.content.strip().upper()

            valid_letters = [opt['letter'] for opt in options]
            for letter in valid_letters:
                if letter in answer_text:
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

    def _wait_for_video_complete(self, video, duration: float):
        max_wait = duration / 2 + 30
        start_time = time.time()
        last_progress = 0

        while time.time() - start_time < max_wait:
            try:
                if self.stop_requested.is_set() or self.stop_monitoring.is_set():
                    break

                current = self.driver.execute_script("return arguments[0].currentTime;", video)
                ended = self.driver.execute_script("return arguments[0].ended;", video)

                if ended or current >= duration - 1:
                    print(f"       视频播放完成")
                    break

                elapsed = int(time.time() - start_time)
                if elapsed - last_progress >= 5:
                    print(f"      播放进度: {int(current)}/{int(duration)} 秒")
                    last_progress = elapsed

                if self.stop_requested.wait(0.5):
                    break

            except:
                break

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
    """单词闪卡处理器 """

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


class AISolver:
    """AI答题器 - 协调解析、构建、执行流程"""

    def __init__(self, driver, config: Config):
        self.driver = driver
        self.config = config
        self.stop_requested = threading.Event()
        self.ai_client = OpenAICompatibleClient(self.config)
        self.parser = QuestionParser(driver)
        self.prompt_builder = PromptBuilder(self.ai_client)
        self.executor = AnswerExecutor(driver)
        self.publish_parser = PublishQuestionParser(driver)
        self.publish_executor = PublishAnswerExecutor(driver)
        self.video_handler = VideoHandler(driver, self.config, self.stop_requested)
        self.content_handlers: List[ContentHandler] = [
            self.video_handler,
            FlashcardHandler(driver, self.stop_requested),
            SelfCheckHandler(driver, self.stop_requested),
            DiscussionBoardHandler(driver),
        ]
        self.processed_hashes: set = set()
        self._processed_video_tabs: set = set()
        self._processed_audio_tabs: set = set()
        self._publish_processed_groups: set = set()
        self._publish_scope: Optional[PublishTaskScope] = None
        self._publish_scope_url: str = ''

    def request_stop(self):
        self.stop_requested.set()

    def clear_stop(self):
        self.stop_requested.clear()

    def _should_stop(self) -> bool:
        return self.stop_requested.is_set()

    def process_selected_tabs(self, selected_tabs: List[Dict], chapter_name: str = ""):
        """
        按用户勾选的 Tab 列表逐个处理（自动模式）。
        selected_tabs: 完整的 tab dict 列表（来自扫描结果）
        """
        if not chapter_name:
            chapter_name = "selected_chapter"

        print(f"\n{'=' * 60}")
        print(f"开始处理 {len(selected_tabs)} 个选中任务")
        print(f"{'=' * 60}")

        self.ai_client.start_new_chapter(chapter_name)
        self.processed_hashes.clear()
        self._publish_processed_groups = set()

        course_home_url = self.driver.current_url

        for task_idx, tab in enumerate(selected_tabs):
            if self._should_stop():
                print("  已请求停止，批量处理提前结束")
                break

            tab_name = tab.get('l1_title', 'unknown')

            if '_element' in tab and tab['_element'] is not None:
                print(f"\n  [{task_idx+1}/{len(selected_tabs)}] {tab['display']}")

                if task_idx > 0:
                    self.driver.get(course_home_url)
                    if self.stop_requested.wait(3):
                        break
                    self.ai_client.force_reset(f"{chapter_name}_{tab_name}")

                if '_unit_idx' in tab:
                    try:
                        unit_container = WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located(
                                (By.CLASS_NAME, 'unipus-tabs_unitTabScrollContainer__fXBxR'))
                        )
                        unit_tabs = unit_container.find_elements(By.CSS_SELECTOR, ':scope > *')
                        if tab['_unit_idx'] >= len(unit_tabs):
                            print("    目标Unit不存在，跳过")
                            continue
                        if self._should_stop():
                            break
                        self.driver.execute_script("arguments[0].click();", unit_tabs[tab['_unit_idx']])
                        if self.stop_requested.wait(1.2):
                            break
                    except Exception as e:
                        print(f"    切换Unit失败: {str(e)[:50]}")
                        continue

                chapter_clicked = False
                try:
                    chapters = self.driver.find_elements(
                        By.CSS_SELECTOR, '[class*="courses-unit_taskItemInnerLayout__"], [class*="courses-unit_nodeItemInnerLayout__"]'
                    )
                    name_occurrence = 0
                    for ch in chapters:
                        if self._should_stop():
                            break
                        try:
                            name_elem = ch.find_element(By.CSS_SELECTOR, '[class*="courses-unit_taskTypeName__"], [class*="courses-unit_nodeTypeName__"]')
                            if name_elem.text.strip() == tab_name:
                                if name_occurrence == tab.get('_name_occurrence', 0):
                                    if self._should_stop():
                                        break
                                    self.driver.execute_script("arguments[0].click();", name_elem)
                                    chapter_clicked = True
                                    break
                                name_occurrence += 1
                        except:
                            continue
                except Exception as e:
                    print(f"    重新定位章节失败: {str(e)[:50]}")

                if self._should_stop():
                    break
                if chapter_clicked:
                    if self.stop_requested.wait(3):
                        break
                    self._process_tab_with_accumulation(tab_name, task_idx, 0)
                    if self.stop_requested.wait(2):
                        break
                else:
                    print(f"  点击章节失败，跳过")

            else:
                l1_idx = tab.get('l1_idx', 0)
                l2_idx = tab.get('l2_idx', -1)

                level1_tabs = self._get_level1_tabs()
                if l1_idx >= len(level1_tabs):
                    print(f"  一级Tab索引 {l1_idx} 越界，跳过")
                    continue

                l1_tab = level1_tabs[l1_idx]
                print(f"\n  [{task_idx+1}/{len(selected_tabs)}] 一级Tab: {l1_tab['title']}")

                if self._should_stop():
                    break
                if not WebDriverHelper.safe_click(self.driver, l1_tab['element']):
                    print(f"  点击一级Tab失败，跳过")
                    continue
                if self.stop_requested.wait(1.5):
                    break
                if l2_idx < 0:
                    self._process_tab_with_accumulation(l1_tab['title'], l1_idx, 0)
                    if self.stop_requested.wait(2):
                        break
                else:
                    level2_tabs = self._get_level2_tabs()
                    if l2_idx >= len(level2_tabs):
                        print(f"  二级Tab索引 {l2_idx} 越界，跳过")
                        continue

                    l2_tab = level2_tabs[l2_idx]
                    print(f"    二级Tab: {l2_tab['title']}")

                    if self._should_stop():
                        break
                    if not WebDriverHelper.safe_click(self.driver, l2_tab['element']):
                        print(f"  点击二级Tab失败，跳过")
                        continue
                    if self.stop_requested.wait(1.5):
                        break
                    combined_name = f"{l1_tab['title']}_{l2_tab['title']}"
                    self._process_tab_with_accumulation(combined_name, l1_idx, l2_idx)
                    if self.stop_requested.wait(2):
                        break
        print(f"\n{'=' * 60}")
        print("批量处理已停止" if self._should_stop() else f"全部 {len(selected_tabs)} 个任务处理完毕")
        print(f"{'=' * 60}")

    def _process_tab_with_accumulation(self, tab_name: str, l1_idx: int, l2_idx: int) -> bool:
        """处理Tab - 累积原文模式，包含视频/音频预处理"""

        if self._should_stop():
            return False
        if self._is_publish_page():
            print("    检测到新版 ipublish 播放器，跳过旧版音视频预处理")
            return self._process_current_tab_content(
                self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx
            )
        self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx)
        if self._should_stop():
            return False
        self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx)
        if self._should_stop():
            return False

        current_passage = self._extract_passage()
        if self._should_stop():
            return False
        if current_passage:
            self.ai_client.add_passage_if_new(current_passage)

        return self._process_current_tab_content(
            self.ai_client.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx
        )

    def _process_current_tab_content(self, chapter_name: str, tab_name: str, l1_idx: int, l2_idx: int) -> bool:
        if self._should_stop():
            return False
        direction_part = self._generate_content_hash_from_direction()
        if direction_part == "empty":
            direction_part = "no_direction"

        content_hash = f"{chapter_name}|{tab_name}|{l1_idx}|{l2_idx}|{direction_part}"

        print(f"    内容标识: {hashlib.md5(content_hash.encode()).hexdigest()[:16]}...")

        if content_hash in self.processed_hashes:
            print(f"   ⏭ 已处理过，跳过")
            return False

        self.processed_hashes.add(content_hash)

        if self._is_publish_page():
            return self._process_publish_content(tab_name)

        page_num = 1
        total_answered = 0
        last_questions_signature = ""

        while True:
            if self._should_stop():
                print("    已请求停止，当前任务提前结束")
                return False

            self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx)
            if self._should_stop():
                return False
            self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx)
            if self._should_stop():
                return False
            questions, directions = self.parser.parse_all()
            if self._should_stop():
                return False
            print(f"\n    处理第 {page_num} 页题目...")
            print(f"    找到 {len(questions)} 个可见题目")

            current_signature = self._generate_questions_signature(questions)

            if current_signature == last_questions_signature and page_num > 1:
                print(f"    题目内容与上次相同，可能已到达最后一页")
                break

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
                        print(f"     使用 {handler.__class__.__name__} 处理")
                        handler.handle(q)
                        if self._should_stop():
                            return False
                        special_handled = True
                        if q.q_type == QuestionType.SELF_CHECK:
                            self_check_handled = True
                        if q.q_type in [QuestionType.VOCABULARY_FLASHCARD, QuestionType.VIDEO]:
                            print(f"    特殊内容处理完成")
                            return True
                        break

            normal_questions = [q for q in questions if q.q_type not in [
                QuestionType.VOCABULARY_FLASHCARD,
                QuestionType.VIDEO,
                QuestionType.SELF_CHECK,
                QuestionType.DISCUSSION_BOARD
            ]]

            if normal_questions:
                print(f"    共 {len(normal_questions)} 道题目需要回答")
                prompt = self.prompt_builder.build(normal_questions, directions)
                if self._should_stop():
                    return False
                ai_response = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
                if self._should_stop():
                    return False
                if ai_response:
                    success_count = 0

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
                        else:
                            ans = ai_response

                        if ans:
                            if self.executor.execute(q, ans):
                                success_count += 1
                        else:
                            print(f"    题目 {q.number} 无答案")

                    total_answered += success_count
                    print(f"    本页成功填写 {success_count}/{len(normal_questions)} 题")
                    if success_count != len(normal_questions):
                        print("    本页未完整填写，停止当前任务，请检查答案和选项状态")
                        return False
                else:
                    print("    AI 未返回答案，停止当前任务")
                    return False

            if self._should_stop():
                return False
            if self_check_handled and not normal_questions and self.executor.submit():
                self._wait_for_submit_complete()
                if self._should_stop():
                    return False
                self._handle_confirm_dialog()
            next_btn = self._find_next_question_button()
            if next_btn:
                print(f"    点击下一题...")
                pre_click_signature = current_signature

                if self._should_stop():
                    return False
                if not WebDriverHelper.safe_click(self.driver, next_btn):
                    print(f"    点击下一题失败")
                    break

                if not self._wait_for_content_change(pre_click_signature, timeout=5):
                    if self._should_stop():
                        return False
                    print(f"    内容未变化，可能已到最后一页")
                    break

                page_num += 1

                if page_num > 50:
                    print(f"    达到最大页数限制，停止")
                    break
                continue

            if self._should_stop():
                return False
            if normal_questions and self.executor.submit():
                self._wait_for_submit_complete()
                if self._should_stop():
                    return False
                self._handle_confirm_dialog()
            print(f"    没有更多题目了")
            break
        print(f"    总共回答 {total_answered} 题")
        return True

    # ---- 新版 ipublish 播放器（course-v3）---------------------------------
    def _is_publish_page(self) -> bool:
        """当前页面是否为新版 ipublish 播放器；无 driver 时安全返回 False。"""
        driver = getattr(self, 'driver', None)
        if driver is None:
            return False
        try:
            return PublishPlayer.is_publish_page(driver)
        except Exception:
            return False

    def _process_publish_content(self, task_name: str = '') -> bool:
        """只处理当前打开的目录任务；批量任务切换由勾选列表控制。"""
        print("    检测到新版 ipublish 播放器（course-v3），启用专用适配流程")
        self.publish_executor.dismiss_rules_popup()
        if not self._publish_prepare_scope(task_name):
            return False

        handled = 0
        skipped = 0
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
            if result == 'skipped':
                skipped += 1
                idle_reveals = 0
                continue
            if result == 'rejected':
                rejected += 1
                idle_reveals = 0
                continue

            # 只在当前任务边界内补充懒加载，不点击「下一节」进入未勾选任务。
            self.publish_executor.dismiss_rules_popup()
            if idle_reveals < 25 and self._publish_reveal_more_groups():
                idle_reveals += 1
                continue
            break

        if handled:
            print(f"    新版播放器共提交 {handled} 个题目组")
        if rejected:
            print(f"    另有 {rejected} 个题目组因正确率不足 60% 被平台拒绝提交")
        if skipped:
            print(f"    另有 {skipped} 个题目组题型暂不支持或未渲染，已跳过")
        if not handled and not skipped and not rejected:
            print("    当前任务没有待自动作答的题目组（已完成或需人工处理），不进入其他任务")
        return not self._should_stop()

    def _publish_prepare_scope(self, task_name: str = '', timeout: float = 8.0) -> bool:
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
        # 播放器会随题块状态自动更新外壳路由，不能用它重新划定作答范围。
        # 始终使用最初的正文锚点；select 同时校验 iframe 文档和边界是否仍有效。
        return scope.select(self.driver, groups)

    def _publish_group_in_scope(self, group) -> bool:
        try:
            return bool(self._publish_scoped_groups([{'element': group}]))
        except Exception as exc:
            print(f"    题组范围校验失败：{exc}")
            return False

    def _publish_reveal_more_groups(self) -> bool:
        """仅滚动到本任务最后一个题块，检查范围内是否出现更多题组。"""
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
        """在当前页处理下一个待作答题目组，返回 answered / done / error。"""
        # 切单元后可能再次弹出「本单元学习规则」，每组开始前先顺手关掉
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
            for state in scoped_groups:
                if state['kind'] in ('skip', 'upload'):
                    if state['kind'] == 'upload':
                        print(f"    跳过需要手动上传附件的题目组: {state['type']}")
                    continue
                if parser.group_submitted(state['element']):
                    continue
                key = parser.group_key(state['element'])
                if key in self._publish_processed_groups:
                    continue
                pending = (state, key)
                break

            if pending is None:
                return 'done'

            state, key = pending
            outcome = self._publish_answer_group(state['element'], state['type'], key)
            if outcome == 'submitted':
                return 'answered'
            if outcome == 'rejected':
                return 'rejected'
            if outcome == 'skipped':
                return 'skipped'
            return 'error'

    def _publish_answer_group(self, group, group_type: str, key: str):
        """作答并提交一个题目组（调用前需已在内容 iframe 内）。

        返回 'submitted' / 'skipped' / False。
        """
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

        # 播放器是懒渲染：没滚到可视区时组内只有占位 DOM（innerText 为空）。
        # 必须先等它真正挂载，否则会对着空组填答案、提交也等不到已批改状态。
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

        prompt = self.prompt_builder.build(questions, direction)
        prompt += self._publish_fill_format_hint(questions)
        if group_type == 'fill_blanks_dropdown_group':
            prompt += self._publish_dropdown_options_prompt(questions)
        ai_response = self.ai_client.ask(prompt, stop_requested=self.stop_requested)
        if self._should_stop():
            return False
        if not ai_response:
            print("    AI 未返回答案")
            return False

        filled = 0
        blank_offset = 0
        for question in questions:
            if self._should_stop() or not self._publish_group_in_scope(group):
                return False
            answer = self._publish_answer_text(question, ai_response, blank_offset)
            if question.q_type == QuestionType.FILL_IN:
                answer = self._normalize_fill_answer_capitalization(question, answer)
            if answer and self.publish_executor.execute(question, answer):
                filled += 1
            if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT):
                blank_offset += question.answer_count or 1
        print(f"    本组填写 {filled}/{len(questions)} 题")

        if filled != len(questions):
            print("    本组未完整填写，不提交，停止当前任务以免留下半成品")
            return False

        if self._should_stop():
            return False

        # 提交前退出内容层：切单元后「本单元学习规则」弹窗可能再次出现，
        # 它会挡住提交按钮（它在外壳层，只有真实鼠标点击才有效）。
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
                # 关弹窗会重排外壳 DOM，重新定位一次更稳；定位不到就沿用原元素
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
                # 批改后组 id/key 可能变化，用「已批改组数量是否增加」兜底确认
                if self._publish_submitted_count() > submitted_before:
                    print("    已批改题目组数量增加，判定提交成功")
                    submitted_ok = True
            if not submitted_ok:
                # 主观题批改后播放器可能重载内容（iframe 文档换了），
                # 旧元素全部失效，这里退出重进后再确认一次。
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
        """退出并重新进入内容 iframe，再确认该组是否已批改。"""
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
        """题目组在当前页的顺序下标；批改后组 id 会变，用下标做兜底定位。"""
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
        """当前页已批改（按钮为「重新作答」）的题目组数量。"""
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

    def _publish_dropdown_options_prompt(self, questions: List[Question]) -> str:
        """下拉填空：尽力抓取每个空位的候选列表，供模型按原样选择。"""
        if not questions:
            return ''
        lines = []
        offset = 0
        for question in questions:
            blanks = self.publish_executor._dropdown_blanks(question)
            for index, blank in enumerate(blanks, offset + 1):
                try:
                    options = self.publish_executor.collect_dropdown_options(blank)
                except Exception:
                    options = []
                print(f"    空{index} 候选抓取: {len(options)} 个")
                if options:
                    lines.append(f"   空{index} 候选: {' / '.join(options)}")
            offset += question.answer_count or len(blanks) or 1
        if not lines:
            return ''
        return ("\n【空位候选】每个空只能从下列候选中选择，尽量按原样（含大小写）作答：\n"
                + "\n".join(lines) + "\n")

    def _publish_fill_format_hint(self, questions: List[Question]) -> str:
        """一个题目组里有多道填空时，要求按整组空号连续编号，避免题号/空号混在一起。"""
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

    def _publish_wait_group_rendered(self, group, timeout: float = 12.0) -> bool:
        """等题目组真正渲染：innerText 非空即视为已挂载。"""
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
                             blank_offset: int = 0) -> str:
        if question.q_type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
                               QuestionType.TRUE_FALSE):
            return self._extract_single_answer(ai_response, question.number)
        answer = ai_response
        if question.q_type in (QuestionType.FILL_IN, QuestionType.DROPDOWN_SELECT):
            answer = self._sanitize_fill_answer(answer)
            if blank_offset:
                return self._shift_blank_answer(answer, blank_offset,
                                                question.answer_count or 1)
        return answer

    @staticmethod
    def _sanitize_fill_answer(answer: str) -> str:
        """去掉「1. (1) Iraq」这种题号后紧跟空号括号的重复编号。"""
        cleaned = re.sub(r'(?<=\d\.)\s*\(\s*\d+\s*\)', ' ', answer or '')
        return re.sub(r'[ \t]+', ' ', cleaned).strip()

    def _normalize_fill_answer_capitalization(self, question: Question, answer: str) -> str:
        """句首空自动补首字母大写（平台按正确率判分，大小写也算分）。"""
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
    def _shift_blank_answer(answer: str, offset: int, count: int) -> str:
        """同组多道填空题：把第 offset+1..offset+count 个空映射回 1..count。

        course-v3 的题目组里可能有多道填空，AI 一次给的是整组空号；
        每道题只能拿自己那一段，否则第二题会重复第一题的答案。
        """
        parsed = AnswerExecutor._parse_banked_answer(answer, offset + count)
        values = parsed[offset:offset + count]
        return ' '.join(f'{index + 1}.{value}'
                        for index, value in enumerate(values) if value)

    def _publish_advance_page(self) -> bool:
        """尝试切换到下一节，返回是否真的切换成功。"""
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
        """内容层可见文本长度，用于判断是否真的换页。"""
        with PublishFrameContext(self.driver) as context:
            if not context.entered:
                return 0
            try:
                return self.driver.execute_script(
                    'return document.body ? document.body.innerText.length : 0;') or 0
            except Exception:
                return 0

    def _publish_click_next(self) -> bool:
        """先找内容层的「下一节」，再退回外壳层。"""
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
        """查找「下一节/下一题」控制（在当前执行上下文里查找）。"""
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
            text_preview = q.text[:30] if q.text else ""
            option_preview = "|".join(opt.text[:30] for opt in q.options[:4])
            parts.append(f"{q.number}:{q_type}:{text_preview}:{option_preview}")

        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

    def solve_current_page(self, chapter_name: str = "unknown") -> bool:
        print("\n" + "=" * 60)
        print(" 开始处理当前停留的页面")
        print("=" * 60)

        self._publish_processed_groups = set()

        quick_tab_name = f"quick_current_page_{int(time.time())}"
        self.ai_client.force_reset(f"{chapter_name}_{quick_tab_name}")

        state_key = self._generate_content_hash()

        if not state_key or state_key == "empty":
            state_key = f"{chapter_name}_{int(time.time())}"

        print(f"    内容标识: {state_key[:50]}...")

        if state_key in self.processed_hashes:
            print(f"   ⏭ 该页面的哈希已被记录，正在执行作答...")

        self.processed_hashes.add(state_key)

        success = self._process_current_tab_content(
            chapter_name,
            quick_tab_name,
            0,
            0
        )

        print(f"\n{'=' * 60}")
        print(" 当前页面处理已停止" if self._should_stop() else " 当前页面处理完毕")
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
            # 新版路由包含教材/单元/任务，外壳没有旧版题干；不要白等 3 × 5 秒。
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
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, 'button')
            for btn in buttons:
                text = btn.text.strip()
                if self._should_stop():
                    return False
                if any(k in text for k in ['确认', '确定', '我知道了', '继续', 'OK']):
                    if btn.is_displayed():
                        btn.click()
                        if self.stop_requested.wait(1):
                            return False
                        return True
        except:
            pass
        return False


    def _preprocess_audio_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int):
        """检测并预处理音频：下载+转录，将转录文本注入 AI 上下文"""
        if self._should_stop():
            return
        if self._has_video_on_page():
            return

        if not self._has_audio_on_page():
            return

        try:
            audio_url = self._extract_audio_url_from_page()
            if not audio_url:
                print("   未找到有效音频URL，跳过")
                return

            audio_key = audio_url.split('#')[0]
            if audio_key in self._processed_audio_tabs:
                return

            print("   检测到音频，开始预处理（下载+转录）...")
            transcript = self.video_handler.transcriber.transcribe(audio_url, language="en")

            if self._should_stop():
                return
            if transcript:
                self.ai_client.add_audio_transcript_if_new(transcript)
                print(f"   已将音频转录（{len(transcript)}字符）加入上下文")
            else:
                print("   音频转录为空")

            self._processed_audio_tabs.add(audio_key)

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"音频预处理异常: {e}", exc_info=True)

    def _has_audio_on_page(self) -> bool:
        """检测页面是否包含音频元素或音频材料"""
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

    def _extract_audio_url_from_page(self) -> Optional[str]:
        """从页面提取音频URL"""
        try:
            audio_elem = self.driver.find_element(By.CSS_SELECTOR, 'audio')
            src = audio_elem.get_attribute('src')
            if src:
                return src.split('#')[0]

            sources = self.driver.find_elements(By.CSS_SELECTOR, 'audio source')
            for source in sources:
                src = source.get_attribute('src')
                if src:
                    return src.split('#')[0]

            return None
        except:
            return None

    def _preprocess_video_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int):
        """检测并预处理视频：播放、转录、处理弹窗，将转录文本注入 AI 上下文"""
        if self._should_stop():
            return
        if not self._has_video_on_page():
            return

        try:
            video_info = self.video_handler._get_video_info()
            video_url = (video_info or {}).get('url', '')
            video_key = video_url.split('#')[0] if video_url else f"{tab_name}|{l1_idx}|{l2_idx}"
            if video_key in self._processed_video_tabs:
                return

            print("   检测到视频，开始预处理（播放+转录）...")
            self.video_handler._play_video_and_handle_popups()

            if self._should_stop():
                return
            transcript = self.video_handler.video_transcript
            if transcript:
                self.ai_client.add_video_transcript_if_new(transcript)
                print(f"   已将视频转录（{len(transcript)}字符）加入上下文")
            else:
                print("   未获得视频转录，后续题目可能缺乏上下文")

            self._processed_video_tabs.add(video_key)

        except Exception as e:
            print(f"   预处理失败: {str(e)[:80]}")
            logger.error(f"视频预处理异常: {e}", exc_info=True)

    def _has_video_on_page(self) -> bool:
        """检测当前页面是否包含可见的视频元素"""
        try:
            videos = self.driver.find_elements(By.TAG_NAME, 'video')
            return any(v.is_displayed() for v in videos)
        except:
            return False

    def _wait_for_submit_complete(self, timeout: int = 8):
        """
        等待提交完成。策略：
          1. 等待提交按钮消失
          2. 或等待 '提交成功' / '保存成功' 等提示出现
          3. 最少睡眠 1.5 秒兜底
        """
        if self.stop_requested.wait(1.5):
            return
        start = time.time()

        while time.time() - start < timeout and not self._should_stop():
            try:
                submit_btn = self._find_visible_submit_button()
                if submit_btn is None:
                    print(f"   提交按钮已消失，提交完成")
                    return

                body_text = self.driver.find_element(By.TAG_NAME, 'body').text[:500].lower()
                if any(kw in body_text for kw in ['提交成功', '保存成功', 'success', '已提交', 'submitted']):
                    print(f"   检测到成功提示")
                    return

            except:
                pass

            if self.stop_requested.wait(0.5):
                return

        print(f"   等待超时（{timeout}s），继续执行")

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
    """U校园机器人 - 组装所有组件"""

    def __init__(self, config_path: str = 'config.json', skip_check: bool = False):
        self.config = Config.from_json(config_path)
        self.driver = None

        if not skip_check:
            self._ensure_environment()

        self.driver = self._create_driver()
        self.popup_watcher = PopupWatcher(self.driver)

    def _ensure_environment(self):
        checker = EnvironmentChecker()

        if not checker.check_all():
            while True:
                choice = checker.show_fix_guide()

                if choice == '1':
                    checker.auto_install_edge()
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
        print("""
    【问题诊断】

    1. Edge 浏览器问题
       原因：Edge 未安装
       解决：选择 [1] 自动安装，或访问 https://www.microsoft.com/edge

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

    COMPAT_EDGE_ARGS = ('--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                        '--disable-extensions')

    def _start_edge(self, temp_dir: str, extra_args=()):
        """按指定临时目录启动 Edge，并注入反检测脚本。"""
        options = webdriver.EdgeOptions()
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        for argument in extra_args:
            options.add_argument(argument)

        try:
            user_data_dir = tempfile.mkdtemp(prefix='ucampus_', dir=temp_dir or None)
        except Exception:
            user_data_dir = tempfile.mkdtemp(prefix='ucampus_')
        options.add_argument(f'--user-data-dir={user_data_dir}')
        if temp_dir:
            options.add_argument(f'--disk-cache-dir={os.path.join(temp_dir, "edge_cache")}')
            options.add_argument(f'--crash-dumps-dir={temp_dir}')

        driver = webdriver.Edge(options=options)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
        })
        return driver

    def _create_driver(self):
        # 系统临时目录不可写时，Edge 会报
        # "cannot create temp dir for unpacking extensions"，这里统一换成可写目录
        temp_dir = ensure_writable_temp_dir()

        # Selenium Manager 的驱动缓存也放到可写目录，避免用户目录 .cache 拒绝访问
        try:
            selenium_cache = os.path.join(BASE_DIR, 'logs', 'selenium_cache')
            os.makedirs(selenium_cache, exist_ok=True)
            os.environ.setdefault('SE_CACHE_PATH', selenium_cache)
        except Exception:
            pass

        try:
            return self._start_edge(temp_dir)
        except (SessionNotCreatedException, WebDriverException) as exc:
            # 受限环境（沙箱/受限用户/显卡驱动异常）下首次启动可能失败，降级重试一次：
            # 换新的临时 profile 并加上兼容参数
            print(f" 启动 Edge 失败（{str(exc)[:120]}），尝试兼容模式重试...")
            return self._start_edge(temp_dir, extra_args=self.COMPAT_EDGE_ARGS)

    def start(self):
        solver = AISolver(self.driver, self.config)

        self.gui = FluentModernGUI(self.driver, solver, self, globals())

        threading.Thread(target=self.popup_watcher.run, daemon=True).start()
        threading.Thread(target=self._background_login_flow, daemon=True).start()

        self.gui.mainloop()

        return True

    def _background_login_flow(self):
        gui_log_queue.put(" 正在与 U校园 建立连接，请稍候...")
        success = self._login()
        if success:
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
                username.clear()
                username.send_keys(self.config.username)
                password.clear()
                password.send_keys(self.config.password)
                if not agreement_check.is_selected():
                    agreement_control = WebDriverWait(self.driver, 20).until(
                        EC.element_to_be_clickable((
                            By.CSS_SELECTOR,
                            'label.usso-agreements-checkbox .usso-checkbox'
                        )))
                    agreement_control.click()
                    # 新版账号中心点击协议会弹「服务协议与隐私政策」确认框，
                    # 必须再点一次「同意」，否则协议不会勾选、登录会被拦下。
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
                gui_log_queue.put("⏳ 正在智能轮询登录状态...")
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
            error_msg = str(e)
            gui_log_queue.put(f" 登录执行流阻断: {error_msg[:50]}")
            logger.error(f"详细错误: {error_msg}", exc_info=True)
            return False

    def _sync_auth_state(self, legacy_token: Optional[str], configured_jwt: Optional[str]) -> Dict[str, Any]:
        """同步旧版存储、新版门户状态和共享 JWT cookie。"""
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
        """优先使用本次 SSO 会话，并兼容旧版完整 token 与新版 JWT。"""
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

        # Remove the one-time ticket from the URL; a consumed ticket can leave the SPA loading forever.
        self.driver.get(UCLOUD_HOME_URL)
        WebDriverWait(self.driver, 45).until(self._home_ready)
        self._sync_auth_state(legacy_token, configured_jwt)



class PopupWatcher:
    """弹窗监控器"""

    def __init__(self, driver):
        self.driver = driver

    def run(self):
        while True:
            try:
                self._click_known_buttons()
                time.sleep(0.5)
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)

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

    # 先把临时目录换成可写的：受限环境下 Edge 启动会因为系统 TEMP 不可写而失败
    ensure_writable_temp_dir()

    skip_check = '--skip-check' in sys.argv

    if not skip_check:
        print("\n 提示：")
        print("   - 首次运行需要检查环境")
        print("   - 语音识别需要 FFmpeg（约130MB，可自动安装）")
        print("   - 如检查通过但无法启动，使用 --skip-check 跳过")

    logger = setup_logging()

    try:
        bot = UCampusBot(os.path.join(BASE_DIR, 'config.json'), skip_check=skip_check)
        bot.start()
    except Exception as e:
        error_msg = str(e)
        print(f"\n 程序运行失败: {error_msg[:100]}")
        logger.error(f"程序异常: {error_msg}", exc_info=True)
        input("\n按任意键退出...")
