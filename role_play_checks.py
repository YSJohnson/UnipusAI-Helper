import threading
import unittest
from unittest.mock import MagicMock, patch

import UnipusAI_Helper as app


class RolePlayTests(unittest.TestCase):
    def test_parser_recognizes_role_play_without_speech_generation(self):
        container = MagicMock()
        container.find_elements.return_value = [MagicMock()]
        strategy = app.RolePlayStrategy()
        self.assertTrue(strategy.can_parse(container, None))
        question = strategy.parse(container, None, 1, 'Role-play')
        self.assertEqual(question.q_type, app.QuestionType.RECORD)
        self.assertFalse(question.needs_speech_script)
        self.assertTrue(question.record_items[0]['role_play'])

    @patch.object(app, 'WebDriverWait')
    @patch.object(app.WebDriverHelper, 'safe_click', return_value=True)
    def test_role_play_waits_for_all_scores(self, click, wait):
        driver = MagicMock()
        driver.execute_async_script.return_value = True
        driver.execute_script.side_effect = [
            {'lines': [{'url': 'https://example.com/1.mp3'}], 'seconds': 2},
            {'left': 0, 'scores': ['94'], 'finished': False},
            {'left': 0, 'scores': ['94'], 'finished': True},
            None,
        ]
        executor = app.AnswerExecutor(driver)
        executor._dismiss_record_popup = MagicMock()
        executor._download_record_audio = MagicMock(return_value=b'audio')
        executor.stop_requested = MagicMock(spec=threading.Event)
        executor.stop_requested.is_set.return_value = False
        question = MagicMock()
        question.element.find_elements.return_value = [MagicMock()]
        self.assertTrue(executor._answer_role_play(question))
        self.assertEqual(driver.execute_script.call_count, 4)
        self.assertEqual(click.call_count, 2)

    @patch.object(app, 'WebDriverWait')
    @patch.object(app.WebDriverHelper, 'safe_click', return_value=True)
    def test_missing_reference_audio_does_not_start_recording(self, click, wait):
        driver = MagicMock()
        driver.execute_script.return_value = {'lines': [{'url': ''}], 'seconds': 1}
        executor = app.AnswerExecutor(driver)
        executor._dismiss_record_popup = MagicMock()
        executor._download_record_audio = MagicMock(return_value=None)
        question = MagicMock()
        question.element.find_elements.return_value = [MagicMock()]
        self.assertFalse(executor._answer_role_play(question))
        self.assertEqual(click.call_count, 1)
        driver.execute_async_script.assert_not_called()


if __name__ == '__main__':
    unittest.main()
