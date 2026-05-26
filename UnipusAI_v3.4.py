from AudioRecognizer import *
from EnvironmentChecker import *
import hashlib, json, logging, os, sys, random, re, threading, time, warnings, winsound
import queue
import tkinter as tk
from tkinter import scrolledtext
import customtkinter as ctk
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from glob import glob
from typing import List, Optional, Dict, Any, Tuple, Callable

from openai import OpenAI
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import webbrowser as wb

gui_log_queue = queue.Queue()

DEBUG_MODE = False

DEBUG_MODE = False


def deprecated(func):
    def wrapper(*args, **kwargs):
        warnings.warn(f"Function {func.__name__} is deprecated and will be removed in future versions.",
                      DeprecationWarning, stacklevel=2)
        return func(*args, **kwargs)

    return wrapper


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
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
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
                if any(x in text for x in ['', 'Error', 'Exception', 'Traceback']):
                    self.logger.error(text.strip())
                elif any(x in text for x in ['', 'Warning']):
                    self.logger.warning(text.strip())
                else:
                    self.logger.info(text.strip())
                gui_log_queue.put(text.strip())

        def flush(self):
            pass

    sys._original_stdout = sys.stdout
    sys.stdout = PrintRedirector(logger)
    return logger, log_file


@dataclass(frozen=True)
class Config:
    """不可变配置类"""
    url: str
    username: str
    password: str
    api_key: str
    token_full: str
    target_course: str
    learning_strategy: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    timeout: int
    whisper_api: str
    debug_mode: bool

    @classmethod
    def from_json(cls, path: str = "config.json") -> "Config":
        with open(path, "r", encoding="UTF-8") as f:
            data = json.load(f)
        global DEBUG_MODE
        DEBUG_MODE = data.get("debug_mode", False)
        return cls(
            url=data.get("url"),
            username=data.get("username"),
            password=data.get("password"),
            token_full=data.get("token_full"),
            api_key=data.get("api_key"),
            target_course=data.get("target_course", "新视野大学英语（第四版）读写教程1"),
            learning_strategy=data.get("learning_strategy", "learn_all_compulsory_course"),
            base_url=data.get("base_url", "https://api.moonshot.cn/v1"),
            model=data.get("model", "kimi-k2-turbo-preview"),
            temperature=data.get("temperature", 0.3),
            max_tokens=data.get("max_tokens", 2000),
            timeout=data.get("timeout", 10),
            whisper_api=data.get("whisper_api", None),
            debug_mode=DEBUG_MODE
        )


class QuestionType(Enum):
    """题目类型"""
    SINGLE_CHOICE = auto()
    MULTIPLE_CHOICE = auto()
    FILL_IN = auto()
    TEXT = auto()
    SORTING = auto()
    DROPDOWN = auto()
    VOCABULARY_FLASHCARD = auto()
    BANKED_CLOZE = auto()
    VOCABULARY_TEST = auto()  # 词汇测试（英汉互译）
    VIDEO = auto()  # 纯视频页面
    VIDEO_POPUP = auto()  # 视频带有弹窗问题
    DISCUSSION_BOARD = auto()
    DROPDOWN_SELECT = auto()
    LISTENING_FILL_IN = auto()
    UNKNOWN = auto()


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

    def is_interactive(self) -> bool:
        """是否有交互元素"""
        if self.q_type in [QuestionType.VIDEO,
                           QuestionType.VOCABULARY_FLASHCARD,  # 闪卡需要交互
                           QuestionType.DISCUSSION_BOARD]:
            return True
        return bool(self.options or self.inputs or self.banked_blanks)

    @property
    def is_phrase_mode(self) -> bool:
        """检测是短语填空还是单词填空"""
        if not self.banked_options:
            return False
        phrase_count = sum(1 for opt in self.banked_options if ' ' in opt.strip() or len(opt) > 15)
        return phrase_count / len(self.banked_options) > 0.3


@dataclass
class AnswerResult:
    """答题结果"""
    success: bool
    question_number: int
    answer: str
    message: str = ""


class Selectors:
    """CSS选择器仓库"""
    FILL_BLANK_INPUTS = [
        '.fe-scoop input:not([type="hidden"])',  # 严格限定input
        '.comp-abs-input input',
        'input.fill-blank--bc-input-DelG1',
    ]
    TEXTAREA_INPUTS = [
        'textarea.question-textarea-content',
        'textarea.writing--textarea-36VPs',
    ]
    MATERIAL_CONTAINER = '.layout-material-container'
    QUESTION_CONTAINERS = [
        '.question-common-abs-reply',
        '.question-common-abs-banked-cloze',
        '.question-wrap',
        '.question-basic',
        '.layoutBody-container.has-reply',
        '.question-material-banked-cloze.question-abs-question',
        '.itest-section',
        '.oral-study-sentence',
        '.question-common-abs-choice',
        '.question-vocabulary',
        '.vocContainer',
    ]
    CHOICE_OPTIONS = [
        '.option.isNotReview',
        'div.option',
        '.MultipleChoice--checkbox-item-34A_-',
        'ul[class*="single-choice"] li label',
        '.option-wrap',
    ]
    OPTION_CAPTION = ['.caption', 'span[class*="index"]', '.MultipleChoice--checkbox-opt-2F4xY']
    OPTION_CONTENT = ['.component-htmlview.content', 'div.html-view[class*="content"]', '.html-view', '.content', 'p']
    FILL_INPUTS = [
        'input.fill-blank--bc-input-DelG1',
        '.fe-scoop input:not([type="hidden"])',
        '.comp-abs-input input',
        'textarea.question-inputbox-input',
        '.question-inputbox-input',
        'textarea.question-textarea-content',
        'textarea.writing--textarea-36VPs',
        'textarea.scoopFill_textarea',
        '.blankinput',
        'input[type="text"]',
    ]
    TEXTAREAS = [
        'textarea.writing--textarea-36VPs',
        'textarea.scoopFill_textarea',
        'textarea.question-inputbox-input',
        '.question-inputbox-input-container textarea',
        'textarea.question-textarea-content',
    ]
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
    VIDEO = ['video.vjs-tech', 'video']
    VOCABULARY_ACTIONS = ['.vocActions', '.vocabulary-actions']
    BANKED_OPTIONS = [
        '.question-material-banked-cloze-reply .option-wrapper .option',
        '.banked-options .option',
        '[data-rbd-draggable-id^="options-"]'
    ]
    BANKED_BLANKS = ['.fe-scoop', '.scoop-wrapper', '.comp-abs-input']
    LEVEL1_TABS = [
        '.pc-header-tabs-container .pc-tab-row > .tab',
        '.pc-header-tabs-container .ant-col.tab',
        '.pc-tab-row > [class*="pc-header-tab"]',
    ]
    LEVEL2_TABS = [
        '.pc-header-tasks-row > .pc-task',
        ':scope > div > div > .pc-header-tasks-row > .pc-task',
    ]
    SIDEBAR = [
        '.pc-slider-content-menu',
        '.pc-slier-menu-container',
        '.pc-slider-menu',
        '#sidemenu',
        '.menu--u3menu-3Xu4h',
        '[class*="slider-menu"]',
        '[class*="side-menu"]'
    ]
    SIDEBAR_NODES = [
        'div[data-role="node"]',
        'div[data-role="micro"]',
        'li.group.courseware',
        '.pc-menu-node',
        '[class*="menu-node"]',
        '.group.courseware'
    ]


class WebDriverHelper:
    """WebDriver辅助工具类（静态方法）"""

    @staticmethod
    def safe_find_element(driver, selectors: List[str], parent=None, timeout: int = 5) -> Optional[Any]:
        """安全查找单个元素"""
        search_context = parent if parent else driver
        wait = WebDriverWait(search_context, timeout)
        for selector in selectors:
            try:
                element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                if element.is_displayed():
                    return element
            except (TimeoutException, NoSuchElementException):
                continue
        return None

    @staticmethod
    def safe_find_elements(driver, selectors: List[str], parent=None, visible_only: bool = True) -> List[Any]:
        """安全查找多个元素"""
        search_context = parent if parent else driver
        for selector in selectors:
            try:
                elements = search_context.find_elements(By.CSS_SELECTOR, selector)
                if visible_only:
                    elements = [e for e in elements if e.is_displayed()]
                if elements:
                    return elements
            except Exception as e:
                error_msg = str(e)
                print(f"操作失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件
                continue
        return []

    @staticmethod
    def is_in_viewport(driver, element) -> bool:
        """检查元素是否在视口内"""
        try:
            return driver.execute_script("""
                var rect = arguments[0].getBoundingClientRect();
                var html = document.documentElement;
                return (
                    rect.top >= 0 && rect.left >= 0 &&
                    rect.bottom <= (window.innerHeight || html.clientHeight) &&
                    rect.right <= (window.innerWidth || html.clientWidth)
                );
            """, element)
        except:
            return True

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


class KimiClient:
    """Kimi API客户端 - 职责：仅处理API通信"""

    SYSTEM_PROMPT = """你是一个专业的英语教学助手，擅长分析英语题目。
请根据题目要求给出准确答案，注意区分不同题型：
- 词汇匹配题：根据英文选中文，或根据中文选英文
- 选词填空：选择最合适的单词填入
- 阅读理解：基于文章内容作答"""

    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url)
        self.conversation_history: List[Dict] = []
        self.current_chapter_id: Optional[str] = None
        self.accumulated_passages: set = set()  # 已累积的原文哈希，防重复

    def start_new_chapter(self, chapter_id: str):
        """开始新章节，记录章节ID（不自动清空历史）"""
        self.current_chapter_id = chapter_id
        print(f" 记录章节: {chapter_id[:50]}")

    def force_reset(self, chapter_id: str):
        """强制清空所有历史，无论章节是否相同"""
        self.conversation_history = []
        self.current_chapter_id = chapter_id
        self.accumulated_passages = set()
        print(f" 强制重置章节: {chapter_id[:50]}")

    def add_passage_if_new(self, passage: str) -> bool:
        """添加原文（如果是新的），返回是否添加成功"""
        if not passage or len(passage) < 50:
            return False

        passage_hash = hashlib.md5(passage.encode()).hexdigest()[:16]

        if passage_hash in self.accumulated_passages:
            print(f"    原文已存在，跳过")
            return False

        self.accumulated_passages.add(passage_hash)

        passage_msg = {
            "role": "user",
            "content": f"【阅读材料 {len(self.accumulated_passages)}】\n\n{passage}\n\n请理解以上材料，等待后续问题。"
        }
        self.conversation_history.append(passage_msg)
        self.conversation_history.append({
            "role": "assistant",
            "content": f"我已理解材料 {len(self.accumulated_passages)}。请提出问题。"
        })

        print(f"    新增原文（{len(passage)}字符），当前共{len(self.accumulated_passages)}篇")
        return True

    def ask(self, prompt: str, retry_count: int = 3) -> Optional[str]:
        """发送问题并获取回答"""
        print(f"当前ai对话历史共{len(self.conversation_history)}条")
        for attempt in range(retry_count):
            try:
                messages = [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    *self.conversation_history,
                    {"role": "user", "content": prompt}
                ]

                if DEBUG_MODE:
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 请求详情")
                    print(f"   base_url: {self.config.base_url}")
                    print(f"   model:    {self.config.model}")
                    print(f"   temperature: {self.config.temperature}")
                    print(f"   max_tokens:  {self.config.max_tokens}")
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
                    max_tokens=self.config.max_tokens
                )

                answer = response.choices[0].message.content.strip()

                if DEBUG_MODE:
                    print("\n" + "=" * 60)
                    print(" [DEBUG] API 响应详情")
                    print(f"   model:        {response.model}")
                    print(f"   finish_reason:{response.choices[0].finish_reason}")
                    print(f"   usage:        {response.usage}")
                    print(f"   answer:       {answer[:500]}{'...' if len(answer) > 500 else ''}")
                    print("=" * 60 + "\n")

                self.conversation_history.append({"role": "user", "content": prompt})
                self.conversation_history.append({"role": "assistant", "content": answer})

                if len(self.conversation_history) > 22:
                    self.conversation_history = self.conversation_history[:2] + self.conversation_history[-20:]

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
                    time.sleep((2 ** attempt) + random.random())
                error_msg = str(e)
                print(f"AI调用失败: {error_msg[:50]}")  # 控制台只显示简短信息
                logger.error(f"详细错误: {error_msg}", exc_info=True)  # 详细堆栈保存到文件

        return None


class QuestionParserStrategy(ABC):
    """题目解析策略基类"""

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

    def __init__(self, driver, whisper_api_key: Optional[str] = None):
        self.driver = driver
        self.strategies: List[QuestionParserStrategy] = [
            VideoStrategy(),
            DiscussionBoardStrategy(),
            VocabularyFlashcardStrategy(),
            VocabularyTestStrategy(),
            DropdownSelectStrategy(),
            BankedClozeStrategy(),
            ListeningFillInStrategy(),  # 听力填空（转录由AISolver预处理完成）
            StandardChoiceStrategy(),
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


class VocabularyTestStrategy(QuestionParserStrategy):
    """词汇测试题解析策略"""

    def can_parse(self, container, driver) -> bool:
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
    def __init__(self):
        self._material_cache: Optional[str] = None

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
        if question_number == 1:
            self._material_cache = None
        if 'question-common-abs-choice' in (container.get_attribute('class') or ''):
            choice_container = container
        else:
            choices = container.find_elements(By.CSS_SELECTOR, '.question-common-abs-choice')
            choice_container = choices[0] if choices else container

        title_elem = choice_container.find_element(By.CSS_SELECTOR, '.ques-title')
        text = title_elem.text.strip() if title_elem else ""

        vocab_strategy = VocabularyTestStrategy()
        options = vocab_strategy._extract_options(container, driver)

        checkboxes = container.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]')
        is_multi = (
                checkboxes or
                'multipleChoice' in (container.get_attribute('class') or '').lower() or
                '多选' in text or
                len(options) > 4
        )
        q_type = QuestionType.MULTIPLE_CHOICE if is_multi else QuestionType.SINGLE_CHOICE

        return Question(
            number=question_number,
            text=text,
            q_type=q_type,
            element=container,
            options=options,
            directions=directions
        )


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
    """听力填空题解析策略（仅解析题目，转录由预处理完成）"""

    def can_parse(self, container, driver) -> bool:
        has_blanks = bool(container.find_elements(By.CSS_SELECTOR, '.fe-scoop input'))
        has_option_pool = bool(container.find_elements(By.CSS_SELECTOR, '.option-wrapper, .banked-options'))
        if not has_blanks or has_option_pool:
            return False

        try:
            direction = driver.find_element(By.CSS_SELECTOR, '.layout-direction-container, .abs-direction')
            text = direction.text.lower()
            return any(kw in text for kw in ['listen', 'audio', 'hear', 'talk', 'conversation'])
        except:
            return False

    def parse(self, container, driver, question_number: int, directions: str = "") -> Optional[Question]:
        inputs = container.find_elements(By.CSS_SELECTOR, '.fe-scoop input')
        if not inputs:
            return None

        full_text = f"听力填空题（共{len(inputs)}个空）"
        if directions:
            full_text = f"【题目要求】{directions}\n\n{full_text}"

        blank_contexts = []
        for i, inp in enumerate(inputs):
            try:
                scoop = inp.find_element(By.XPATH, './ancestor::span[@class="fe-scoop"]')
                sentence = scoop.find_element(By.XPATH, './ancestor::p').text
            except:
                sentence = ""
            blank_contexts.append({
                'index': i,
                'sentence': sentence,
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

    def __init__(self, kimi_client=None):
        self.kimi = kimi_client

    def build(self, questions: List[Question], global_directions: str = "") -> str:
        lines = []

        if len(self.kimi.accumulated_passages) > 1:
            lines.append(f"【注意】本章节共有 {len(self.kimi.accumulated_passages)} 篇阅读材料，请根据问题判断使用哪篇。")
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
            QuestionType.FILL_IN: self._build_fill_in,
            QuestionType.TEXT: self._build_text,
            QuestionType.VIDEO: lambda q: [],
            QuestionType.VOCABULARY_FLASHCARD: lambda q: [],
            QuestionType.LISTENING_FILL_IN: self._build_listening_fill_in,
        }
        return builders.get(q_type, self._build_unknown)

    def _build_listening_fill_in(self, q: Question) -> List[str]:
        lines = [
            f"{q.number}. 【听力填空题】",
            f"{q.text}",
            "",
            "【答题要求】",
            "1. 这是一个听力理解题，请根据句子上下文和逻辑填写最合适的单词或短语",
            "2. 每个空填写一个简洁的答案（单词或短句）",
            "3. 注意语法正确性和上下文连贯性",
            ""
        ]

        for blank in q.banked_blanks:
            lines.append(f"   空{blank['index'] + 1}: {blank['sentence']}")

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

    def _build_fill_in(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【填空】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 个空）")
        lines.append("")
        return lines

    def _build_text(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【简答题】{q.text}"]
        if len(q.inputs) > 1:
            lines.append(f"   （共 {len(q.inputs)} 小题）")
        lines.append("   （请提供简洁准确的回答，如果不是翻译题，那么只用英文回答）")
        lines.append("")
        return lines

    def _build_unknown(self, q: Question) -> List[str]:
        lines = [f"{q.number}. 【题】{q.text}"]
        for opt in q.options:
            lines.append(f"   {opt.letter}. {opt.text}")
        lines.append("")
        return lines

    def _format_instructions(self, type_counts: Dict[QuestionType, int]) -> List[str]:
        lines = ["-" * 50, "请按以下格式回答："]

        has_single = QuestionType.SINGLE_CHOICE in type_counts or QuestionType.VOCABULARY_TEST in type_counts
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

        if QuestionType.TEXT in type_counts:
            lines.append("简答题: 1.答案内容...")

        lines.append("-" * 50)
        return lines

    def _build_dropdown_select(self, q: Question) -> List[str]:
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

    def __init__(self, driver):
        self.driver = driver

    def execute(self, question: Question, answer: str) -> AnswerResult:
        executors = {
            QuestionType.SINGLE_CHOICE: self._fill_single_choice,
            QuestionType.VOCABULARY_TEST: self._fill_single_choice,
            QuestionType.MULTIPLE_CHOICE: self._fill_multiple_choice,
            QuestionType.BANKED_CLOZE: self._fill_banked_cloze,
            QuestionType.DROPDOWN_SELECT: self._fill_dropdown_select,
            QuestionType.FILL_IN: self._fill_fill_in,
            QuestionType.TEXT: self._fill_text,
            QuestionType.LISTENING_FILL_IN: self._fill_listening_fill_in,
        }

        executor = executors.get(question.q_type, self._fill_unknown)
        return executor(question, answer)

    def _fill_single_choice(self, q: Question, answer: str) -> AnswerResult:
        answer_letter = self._extract_letter(answer)
        if not answer_letter:
            return AnswerResult(False, q.number, answer, "无法解析答案")

        print(f"\t寻找选项: {answer_letter}")
        print(f"\t可用选项: {[opt.letter for opt in q.options]}")

        for opt in q.options:
            if opt.letter.upper() == answer_letter.upper():
                print(f"\t点击选项 {opt.letter}: {opt.text[:30]}...")
                success = WebDriverHelper.safe_click(self.driver, opt.element)
                if success:
                    return AnswerResult(True, q.number, answer_letter, f"选择成功: {opt.text[:30]}")
                else:
                    return AnswerResult(False, q.number, answer, "点击失败")

        try:
            idx = ord(answer_letter.upper()) - ord('A')
            if 0 <= idx < len(q.options):
                opt = q.options[idx]
                print(f"\t通过索引匹配选项 {opt.letter}: {opt.text[:30]}...")
                success = WebDriverHelper.safe_click(self.driver, opt.element)
                if success:
                    return AnswerResult(True, q.number, answer_letter, f"选择成功: {opt.text[:30]}")
        except:
            pass

        return AnswerResult(False, q.number, answer, f"未找到选项 {answer_letter}")

    def _fill_multiple_choice(self, q: Question, answer: str) -> AnswerResult:
        letters = re.findall(r'[A-D]', answer.upper())
        selected = []

        for letter in letters:
            for opt in q.options:
                if opt.letter.upper() == letter and not opt.is_selected:
                    if WebDriverHelper.safe_click(self.driver, opt.element):
                        selected.append(letter)
                    break

        return AnswerResult(
            bool(selected), q.number, ','.join(selected),
            f"选中 {len(selected)}/{len(letters)} 个选项"
        )

    def _fill_banked_cloze(self, q: Question, answer: str) -> AnswerResult:
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

        return AnswerResult(
            success_count > 0, q.number, answer,
            f"填写 {success_count}/{len(q.banked_blanks)} 个空"
        )

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

    def _fill_fill_in(self, q: Question, answer: str) -> AnswerResult:
        answers = self._parse_banked_answer(answer, len(q.inputs))
        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, inp in enumerate(q.inputs):
            ans = answers[i] if i < len(answers) else ""
            if ans:
                print(f"\t空{i + 1}: {ans}")
                WebDriverHelper.simulate_typing(self.driver, inp, ans)
                success_count += 1
            else:
                print(f"\t空{i + 1}: (空)")

        return AnswerResult(
            success_count > 0,
            q.number,
            answer,
            f"填写 {success_count}/{len(q.inputs)} 个空"
        )

    def _extract_answer_by_number(self, answer: str, question_number: int) -> str:
        pattern = rf'{question_number}\s*[.、\)\]]\s*(.+?)(?=\s*\d+\s*[.、\)\]]|$)'
        match = re.search(pattern, answer, re.DOTALL)
        if match:
            return match.group(1).strip()

        lines = [l.strip() for l in answer.split('\n') if l.strip()]
        for line in lines:
            clean = re.sub(r'^\d+\s*[.、)\]]\s*', '', line).strip()
            if clean and not re.match(r'^\d', clean):
                if line.startswith(
                        str(question_number)) or f"{question_number}." in line or f"{question_number})" in line:
                    return clean

        return ""

    def _fill_text(self, q: Question, answer: str) -> AnswerResult:
        if not q.inputs:
            return AnswerResult(False, q.number, answer, "无输入框")

        expected_count = len(q.inputs)

        if expected_count == 1:
            ans = self._extract_answer_by_number(answer, q.number)
            if not ans:
                answers = self._parse_banked_answer(answer, expected_count)
                ans = answers[0] if answers else ""
        else:
            answers = self._parse_banked_answer(answer, expected_count)
            ans = answers[0] if answers else ""

        print(f"\t题{q.number}: {ans[:60]}..." if ans else f"\t题{q.number}: (空)")

        if ans:
            inp = q.inputs[0]
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                inp
            )
            time.sleep(0.2)
            WebDriverHelper.simulate_typing(self.driver, inp, ans)
            return AnswerResult(True, q.number, ans, f"填写题{q.number}成功")

        return AnswerResult(False, q.number, answer, f"题{q.number}无答案")

    def _fill_unknown(self, q: Question, answer: str) -> AnswerResult:
        return AnswerResult(False, q.number, answer, "未知题型，无法填写")

    def submit(self) -> bool:
        priority_selectors = [
            '.submit-bar-pc--btn-1_Xvo',
            'button[type="submit"]',
            'button.submit-btn',
        ]
        for selector in priority_selectors:
            try:
                btn = WebDriverWait(self.driver, 2).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                if btn:
                    return WebDriverHelper.safe_click(self.driver, btn)
            except:
                continue
        btn = WebDriverHelper.safe_find_element(self.driver, Selectors.SUBMIT_BUTTON, timeout=3)
        if btn:
            return WebDriverHelper.safe_click(self.driver, btn)
        return False

    @staticmethod
    def _extract_letter(answer: str) -> Optional[str]:
        match = re.search(r'[A-D]', answer.upper())
        return match.group() if match else None

    @staticmethod
    def _parse_banked_answer(answer: str, expected_count: int) -> List[str]:
        result = [''] * expected_count
        answer = re.sub(r'^(简答题|选词/选择填空|填空题|答案|选词填空|翻译)[：:]\s*', '', answer.strip())
        print(f"    [调试] 清理后答案前200字: {answer[:200]}...")

        matched_any = False

        for i in range(1, expected_count + 1):
            pattern = rf'{i}\s*[.、\)\]]\s*(.*?)(?=\s*[1-9][0-9]*\s*[.、\)\]]|$)'
            match = re.search(pattern, answer, re.DOTALL)

            if match:
                clean_ans = match.group(1).strip().replace('\n', ' ')
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
            clean = re.sub(r'^\d+\s*[.、)\]]\s*', '', line).strip()
            if clean and not re.match(r'^\d+$', clean):
                content_lines.append(clean)

        for i, content in enumerate(content_lines[:expected_count]):
            result[i] = content

        return result

    def _fill_dropdown_select(self, q: Question, answer: str) -> AnswerResult:
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

        return AnswerResult(
            success_count > 0,
            q.number,
            answer,
            f"成功 {success_count}/{len(q.banked_blanks)} 个"
        )

    def _fill_listening_fill_in(self, q: Question, answer: str) -> AnswerResult:
        answers = self._parse_banked_answer(answer, len(q.inputs))

        print(f"\t解析答案: {answers}")
        print(f"\t输入框数量: {len(q.inputs)}")

        success_count = 0
        for i, (blank_info, ans) in enumerate(zip(q.banked_blanks, answers)):
            if ans and blank_info['input']:
                print(f"\t空{i + 1}: {ans}")
                WebDriverHelper.simulate_typing(self.driver, blank_info['input'], ans)
                success_count += 1

        return AnswerResult(
            success_count > 0,
            q.number,
            answer,
            f"填写 {success_count}/{len(q.inputs)} 个空"
        )

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


class VideoHandler(ContentHandler):
    """视频处理器"""

    def __init__(self, driver, config: Config):
        self.driver = driver
        self.config = config
        self.popup_monitor_thread = None
        self.stop_monitoring = threading.Event()

        self.transcriber = AudioTranscriber(
            api_key=config.whisper_api,
            use_local=True
        )

        self.analyzer_client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url
        )

        self.video_transcript = ""
        self.current_video_url = ""

    def _play_video_and_handle_popups(self):
        """播放视频并自动处理弹窗选择题（供外部预处理调用）"""
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

        self.stop_monitoring.clear()
        self.popup_monitor_thread = threading.Thread(
            target=self._monitor_popup_questions,
            daemon=True
        )
        self.popup_monitor_thread.start()

        self._play_video(duration)

        print("      视频播放完成")
        self.stop_monitoring.set()
        if self.popup_monitor_thread.is_alive():
            self.popup_monitor_thread.join(timeout=5)

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VIDEO

    def handle(self, question: Question) -> bool:
        if self._check_video_completed():
            print("     视频已标记为完成，跳过")
            return True

        print("     视频页面，开始处理...")

        video_info = self._get_video_info()
        if not video_info:
            print("       未找到视频元素")
            return True

        video_url = video_info.get('url', '')
        duration = video_info.get('duration', 0)

        if video_url and video_url == self.current_video_url and self.video_transcript:
            print(f"     使用已缓存的视频转录（{len(self.video_transcript)}字符）")
        else:
            self.current_video_url = video_url
            self.video_transcript = self._transcribe_video(video_url, duration)

        self.stop_monitoring.clear()
        self.popup_monitor_thread = threading.Thread(
            target=self._monitor_popup_questions,
            daemon=True
        )
        self.popup_monitor_thread.start()

        self._play_video(duration)

        print("     视频处理完成")
        self.stop_monitoring.set()

        if self.popup_monitor_thread.is_alive():
            self.popup_monitor_thread.join(timeout=5)

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
            if duration > 120:
                transcript = self.transcriber.transcribe_long_audio(
                    video_url,
                    language="en",
                    chunk_length=30
                )
            else:
                transcript = self.transcriber.transcribe(
                    video_url,
                    language="en"
                )

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
                time.sleep(10)

        except Exception as e:
            print(f"       视频播放失败: {str(e)[:50]}")

    def _monitor_popup_questions(self):
        print("      [监视器] 开始监视弹窗...")
        check_interval = 0.5
        processed_popups = set()

        while not self.stop_monitoring.is_set():
            try:
                popup = self._find_popup_question()

                if popup and popup.is_displayed():
                    popup_id = self._get_popup_id(popup)

                    if popup_id in processed_popups:
                        time.sleep(0.5)
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

                    success = self._click_option(popup, answer)

                    if success:
                        print(f"      [监视器]  已选择: {answer}")
                        processed_popups.add(popup_id)
                        time.sleep(0.5)
                        self._click_submit_if_exists(popup)
                        time.sleep(1.0)
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
        import random
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
                if self.stop_monitoring.is_set():
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

                time.sleep(0.5)

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

    def __init__(self, driver):
        self.driver = driver

    def can_handle(self, question: Question) -> bool:
        return question.q_type == QuestionType.VOCABULARY_FLASHCARD

    def handle(self, question: Question) -> bool:
        print("     处理单词闪卡...")
        max_cards = 100
        clicked = 0
        time.sleep(2)
        for i in range(max_cards):
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
                time.sleep(0.5)
                try:
                    next_btn.click()
                except:
                    self.driver.execute_script("arguments[0].click();", next_btn)
                clicked += 1
                time.sleep(0.5)
                current_word = self.driver.find_element(By.XPATH,
                                                        '//*[@id="question-vocabulary-base-id"]/div/div[2]/div')
                print(f" 学习{current_word.text}")
            except Exception as e:
                error_msg = str(e)
                print(f"      处理闪卡失败: {error_msg[:50]}")
                logger.error(f"详细错误: {error_msg}", exc_info=True)
                time.sleep(1)
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
        self.kimi = KimiClient(self.config)
        self.parser = QuestionParser(driver, self.config.whisper_api)
        self.prompt_builder = PromptBuilder(self.kimi)
        self.executor = AnswerExecutor(driver)
        self.content_handlers: List[ContentHandler] = [
            VideoHandler(driver, self.config),
            FlashcardHandler(driver),
            DiscussionBoardHandler(driver),
        ]
        self.processed_hashes: set = set()
        self._processed_video_tabs: set = set()
        self._processed_audio_tabs: set = set()

    def solve_current_chapter(self, chapter_name: str) -> bool:
        print(f"\n{'=' * 60}")
        print(f" 开始处理章节: {chapter_name}")
        print(f"{'=' * 60}")

        self.kimi.start_new_chapter(chapter_name)

        level1_tabs = self._get_level1_tabs()

        for l1_idx, l1_tab in enumerate(level1_tabs):
            print(f" 一级Tab [{l1_idx}]: {l1_tab['title']}")
            if l1_idx > 0:
                self.kimi.force_reset(f"{chapter_name}_{l1_tab['title']}")
                print(f"    切换一级Tab，已清空AI对话历史")
            if not WebDriverHelper.safe_click(self.driver, l1_tab['element']):
                continue
            time.sleep(1.5)

            level2_tabs = self._get_level2_tabs()

            if not level2_tabs:
                self._process_tab_with_accumulation(f"{l1_tab['title']}", l1_idx, 0)
            else:
                for l2_idx, l2_tab in enumerate(level2_tabs):
                    print(f"\n   二级Tab [{l2_idx}]: {l2_tab['title']}")

                    if not WebDriverHelper.safe_click(self.driver, l2_tab['element']):
                        continue
                    time.sleep(1.5)

                    tab_name = f"{l1_tab['title']}_{l2_tab['title']}"
                    self._process_tab_with_accumulation(tab_name, l1_idx, l2_idx)

                    level2_tabs = self._get_level2_tabs()
                    if l2_idx < len(level2_tabs):
                        l2_tab['element'] = level2_tabs[l2_idx]['element']

        print(f"\n{'=' * 60}")
        print(f" 章节 {chapter_name} 处理完成")
        print(f"{'=' * 60}")
        return True

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

        self.kimi.start_new_chapter(chapter_name)
        self.processed_hashes.clear()

        course_home_url = self.driver.current_url

        for task_idx, tab in enumerate(selected_tabs):
            tab_name = tab.get('l1_title', 'unknown')

            if '_element' in tab and tab['_element'] is not None:
                print(f"\n  [{task_idx+1}/{len(selected_tabs)}] {tab['display']}")

                if task_idx > 0:
                    self.driver.get(course_home_url)
                    time.sleep(3)
                    self.kimi.force_reset(f"{chapter_name}_{tab_name}")

                if '_unit_idx' in tab:
                    try:
                        unit_container = WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located(
                                (By.CLASS_NAME, 'unipus-tabs_unitTabScrollContainer__fXBxR'))
                        )
                        unit_tabs = unit_container.find_elements(By.CSS_SELECTOR, ':scope > *')
                        if tab['_unit_idx'] < len(unit_tabs):
                            self.driver.execute_script("arguments[0].click();", unit_tabs[tab['_unit_idx']])
                            time.sleep(1.2)
                    except Exception as e:
                        print(f"    切换Unit失败: {str(e)[:50]}")

                chapter_clicked = False
                try:
                    chapters = self.driver.find_elements(
                        By.CLASS_NAME, 'courses-unit_taskItemInnerLayout__DTYuN'
                    )
                    for ch in chapters:
                        try:
                            name_elem = ch.find_element(By.CLASS_NAME, 'courses-unit_taskTypeName__99BXj')
                            if name_elem.text.strip() == tab_name:
                                self.driver.execute_script("arguments[0].click();", name_elem)
                                chapter_clicked = True
                                break
                        except:
                            continue
                except Exception as e:
                    print(f"    重新定位章节失败: {str(e)[:50]}")

                if chapter_clicked:
                    time.sleep(3)
                    self._process_tab_with_accumulation(tab_name, task_idx, 0)
                    time.sleep(2)
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

                if not WebDriverHelper.safe_click(self.driver, l1_tab['element']):
                    print(f"  点击一级Tab失败，跳过")
                    continue
                time.sleep(1.5)

                if l2_idx < 0:
                    self._process_tab_with_accumulation(l1_tab['title'], l1_idx, 0)
                    time.sleep(2)
                else:
                    level2_tabs = self._get_level2_tabs()
                    if l2_idx >= len(level2_tabs):
                        print(f"  二级Tab索引 {l2_idx} 越界，跳过")
                        continue

                    l2_tab = level2_tabs[l2_idx]
                    print(f"    二级Tab: {l2_tab['title']}")

                    if not WebDriverHelper.safe_click(self.driver, l2_tab['element']):
                        print(f"  点击二级Tab失败，跳过")
                        continue
                    time.sleep(1.5)

                    combined_name = f"{l1_tab['title']}_{l2_tab['title']}"
                    self._process_tab_with_accumulation(combined_name, l1_idx, l2_idx)
                    time.sleep(2)

        print(f"\n{'=' * 60}")
        print(f"全部 {len(selected_tabs)} 个任务处理完毕")
        print(f"{'=' * 60}")

    def _process_tab_with_accumulation(self, tab_name: str, l1_idx: int, l2_idx: int) -> bool:
        """处理Tab - 累积原文模式，包含视频/音频预处理"""

        self._preprocess_video_if_needed(tab_name, l1_idx, l2_idx)
        self._preprocess_audio_if_needed(tab_name, l1_idx, l2_idx)

        current_passage = self._extract_passage()
        if current_passage:
            self.kimi.add_passage_if_new(current_passage)

        return self._process_current_tab_content(
            self.kimi.current_chapter_id or "unknown", tab_name, l1_idx, l2_idx
        )

    def _process_current_tab_content(self, chapter_name: str, tab_name: str, l1_idx: int, l2_idx: int) -> bool:
        direction_part = self._generate_content_hash_from_direction()
        if direction_part == "empty":
            direction_part = "no_direction"

        content_hash = f"{chapter_name}|{tab_name}|{l1_idx}|{l2_idx}|{direction_part}"

        print(f"    内容标识: {hashlib.md5(content_hash.encode()).hexdigest()[:16]}...")

        if content_hash in self.processed_hashes:
            print(f"   ⏭ 已处理过，跳过")
            return False

        self.processed_hashes.add(content_hash)

        page_num = 1
        total_answered = 0
        last_questions_signature = ""

        while True:
            questions, directions = self.parser.parse_all()
            print(f"\n    处理第 {page_num} 页题目...")
            print(f"    找到 {len(questions)} 个可见题目")

            current_signature = self._generate_questions_signature(questions)

            if current_signature == last_questions_signature and page_num > 1:
                print(f"    题目内容与上次相同，可能已到达最后一页")
                break

            last_questions_signature = current_signature

            special_handled = False
            for q in questions:
                for handler in self.content_handlers:
                    if handler.can_handle(q):
                        print(f"     使用 {handler.__class__.__name__} 处理")
                        handler.handle(q)
                        special_handled = True
                        if q.q_type in [QuestionType.VOCABULARY_FLASHCARD, QuestionType.VIDEO]:
                            print(f"    特殊内容处理完成")
                            return True
                        break

            normal_questions = [q for q in questions if q.q_type not in [
                QuestionType.VOCABULARY_FLASHCARD,
                QuestionType.VIDEO,
                QuestionType.DISCUSSION_BOARD
            ]]

            if normal_questions:
                print(f"    共 {len(normal_questions)} 道题目需要回答")
                prompt = self.prompt_builder.build(normal_questions, directions)
                ai_response = self.kimi.ask(prompt)

                if ai_response:
                    success_count = 0

                    for q in normal_questions:
                        if q.q_type in [QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
                                        QuestionType.VOCABULARY_TEST]:
                            ans = self._extract_single_answer(ai_response, q.number)
                        elif q.q_type in [QuestionType.BANKED_CLOZE, QuestionType.FILL_IN,
                                          QuestionType.DROPDOWN_SELECT]:
                            ans = ai_response
                        else:
                            ans = ai_response

                        if ans:
                            result = self.executor.execute(q, ans)
                            if result.success:
                                success_count += 1
                        else:
                            print(f"    题目 {q.number} 无答案")

                    total_answered += success_count
                    print(f"    本页成功填写 {success_count}/{len(normal_questions)} 题")

            if normal_questions and self.executor.submit():
                self._wait_for_submit_complete()
                self._handle_confirm_dialog()

            next_btn = self._find_next_question_button()
            if not next_btn:
                print(f"    没有更多题目了")
                break

            print(f"    点击下一题...")
            pre_click_signature = current_signature

            if not WebDriverHelper.safe_click(self.driver, next_btn):
                print(f"    点击下一题失败")
                break

            if not self._wait_for_content_change(pre_click_signature, timeout=5):
                print(f"    内容未变化，可能已到最后一页")
                break

            page_num += 1

            if page_num > 50:
                print(f"    达到最大页数限制，停止")
                break
        print(f"    总共回答 {total_answered} 题")
        return True

    def _extract_single_answer(self, ai_response: str, question_number: int) -> str:
        pattern = rf'{question_number}\s*[.、\)\]]\s*([A-D]+)'
        match = re.search(pattern, ai_response, re.IGNORECASE)
        if match:
            return match.group(1).upper()

        lines = [l.strip() for l in ai_response.split('\n') if l.strip()]
        if question_number <= len(lines):
            line = lines[question_number - 1]
            letters = re.findall(r'[A-D]', line.upper())
            return ''.join(letters) if letters else line

        return ""

    def _generate_questions_signature(self, questions: List[Question]) -> str:
        if not questions:
            return "empty"

        parts = []
        for q in questions:
            q_type = q.q_type.name if q.q_type else "UNKNOWN"
            text_preview = q.text[:30] if q.text else ""
            parts.append(f"{q.number}:{q_type}:{text_preview}")

        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

    def solve_current_page(self, chapter_name: str = "unknown") -> bool:
        print("\n" + "=" * 60)
        print(" 开始处理当前停留的页面")
        print("=" * 60)

        state_key = self._generate_content_hash()

        if not state_key or state_key == "empty":
            state_key = f"{chapter_name}_{int(time.time())}"

        print(f"    内容标识: {state_key[:50]}...")

        if state_key in self.processed_hashes:
            print(f"   ⏭ 该页面的哈希已被记录，正在执行作答...")

        self.processed_hashes.add(state_key)

        success = self._process_current_content(chapter_name)

        print(f"\n{'=' * 60}")
        print(" 当前页面处理完毕")
        print(f"{'=' * 60}")
        return success

    def solve(self) -> bool:
        """完整答题流程（扫描所有Tab）- 半自动模式下已弃用"""
        pass

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
            all_btns = self.driver.find_elements(By.TAG_NAME, 'button')
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
        try:
            direction_elem = WebDriverHelper.safe_find_element(
                self.driver,
                ['.abs-direction', '.layout-direction-container', '.direction-container']
            )
            if direction_elem:
                text = direction_elem.text.strip()
                if text:
                    import hashlib
                    return hashlib.md5(text.encode()).hexdigest()[:16]
        except:
            pass
        return "empty"

    def _generate_content_hash(self) -> str:
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

    def _process_tab_content(self, l1_title: str, l2_title: str, tab_indices: Tuple[int, int]):
        state_key = self._generate_content_hash()

        if not state_key or state_key == "empty":
            state_key = f"{l1_title}_{l2_title}_{tab_indices[0]}_{tab_indices[1]}"

        print(f"    内容标识: {state_key[:50]}...")

        if state_key in self.processed_hashes:
            print(f"   ⏭ 已处理过，跳过")
            return

        self.processed_hashes.add(state_key)

        chapter_name = f"{l1_title}_{l2_title}" if l2_title != "default" else l1_title
        self._process_current_content(chapter_name)

    def _process_current_content(self, chapter_name: str) -> bool:
        print(f"    正在分析页面结构...")

        questions, directions = self.parser.parse_all()
        print(f"    找到 {len(questions)} 个题目")

        normal_questions = []
        for q in questions:
            handled = False
            for handler in self.content_handlers:
                if handler.can_handle(q):
                    handler.handle(q)
                    handled = True
                    break
            if not handled:
                normal_questions.append(q)

        if not normal_questions:
            print("    ℹ 当前页面未检测到需要AI作答的常规题目")
            return False

        print(f"     共 {len(normal_questions)} 道题目需要回答")

        prompt = self.prompt_builder.build(normal_questions, directions)
        ai_response = self.kimi.ask(prompt)

        if not ai_response:
            print("     AI未返回答案")
            return False

        success_count = 0

        for q in normal_questions:
            if q.q_type in [QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE,
                            QuestionType.VOCABULARY_TEST]:
                ans = self._extract_single_answer(ai_response, q.number)
            elif q.q_type in [QuestionType.BANKED_CLOZE, QuestionType.FILL_IN,
                              QuestionType.DROPDOWN_SELECT, QuestionType.LISTENING_FILL_IN]:
                ans = ai_response
            else:
                ans = ai_response

            if ans:
                result = self.executor.execute(q, ans)
                if result.success:
                    success_count += 1
            else:
                print(f"    题目 {q.number} 无答案")

        print(f"     成功填写 {success_count}/{len(normal_questions)} 题")

        if self.executor.submit():
            self._wait_for_submit_complete()
            self._handle_confirm_dialog()

        return success_count > 0

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
                    text = elem.text.strip()
                    if len(text) > 200:
                        return text
            except:
                continue

        return ""

    def _parse_ai_response(self, response: str, expected_count: int, q_type: QuestionType = None) -> List[str]:
        answers = []
        response = response.strip()

        pattern1 = r'(\d+)\s*[.、\)\]]\s*([A-Za-z]+)(?=\s*\d+\s*[.、\)\]]|$)'
        matches = re.findall(pattern1, response, re.DOTALL | re.IGNORECASE)

        if matches and len(matches) >= expected_count:
            match_dict = {}
            for num, content in matches:
                idx = int(num) - 1
                clean = content.upper().strip()
                match_dict[idx] = clean

            for i in range(expected_count):
                answers.append(match_dict.get(i, ''))
            return answers

        words = re.findall(r'\b(True|False|Not\s*given|Not\s*mentioned|[A-D])\b',
                           response, re.IGNORECASE)

        if len(words) >= expected_count:
            return [w.upper() for w in words[:expected_count]]

        lines = [line.strip() for line in response.split('\n') if line.strip()]
        for line in lines[:expected_count]:
            match = re.search(r'\b(True|False|Not\s*given|[A-D])\b', line, re.IGNORECASE)
            answers.append(match.group(1).upper() if match else '')

        while len(answers) < expected_count:
            answers.append('')

        return answers[:expected_count]

    def _handle_confirm_dialog(self):
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, 'button')
            for btn in buttons:
                text = btn.text.strip()
                if any(k in text for k in ['确认', '确定', '我知道了', '继续', 'OK']):
                    if btn.is_displayed():
                        btn.click()
                        time.sleep(1)
                        return True
        except:
            pass
        return False


    def _preprocess_audio_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int):
        """检测并预处理音频：下载+转录，将转录文本注入 AI 上下文"""
        if (tab_name, l1_idx, l2_idx) in self._processed_audio_tabs:
            return

        if not self._has_audio_on_page():
            return

        print("   检测到音频，开始预处理（下载+转录）...")
        try:
            audio_url = self._extract_audio_url_from_page()
            if not audio_url:
                print("   未找到有效音频URL，跳过")
                return

            transcriber = AudioTranscriber(use_local=True)
            duration = self._get_audio_duration()
            if duration > 120:
                transcript = transcriber.transcribe_long_audio(audio_url, language="en")
            else:
                transcript = transcriber.transcribe(audio_url, language="en")

            if transcript:
                self.kimi.add_passage_if_new(transcript)
                print(f"   已将音频转录（{len(transcript)}字符）加入上下文")
            else:
                print("   音频转录为空")

            self._processed_audio_tabs.add((tab_name, l1_idx, l2_idx))

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

    def _get_audio_duration(self) -> float:
        """获取音频时长（秒）"""
        try:
            audio = self.driver.find_element(By.TAG_NAME, 'audio')
            duration = self.driver.execute_script("return arguments[0].duration;", audio)
            return float(duration) if duration else 0
        except:
            return 0

    def _preprocess_video_if_needed(self, tab_name: str, l1_idx: int, l2_idx: int):
        """检测并预处理视频：播放、转录、处理弹窗，将转录文本注入 AI 上下文"""
        if (tab_name, l1_idx, l2_idx) in self._processed_video_tabs:
            return

        if not self._has_video_on_page():
            return

        print("   检测到视频，开始预处理（播放+转录）...")
        try:
            video_handler = VideoHandler(self.driver, self.config)
            video_handler._play_video_and_handle_popups()

            transcript = video_handler.video_transcript
            if transcript:
                self.kimi.add_passage_if_new(transcript)
                print(f"   已将视频转录（{len(transcript)}字符）加入上下文")
            else:
                print("   未获得视频转录，后续题目可能缺乏上下文")

            self._processed_video_tabs.add((tab_name, l1_idx, l2_idx))

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
        time.sleep(1.5)  # 最小等待：服务端至少需要 1-2 秒处理
        start = time.time()

        while time.time() - start < timeout:
            try:
                submit_btn = WebDriverHelper.safe_find_element(
                    self.driver,
                    ['.submit-bar-pc--btn-1_Xvo', 'button.submit-btn', 'button[type="submit"]'],
                    timeout=1
                )
                if submit_btn is None:
                    print(f"   提交按钮已消失，提交完成")
                    return

                body_text = self.driver.find_element(By.TAG_NAME, 'body').text[:500].lower()
                if any(kw in body_text for kw in ['提交成功', '保存成功', 'success', '已提交', 'submitted']):
                    print(f"   检测到成功提示")
                    return

            except:
                pass

            time.sleep(0.5)

        print(f"   等待超时（{timeout}s），继续执行")

    def _wait_for_content_change(self, previous_signature: str, timeout: int = 10) -> bool:
        start_time = time.time()
        check_interval = 0.5

        while time.time() - start_time < timeout:
            try:
                questions, _ = self.parser.parse_all()
                current_signature = self._generate_questions_signature(questions)

                if current_signature != previous_signature and current_signature != "empty":
                    print(f"         内容已变化: {previous_signature[:8]}... -> {current_signature[:8]}...")
                    return True

            except Exception as e:
                logger.debug(f"等待内容变化时出错: {e}")

            time.sleep(check_interval)

        print(f"         等待内容变化超时")
        return False


class ModernGUI:
    """基于 CustomTkinter 的 Cursor 主题 Dashboard 仪表盘布局，支持深/浅色模式无缝切换"""

    def __init__(self, driver, solver, bot):
        self.driver = driver
        self.solver = solver
        self.bot = bot

        ctk.set_appearance_mode("dark")

        self.root = ctk.CTk()
        self.root.title("UnipusAI Helper v3.4")
        self.root.geometry("1150x760")
        self.root.minsize(950, 620)

        self.root.configure(fg_color="#0d1117")

        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self.top_bar = ctk.CTkFrame(self.root, height=60, corner_radius=0, fg_color="#161b22")
        self.top_bar.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        self.top_bar.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(self.top_bar, text="UnipusAI Helper", font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"), text_color="#e6edf3").grid(row=0, column=0, padx=(20,6), pady=10, sticky="w")
        ctk.CTkLabel(self.top_bar, text="v3.4", font=ctk.CTkFont(family="Consolas", size=12), text_color="#484f58").grid(row=0, column=1, padx=0, pady=10, sticky="w")

        info_frame = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        info_frame.grid(row=0, column=2, padx=16, sticky="e")
        ctk.CTkLabel(info_frame, text=f"账号：{self.bot.config.username[:12]}", font=ctk.CTkFont(size=13), text_color="#8b949e").pack(side="left", padx=4)
        ctk.CTkLabel(info_frame, text=f"当前AI模型：{self.bot.config.model}", font=ctk.CTkFont(size=13), text_color="#58a6ff").pack(side="left", padx=4)

        self.debug_var = ctk.BooleanVar(value=DEBUG_MODE)
        self.debug_switch = ctk.CTkSwitch(self.top_bar, text="调试", command=self._on_debug_toggle, variable=self.debug_var, onvalue=True, offvalue=False, font=ctk.CTkFont(size=13), progress_color="#58a6ff", button_color="#30363d", text_color="#8b949e")
        self.debug_switch.grid(row=0, column=3, padx=8, pady=12, sticky="e")

        self.btn_quit = ctk.CTkButton(self.top_bar, text="退出", font=ctk.CTkFont(size=13), fg_color="#21262d", text_color="#f85149", hover_color="#30363d", width=60, height=32, corner_radius=6, command=self.on_quit_clicked)
        self.btn_quit.grid(row=0, column=4, padx=(8,20), pady=14, sticky="e")

        self.main_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        self.main_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=(4, 16))
        self.root.grid_rowconfigure(1, weight=1)
        self.main_frame.grid_rowconfigure(2, weight=1)
        self.main_frame.grid_columnconfigure(0, weight=1)

        self.action_bar = ctk.CTkFrame(self.main_frame, fg_color="#161b22", corner_radius=8, height=64)
        self.action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.action_bar.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(self.action_bar, text=" 初始化中...", font=ctk.CTkFont(size=14), text_color="#8b949e")
        self.status_label.grid(row=0, column=0, padx=16, pady=14, sticky="w")

        self.progress_bar = ctk.CTkProgressBar(self.action_bar, mode="indeterminate", width=140, fg_color="#21262d", progress_color="#58a6ff")
        self.progress_bar.grid(row=0, column=1, padx=8, pady=12, sticky="e")
        self.progress_bar.grid_remove()

        self.btn_scan = ctk.CTkButton(self.action_bar, text="系统未就绪", font=ctk.CTkFont(size=14), fg_color="#21262d", text_color="#8b949e", hover_color="#30363d", corner_radius=6, height=44, state="disabled", command=self._on_scan_clicked)
        self.btn_scan.grid(row=0, column=2, padx=4, pady=10, sticky="e")

        self.btn_quick = ctk.CTkButton(self.action_bar, text="系统未就绪", font=ctk.CTkFont(size=14), fg_color="#21262d", text_color="#8b949e", hover_color="#30363d", corner_radius=6, height=44, state="disabled", command=self._on_quick_clicked)
        self.btn_quick.grid(row=0, column=3, padx=4, pady=10, sticky="e")

        self.btn_auto = ctk.CTkButton(self.action_bar, text="开始处理", font=ctk.CTkFont(size=14, weight="bold"), fg_color="#238636", text_color="#ffffff", hover_color="#2ea043", corner_radius=6, height=44, state="disabled", command=self._on_auto_clicked)
        self.btn_auto.grid(row=0, column=4, padx=(4,12), pady=10, sticky="e")

        self.task_header = ctk.CTkFrame(self.main_frame, fg_color="transparent", height=40)
        self.task_header.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.task_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.task_header, text="任务清单", font=ctk.CTkFont(size=16, weight="bold"), text_color="#e6edf3").grid(row=0, column=0, padx=4, sticky="w")

        self.task_search = ctk.CTkEntry(self.task_header, placeholder_text="搜索任务...", font=ctk.CTkFont(size=14), fg_color="#0d1117", text_color="#e6edf3", border_color="#30363d", corner_radius=6, height=34, width=220)
        self.task_search.grid(row=0, column=1, padx=8, sticky="e")
        self.task_search.bind("<KeyRelease>", self._on_search_key)

        ctk.CTkButton(self.task_header, text="全选", font=ctk.CTkFont(size=13), fg_color="#21262d", text_color="#c9d1d9", hover_color="#30363d", corner_radius=6, height=30, width=56, command=self._on_select_all).grid(row=0, column=2, padx=2, sticky="e")
        ctk.CTkButton(self.task_header, text="必修", font=ctk.CTkFont(size=13), fg_color="#21262d", text_color="#c9d1d9", hover_color="#30363d", corner_radius=6, height=30, width=56, command=self._on_select_compulsory).grid(row=0, column=3, padx=2, sticky="e")
        ctk.CTkButton(self.task_header, text="取消", font=ctk.CTkFont(size=13), fg_color="#21262d", text_color="#c9d1d9", hover_color="#30363d", corner_radius=6, height=30, width=56, command=self._on_deselect_all).grid(row=0, column=4, padx=2, sticky="e")
        self.btn_select_visible = ctk.CTkButton(self.task_header, text="全选当前", font=ctk.CTkFont(size=13), fg_color="#1f6feb", text_color="#ffffff", hover_color="#388bfd", corner_radius=6, height=30, width=80, command=self._on_select_visible)
        self.btn_select_visible.grid(row=0, column=5, padx=2, sticky="e")
        self.btn_select_visible.grid_remove()

        self.task_list_card = ctk.CTkFrame(self.main_frame, fg_color="#161b22", corner_radius=8)
        self.task_list_card.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        self.task_list_card.grid_rowconfigure(0, weight=1)
        self.task_list_card.grid_columnconfigure(0, weight=1)

        self._tab_list_frame = ctk.CTkScrollableFrame(self.task_list_card, fg_color="transparent", corner_radius=0)
        self._tab_list_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        self.log_header = ctk.CTkFrame(self.main_frame, fg_color="transparent", height=36)
        self.log_header.grid(row=3, column=0, sticky="ew", pady=(0, 0))
        self.log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.log_header, text="终端日志", font=ctk.CTkFont(size=16, weight="bold"), text_color="#e6edf3").grid(row=0, column=0, padx=4, sticky="w")

        self.log_visible = True
        self.log_toggle_btn = ctk.CTkButton(self.log_header, text="折叠日志", font=ctk.CTkFont(size=14), fg_color="transparent", text_color="#8b949e", hover_color="#21262d", height=28, width=90, command=self._on_toggle_log)
        self.log_toggle_btn.grid(row=0, column=1, padx=8, pady=4, sticky="e")

        self.log_card = ctk.CTkFrame(self.main_frame, fg_color="#0d1117", corner_radius=8)
        self.log_card.grid(row=4, column=0, sticky="nsew")
        self.log_card.grid_rowconfigure(0, weight=1)
        self.log_card.grid_columnconfigure(0, weight=1)

        self.log_area = ctk.CTkTextbox(self.log_card, fg_color="#0d1117", text_color="#c9d1d9", font=ctk.CTkFont(family="Consolas", size=12), corner_radius=0, wrap="word", state="disabled", border_width=0)
        self.log_area.grid(row=0, column=0, sticky="nsew", padx=12, pady=8)

        self._all_tabs = []
        self._tab_check_vars = []
        self._tab_checkboxes = []
        self._tab_list_built = False
        self._auto_running = False
        self._quick_running = False
        self._current_filter = ""
        self._task_groups = {}  # unit_name -> list of tab indices

        self.root.protocol("WM_DELETE_WINDOW", self.on_quit_clicked)
        self.root.after(100, self._poll_logs)

    def enable_scan_button(self):
        """登录成功后激活所有按钮"""
        self.btn_scan.configure(
            state="normal", text="扫描任务列表",
            fg_color="#238636", text_color="#ffffff", hover_color="#2ea043", corner_radius=6
        )
        self.btn_quick.configure(
            state="normal", text="快速处理当前页",
            fg_color="#21262d", text_color="#c9d1d9", hover_color="#30363d", corner_radius=6
        )
        self.status_label.configure(text="就绪 - 点击[扫描任务列表]或[快速处理当前页]")
        gui_log_queue.put("\n" + "=" * 60)
        gui_log_queue.put("系统就绪")
        gui_log_queue.put("模式1: 点击[扫描任务列表] -> 勾选 -> 自动处理")
        gui_log_queue.put("模式2: 手动翻到题目页 -> 点击[快速处理当前页]")
        try:
            winsound.MessageBeep()
        except:
            pass

    def _on_scan_clicked(self):
        """扫描Tab按钮：列出当前页面所有Tab供选择"""
        if self.btn_scan.cget("state") == "disabled":
            return
        self.btn_scan.configure(state="disabled", text="⏳ 扫描中...")
        self.status_label.configure(text="扫描中...", text_color=("#f54e00", "#f54e00"))
        self.progress_bar.grid()
        self.progress_bar.start()
        threading.Thread(target=self._scan_tabs_thread, daemon=True).start()

    def _scan_tabs_thread(self):
        """后台线程：扫描Tab — 自动检测页面层级（课程目录 / 章节内部）"""
        try:
            tabs = []
            course_tabs = self._scan_course_directory()
            if course_tabs:
                tabs = course_tabs
                gui_log_queue.put(f"课程目录页发现 {len(tabs)} 个任务")
            else:
                level1 = self.solver._get_level1_tabs()
                if level1:
                    for l1_idx, l1_tab in enumerate(level1):
                        if not WebDriverHelper.safe_click(self.driver, l1_tab['element']):
                            continue
                        time.sleep(1.2)
                        level2 = self.solver._get_level2_tabs()
                        if not level2:
                            tabs.append({'l1_idx': l1_idx, 'l2_idx': -1, 'l1_title': l1_tab['title'], 'l2_title': '', 'display': l1_tab['title'], 'is_l2': False, 'is_compulsory': True})
                        else:
                            for l2_idx, l2_tab in enumerate(level2):
                                tabs.append({'l1_idx': l1_idx, 'l2_idx': l2_idx, 'l1_title': l1_tab['title'], 'l2_title': l2_tab['title'], 'display': f"  └ {l2_tab['title']}", 'is_l2': True, 'is_compulsory': True})
                    gui_log_queue.put(f"章节内部页发现 {len(tabs)} 个任务")
            self._all_tabs = tabs
            self.root.after(0, self._build_tab_list_ui)
        except Exception as e:
            gui_log_queue.put(f"扫描失败: {str(e)[:80]}")
            logger.error(f"扫描异常: {e}", exc_info=True)
            self.root.after(0, self._reset_scan_button)

    def _scan_course_directory(self):
        """扫描课程目录页（Unit 列表视图）"""
        tabs = []
        try:
            unit_container = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.CLASS_NAME, 'unipus-tabs_unitTabScrollContainer__fXBxR')))
            unit_tabs = unit_container.find_elements(By.CSS_SELECTOR, ':scope > *')
            gui_log_queue.put(f"课程目录页，找到 {len(unit_tabs)} 个Unit")
            for unit_idx, unit_tab in enumerate(unit_tabs):
                try:
                    self.driver.execute_script("arguments[0].click();", unit_tab)
                except:
                    unit_tab.click()
                time.sleep(0.8)
                chapters = self.driver.find_elements(By.CLASS_NAME, 'courses-unit_taskItemInnerLayout__DTYuN')
                for chapter in chapters:
                    try:
                        name_elem = chapter.find_element(By.CLASS_NAME, 'courses-unit_taskTypeName__99BXj')
                        name = name_elem.text.strip()
                        if not name:
                            continue
                        try:
                            chapter.find_element(By.CLASS_NAME, 'courses-unit_taskRequireIcon__zZldK')
                            is_compulsory = True
                        except NoSuchElementException:
                            is_compulsory = False
                        prefix = "[必修]" if is_compulsory else "[选修]"
                        tabs.append({'l1_idx': len(tabs), 'l2_idx': -1, 'l1_title': name, 'l2_title': '', 'display': f"{prefix} Unit{unit_idx+1} - {name}", 'is_l2': False, 'is_compulsory': is_compulsory, '_element': name_elem, '_unit_idx': unit_idx})
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"课程目录扫描未命中: {e}")
        return tabs

    def _build_tab_list_ui(self):
        """构建任务勾选列表 - 按 Unit 分组，可折叠"""
        self.progress_bar.grid_remove()
        if not self._all_tabs:
            gui_log_queue.put("未扫描到任何Tab")
            self._reset_scan_button()
            return
        for w in self._tab_list_frame.winfo_children():
            w.destroy()
        self._tab_check_vars = []
        self._tab_checkboxes = []
        self._task_groups = {}

        compulsory_count = sum(1 for t in self._all_tabs if t.get('is_compulsory', False))
        gui_log_queue.put(f"必修: {compulsory_count}, 选修: {len(self._all_tabs) - compulsory_count}")

        groups = {}
        for i, tab in enumerate(self._all_tabs):
            unit_key = tab.get('display', '').split('Unit')[1].split(' - ')[0].strip() if 'Unit' in tab.get('display', '') else '其他'
            groups.setdefault(unit_key, []).append(i)

        row = 0
        for unit_name, indices in groups.items():
            unit_tabs = [self._all_tabs[i] for i in indices]
            comp = sum(1 for t in unit_tabs if t.get('is_compulsory', False))
            header_text = f"Unit {unit_name} ({len(indices)}个任务, {comp}必修)"
            header_btn = ctk.CTkButton(self._tab_list_frame, text=header_text, font=ctk.CTkFont(size=12, weight="bold"), fg_color="#21262d", text_color="#e6edf3", hover_color="#30363d", corner_radius=4, height=28, anchor="w")
            header_btn.grid(row=row, column=0, padx=0, pady=(4,2), sticky="ew")
            row += 1

            for idx in indices:
                tab = self._all_tabs[idx]
                var = ctk.BooleanVar(value=tab.get('is_compulsory', False))
                self._tab_check_vars.append(var)
                txt = tab['display']
                tc = "#e6edf3" if tab.get('is_compulsory', False) else "#8b949e"
                cb = ctk.CTkCheckBox(self._tab_list_frame, text=txt, variable=var, font=ctk.CTkFont(size=12), text_color=tc, fg_color="#58a6ff", hover_color="#1f6feb", checkbox_width=18, checkbox_height=18, corner_radius=4)
                cb.grid(row=row, column=0, padx=8, pady=1, sticky="w")
                self._tab_checkboxes.append(cb)
                row += 1

            row += 1  # gap between groups

        self._tab_list_built = True
        self.btn_auto.grid()
        self.btn_auto.configure(state="normal", text=f"开始处理选中任务 ({len(self._all_tabs)}项)")
        self.btn_scan.configure(state="normal", text="重新扫描", fg_color="#21262d", text_color="#c9d1d9", hover_color="#30363d")
        self.status_label.configure(text=f"已扫描 {len(self._all_tabs)} 个任务，勾选后点击[开始处理]")
        self._tab_list_built = True
        self.btn_auto.grid()
        self.btn_auto.configure(state="normal", text=f"▶ 开始处理选中任务 ({len(self._all_tabs)}项)")
        self.btn_scan.configure(state="normal", text=" 重新扫描", fg_color=("#ebeae5", "#2a2922"), text_color=("#26251e", "#e6e5e0"), hover_color=("#e1e0db", "#33322a"))
        self.status_label.configure(text=f" 已扫描 {len(self._all_tabs)} 个任务，请勾选后点击[开始处理选中任务]", text_color=("#1f8a65", "#2fba8a"))
        gui_log_queue.put(f"\n扫描完成，共 {len(self._all_tabs)} 个任务可供选择")

    def _on_select_all(self):
        for var in self._tab_check_vars:
            var.set(True)

    def _on_select_compulsory(self):
        for i, var in enumerate(self._tab_check_vars):
            if i < len(self._all_tabs):
                var.set(self._all_tabs[i].get('is_compulsory', False))

    def _on_deselect_all(self):
        for var in self._tab_check_vars:
            var.set(False)

    def _on_select_visible(self):
        """全选当前搜索可见的任务"""
        query = self.task_search.get().lower()
        for i, cb in enumerate(self._tab_checkboxes):
            if i < len(self._all_tabs):
                if query == "" or query in self._all_tabs[i]['display'].lower():
                    self._tab_check_vars[i].set(True)

    def _on_search_key(self, event=None):
        """搜索过滤任务列表"""
        query = self.task_search.get().lower()
        for i, cb in enumerate(self._tab_checkboxes):
            if i < len(self._all_tabs):
                visible = query == "" or query in self._all_tabs[i]['display'].lower()
                if visible:
                    cb.grid()
                else:
                    cb.grid_remove()
        if query:
            self.btn_select_visible.grid()
        else:
            self.btn_select_visible.grid_remove()

    def _on_toggle_log(self):
        """折叠/展开日志区域"""
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_card.grid()
            self.log_toggle_btn.configure(text="折叠日志")
            self.main_frame.grid_rowconfigure(4, weight=1)
        else:
            self.log_card.grid_remove()
            self.log_toggle_btn.configure(text="展开日志")
            self.main_frame.grid_rowconfigure(4, weight=0)

    def _on_auto_clicked(self):
        if self._auto_running or self.btn_auto.cget("state") == "disabled":
            return
        selected = []
        for i, tab in enumerate(self._all_tabs):
            if self._tab_check_vars[i].get():
                selected.append(tab)
        if not selected:
            gui_log_queue.put("没有勾选任何任务，请先勾选")
            return
        gui_log_queue.put(f"\n{'='*60}")
        gui_log_queue.put(f"用户选择了 {len(selected)} 个任务，开始全自动处理...")
        gui_log_queue.put(f"{'='*60}")
        self._auto_running = True
        self.btn_auto.configure(state="disabled", text="⏳ 自动处理中...")
        self.btn_scan.configure(state="disabled")
        self.btn_quick.configure(state="disabled")
        self.status_label.configure(text="处理中，请勿操作浏览器", text_color=("#f54e00", "#f54e00"))
        self.progress_bar.grid()
        self.progress_bar.start()
        threading.Thread(target=self._run_auto_task, args=(selected,), daemon=True).start()

    def _run_auto_task(self, selected):
        try:
            self.solver.process_selected_tabs(selected)
            gui_log_queue.put("\n全部选中任务处理完成！")
            winsound.MessageBeep()
        except Exception as e:
            gui_log_queue.put(f"\n自动处理异常: {str(e)}")
            logger.error(f"自动处理异常: {e}", exc_info=True)
        finally:
            self._auto_running = False
            self.root.after(0, self._reset_auto_button)

    def _reset_auto_button(self):
        self.progress_bar.stop()
        self.progress_bar.grid_remove()
        self.btn_auto.configure(state="normal", text="▶ 开始处理选中任务")
        self.btn_scan.configure(state="normal", text=" 重新扫描", fg_color=("#ebeae5", "#2a2922"), text_color=("#26251e", "#e6e5e0"), hover_color=("#e1e0db", "#33322a"))
        self.btn_quick.configure(state="normal", text="快速处理当前页", fg_color=("#ebeae5", "#2a2922"), text_color=("#26251e", "#e6e5e0"), hover_color=("#e1e0db", "#33322a"))
        self.status_label.configure(text="任务完成", text_color=("#1f8a65", "#2fba8a"))
        try: winsound.MessageBeep()
        except: pass

    def _on_quick_clicked(self):
        if self._quick_running or self.btn_quick.cget("state") == "disabled":
            return
        gui_log_queue.put(f"\n开始处理当前停留的页面...")
        self._quick_running = True
        self.btn_quick.configure(state="disabled", text="⏳ 处理中...")
        self.btn_scan.configure(state="disabled")
        if self.btn_auto.winfo_ismapped():
            self.btn_auto.configure(state="disabled")
        self.status_label.configure(text="处理当前页...", text_color=("#f54e00", "#f54e00"))
        self.progress_bar.grid()
        self.progress_bar.start()
        threading.Thread(target=self._run_quick_task, daemon=True).start()

    def _run_quick_task(self):
        self.solver.processed_hashes.clear()
        try:
            success = self.solver.solve_current_page()
            if success:
                gui_log_queue.put("\n当前页面处理完成！")
            else:
                gui_log_queue.put("\n当前页面没有需要处理的题目")
        except Exception as e:
            gui_log_queue.put(f"\n处理异常: {str(e)}")
            logger.error(f"快速处理异常: {e}", exc_info=True)
        finally:
            self._quick_running = False
            self.root.after(0, self._reset_quick_button)

    def _reset_quick_button(self):
        self.progress_bar.stop()
        self.progress_bar.grid_remove()
        self.btn_quick.configure(state="normal", text="快速处理当前页", fg_color=("#ebeae5", "#2a2922"), text_color=("#26251e", "#e6e5e0"), hover_color=("#e1e0db", "#33322a"))
        self.btn_scan.configure(state="normal", text="扫描任务列表", fg_color=("#f54e00", "#f54e00"), text_color=("#ffffff", "#ffffff"), hover_color=("#d94400", "#d94400"))
        if self.btn_auto.winfo_ismapped():
            self.btn_auto.configure(state="normal")
        self.status_label.configure(text="就绪", text_color=("#1f8a65", "#2fba8a"))
        try: winsound.MessageBeep()
        except: pass

    def _reset_scan_button(self):
        self.progress_bar.stop()
        self.progress_bar.grid_remove()
        self.btn_scan.configure(state="normal", text="重新扫描", fg_color="#21262d", text_color="#c9d1d9", hover_color="#30363d")
        self.status_label.configure(text="扫描失败，请确认已进入课程页面后重试")

    def _on_debug_toggle(self):
        global DEBUG_MODE
        DEBUG_MODE = self.debug_var.get()
        gui_log_queue.put(f"调试模式: {'开启' if DEBUG_MODE else '关闭'}")

    def _poll_logs(self):
        """跨线程日志渲染, 超出800行自动裁剪旧内容"""
        max_lines = 800
        while not gui_log_queue.empty():
            msg = gui_log_queue.get()
            self.log_area.configure(state="normal")
            self.log_area.insert("end", msg + "\n")
            line_count = int(self.log_area.index("end-1c").split(".")[0])
            if line_count > max_lines:
                self.log_area.delete("1.0", "200.0")
            self.log_area.see("end")
            self.log_area.configure(state="disabled")
        self.root.after(100, self._poll_logs)

    def on_quit_clicked(self):
        """安全释放资源"""
        gui_log_queue.put(" 正在关闭浏览器并释放资源...")
        self.root.destroy()
        try:
            self.driver.quit()
        except:
            pass
        sys.exit(0)


class UCampusBot:
    """U校园机器人 - 组装所有组件"""

    def __init__(self, config_path: str = 'config.json', skip_check: bool = False):
        self.config = Config.from_json(config_path)
        self.temp_dirs: List[str] = []
        self.driver = None
        self.popup_watcher = None

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
                    driver_manager = DriverManager()
                    target_dir = os.path.expandvars(r'%LOCALAPPDATA%\U校园AI答题')
                    os.makedirs(target_dir, exist_ok=True)

                    driver_path = checker.auto_download_driver(target_dir)
                    if driver_path:
                        print(" 驱动准备完成，请重新运行程序")
                        input("按回车键退出...")
                        sys.exit(0)

                elif choice == '3':
                    if checker.auto_install_ffmpeg():
                        sys.exit(0)

                elif choice == '4':
                    if checker.add_ffmpeg_to_path():
                        sys.exit(0)

                elif choice == '5':
                    edge_path, driver_path, ffmpeg_path = checker.manual_specify_path()

                    if edge_path and os.path.exists(edge_path):
                        print(f" 已指定 Edge: {edge_path}")

                    if driver_path:
                        manager = DriverManager()
                        saved_path = manager.save_driver(driver_path)
                        print(f" 驱动已保存: {saved_path}")
                        print("请重新运行程序")
                        input("按回车键退出...")
                        sys.exit(0)

                    if ffmpeg_path:
                        bin_dir = os.path.dirname(ffmpeg_path)
                        checker._add_to_system_path(bin_dir)
                        print(f" FFmpeg 已添加到 PATH: {bin_dir}")
                        print("请重新运行程序")
                        input("按回车键退出...")
                        sys.exit(0)

                elif choice == '6':
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
       原因：Edge 未安装或版本不匹配
       解决：选择 [1] 自动安装，或访问 https://www.microsoft.com/edge

    2. Edge 驱动问题
       原因：msedgedriver.exe 未找到
       解决：选择 [2] 自动下载，或手动放置到程序目录

    3. FFmpeg 问题（语音识别必需）
       原因：未安装 FFmpeg 或未添加到系统 PATH
       解决：
          - 方法A（推荐）：选择 [3] 自动下载安装（约130MB）
          - 方法B：选择 [4] 将已安装的 FFmpeg 添加到 PATH
          - 方法C：手动下载 https://ffmpeg.org/download.html
            解压后将 bin 目录添加到系统环境变量 PATH

    4. 验证 FFmpeg 安装
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

    def _create_driver(self):
        options = webdriver.EdgeOptions()
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        temp_dir = tempfile.mkdtemp(prefix='ucampus_')
        self.temp_dirs.append(temp_dir)
        options.add_argument(f'--user-data-dir={temp_dir}')

        driver = self._try_start_driver(options)

        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
        })

        return driver

    def _try_start_driver(self, options) -> webdriver.Edge:
        errors = []

        try:
            from selenium.webdriver.edge.service import Service
            from webdriver_manager.microsoft import EdgeChromiumDriverManager

            service = Service(EdgeChromiumDriverManager().install())
            return webdriver.Edge(service=service, options=options)
        except Exception as e:
            errors.append(f"自动管理: {str(e)[:40]}")

        manager = DriverManager()
        user_driver = manager.get_driver_path()
        if user_driver:
            try:
                from selenium.webdriver.edge.service import Service
                service = Service(user_driver)
                return webdriver.Edge(service=service, options=options)
            except Exception as e:
                errors.append(f"用户驱动: {str(e)[:40]}")

        bundled = get_resource_path('msedgedriver.exe')
        if os.path.exists(bundled):
            try:
                from selenium.webdriver.edge.service import Service
                service = Service(bundled)
                return webdriver.Edge(service=service, options=options)
            except Exception as e:
                errors.append(f"自带驱动: {str(e)[:40]}")

        try:
            return webdriver.Edge(options=options)
        except Exception as e:
            errors.append(f"系统PATH: {str(e)[:40]}")

        print("\n 浏览器启动失败:")
        for err in errors:
            print(f"   - {err}")
        raise Exception("无法启动 Edge 浏览器")

    def start(self):
        solver = AISolver(self.driver, self.config)

        self.gui = ModernGUI(self.driver, solver, self)

        threading.Thread(target=self.popup_watcher.run, daemon=True).start()
        threading.Thread(target=self._background_login_flow, daemon=True).start()

        self.gui.root.mainloop()

        return True

    def _background_login_flow(self):
        gui_log_queue.put(" 正在与 U校园 建立连接，请稍候...")
        success = self._login()
        if success:
            self.gui.root.after(0, self.gui.enable_scan_button)

    def _login(self) -> bool:
        try:
            self.driver.get(self.config.url)
            time.sleep(3)
            username = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="username"]')))
            password = self.driver.find_element(By.XPATH, '//*[@id="password"]')
            agreement_check = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="agreement"]')))
            username.send_keys(self.config.username)
            password.send_keys(self.config.password)
            if not agreement_check.is_selected():
                agreement_check.click()

            login_btn = self.driver.find_element(By.XPATH,
                                                 '//*[@id="rc-tabs-0-panel-1"]/form/div[4]/div/div/div/div/button')
            login_btn.click()

            gui_log_queue.put(" 如果遇到验证码，请在弹出的浏览器中手动进行人机验证。")
            gui_log_queue.put("⏳ 正在智能轮询登录状态...")

            for _ in range(60):
                time.sleep(2)
                current_url = self.driver.current_url
                if "course" in current_url or "home" in current_url or "space" in current_url or "student" in current_url:
                    break

            self.anti_anti_cheat()
            time.sleep(3)

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

    def anti_anti_cheat(self):
        """注入token绕过防作弊检测"""
        self.driver.execute_script('window.localStorage.setItem("__token", `{}`);'.format(self.config.token_full))
        self.driver.get("https://ucloud.unipus.cn/home")


class PopupWatcher:
    """弹窗监控器"""

    def __init__(self, driver):
        self.driver = driver
        self.running = False

    def run(self):
        self.running = True
        while self.running:
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

    def stop(self):
        self.running = False


if __name__ == '__main__':
    print('*' * 25 + "Unipus-Helper" + '*' * 25)

    skip_check = '--skip-check' in sys.argv

    if not skip_check:
        print("\n 提示：")
        print("   - 首次运行需要检查环境")
        print("   - 语音识别需要 FFmpeg（约130MB，可自动安装）")
        print("   - 如检查通过但无法启动，使用 --skip-check 跳过")

    logger, LOG_FILE = setup_logging()

    try:
        bot = UCampusBot('config.json', skip_check=skip_check)
        bot.start()
    except Exception as e:
        error_msg = str(e)
        print(f"\n 程序运行失败: {error_msg[:100]}")
        logger.error(f"程序异常: {error_msg}", exc_info=True)
        input("\n按任意键退出...")