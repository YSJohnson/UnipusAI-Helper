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

    def test_solver_shares_stop_event_with_both_executors(self):
        config = Mock(tts_engine='off', tts_voice='', tts_proxy='')
        with patch.object(app, 'OpenAICompatibleClient'), patch.object(app, 'VideoHandler'):
            solver = app.AISolver(Mock(), config)
        self.assertIs(solver.executor.stop_requested, solver.stop_requested)
        self.assertIs(solver.publish_executor.stop_requested, solver.stop_requested)
        solver.request_stop()
        self.assertTrue(solver.executor.stop_requested.is_set())

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

    def vocabulary(self, index=1):
        self.load('''<div class="vocabulary-wrapper">
          <div class="progressBar"><div class="ratio"><span>1</span><span>/3</span></div></div>
          <div class="vocContainer"><div class="swiper-container">
            <div class="swiper-slide swiper-vocabulary-slide swiper-slide-active"></div>
          </div></div></div>''')
        self.driver.execute_script(r'''
            const words = ['cognitive', 'census', 'a thirst for sth.'];
            const slide = document.querySelector('.swiper-slide-active');
            const progress = document.querySelector('.ratio span');
            window.starts = []; window.stops = []; window.uploads = [];
            window.premature = false; let current = arguments[0] - 1;
            const button = recording => '<span class="record-fill-icon button-record" ' +
                'style="display:inline-block;width:24px;height:24px"><svg>' +
                (recording ? '<rect fill="currentColor"/>' : '<rect opacity="0"/>') +
                '</svg></span>';
            const render = () => {
                slide.innerHTML = '<div class="vocWord"><div class="component-htmlview">' +
                    words[current] + '</div></div><div class="soundWrap">' +
                    '<div class="question-audio"><audio preload="none" src="' + arguments[1] +
                    '"></audio></div></div><span class="practice-score"><span class="score">100</span></span>' +
                    '<span class="practice-recorder"><div class="ucomp-recorder">' + button(false) +
                    '</div><audio preload="none" src="https://example.invalid/old-' + current + '.mp3">' +
                    '</audio></span>';
            };
            const show = (n, lazy) => {
                current = n; progress.textContent = current + 1;
                if (lazy) { slide.innerHTML = 'vocabulary card'; setTimeout(render, 50); }
                else { render(); }
            };
            slide.addEventListener('click', event => {
                const btn = event.target.closest('.button-record');
                if (!btn) { return; }
                const recording = !!btn.querySelector('rect[fill="currentColor"]');
                slide.querySelector('.ucomp-recorder').innerHTML = button(!recording);
                if (!recording) { window.starts.push(current + 1); return; }
                window.stops.push(current + 1);
                const card = current + 1;
                const audio = slide.querySelector('.practice-recorder audio');
                audio.src = 'blob:pending';
                setTimeout(() => {
                    audio.src = 'https://example.invalid/new-' + card + '.mp3';
                    window.uploads.push(card);
                }, 350);
            });
            document.querySelector('.swiper-container').swiper = {
                slideTo: n => show(n, true),
                slideNext: () => {
                    if (!window.uploads.includes(current + 1)) { window.premature = true; }
                    show(Math.min(current + 1, 2), true);
                }
            };
            show(current, false);
        ''', index, self.sample)
        questions, _ = app.QuestionParser(self.driver).parse_all()
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].q_type, app.QuestionType.RECORD)
        self.assertFalse(questions[0].needs_speech_script)
        return questions[0]

    def test_vocabulary_distinguishes_original_from_historical_replay(self):
        q = self.vocabulary()
        info = self.driver.execute_script(app.VOCAB_CARD_JS, q.element)
        self.assertEqual(info['audio'], self.sample)
        self.assertEqual(info['recordings'], ['https://example.invalid/old-0.mp3'])
        self.assertFalse(info['recording'])
        baseline = dict(info)
        executor = app.AnswerExecutor(self.driver)
        self.assertFalse(executor._wait_vocab_upload(q, baseline, timeout=0.25))

    def test_vocabulary_rewinds_and_waits_for_each_upload_before_flipping(self):
        q = self.vocabulary(index=3)
        executor = app.AnswerExecutor(self.driver)
        with patch.object(executor, '_download_record_audio', return_value=b'audio'), patch.object(
                self.driver, 'execute_async_script', return_value={'ok': True, 'dur': 0.01}):
            self.assertTrue(executor._answer_record(q))
        state = self.driver.execute_script('''return {
            starts: window.starts, stops: window.stops,
            uploads: window.uploads, premature: window.premature
        };''')
        self.assertEqual(state, {'starts': [1, 2, 3], 'stops': [1, 2, 3],
                                 'uploads': [1, 2, 3], 'premature': False})

    def test_vocabulary_cancellation_stops_the_current_recording(self):
        q = self.vocabulary()
        stop = threading.Event()
        executor = app.AnswerExecutor(self.driver, stop_requested=stop)
        click = app.WebDriverHelper.safe_click
        def click_then_cancel(driver, button):
            result = click(driver, button)
            stop.set()
            return result
        with patch.object(executor, '_download_record_audio', return_value=b'audio'), patch.object(
                self.driver, 'execute_async_script', return_value={'ok': True, 'dur': 0.01}), patch.object(
                app.WebDriverHelper, 'safe_click', side_effect=click_then_cancel):
            self.assertFalse(executor._answer_record(q))
        info = self.driver.execute_script(app.VOCAB_CARD_JS, q.element)
        self.assertFalse(info['recording'])
        self.assertEqual(self.driver.execute_script('return [window.starts, window.stops]'),
                         [[1], [1]])


if __name__ == '__main__':
    unittest.main()
