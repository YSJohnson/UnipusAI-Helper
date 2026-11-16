import threading
import unittest
from unittest.mock import MagicMock

import UnipusAI_Helper as app


class SortingAnswerTests(unittest.TestCase):
    def setUp(self):
        self.executor = object.__new__(app.AnswerExecutor)
        self.solver = object.__new__(app.AISolver)

    def test_single_sort_with_question_number(self):
        result = self.executor._parse_sorting_order('1. B E A D C F', list('ABCDEF'))
        self.assertEqual(result, list('BEADCF'))

    def test_numbered_positions_still_work(self):
        result = self.executor._parse_sorting_order('1.B 2.C 3.A', list('ABC'))
        self.assertEqual(result, list('BCA'))

    def test_mixed_answers_extract_sort_question(self):
        response = '1.A 2.B 3.C 4.D 5. D C B A'
        answer = self.solver._extract_single_answer(response, 5)
        self.assertEqual(self.executor._parse_sorting_order(answer, list('ABCD')), list('DCBA'))

    def test_repair_extracts_sort_question(self):
        self.solver.stop_requested = threading.Event()
        self.solver.prompt_builder = MagicMock()
        self.solver.prompt_builder.build.return_value = 'prompt'
        self.solver.ai_client = MagicMock()
        self.solver.ai_client.ask.return_value = '1.A 2. D C B A'
        self.solver.executor = MagicMock()
        self.solver.executor.execute.return_value = True
        choice = MagicMock(q_type=app.QuestionType.SINGLE_CHOICE, number=1)
        sorting = MagicMock(q_type=app.QuestionType.SORTING, number=2)
        self.assertEqual(self.solver._repair_questions([choice, sorting]), 2)
        self.solver.executor.execute.assert_any_call(sorting, 'D C B A')


if __name__ == '__main__':
    unittest.main()
