from types import SimpleNamespace

import UnipusAI_Helper as app


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


if __name__ == "__main__":
    test_answer_executor_returns_bool()
