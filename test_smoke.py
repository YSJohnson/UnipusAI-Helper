import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import UnipusAI_Helper as app


def test_auth_token_formats_are_kept_separate():
    jwt = "header.payload.signature"
    legacy = {
        "rt": "refresh",
        "jwt": jwt,
        "rtExpire": 1,
        "jwtExpire": 2,
        "effectiveTime": 3,
        "links": {},
    }

    legacy_json, parsed_jwt = app._parse_auth_token(json.dumps(legacy))
    assert json.loads(legacy_json) == legacy
    assert parsed_jwt == jwt

    assert app._parse_auth_token(f"Bearer {jwt}") == (None, jwt)
    assert app._parse_auth_token(json.dumps(jwt)) == (None, jwt)
    assert app._parse_auth_token(json.dumps(json.dumps(legacy))) == (
        json.dumps(legacy, separators=(",", ":")), jwt)
    assert app._parse_auth_token(json.dumps({"Authorization": jwt})) == (None, jwt)
    assert app._parse_auth_token("not-a-token") == (None, None)



def test_cookie_is_live_auth_signal_without_configured_token():
    driver = Mock()
    driver.get_cookie.return_value = {"value": "header.payload.signature"}
    driver.execute_script.return_value = False

    assert app.UCampusBot._has_live_auth(driver) is True
    driver.execute_script.assert_not_called()


def test_custom_portal_url_falls_back_to_sso():
    driver = Mock(current_url="https://ucloud.unipus.cn/")

    def navigate(url):
        driver.current_url = url

    driver.get.side_effect = navigate
    username = Mock()
    password = Mock()
    agreement = Mock()
    agreement.is_selected.return_value = False
    agreement_control = Mock()
    login_button = Mock()
    driver.find_element.return_value = agreement

    wait = Mock()
    wait.until.side_effect = [
        app.TimeoutException(),
        app.TimeoutException(),
        username,
        password,
        agreement_control,
        login_button,
        True,
        app.TimeoutException(),
        app.TimeoutException(),
    ]

    bot = app.UCampusBot.__new__(app.UCampusBot)
    bot.driver = driver
    bot.config = SimpleNamespace(
        url="https://ucloud.unipus.cn/",
        username="user",
        password="password",
    )
    bot.anti_anti_cheat = Mock()

    with patch.object(app, "WebDriverWait", return_value=wait):
        assert bot._login() is True

    assert driver.get.call_args_list[-1].args[0] == app.DEFAULT_LOGIN_URL
    username.send_keys.assert_called_once_with("user")
    password.send_keys.assert_called_once_with("password")
    agreement_control.click.assert_called_once()
    bot.anti_anti_cheat.assert_called_once()


def test_answer_executor_returns_bool():
    safe_click = app.WebDriverHelper.safe_click
    app.WebDriverHelper.safe_click = staticmethod(lambda *_: True)
    try:
        question = SimpleNamespace(
            number=1,
            options=[app.Option("A", "alpha", object())],
        )
        executor = app.AnswerExecutor(None)
        assert executor._fill_single_choice(question, "A") is True
        assert executor._fill_single_choice(question, "Z") is False
    finally:
        app.WebDriverHelper.safe_click = safe_click


def test_selected_duplicate_task_uses_scanned_occurrence():
    unit = Mock()
    names = [SimpleNamespace(text=name) for name in ("Vocabulary", "Reading", "Vocabulary", "Vocabulary")]
    chapters = [Mock() for _ in names]
    for chapter, name in zip(chapters, names):
        chapter.find_element.return_value = name

    driver = Mock(current_url="https://example.test/course")
    driver.find_elements.return_value = chapters
    unit_container = Mock()
    unit_container.find_elements.return_value = [unit]
    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = driver
    solver.ai_client = Mock()
    solver.processed_hashes = set()
    solver._should_stop = Mock(return_value=False)
    solver.stop_requested = Mock()
    solver.stop_requested.wait.return_value = False
    solver._process_tab_with_accumulation = Mock()
    tab = {"_element": names[0], "_unit_idx": 0, "_name_occurrence": 2,
           "l1_title": "Vocabulary", "display": "Vocabulary #3"}

    with patch.object(app, "WebDriverWait") as wait, patch.object(app.time, "sleep"):
        wait.return_value.until.return_value = unit_container
        solver.process_selected_tabs([tab])
        assert driver.execute_script.call_args_list[-1].args[1] is names[3]
        solver._process_tab_with_accumulation.assert_called_once()

        driver.execute_script.reset_mock()
        solver._process_tab_with_accumulation.reset_mock()
        solver.process_selected_tabs([{**tab, "_name_occurrence": 3}])
        assert driver.execute_script.call_count == 1  # Only the Unit was clicked.
        solver._process_tab_with_accumulation.assert_not_called()

def test_stop_after_ai_response_skips_answer_and_submit():
    solver = app.AISolver.__new__(app.AISolver)
    solver.stop_requested = Event()
    solver.processed_hashes = set()
    solver._generate_content_hash_from_direction = Mock(return_value="page")
    solver._preprocess_video_if_needed = Mock()
    solver._preprocess_audio_if_needed = Mock()
    solver.parser = Mock()
    question = SimpleNamespace(q_type=app.QuestionType.SINGLE_CHOICE, number=1)
    solver.parser.parse_all.return_value = ([question], "")
    solver._generate_questions_signature = Mock(return_value="questions")
    solver.content_handlers = []
    solver.prompt_builder = Mock()
    solver.ai_client = Mock()
    solver.ai_client.ask.side_effect = lambda *_, **__: (solver.request_stop(), "1. A")[1]
    solver.executor = Mock()

    assert solver._process_current_tab_content("chapter", "task", 0, 0) is False
    solver.executor.execute.assert_not_called()
    solver.executor.submit.assert_not_called()


def test_stop_interrupts_flashcard_and_video_waits():
    stop = Mock()
    stop.wait.side_effect = [False, True]
    stop.is_set.return_value = False
    button = Mock()
    button.is_displayed.return_value = True
    button.is_enabled.return_value = True
    driver = Mock()
    driver.find_element.side_effect = Exception("no disabled button")
    cards = app.FlashcardHandler(driver, stop)
    cards._find_next_button = Mock(return_value=button)
    assert cards.handle(None) is False
    button.click.assert_not_called()

    check_stop = Event()
    check_driver = Mock()
    check_question = SimpleNamespace(element=Mock())
    check_question.element.find_elements.side_effect = lambda *_: (check_stop.set(), [Mock()])[1]
    assert app.SelfCheckHandler(check_driver, check_stop).handle(check_question) is False
    check_driver.execute_script.assert_not_called()

    video_stop = Event()
    video_driver = Mock()
    video_driver.execute_script.side_effect = lambda script, *_: video_stop.set() if 'play()' in script else None
    video = app.VideoHandler.__new__(app.VideoHandler)
    video.driver = video_driver
    video.stop_requested = video_stop
    video._play_video(0)
    assert video_stop.is_set()
    assert any('pause()' in call.args[0] for call in video_driver.execute_script.call_args_list)


if __name__ == "__main__":
    test_auth_token_formats_are_kept_separate()
    test_cookie_is_live_auth_signal_without_configured_token()
    test_custom_portal_url_falls_back_to_sso()
    test_answer_executor_returns_bool()
    test_selected_duplicate_task_uses_scanned_occurrence()
    test_stop_after_ai_response_skips_answer_and_submit()
    test_stop_interrupts_flashcard_and_video_waits()
