"""Issue #41: audio playback and paragraph read-aloud, with offline Edge fixtures.

Run: .venv/Scripts/python.exe -m unittest discover -s tests -p issue41_audio_test.py -v
"""
import base64
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.append(str(Path(__file__).resolve().parents[1]))
import UnipusAI_Helper as app


class AudioLogicTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_only_free_speech_requires_ai(self):
        for item, expected in [({'url': 'sample.mp3'}, False),
                               ({'text': 'Read this exact paragraph.'}, False),
                               ({}, True)]:
            q = app.Question(1, 'Prompt', app.QuestionType.RECORD, None,
                             record_items=[item])
            self.assertEqual(q.needs_speech_script, expected)

    def test_given_text_takes_priority_over_model_answer(self):
        q = app.Question(1, 'Instructions', app.QuestionType.RECORD, None,
                         record_items=[{'text': 'The actual paragraph.'}, {}])
        texts = app.AnswerExecutor(Mock())._record_speech_texts(q, 'Model one. Model two.')
        self.assertEqual(texts, ['The actual paragraph.', 'Model two.'])

    def test_partial_recording_is_failure(self):
        executor = app.AnswerExecutor(Mock())
        executor.driver.execute_async_script.return_value = {'ok': True}
        executor._dismiss_record_popup = Mock()
        executor._record_one_item = Mock()
        executor._row_done = lambda q, i: i == 0
        q = app.Question(1, 'Prompt', app.QuestionType.RECORD, None,
                         record_items=[{'url': 'a'}, {'url': 'b'}])
        self.assertFalse(executor._answer_record(q))

    def test_preprocessing_skips_audio_only_page(self):
        solver = object.__new__(app.AISolver)
        solver.driver = Mock()
        solver.stop_requested = threading.Event()
        solver._has_video_on_page = Mock(return_value=False)
        solver._has_audio_on_page = Mock()
        with patch.object(app, 'find_record_buttons', return_value=[]), patch.object(
                app.AudioStrategy, 'is_audio_only_page', return_value=True):
            solver._preprocess_audio_if_needed('Practice-1', 0, 0)
        solver._has_audio_on_page.assert_not_called()

    def run_audio(self, states, stop=None):
        driver = Mock()
        audio = object()
        element = Mock()
        element.find_elements.return_value = [audio]
        driver.execute_script.side_effect = [None, *states, None]
        handler = app.AudioHandler(driver, stop or threading.Event())
        q = app.Question(1, '', app.QuestionType.AUDIO, element)
        return handler.handle(q), driver

    def test_three_seconds_does_not_count_as_complete(self):
        result, driver = self.run_audio([
            {'time': 3, 'ended': False}, {'time': 5, 'ended': True}])
        self.assertTrue(result)
        self.assertEqual(driver.execute_script.call_count, 4)

    def test_playback_error_is_failure_and_pauses(self):
        result, driver = self.run_audio([{'time': 0, 'error': True}])
        self.assertFalse(result)
        self.assertIn('pause()', driver.execute_script.call_args.args[0])

    def test_stalled_playback_is_failure(self):
        with patch.object(app.time, 'monotonic', side_effect=[0, 31]):
            result, _ = self.run_audio([{'time': 0, 'ended': False}])
        self.assertFalse(result)

    def test_stop_pauses_playback(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        result, driver = self.run_audio([{'time': 1, 'ended': False}], stop)
        self.assertFalse(result)
        self.assertIn('pause()', driver.execute_script.call_args.args[0])

    def test_failed_audio_remains_retryable(self):
        solver = object.__new__(app.AISolver)
        solver.stop_requested = threading.Event()
        solver.processed_hashes = set()
        solver._is_publish_page = Mock(return_value=False)
        solver._generate_content_hash_from_direction = Mock(return_value='audio')
        solver._preprocess_video_if_needed = Mock()
        solver._preprocess_audio_if_needed = Mock()
        solver._harvest_vocabulary = Mock()
        solver._needs_vocabulary = Mock(return_value=False)
        solver._generate_questions_signature = Mock(return_value='audio')
        q = app.Question(1, '', app.QuestionType.AUDIO, Mock())
        solver.parser = Mock()
        solver.parser.parse_all.return_value = ([q], '')
        handler = Mock()
        handler.handle.return_value = False
        solver.content_handlers = [handler]
        solver.executor = Mock()
        self.assertFalse(solver._process_current_tab_content('chapter', 'task', 0, 0))
        self.assertFalse(solver.processed_hashes)
        solver.executor.submit.assert_not_called()


class AudioBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='issue41_')
        options = app.webdriver.EdgeOptions()
        for arg in app.UCampusBot.COMPAT_EDGE_ARGS + app.UCampusBot.MIC_EDGE_ARGS:
            options.add_argument(arg)
        options.add_argument('--headless=new')
        options.add_argument('--user-data-dir=' + cls.work.name + '/profile')
        cls.driver = app.webdriver.Edge(options=options)
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(8000)
            clip.writeframes(b'\0\0' * 1600)
        cls.sample = 'data:audio/wav;base64,' + base64.b64encode(wav.getvalue()).decode()

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.work.cleanup()

    def load(self, content):
        path = Path(self.work.name) / 'fixture.html'
        path.write_text('<div class="layout-direction-container">Listen carefully.</div>'
                        '<div class="layoutBody-container">' + content + '</div>', encoding='utf-8')
        self.driver.get(path.as_uri())

    def paragraph(self, sample=True, replay=''):
        self.load('<div class="oral-paragraph-container">'
                  '<div class="paragraphPlayerContainer"><audio src="' +
                  (self.sample if sample else '') + '"></audio></div>'
                  '<div class="sentence-container"><span class="score_layout"></span>'
                  '<div class="sentence-html-container">Travel <u>and</u> (w) discovery '
                  'help us grow. Life is <u>but</u> (s)a dream.</div></div>'
                  '<div class="record-button-group">'
                  '<div class="record-button-wrap"><span class="record-icon button-record">'
                  'Record</span></div><div class="record-button-wrap">'
                  '<div class="question-audio audio-replay"><audio src="' + replay + '"></audio>'
                  '</div></div></div></div>')
        questions, _ = app.QuestionParser(self.driver).parse_all()
        self.assertEqual(len(questions), 1)
        return questions[0]

    def test_pure_audio_parses_and_plays_to_end(self):
        self.load('<div class="audio-material-wrapper"><button>Play</button><audio src="' +
                  self.sample + '"></audio></div>')
        questions, _ = app.QuestionParser(self.driver).parse_all()
        self.assertEqual([q.q_type for q in questions], [app.QuestionType.AUDIO])
        self.assertTrue(app.AudioStrategy.is_audio_only_page(self.driver))
        self.assertTrue(app.AudioHandler(self.driver, threading.Event()).handle(questions[0]))
        self.assertTrue(self.driver.execute_script('return document.querySelector("audio").ended'))

    def test_listening_questions_still_need_transcription(self):
        self.load('<audio src="' + self.sample + '"></audio><input type="text">')
        self.assertFalse(app.AudioStrategy.is_audio_only_page(self.driver))

    def test_paragraph_sample_is_not_mistaken_for_uploaded_recording(self):
        q = self.paragraph()
        self.assertEqual(q.q_type, app.QuestionType.RECORD)
        self.assertEqual(q.record_items[0]['url'], self.sample)
        self.assertFalse(q.needs_speech_script)
        state = app.AnswerExecutor(self.driver)._row_state(q, 0)
        self.assertFalse(state['recorded'])
        self.assertIsNone(state['score'])
        self.assertEqual(state['uploaded'], 0)

    def test_uploaded_replay_is_detected_and_never_reused_as_sample(self):
        q = self.paragraph(replay='https://example.invalid/recording.mp3')
        self.assertEqual(q.record_items[0]['url'], self.sample)
        state = app.AnswerExecutor(self.driver)._row_state(q, 0)
        self.assertTrue(state['recorded'])
        self.assertEqual(state['uploaded'], 1)

    def test_no_sample_uses_exact_paragraph_without_pronunciation_marks(self):
        q = self.paragraph(sample=False, replay='https://example.invalid/recording.mp3')
        self.assertEqual(q.record_items[0]['url'], '')
        self.assertFalse(q.needs_speech_script)
        self.assertEqual(app.AnswerExecutor(self.driver)._record_speech_texts(q, ''),
                         ['Travel and discovery help us grow. Life is but a dream.'])

    def test_sentence_samples_align_with_buttons_not_replay_wrappers(self):
        self.load(''.join(
            '<div class="oral-study-sentence"><div class="record-button-group">'
            '<div class="record-button-wrap"><audio class="question-audio audio-origin" '
            f'src="https://example.invalid/{i}.mp3"></audio>'
            '<span class="record-icon button-record">Record</span></div>'
            '<div class="record-button-wrap"><div class="audio-replay">'
            '<audio src="https://example.invalid/replay.mp3"></audio></div></div></div></div>'
            for i in range(2)))
        root = self.driver.find_element(app.By.CSS_SELECTOR, '.layoutBody-container')
        q = app.RecordStrategy().parse(root, self.driver, 1)
        self.assertEqual([item['url'] for item in q.record_items],
                         ['https://example.invalid/0.mp3', 'https://example.invalid/1.mp3'])


if __name__ == '__main__':
    unittest.main()
