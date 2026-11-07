import ast
import contextlib
import io
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


def load_fill_components():
    source = Path(__file__).parents[1] / 'UnipusAI_Helper.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    methods = {
        'AnswerExecutor': {
            '_fill_banked_cloze', '_match_to_option', '_parse_banked_answer',
            '_strip_given_prefix', '_normalize_answer_labels',
            '_clean_extracted_answer', '_number_prefix_pattern',
        },
        'AISolver': {'_answer_chunked_fill', '_parse_numbered_map'},
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in methods:
            node.body = [part for part in node.body if (
                isinstance(part, ast.FunctionDef) and part.name in methods[node.name]
            ) or (
                isinstance(part, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in {
                        'ANSWER_LABEL_PATTERN', 'NUMBER_PREFIX_PATTERN',
                    } for target in part.targets)
            )]
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == 'is_expression_bank_question':
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith('EXPRESSION_')
                for target in node.targets):
            nodes.append(node)
    namespace = {
        're': re,
        'Question': SimpleNamespace,
        'time': SimpleNamespace(sleep=lambda _: None),
        'logger': Mock(),
    }
    code = 'from __future__ import annotations\n' + ast.unparse(
        ast.Module(body=nodes, type_ignores=[]))
    exec(compile(code, str(source), 'exec'), namespace)
    return SimpleNamespace(**namespace)


app = load_fill_components()


class Issue45FillTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def fill_banked(self, outcomes, answer='1.dug up 2.took up', count=2):
        executor = object.__new__(app.AnswerExecutor)
        executor.driver = Mock()
        executor._fill_text_input_verified = Mock(side_effect=outcomes)
        question = SimpleNamespace(
            is_phrase_mode=True,
            banked_options=['dig up', 'take up'],
            banked_blanks=[{'input': object(), 'hint': ''} for _ in range(count)],
            directions='Fill in the blanks with the expressions given below. '
                       'Change the form where necessary. Each expression can be used only once.',
            text='',
        )
        return executor._fill_banked_cloze(question, answer)

    def test_banked_fill_requires_every_input_to_pass_verification(self):
        self.assertFalse(self.fill_banked([True, False]))

    def test_banked_fill_succeeds_when_every_input_is_verified(self):
        self.assertTrue(self.fill_banked([True, True]))

    def test_banked_fill_fails_when_all_writes_fail(self):
        self.assertFalse(self.fill_banked([False, False]))

    def test_banked_fill_rejects_missing_answers(self):
        self.assertFalse(self.fill_banked([True], answer='1.dug up'))

    def test_empty_banked_fill_is_not_success(self):
        self.assertFalse(self.fill_banked([], answer='', count=0))

    def second_chunk_prompt(self, directions='', options=()):
        solver = object.__new__(app.AISolver)
        solver._should_stop = lambda: False
        solver.stop_requested = None
        solver.prompt_builder = SimpleNamespace(build=lambda *_: 'Fill in the blanks.')
        solver.executor = SimpleNamespace(
            _strip_given_prefix=lambda _, answer: answer,
            _fill_text_input_verified=lambda *_: True,
        )
        solver.ai_client = Mock()
        solver.ai_client.ask.side_effect = [
            ' '.join(f'{i}.answer{i}' for i in range(1, 9)),
            '9.answer1',
        ]
        question = SimpleNamespace(
            number=1, text='Nine ordinary fill-in blanks.', q_type='FILL_IN',
            element=None, inputs=list(range(9)), banked_options=list(options),
            directions=directions,
            banked_blanks=[{'index': i, 'context': 'sentence', 'hint': ''}
                           for i in range(9)],
        )
        self.assertTrue(solver._answer_chunked_fill(question))
        return solver.ai_client.ask.call_args_list[1].args[0]

    def test_ordinary_fill_allows_repeated_answers(self):
        self.assertNotIn('本批不得重复使用', self.second_chunk_prompt())

    def test_word_bank_without_once_rule_allows_repeated_answers(self):
        prompt = self.second_chunk_prompt(
            directions='Fill in the blanks with the words below. Words may be repeated.',
            options=['answer1', 'answer2'],
        )
        self.assertNotIn('本批不得重复使用', prompt)

    def test_explicit_once_rule_includes_used_answers(self):
        prompt = self.second_chunk_prompt(
            directions='Each word can be used only once.',
            options=[f'answer{i}' for i in range(1, 10)],
        )
        self.assertIn('本批不得重复使用', prompt)
        self.assertIn('answer1；answer2', prompt)

    def test_numbered_multiword_answers_are_preserved(self):
        self.assertEqual(
            app.AISolver._parse_numbered_map('1.on average 2.come across'),
            {1: 'on average', 2: 'come across'},
        )

    def test_blank_lines_do_not_shift_unnumbered_answers(self):
        self.assertEqual(
            app.AISolver._parse_numbered_map('\non average\n\ncome across\nfigure out\n'),
            {1: 'on average', 2: 'come across', 3: 'figure out'},
        )

    def test_numbered_answers_keep_their_original_numbers(self):
        self.assertEqual(
            app.AISolver._parse_numbered_map('9.on average\n\n10.come across'),
            {9: 'on average', 10: 'come across'},
        )

    def test_expression_matching_preserves_sentence_case(self):
        executor = object.__new__(app.AnswerExecutor)
        self.assertEqual(executor._match_to_option(
            'At the heart of', ['at the heart of'], True, allow_rewrite=False),
            'At the heart of')

    def test_expression_matching_does_not_restore_base_form(self):
        executor = object.__new__(app.AnswerExecutor)
        self.assertIsNone(executor._match_to_option(
            'equipped with', ['be equipped with'], True, allow_rewrite=False))


if __name__ == '__main__':
    unittest.main()
