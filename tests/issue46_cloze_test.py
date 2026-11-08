"""Issue #46: Cloze 下拉选择题的容器发现 / 解析 / 提示词回归测试。

零依赖：从主文件里用 ast 抽出被测的类与函数执行，不需要 selenium / PyQt5 /
课程账号。真实 DOM 交互（点开下拉、点选候选项）仍需真机验证。
"""
import ast
import copy
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

SOURCE = Path(__file__).parents[1] / 'UnipusAI_Helper.py'
SRC = SOURCE.read_text(encoding='utf-8')
TREE = ast.parse(SRC)

ASSIGNS = {
    'CLOZE_DROPDOWN_PLACEHOLDER', 'DROPDOWN_TRIGGER_SELECTOR',
    'DROPDOWN_TRIGGER_JS',
}
FUNCS = {'find_dropdown_triggers', 'has_visible_dropdown'}
CLASS_METHODS = {
    'DropdownSelectStrategy': {
        'can_parse', 'parse', '_cloze_material', '_peek_dropdown_options',
    },
    'PromptBuilder': {'_build_dropdown_select'},
}


class FakeQuestion:
    def __init__(self, **kw):
        self.number = kw.get('number', 1)
        self.text = kw.get('text', '')
        self.q_type = kw.get('q_type')
        self.element = kw.get('element')
        self.directions = kw.get('directions', '')
        self.banked_blanks = kw.get('banked_blanks', [])
        self.banked_options = kw.get('banked_options', [])
        self.options = []
        self.inputs = []
        self.blank_count = 0
        self.answer_count = 0

    def is_interactive(self):
        return bool(self.options or self.inputs or self.banked_blanks)


class FakeBy:
    CSS_SELECTOR = 'css selector'
    XPATH = 'xpath'
    TAG_NAME = 'tag name'


def load_components():
    nodes = []
    for node in TREE.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id in ASSIGNS
                        for t in node.targets)):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in FUNCS:
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in CLASS_METHODS:
            cls = copy.deepcopy(node)
            cls.body = [part for part in cls.body
                        if isinstance(part, ast.FunctionDef)
                        and part.name in CLASS_METHODS[node.name]]
            cls.bases = []
            cls.keywords = []
            cls.decorator_list = []
            nodes.append(cls)
    module = ast.fix_missing_locations(
        ast.Module(body=nodes, type_ignores=[]))
    ns = {
        'json': json, 're': re, 'time': Mock(),
        'List': List, 'Dict': Dict, 'Optional': Optional, 'Any': Any,
        'By': FakeBy,
        'ActionChains': Mock(), 'Keys': Mock(),
        'WebDriverHelper': Mock(),
        'Selectors': SimpleNamespace(QUESTION_TITLE='.title'),
        'Question': FakeQuestion,
        'QuestionType': SimpleNamespace(DROPDOWN_SELECT='DROPDOWN_SELECT'),
        'NoSuchElementException': Exception,
    }
    exec(compile(module, '<extracted>', 'exec'), ns)
    return ns


class ClozeDropdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_components()

    def test_trigger_js_uses_placeholder_and_generic_roles(self):
        js = self.ns['DROPDOWN_TRIGGER_JS']
        self.assertIn('点击选择', js)
        self.assertIn('[role="combobox"]', js)
        self.assertIn('createTreeWalker', js)
        self.assertIn('.scoop-select-wrapper', self.ns['DROPDOWN_TRIGGER_SELECTOR'])

    def test_find_dropdown_triggers_returns_script_result(self):
        driver = Mock()
        driver.execute_script.return_value = ['a', 'b', 'c']
        self.assertEqual(self.ns['find_dropdown_triggers'](driver, 'root'),
                         ['a', 'b', 'c'])

    def test_find_dropdown_triggers_swallows_driver_error(self):
        driver = Mock()
        driver.execute_script.side_effect = RuntimeError('stale element')
        self.assertEqual(self.ns['find_dropdown_triggers'](driver, 'root'), [])
        self.assertFalse(self.ns['has_visible_dropdown'](driver, 'root'))

    def test_has_visible_dropdown_true_when_script_finds_triggers(self):
        driver = Mock()
        driver.execute_script.return_value = ['trigger']
        self.assertTrue(self.ns['has_visible_dropdown'](driver, 'root'))

    def test_can_parse_by_css_without_touching_js(self):
        container = Mock()
        container.find_elements.return_value = ['select']
        driver = Mock()
        self.assertTrue(self.ns['DropdownSelectStrategy']().can_parse(container, driver))
        driver.execute_script.assert_not_called()

    def test_can_parse_falls_back_to_placeholder_text(self):
        container = Mock()
        container.find_elements.return_value = []
        driver = Mock()
        driver.execute_script.return_value = ['trigger']
        self.assertTrue(self.ns['DropdownSelectStrategy']().can_parse(container, driver))

    def test_can_parse_false_when_nothing_found(self):
        container = Mock()
        container.find_elements.return_value = []
        driver = Mock()
        driver.execute_script.return_value = []
        self.assertFalse(self.ns['DropdownSelectStrategy']().can_parse(container, driver))

    def test_parse_falls_back_to_generic_triggers_and_stays_interactive(self):
        triggers = [Mock() for _ in range(3)]
        for t in triggers:
            t.find_element.side_effect = Exception('no ancestor')
            t.find_elements.return_value = []
            t.text = '点击选择'

        container = Mock()
        container.find_elements.return_value = []  # 没有 .scoop-select-wrapper

        def script(js, *args):
            if 'const root = arguments[0]' in js:
                return triggers
            if "trim() === '点击选择'" in js:
                return '1) ____ of us 2) ____ on batteries 3) ____ north'
            return 'Today we live 1) ____ of us'

        driver = Mock()
        driver.execute_script.side_effect = script
        self.ns['WebDriverHelper'].safe_find_element.return_value = None

        q = self.ns['DropdownSelectStrategy']().parse(
            container, driver, 1,
            'Read the following text and choose the best word(s) for each numbered blank.')
        self.assertEqual(len(q.banked_blanks), 3)
        self.assertTrue(q.is_interactive())
        self.assertIn('【全文】', q.text)
        self.assertIn('____', q.text)

    def test_build_prompt_counts_1_style_blank_numbers(self):
        q = FakeQuestion(number=1, text='【全文】1) ____ of us 2) ____ on batteries',
                         banked_blanks=[], q_type='DROPDOWN_SELECT')
        lines = self.ns['PromptBuilder']()._build_dropdown_select(q)
        self.assertIn('（共 2 个空', '\n'.join(lines))

    def test_build_prompt_includes_full_text(self):
        q = FakeQuestion(number=1, text='【全文】1) ____ of us 2) ____ north',
                         banked_blanks=[{'context': '1) ____ of us'}],
                         banked_options=['many', 'few'])
        lines = self.ns['PromptBuilder']()._build_dropdown_select(q)
        self.assertTrue(any('全文:' in line for line in lines))

    def test_container_and_write_fallbacks_are_wired(self):
        self.assertIn('has_visible_dropdown(self.driver, c)', SRC)
        self.assertIn('select_elements = find_dropdown_triggers(driver, container)', SRC)
        self.assertIn('trigger = select_wrapper', SRC)
        self.assertIn('通用候选项点击', SRC)
        self.assertIn('|| wrapper', SRC)


if __name__ == '__main__':
    unittest.main(verbosity=2)
