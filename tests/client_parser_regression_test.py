"""Regressions for exhausted reasoning budgets and link matching dispatch.

Run: .venv/Scripts/python.exe -m unittest discover -s tests -p client_parser_regression_test.py -v
"""
import contextlib
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.append(str(Path(__file__).resolve().parents[1]))
import UnipusAI_Helper as app


def response(content='', reason='length'):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=reason)])


class ClientRetryTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def client(self, responses, max_tokens=8192):
        client = object.__new__(app.OpenAICompatibleClient)
        client.config = SimpleNamespace(base_url='', model='fake', temperature=0,
                                        max_tokens=max_tokens)
        client.conversation_history = []
        client.material_messages = [{'role': 'user', 'content': 'Reading material.'}]
        completions = Mock()
        completions.create.side_effect = responses
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return client, completions.create

    def test_empty_length_retries_at_budget_floor_and_above(self):
        for configured, expected in [(8192, 32768), (32768, 32768), (65536, 65536)]:
            with self.subTest(max_tokens=configured):
                client, create = self.client([response(), response('1.A', 'stop')], configured)
                self.assertEqual(client.ask('Original question'), '1.A')
                self.assertEqual([call.kwargs['max_tokens'] for call in create.call_args_list],
                                 [expected, expected])
                messages = create.call_args_list[-1].kwargs['messages']
                self.assertIn('Original question', messages[-1]['content'])
                self.assertIn('请直接给出最终答案', messages[-1]['content'])
                self.assertIn(client.material_messages[0], messages)
                self.assertEqual(client.conversation_history, [
                    {'role': 'user', 'content': 'Original question'},
                    {'role': 'assistant', 'content': '1.A'},
                ])

    def test_empty_length_retries_are_bounded_and_do_not_pollute_history(self):
        client, create = self.client([response()] * 3)
        self.assertIsNone(client.ask('Question', retry_count=3))
        self.assertEqual(create.call_count, 3)
        self.assertEqual(client.conversation_history, [])

    def test_single_attempt_does_not_make_extra_requests(self):
        client, create = self.client([response()])
        self.assertIsNone(client.ask('Question', retry_count=1))
        self.assertEqual(create.call_count, 1)

    def test_cancellation_after_empty_response_prevents_retry(self):
        stop = threading.Event()

        def cancel(**kwargs):
            stop.set()
            return response()

        client, create = self.client(cancel)
        self.assertIsNone(client.ask('Question', stop_requested=stop))
        self.assertEqual(create.call_count, 1)

    def test_empty_non_length_response_is_not_retried(self):
        for reason in ('stop', 'content_filter'):
            with self.subTest(reason=reason):
                client, create = self.client([response(None, reason)])
                self.assertIsNone(client.ask('Question'))
                self.assertEqual(create.call_count, 1)

    def test_gateway_token_limit_still_reduces_budget(self):
        client, create = self.client([
            ValueError('max_tokens is too large'), response('1.A', 'stop')])
        self.assertEqual(client.ask('Question'), '1.A')
        self.assertEqual([call.kwargs['max_tokens'] for call in create.call_args_list],
                         [32768, 16384])


class LinkMatchingTests(unittest.TestCase):
    def test_supported_and_unsupported_group_kinds(self):
        parser = app.PublishQuestionParser(Mock())
        parser._question_nodes = Mock(return_value=[])
        for group_type in ('oral_personal_state_group', 'oral_sentence_scoop_group'):
            self.assertEqual(parser.classify_group(group_type), 'record')
        for group_type in ('evaluation_group', 'ai_oral_practice_group'):
            self.assertEqual(parser.classify_group(group_type), 'unknown')
            self.assertEqual(parser.parse_group(Mock(), group_type), [])
            self.assertIn(group_type, parser.unsupported)

    def question(self):
        parser = app.PublishQuestionParser(Mock())
        parser._link_nodes = Mock(return_value=[
            {'id': 'prompt', 'side': 'right', 'text': '1. Paragraphs 1-2'},
            {'id': 'option', 'side': 'left', 'text': 'A. Introduction'},
        ])
        parser.group_direction = Mock(return_value='Match each paragraph to its heading.')
        group = Mock()
        self.assertEqual(parser.classify_group('text_match_group'), 'link_match')
        questions = parser.parse_group(group, 'text_match_group')
        self.assertEqual(len(questions), 1)
        self.assertEqual(parser.unsupported, [])
        return questions[0]

    def test_classification_enters_link_parser(self):
        question = self.question()
        self.assertEqual(question.q_type, app.QuestionType.LINK_MATCH)
        self.assertEqual(question.text, '1. Paragraphs 1-2')
        self.assertEqual([(option.letter, option.text) for option in question.options],
                         [('A', 'Introduction')])

    def test_link_question_reaches_link_executor(self):
        question = self.question()
        executor = app.PublishAnswerExecutor(Mock())
        executor._fill_link_match = Mock(return_value=True)
        self.assertTrue(executor.execute(question, '1.A'))
        executor._fill_link_match.assert_called_once_with(question, '1.A')


if __name__ == '__main__':
    unittest.main()
