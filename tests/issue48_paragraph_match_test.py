"""Issue #48: Skimming and scanning 段落信息匹配题回归测试。

零依赖：ast 抽取被测类/方法执行，不需要 selenium / PyQt5 / 课程账号。
"""
import ast
import copy
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

SOURCE = Path(__file__).parents[1] / 'UnipusAI_Helper.py'
SRC = SOURCE.read_text(encoding='utf-8')
TREE = ast.parse(SRC)

CLASS_METHODS = {
    'ParagraphMatchingStrategy': {
        'can_parse', 'parse', '_paragraph_material', '_statements',
    },
    'PromptBuilder': {'_build_paragraph_match'},
    'AnswerExecutor': {'_fill_paragraph_match', '_parse_paragraph_letters'},
}


class FakeQuestion:
    def __init__(self, **kw):
        self.number = kw.get('number', 1)
        self.text = kw.get('text', '')
        self.q_type = kw.get('q_type')
        self.element = kw.get('element')
        self.directions = kw.get('directions', '')
        self.inputs = kw.get('inputs', [])
        self.banked_blanks = kw.get('banked_blanks', [])
        self.banked_options = kw.get('banked_options', [])
        self.options = []

    def is_interactive(self):
        return bool(self.inputs or self.banked_blanks)


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
    ns = {
        're': re, 'time': Mock(),
        'List': List, 'Dict': Dict, 'Optional': Optional, 'Any': Any,
        'By': FakeBy,
        'Question': FakeQuestion,
        'QuestionType': SimpleNamespace(PARAGRAPH_MATCH='PARAGRAPH_MATCH'),
    }
    exec(compile(module, '<extracted>', 'exec'), ns)
    return ns


LETTERS = list("ABCDEFGHIJKLMNO")  # 15 段 A-O


class ParagraphMatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_components()

    def test_can_parse_requires_two_textmatch_inputs(self):
        container = Mock()
        container.find_elements.return_value = [Mock() for _ in range(10)]
        self.assertTrue(self.ns['ParagraphMatchingStrategy']().can_parse(container, Mock()))
        container.find_elements.return_value = [Mock()]
        self.assertFalse(self.ns['ParagraphMatchingStrategy']().can_parse(container, Mock()))

    def test_material_js_generates_letter_prefixes(self):
        # 真实 DOM 里字母由 CSS upper-alpha 生成，不在 textContent 里
        self.assertIn("String.fromCharCode(65 + i)", SRC)
        self.assertIn(".layout-material-container", SRC)
        self.assertIn("input.textbox", SRC)

    def test_parse_builds_letter_pool_and_ten_inputs(self):
        strat = self.ns['ParagraphMatchingStrategy']()
        container = Mock()
        container.find_elements.return_value = [Mock() for _ in range(10)]

        def script(js, *args):
            if '.layout-material-container' in js:
                return {'text': 'A. first paragraph\nB. second paragraph',
                        'letters': LETTERS}
            return ['%d. statement number %d' % (i + 1, i + 1)
                    for i in range(10)]

        driver = Mock()
        driver.execute_script.side_effect = script
        q = strat.parse(container, driver, 1,
                        'Identify the paragraph from which the information is derived.')
        self.assertEqual(len(q.inputs), 10)
        self.assertEqual(len(q.banked_blanks), 10)
        self.assertEqual(q.banked_options, LETTERS)
        self.assertEqual(q.q_type, 'PARAGRAPH_MATCH')
        self.assertTrue(q.is_interactive())
        self.assertIn('【题目要求】', q.text)
        self.assertIn('A. first paragraph', q.text)
        self.assertIn('1. statement number 1', q.banked_blanks[0]['statement'])

    def test_letter_parser_supports_i_through_o(self):
        parse = self.ns['AnswerExecutor']._parse_paragraph_letters
        answer = '1.H 2.N 3.G 4.L 5.O 6.M 7.N 8.F 9.J 10.M'
        self.assertEqual(parse(answer, 10, LETTERS),
                         list("HNGLOMNFJM"))
        # 旧 MATCHING 的 ([A-H]) 正则会把 N/O/M/J 全部漏掉
        self.assertIn('N', parse(answer, 10, LETTERS))

    def test_same_paragraph_can_be_reused(self):
        parse = self.ns['AnswerExecutor']._parse_paragraph_letters
        self.assertEqual(parse('1.H 2.H 3.H', 3, LETTERS), ['H', 'H', 'H'])

    def test_letters_outside_pool_are_rejected(self):
        parse = self.ns['AnswerExecutor']._parse_paragraph_letters
        self.assertEqual(parse('1.Z 2.A', 2, LETTERS), ['', 'A'])

    def test_number_format_variants(self):
        parse = self.ns['AnswerExecutor']._parse_paragraph_letters
        self.assertEqual(parse('答案: 1) K 2、L', 2, LETTERS)[:2], ['K', 'L'])

    def test_plain_letter_sequence_fallback(self):
        parse = self.ns['AnswerExecutor']._parse_paragraph_letters
        self.assertEqual(parse('K L M', 3, LETTERS), ['K', 'L', 'M'])

    def test_prompt_contains_material_statements_and_rules(self):
        q = FakeQuestion(
            number=1, text='【题目要求】...\nA. para one\nO. para fifteen',
            banked_options=LETTERS,
            banked_blanks=[{'index': i, 'statement': 'statement %d' % (i + 1)}
                           for i in range(10)])
        lines = self.ns['PromptBuilder']()._build_paragraph_match(q)
        joined = '\n'.join(lines)
        self.assertIn('【阅读材料】', joined)
        self.assertIn('A. para one', joined)
        self.assertIn('10. statement 10', joined)
        self.assertIn('可选段落字母: A B C', joined)
        self.assertIn('只填一个段落字母', joined)
        self.assertIn('重复选用', joined)

    def test_fill_writes_one_letter_per_input(self):
        executor = self.ns['AnswerExecutor']()
        executor.driver = Mock()
        executor._fill_text_input_verified = Mock(return_value=True)
        q = FakeQuestion(
            inputs=[Mock() for _ in range(10)],
            banked_options=LETTERS,
            banked_blanks=[{'index': i} for i in range(10)])
        ok = executor._fill_paragraph_match(
            q, '1.H 2.N 3.G 4.L 5.O 6.M 7.N 8.F 9.J 10.M')
        self.assertTrue(ok)
        self.assertEqual(executor._fill_text_input_verified.call_count, 10)
        written = [call.args[1] for call in
                   executor._fill_text_input_verified.call_args_list]
        self.assertEqual(written, list("HNGLOMNFJM"))

    def test_wiring(self):
        self.assertIn('ParagraphMatchingStrategy(),', SRC)
        self.assertIn('QuestionType.PARAGRAPH_MATCH: self._build_paragraph_match', SRC)
        self.assertIn('QuestionType.PARAGRAPH_MATCH: self._fill_paragraph_match', SRC)
        self.assertIn('段落信息匹配: 1.H 2.N', SRC)


if __name__ == '__main__':
    unittest.main(verbosity=2)
