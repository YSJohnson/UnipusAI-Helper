"""Issue #51: New words / Phrases and expressions 词汇跟读页回归测试。

零依赖：ast 抽取被测类/方法执行，不需要 selenium / PyQt5 / 课程账号。
"""
import ast
import copy
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

SOURCE = Path(__file__).parents[1] / 'UnipusAI_Helper.py'
SRC = SOURCE.read_text(encoding='utf-8')
TREE = ast.parse(SRC)

CLASS_METHODS = {
    'VocabularyReadAloudStrategy': {'can_parse', 'parse'},
    'QuestionParser': {'_find_containers'},
    'AnswerExecutor': {'__init__', '_answer_record', '_answer_vocabulary_read_aloud',
                       '_record_vocab_card', '_wait_vocab_card', '_vocab_record_buttons',
                       '_wait_vocab_upload'},
}


def module_constants():
    out = {}
    for node in TREE.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if name in ('VOCAB_READ_ALOUD_SELECTOR', 'VOCAB_CARD_JS',
                        'VOCAB_NEXT_JS', 'RECORD_MIC_JS',
                        'RECORD_ROW_STATE_JS'):
                try:
                    out[name] = ast.literal_eval(node.value)
                except Exception:
                    out[name] = None
    return out


CONSTS = module_constants()


class FakeQuestion:
    def __init__(self, **kw):
        self.number = kw.get('number', 1)
        self.text = kw.get('text', '')
        self.q_type = kw.get('q_type')
        self.element = kw.get('element')
        self.directions = kw.get('directions', '')
        self.record_items = kw.get('record_items', [])


class FakeBy:
    CSS_SELECTOR = 'css selector'


def load_components():
    nodes = []
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name in CLASS_METHODS:
            cls = copy.deepcopy(node)
            cls.body = [part for part in cls.body
                        if (isinstance(part, ast.FunctionDef)
                            and part.name in CLASS_METHODS[node.name])
                        or isinstance(part, ast.Assign)]
            cls.bases = []
            cls.keywords = []
            cls.decorator_list = []
            nodes.append(cls)
    module = ast.fix_missing_locations(
        ast.Module(body=nodes, type_ignores=[]))

    # Question.needs_speech_script 是本次行为的关键：抽成独立 property 执行
    prop_node = None
    q_cls = next(n for n in TREE.body
                 if isinstance(n, ast.ClassDef) and n.name == 'Question')
    for part in q_cls.body:
        if isinstance(part, ast.FunctionDef) and part.name == 'needs_speech_script':
            prop_node = copy.deepcopy(part)
            break
    prop_cls = ast.ClassDef(name='_NeedsProbe', bases=[], keywords=[],
                            body=[prop_node], decorator_list=[], type_params=[])
    module.body.append(prop_cls)
    module = ast.fix_missing_locations(module)

    import base64
    import time as _time
    ns = {
        'ast': ast, 'base64': base64, 'time': SimpleNamespace(sleep=Mock()),
        'threading': threading,
        'List': List, 'Dict': Dict, 'Optional': Optional, 'Any': Any,
        'By': FakeBy,
        'Question': FakeQuestion,
        'QuestionType': SimpleNamespace(RECORD='RECORD'),
        'VOCAB_READ_ALOUD_SELECTOR': CONSTS.get('VOCAB_READ_ALOUD_SELECTOR'),
        'VOCAB_CARD_JS': CONSTS.get('VOCAB_CARD_JS'),
        'VOCAB_NEXT_JS': CONSTS.get('VOCAB_NEXT_JS'),
        'RECORD_MIC_JS': CONSTS.get('RECORD_MIC_JS'),
        'RECORD_BUTTON_SELECTOR': '.record-icon.button-record',
        'find_record_buttons': Mock(return_value=[]),
        'WebDriverHelper': SimpleNamespace(safe_click=Mock(return_value=True)),
        'logger': Mock(),
    }
    exec(compile(module, '<extracted>', 'exec'), ns)
    return ns


NS = load_components()
CARD_JS = CONSTS.get('VOCAB_CARD_JS') or ''
NEXT_JS = CONSTS.get('VOCAB_NEXT_JS') or ''
MIC_JS = CONSTS.get('RECORD_MIC_JS') or ''


def make_question(**over):
    info = {
        'word': 'cognitive',
        'definition': 'adj. connected with mental processes 认知的',
        'audio': 'https://course.media.unipus.cn/x/cognitive.mp3#duration=1.200000&name=cognitive.mp3',
        'duration': 1.2,
        'name': 'cognitive.mp3',
        'index': 1,
        'total': 21,
        'hasButton': True,
    }
    info.update(over)
    driver = Mock()
    driver.execute_script = Mock(return_value=info)
    container = Mock()
    container.find_elements = Mock(return_value=[Mock()])
    q = NS['VocabularyReadAloudStrategy']().parse(container, driver, 1)
    return q, driver


class TestStaticWiring(unittest.TestCase):
    def test_selector_is_exact(self):
        self.assertEqual(CONSTS.get('VOCAB_READ_ALOUD_SELECTOR'),
                         '.vocabulary-wrapper')

    def test_card_js_reads_word_audio_and_progress(self):
        for frag in ('.vocWord .component-htmlview', '.soundWrap .question-audio audio',
                     '.progressBar .ratio span', 'swiper-slide-active',
                     'explanations'):
            self.assertIn(frag, CARD_JS, frag)

    def test_next_js_uses_swiper_api_first(self):
        self.assertIn('slideNext', NEXT_JS)
        self.assertIn('swiper-button-next', NEXT_JS)

    def test_strategy_registered_before_record(self):
        frag = 'self.strategies: List[QuestionParserStrategy] = ['
        start = SRC.index(frag)
        block = SRC[start:start + 1600]
        self.assertIn('VocabularyReadAloudStrategy()', block)
        self.assertLess(block.index('VocabularyReadAloudStrategy()'),
                        block.index('RecordStrategy()'))

    def test_container_discovery_priority(self):
        start = SRC.index('def _find_containers(self)')
        block = SRC[start:start + 1400]
        self.assertLess(block.index('VOCAB_READ_ALOUD_SELECTOR'),
                        block.index('record_containers = self._find_record_containers()'))

    def test_answer_record_dispatches_vocab(self):
        start = SRC.index('def _answer_record(self, q: Question, answer: str = "") -> bool:',
                          SRC.index('class AnswerExecutor'))
        self.assertIn("items[0].get('vocab')", SRC[start:start + 500])

    def test_no_ai_script_path_for_vocab(self):
        self.assertIn('不调用 AI', SRC)
        self.assertIn('使用页面原声逐张跟读', SRC)


class TestStrategyParse(unittest.TestCase):
    def test_parse_returns_record_with_original_audio(self):
        q, driver = make_question()
        self.assertEqual(q.q_type, 'RECORD')
        self.assertEqual(len(q.record_items), 1)
        item = q.record_items[0]
        self.assertEqual(item['text'], 'cognitive')
        self.assertEqual(item['url'],
                         'https://course.media.unipus.cn/x/cognitive.mp3')
        self.assertTrue(item['vocab'])
        self.assertEqual(item['vocab_total'], 21)
        self.assertIn('cognitive', q.text)

    def test_needs_speech_script_is_false(self):
        q, _ = make_question()
        probe = NS['_NeedsProbe']()
        probe.record_items = q.record_items
        self.assertFalse(probe.needs_speech_script)

    def test_still_no_script_when_audio_missing(self):
        q, _ = make_question(audio='', duration=0)
        probe = NS['_NeedsProbe']()
        probe.record_items = q.record_items
        self.assertFalse(probe.needs_speech_script)  # text=word 兜底
        self.assertEqual(q.record_items[0]['text'], 'cognitive')
        self.assertTrue(q.record_items[0]['needs_tts'])

    def test_can_parse_requires_vocab_wrapper(self):
        strategy = NS['VocabularyReadAloudStrategy']()
        container = Mock()
        container.find_elements = Mock(side_effect=[[Mock()], []])
        self.assertTrue(strategy.can_parse(container, Mock()))
        container.find_elements = Mock(side_effect=[[], []])
        self.assertFalse(strategy.can_parse(container, Mock()))

    def test_phrase_card_keeps_full_phrase(self):
        q, _ = make_question(word='a thirst for sth.',
                             audio='https://x/a_thirst.mp3#duration=1.5&name=a_thirst.mp3')
        self.assertEqual(q.record_items[0]['text'], 'a thirst for sth.')
        self.assertFalse(q.record_items[0]['needs_tts'])


class TestCardLoop(unittest.TestCase):
    def _executor(self, total=3):
        cls = NS['AnswerExecutor']
        obj = cls.__new__(cls)
        obj.stop_requested = Mock()
        obj.stop_requested.is_set.return_value = False
        obj.stop_requested.wait.return_value = False
        obj._dismiss_record_popup = Mock()
        obj._record_vocab_card = Mock(return_value=True)
        obj._download_record_audio = Mock(return_value=b'audio')
        obj._speech_audio = Mock(return_value=b'tts')
        state = {'i': 1}
        cards = {
            1: {'word': 'cognitive', 'index': 1, 'total': total,
                'audio': 'https://x/a.mp3#duration=1.2&name=a.mp3', 'hasButton': True},
            2: {'word': 'census', 'index': 2, 'total': total,
                'audio': 'https://x/b.mp3#duration=1.2&name=b.mp3', 'hasButton': True},
            3: {'word': 'a thirst for sth.', 'index': 3, 'total': total,
                'audio': 'https://x/c.mp3#duration=1.2&name=c.mp3', 'hasButton': True},
        }

        def execute_script(js, *args):
            if js == CARD_JS:
                return cards[min(state['i'], total)]
            if js == NEXT_JS:
                if len(args) > 1 and args[1]:
                    state['i'] = 1
                    return 'swiper'
                if state['i'] < total:
                    state['i'] += 1
                    return 'swiper'
                return ''
            return {}

        driver = Mock()
        driver.execute_script = Mock(side_effect=execute_script)
        driver.execute_async_script = Mock(return_value={'ok': True})
        driver.find_element = Mock(return_value=Mock())
        obj.driver = driver
        return obj, state

    def test_loops_all_cards_and_stops_at_total(self):
        obj, state = self._executor(total=3)
        q = FakeQuestion(record_items=[{'vocab': True, 'vocab_total': 3}],
                         element=Mock())
        self.assertTrue(obj._answer_vocabulary_read_aloud(q))
        self.assertEqual(obj._record_vocab_card.call_count, 3)
        self.assertEqual(state['i'], 3)
        self.assertEqual([c.args[1]['word'] for c in
                          obj._record_vocab_card.call_args_list],
                         ['cognitive', 'census', 'a thirst for sth.'])

    def test_stops_when_next_unavailable(self):
        obj, _ = self._executor(total=3)
        obj.driver.execute_script = Mock(side_effect=lambda js, *a: (
            {'word': 'cognitive', 'index': 1, 'total': 3,
             'audio': 'https://x/a.mp3#duration=1.2&name=a.mp3', 'hasButton': True}
            if js == CARD_JS else ''))
        q = FakeQuestion(record_items=[{'vocab': True, 'vocab_total': 3}],
                         element=Mock())
        self.assertFalse(obj._answer_vocabulary_read_aloud(q))
        self.assertEqual(obj._record_vocab_card.call_count, 1)

    def test_failed_card_stops_without_marking_page_complete(self):
        obj, state = self._executor(total=3)
        obj._record_vocab_card.side_effect = [True, False, True]
        q = FakeQuestion(record_items=[{'vocab': True, 'vocab_total': 3}], element=Mock())
        self.assertFalse(obj._answer_vocabulary_read_aloud(q))
        self.assertEqual(obj._record_vocab_card.call_count, 2)
        self.assertEqual(state['i'], 2)

    def test_starting_at_last_card_returns_to_first(self):
        obj, state = self._executor(total=3)
        state['i'] = 3
        q = FakeQuestion(record_items=[{'vocab': True, 'vocab_total': 3}], element=Mock())
        self.assertTrue(obj._answer_vocabulary_read_aloud(q))
        self.assertEqual([c.args[1]['index'] for c in obj._record_vocab_card.call_args_list],
                         [1, 2, 3])

    def test_missing_total_cannot_report_success(self):
        obj, _ = self._executor(total=3)
        q = FakeQuestion(record_items=[{'vocab': True, 'vocab_total': 0}], element=Mock())
        self.assertFalse(obj._answer_vocabulary_read_aloud(q))
        obj._record_vocab_card.assert_not_called()

    def test_stop_after_first_card_prevents_navigation(self):
        obj, state = self._executor(total=3)
        def stop_after_record(*args):
            obj.stop_requested.is_set.return_value = True
            return True
        obj._record_vocab_card.side_effect = stop_after_record
        q = FakeQuestion(record_items=[{'vocab': True, 'vocab_total': 3}], element=Mock())
        self.assertFalse(obj._answer_vocabulary_read_aloud(q))
        self.assertEqual(obj._record_vocab_card.call_count, 1)
        self.assertEqual(state['i'], 1)

    def test_waits_for_lazy_card_content(self):
        obj, _ = self._executor()
        obj.driver.execute_script.side_effect = [
            {'index': 2, 'word': '', 'hasButton': False},
            {'index': 2, 'word': 'census', 'hasButton': True},
        ]
        self.assertEqual(obj._wait_vocab_card(FakeQuestion(element=Mock()), 2)['word'],
                         'census')
        obj.stop_requested.wait.assert_called_once_with(0.25)

    def test_stuck_card_never_reports_ready(self):
        obj, _ = self._executor()
        self.assertIsNone(obj._wait_vocab_card(FakeQuestion(element=Mock()), 2))

    def test_executor_uses_supplied_stop_event(self):
        stop = threading.Event()
        obj = NS['AnswerExecutor'](Mock(), stop_requested=stop)
        self.assertIs(obj.stop_requested, stop)
        stop.set()
        self.assertFalse(obj._answer_vocabulary_read_aloud(FakeQuestion()))

    def test_probe_failure_returns_false(self):
        obj, _ = self._executor(total=3)
        obj.driver.execute_async_script = Mock(return_value=None)
        q = FakeQuestion(record_items=[{'vocab': True, 'vocab_total': 3}],
                         element=Mock())
        self.assertFalse(obj._answer_vocabulary_read_aloud(q))
        obj._record_vocab_card.assert_not_called()

    def test_record_card_uses_original_audio_not_tts(self):
        obj, _ = self._executor(total=1)
        obj._record_vocab_card = NS['AnswerExecutor']._record_vocab_card.__get__(obj)
        obj._wait_vocab_upload = Mock(return_value=True)
        NS['find_record_buttons'] = Mock(return_value=[Mock()])
        info = {'index': 1, 'word': 'cognitive', 'audio': 'https://x/a.mp3#duration=1.2&name=a.mp3',
                'duration': 1.2}
        self.assertTrue(obj._record_vocab_card(
            FakeQuestion(element=Mock()), info))
        obj._download_record_audio.assert_called_once_with('https://x/a.mp3')
        obj._speech_audio.assert_not_called()

    def test_record_card_falls_back_to_tts_without_audio(self):
        obj, _ = self._executor(total=1)
        obj._record_vocab_card = NS['AnswerExecutor']._record_vocab_card.__get__(obj)
        obj._wait_vocab_upload = Mock(return_value=True)
        NS['find_record_buttons'] = Mock(return_value=[Mock()])
        info = {'index': 1, 'word': 'cognitive', 'audio': '', 'duration': 0}
        self.assertTrue(obj._record_vocab_card(
            FakeQuestion(element=Mock()), info))
        obj._speech_audio.assert_called_once()
        obj._download_record_audio.assert_not_called()


class TestRecordingCompletion(unittest.TestCase):
    _executor = TestCardLoop._executor
    def setUp(self):
        # 每个用例独立设置点击 mock，避免失败路径污染其它录音用例。
        NS['WebDriverHelper'].safe_click = Mock(return_value=True)
        self.addCleanup(setattr, NS['WebDriverHelper'], 'safe_click', Mock(return_value=True))

    def _recording(self):
        obj, _ = self._executor(total=1)
        obj._record_vocab_card = NS['AnswerExecutor']._record_vocab_card.__get__(obj)
        obj._vocab_record_buttons = Mock(side_effect=[[Mock()], [Mock()]])
        obj._wait_vocab_upload = Mock(return_value=True)
        info = {'index': 1, 'word': 'cognitive', 'audio': 'https://x/a.mp3', 'duration': 1.2}
        return obj, FakeQuestion(element=Mock()), info

    def test_failed_stop_is_failure(self):
        obj, q, info = self._recording()
        NS['WebDriverHelper'].safe_click.side_effect = [True, False]
        self.assertFalse(obj._record_vocab_card(q, info))
        obj._wait_vocab_upload.assert_not_called()

    def test_upload_failure_is_failure(self):
        obj, q, info = self._recording()
        obj._wait_vocab_upload.return_value = False
        self.assertFalse(obj._record_vocab_card(q, info))

    def test_stop_during_recording_clicks_stop_without_waiting_for_upload(self):
        obj, q, info = self._recording()
        obj.stop_requested.wait.side_effect = [False, True]
        self.assertFalse(obj._record_vocab_card(q, info))
        self.assertEqual(NS['WebDriverHelper'].safe_click.call_count, 2)
        obj._wait_vocab_upload.assert_not_called()

    def test_relocates_button_before_stopping(self):
        obj, q, info = self._recording()
        self.assertTrue(obj._record_vocab_card(q, info))
        clicks = NS['WebDriverHelper'].safe_click.call_args_list
        self.assertIsNot(clicks[0].args[1], clicks[1].args[1])

    def test_old_score_and_old_replay_do_not_count_as_uploaded(self):
        obj, _ = self._executor(total=1)
        old = {'index': 1, 'word': 'cognitive', 'recordings': ['https://x/old.mp3'],
               'score': 100, 'recording': False}
        obj.driver.execute_script = Mock(return_value=old)
        self.assertFalse(obj._wait_vocab_upload(FakeQuestion(element=Mock()), old, timeout=1))

    def test_blob_waits_until_http_upload_is_available(self):
        obj, _ = self._executor(total=1)
        baseline = {'index': 1, 'word': 'cognitive', 'recordings': ['https://x/old.mp3']}
        states = [dict(baseline, recordings=['blob:pending']),
                  dict(baseline, recordings=['https://x/new.mp3'])]
        obj.driver.execute_script = Mock(side_effect=states)
        self.assertTrue(obj._wait_vocab_upload(FakeQuestion(element=Mock()), baseline, timeout=1))
        obj.stop_requested.wait.assert_called_once_with(0.25)

    def test_upload_on_a_different_card_is_not_accepted(self):
        obj, _ = self._executor(total=1)
        baseline = {'index': 1, 'word': 'cognitive', 'recordings': []}
        obj.driver.execute_script = Mock(return_value={
            'index': 2, 'word': 'census', 'recordings': ['https://x/new.mp3']})
        self.assertFalse(obj._wait_vocab_upload(FakeQuestion(element=Mock()), baseline))


if __name__ == '__main__':
    unittest.main()
