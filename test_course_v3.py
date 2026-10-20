"""course-v3（新版 ipublish 播放器）适配测试。

纯逻辑测试用 Mock，页面结构测试用本机 Edge 无头浏览器 + 离线 fixture
（fixture 的 DOM 结构取自 issue #23 投稿的 13 份 HTML 抓取件）。

运行方式：
    .venv\\Scripts\\python.exe test_course_v3.py
"""
import html
import os
import re
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import UnipusAI_Helper as app


# ==========================================================================
# 纯逻辑测试
# ==========================================================================

def test_group_classification():
    classify = app.PublishQuestionParser.classify_group
    assert classify('single_choice_group') == 'choice'
    assert classify('true_false_group') == 'true_false'
    assert classify('fill_blanks_group') == 'fill'
    assert classify('fill_blanks_choice_group') == 'fill'
    assert classify('fill_blanks_dropdown_group') == 'dropdown'
    assert classify('short_answer_group') == 'text'
    assert classify('translation_group') == 'text'
    assert classify('writing_group') == 'text'
    assert classify('multi_media_upload_group') == 'upload'
    assert classify('rich_text_read_group') == 'skip'
    assert classify('oral_personal_state_group') == 'skip'
    assert classify('discussion_group') == 'skip'
    assert classify('text_match_group') == 'skip'
    assert classify('unknown_group') == 'unknown'
    assert app.PublishQuestionParser.classify_group(None) == 'unknown'


def test_true_false_normalization():
    normalize = app.PublishAnswerExecutor.normalize_true_false
    assert normalize('T') == 'T'
    assert normalize('t') == 'T'
    assert normalize('True') == 'T'
    assert normalize('1. T') == 'T'
    assert normalize('F') == 'F'
    assert normalize('FALSE') == 'F'
    assert normalize('2. False') == 'F'
    assert normalize('正确') == 'T'
    assert normalize('错误') == 'F'
    assert normalize('Because it is cheap') == ''
    assert normalize('') == ''


def test_prompt_builder_true_false_and_banked_options():
    client = FakeAIClient()
    builder = app.PromptBuilder(client)

    tf = app.Question(1, 'The model grew quickly.', app.QuestionType.TRUE_FALSE, None,
                      options=[app.Option('T', 'True', 'T'), app.Option('F', 'False', 'F')])
    prompt = builder.build([tf], '')
    assert '【判断】' in prompt
    assert '判断题: 每题回答 T 或 F' in prompt

    fill = app.Question(2, 'Plans to renovate an abandoned (1) in the old district.',
                        app.QuestionType.FILL_IN, None, inputs=[object()],
                        banked_options=['cannery', 'farmhouse rental'])
    prompt = builder.build([fill], '')
    assert '可选词: cannery, farmhouse rental' in prompt
    assert '只能从可选词中选择' in prompt
    assert '填空题: 1.答案1 2.答案2' in prompt


def test_publish_answer_routing():
    solver = app.AISolver.__new__(app.AISolver)
    choice = app.Question(1, 'stem', app.QuestionType.SINGLE_CHOICE, None)
    assert solver._publish_answer_text(choice, '1. A\n2. B') == 'A'

    tf = app.Question(1, 'stem', app.QuestionType.TRUE_FALSE, None)
    assert solver._publish_answer_text(tf, '1. T') == 'T'

    text = app.Question(1, 'stem', app.QuestionType.TEXT, None)
    assert solver._publish_answer_text(text, '1. 一段回答') == '1. 一段回答'


def test_dropdown_options_prompt_injection():
    """下拉填空：抓到的候选列表要附加进提示词（真机验证过的关键步骤）。"""
    from types import SimpleNamespace

    solver = app.AISolver.__new__(app.AISolver)
    solver.publish_executor = SimpleNamespace(
        _dropdown_blanks=lambda question: [object(), object()],
        collect_dropdown_options=lambda blank: ['phrase one', 'phrase two'],
    )
    question = app.Question(1, 'The Belt and Road Initiative, (1), ...',
                            app.QuestionType.DROPDOWN_SELECT, None)
    prompt = solver._publish_dropdown_options_prompt([question])
    assert '空位候选' in prompt
    assert '空1 候选: phrase one / phrase two' in prompt
    assert '空2 候选' in prompt

    solver.publish_executor = SimpleNamespace(
        _dropdown_blanks=lambda question: [object()],
        collect_dropdown_options=lambda blank: [],
    )
    assert solver._publish_dropdown_options_prompt([question]) == ''


def test_publish_requires_every_answer_and_keyboard_write():
    executor = app.PublishAnswerExecutor(Mock())
    executor._fill_text_input_verified = Mock(return_value=True)
    inputs = [object(), object()]
    mismatch = app.Question(1, 'stem', app.QuestionType.FILL_IN, None,
                            inputs=inputs, blank_count=3)
    assert executor.execute(mismatch, '1. alpha 2. beta') is False
    executor._fill_text_input_verified.assert_not_called()
    for kind in (app.QuestionType.FILL_IN, app.QuestionType.TEXT):
        question = app.Question(1, 'stem', kind, None, inputs=inputs)
        executor._fill_text_input_verified.reset_mock()
        assert executor.execute(question, '1. alpha') is False
        executor._fill_text_input_verified.assert_not_called()
        assert executor.execute(question, '1. alpha 2. beta') is True
        executor._fill_text_input_verified.side_effect = [True, False]
        assert executor.execute(question, '1. alpha 2. beta') is False
        executor._fill_text_input_verified.side_effect = None

    question = app.Question(1, 'stem', app.QuestionType.DROPDOWN_SELECT, None)
    executor._dropdown_blanks = Mock(return_value=[object(), object()])
    executor._select_dropdown_value = Mock(return_value=True)
    assert executor.execute(question, '1. alpha') is False
    executor._select_dropdown_value.assert_not_called()
    assert executor.execute(question, '1. alpha 2. beta') is True
    executor._select_dropdown_value.side_effect = [True, False]
    assert executor.execute(question, '1. alpha 2. beta') is False

    executor = app.PublishAnswerExecutor(Mock())
    executor._type_by_keyboard = Mock(return_value=False)
    with patch.object(app.time, 'sleep'), patch.object(
            app.AnswerExecutor, '_fill_text_input_verified') as js_fallback:
        assert executor._fill_text_input_verified(Mock(), 'alpha') is False
    assert executor._type_by_keyboard.call_count == 2
    js_fallback.assert_not_called()


def test_publish_multi_question_dropdown_without_inputs():
    solver = app.AISolver.__new__(app.AISolver)
    questions = [
        app.Question(1, 'A (1), B (2).', app.QuestionType.DROPDOWN_SELECT,
                     None, blank_count=2),
        app.Question(2, 'C (1), D (2), E (3).', app.QuestionType.DROPDOWN_SELECT,
                     None, blank_count=3),
    ]
    solver.publish_executor = SimpleNamespace(
        _dropdown_blanks=lambda q: list(range(1, q.answer_count + 1)),
        collect_dropdown_options=lambda blank: [f'option{blank}'],
    )
    prompt = solver._publish_dropdown_options_prompt(questions)
    assert '空3 候选: option1' in prompt
    assert '空5 候选: option3' in prompt
    hint = solver._publish_fill_format_hint(questions)
    assert '5 个空' in hint
    assert '对应整组空号 3..5' in hint
    answer = solver._publish_answer_text(questions[1], '1.a 2.b 3.c 4.d 5.e', 2)
    assert answer == '1.c 2.d 3.e'
    assert app.AISolver._capitalize_sentence_initial_answers(
        'No blank markers here.', ['alpha', 'beta']) == ['alpha', 'beta']


def test_dropdown_requires_exact_option_and_nonempty_readback():
    driver = Mock()
    executor = app.PublishAnswerExecutor(driver)
    trigger = SimpleNamespace(text='farmhouse rental')
    shorter = SimpleNamespace(text='farmhouse', is_displayed=lambda: True)
    longer = SimpleNamespace(text='farmhouse rental', is_displayed=lambda: True)
    executor._open_dropdown = Mock(return_value=True)
    executor._visible_dropdown_popup = Mock(
        return_value=SimpleNamespace(find_elements=lambda *_: [longer, shorter]))
    executor._close_visible_dropdowns = Mock()
    executor._js_mouse_click = Mock()
    chain = Mock()
    chain.move_to_element.return_value = chain
    chain.pause.return_value = chain
    chain.click.return_value = chain
    chain.perform.side_effect = lambda: setattr(trigger, 'text', 'farmhouse')
    with patch.object(app, 'ActionChains', return_value=chain), patch.object(app.time, 'sleep'):
        assert executor._select_trigger_option(trigger, 'farmhouse') is True
        chain.move_to_element.assert_called_with(shorter)
        trigger.text = 'farmhouse rental'
        chain.perform.side_effect = lambda: setattr(trigger, 'text', '')
        assert executor._select_trigger_option(trigger, 'farmhouse') is False
        trigger.text = 'farmhouse rental'
        assert executor._select_trigger_option(trigger, 'farm') is False


def test_publish_hash_does_not_wait_for_legacy_directions():
    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = SimpleNamespace(current_url=(
        'https://ucontent.unipus.cn/_ipublishpc_default/pc.html#/course-v3:book/unit1/task1'))
    with patch.object(app.WebDriverHelper, 'safe_find_element') as legacy_find:
        first = solver._generate_content_hash_from_direction()
        assert first == solver._generate_content_hash()
        assert len(first) == 16
        solver.driver.current_url = solver.driver.current_url.replace('task1', 'task2')
        assert solver._generate_content_hash_from_direction() != first
    legacy_find.assert_not_called()


def test_ai_ask_retries_when_reasoning_exhausts_tokens():
    """推理模型把 max_tokens 全用在 reasoning 上导致空 content 时，要放宽预算重试。"""
    from types import SimpleNamespace

    class FakeResponse:
        def __init__(self, content, finish_reason):
            self.message = SimpleNamespace(content=content)
            self.finish_reason = finish_reason

    class FakeCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs.get('max_tokens'))
            if len(self.calls) == 1:
                class R1:
                    choices = [SimpleNamespace(message=SimpleNamespace(content=''),
                                               finish_reason='length')]
                return R1()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='1.A'),
                                        finish_reason='stop')])

    client = app.OpenAICompatibleClient.__new__(app.OpenAICompatibleClient)
    client.config = SimpleNamespace(base_url='', model='fake', temperature=0.0, max_tokens=8192)
    completions = FakeCompletions()
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client.conversation_history = []

    answer = client.ask('题目')
    assert answer == '1.A'
    assert completions.calls == [8192, 16384]


def test_shift_blank_answer_for_multi_question_group():
    """同组多道填空题：每道题只拿自己那段空号（真机发现的缺陷回归）。"""
    solver = app.AISolver.__new__(app.AISolver)
    answer = ('填空题: 1.Iraq 2.China 3.Mexico '
              '4.wheat and barley 5.rice and millet 6.maize')
    assert solver._shift_blank_answer(answer, 0, 3) == '1.Iraq 2.China 3.Mexico'
    assert solver._shift_blank_answer(answer, 3, 3) == \
        '1.wheat and barley 2.rice and millet 3.maize'

    fill = app.Question(1, 'stem', app.QuestionType.FILL_IN, None,
                        inputs=[object(), object(), object()])
    assert solver._publish_answer_text(fill, answer, 3) == \
        '1.wheat and barley 2.rice and millet 3.maize'
    assert solver._publish_answer_text(fill, answer, 0) == answer


def test_sanitize_fill_answer_nested_numbering():
    """AI 输出「1. (1) Iraq」这类题号+空号重复编号时要能清洗（真机发现的缺陷）。"""
    solver = app.AISolver.__new__(app.AISolver)
    assert app.AISolver._sanitize_fill_answer(
        '1. (1) Iraq 2. (2) China 3. (3) Mexico') == '1. Iraq 2. China 3. Mexico'
    assert app.AISolver._sanitize_fill_answer('maize (corn), beans and squash') == \
        'maize (corn), beans and squash'


def test_fill_answer_sentence_initial_capitalization():
    """真机：句首小写会扣分，按空位前文自动补大写。"""
    text = ("(1) is the cultivation or growth of two or more compatible plants or organisms, "
            "especially crops or fish, in a single area."
            "The (2) involves clearing wild or forested land and burning the remaining vegetation."
            "(3) simplifies farming operations by standardizing field management for a single crop."
            "The overuse of (4), such as synthetic fertilizers and pesticides, can degrade soil."
            "(5) typically consists of fast-growing pioneer species.")
    values = ['polyculture', 'slash-and-burn practice', 'monoculture',
              'agrochemicals', 'secondary vegetation']
    expected = ['Polyculture', 'slash-and-burn practice', 'Monoculture',
                'agrochemicals', 'Secondary vegetation']
    assert app.AISolver._capitalize_sentence_initial_answers(text, values) == expected

    solver = app.AISolver.__new__(app.AISolver)
    question = app.Question(1, text, app.QuestionType.FILL_IN, None,
                            inputs=[object()] * 5)
    answer = ('填空题: 1.polyculture 2.slash-and-burn practice 3.monoculture '
              '4.agrochemicals 5.secondary vegetation')
    normalized = solver._normalize_fill_answer_capitalization(question, answer)
    assert '1.Polyculture' in normalized
    assert '3.Monoculture' in normalized
    assert '5.Secondary vegetation' in normalized
    assert '4.agrochemicals' in normalized


def test_publish_player_button_text():
    assert app.PublishPlayer.button_text(SimpleNamespace(text=' 重新 作答 ')) == '重新作答'


def test_select_native_option_matches_text():
    picked = []

    def option(text):
        return SimpleNamespace(text=text, click=lambda: picked.append(text))

    options = [option('cannery'), option('farmhouse rental')]
    select = SimpleNamespace(find_elements=lambda by, tag: options)
    picker = app.PublishAnswerExecutor(None)
    assert picker._select_native_option(select, 'Cannery') is True
    assert picked == ['cannery']
    assert picker._select_native_option(select, 'nope') is False


def test_rejection_message_reads_accuracy_toast():
    """正确率不足 60% 的 toast 要能被识别（真机：提交后按钮保持「提交」）。"""

    class StubDriver:
        def __init__(self):
            self.calls = 0

        def execute_script(self, script, *args):
            self.calls += 1
            if self.calls == 1:
                return ['当前作答客观题正确率未达到60%，请检查作答内容']
            return []

        class switch_to:
            @staticmethod
            def default_content():
                pass

    executor = app.PublishAnswerExecutor(StubDriver())
    message = executor.rejection_message()
    assert '正确率' in message and '60%' in message

    class CleanDriver(StubDriver):
        def execute_script(self, script, *args):
            return []

    assert app.PublishAnswerExecutor(CleanDriver()).rejection_message() == ''


# ==========================================================================
# 页面结构测试（离线 fixture + 无头 Edge）
# ==========================================================================

class BrowserUnavailable(RuntimeError):
    pass


class FakeAIClient:
    """按 prompt 里的题型标签给出格式正确的假答案，避免真实网络调用。"""

    def __init__(self):
        self.accumulated_passages = []
        self.prompts = []

    def add_passage_if_new(self, text):
        if text and text not in self.accumulated_passages:
            self.accumulated_passages.append(text)
            return True
        return False

    def ask(self, prompt, retry_count=3, stop_requested=None):
        self.prompts.append(prompt)
        lines = []
        for raw in prompt.splitlines():
            match = re.match(r'^(\d+)\. 【(判断|单选|多选|填空|选择填空|简答题)】', raw.strip())
            if not match:
                continue
            number, kind = int(match.group(1)), match.group(2)
            if kind == '判断':
                lines.append(f'{number}. {"T" if number % 2 else "F"}')
            elif kind in ('单选', '多选'):
                lines.append(f'{number}. {"A" if number % 2 else "B"}')
            elif kind in ('填空', '选择填空'):
                # 每个空一行，覆盖 1~6 个空的情况
                for blank in range(1, 7):
                    lines.append(f'{blank}. word{blank}')
            else:
                lines.append(f'{number}. sample answer {number}')
        return '\n'.join(lines)


def _fixture_html() -> str:
    submitted_button = '<span class="i-button_btn-text__jiMYT">重新作答</span>'
    submit_button = '<span class="i-button_btn-text__jiMYT">提交</span>'

    def group(group_id, group_type, inner, button=submit_button):
        return f"""
<div class="i-question_i-question-container__2po-e" id="{group_id}"
     data-ipublish-question-group-type="{group_type}">
  <div class="answer-container-new_answer-container__sXAuD">
    <div class="q-view-title_questionTitleView__9Yb5D">
      <div class="q-view-title_title-container__Se0Sh">
        <div class="q-view-title_title-content__AmufJ">自动判题 计分</div>
        <div class="q-view-title_title-extra__tCD8z">
          <div class="i-button_default-container__cnPj6"
               data-ipublish-event-type="question-submit" onclick="gradeGroup(this)">{button}</div>
        </div>
      </div>
    </div>
    <div class="answer-container-new_answer-question-wrapper__LEjTm">{inner}</div>
  </div>
</div>"""

    def choice_question(qid, number, label, options):
        rows = "".join(
            f'<div class="chioceView_strongListView__QYUB+">'
            f'<div class="chioceView_strongItem__iIqBK" onclick="pickOption(this)">{letter}</div>'
            f'<div class="chiosoView-richTextEditorView__8S3q3">'
            f'<div class="editor-content" contenteditable="false">{text}</div></div></div>'
            for letter, text in options
        )
        return f"""
<div class="undefined" data-question-id="{qid}">
  <div class="answer-question-box_questionItem__pMexa">
    <div class="q-question-stem_questionTitle__dF+ck">
      <div class="i-question-number_i-question-number__jTLaR"
           data-ipublish-question-type="single_choice"></div>
      <div class="i-rich-text-editor_i-rich-text-editor-container__f8DG7">
        <div class="editor-content" contenteditable="false">{number}. {label}</div>
      </div>
    </div>
    <div class="chioceView_options__svMSg">{rows}</div>
  </div>
</div>"""

    def true_false_question(qid, statement):
        return f"""
<div class="undefined" data-question-id="{qid}">
  <div class="answer_questionBox__sVwb7">
    <div class="answer_options__2OVwb">
      <div class="answer_option__OpBx7 answer_optionTF__QgJe4" onclick="pickOption(this)">T</div>
      <div class="answer_option__OpBx7 answer_optionTF__QgJe4" onclick="pickOption(this)">F</div>
    </div>
    <div class="answer_questionText__5LTCE">
      <div class="answer_questionWrap__P+0qf">
        <div class="q-question-stem_questionTitle__dF+ck">
          <div class="i-question-number_i-question-number__jTLaR"
               data-ipublish-question-type="true_false"></div>
          <div class="i-rich-text-editor_i-rich-text-editor-container__f8DG7">
            <div class="editor-content" contenteditable="false">{statement}</div>
          </div>
        </div>
      </div>
    </div>
    <div class="answer_shortAnswerWrap__WrmFa">
      <div class="i-print_input-container__RQB5v answer_childExplain__6HgSJ">
        <textarea class="ant-input css-1qe4ilw" placeholder="请输入"></textarea>
      </div>
    </div>
  </div>
</div>"""

    def blank(blank_id, number):
        # 真实页面由 React 构建 DOM，span 里可以嵌 div；
        # 静态 fixture 走 HTML 解析会把 div 从 p 里挤出来，这里用 span 保持嵌套关系。
        return (f'<span data-dig-empty-id="{blank_id}" class="editor-dig-empty" '
                f'contenteditable="false"><span class="digEmptyView_blankView__jF3Qb ">'
                f'<span class="digEmptyView_digEmptyIndexIcon__x2C7B">({number})</span>'
                f'<span class="answer_answerDigEmpty__mifIJ">'
                f'<input class="answer_answerInput__+BqIN" type="text" value=""></span>'
                f'</span></span>')

    fill_question = f"""
<div class="undefined" data-question-id="q-fill">
  <div class="answer-question-box_questionItem__pMexa">
    <div class="answerView_questionBox__ahSi4">
      <div class="answerView_subQuestionText__mnn7i">
        <div class="i-question-number_i-question-number__jTLaR"
             data-ipublish-question-type="fill_blanks"></div>
        <div class="i-rich-text-editor_i-rich-text-editor-container__f8DG7">
          <div class="editor-content" contenteditable="false"><p>
            <span>It helps preserve </span>{blank('b1', 1)}
            <span>, create jobs, and supply a(n) </span>{blank('b2', 2)}<span>.</span>
          </p></div>
        </div>
      </div>
    </div>
  </div>
</div>"""

    chips = "".join(
        f'<span class="answerView_questionViewOptionsItem__OU93b" '
        f'data-question-id="q-banked" data-option-value="{value}">{value}</span>'
        for value in ('cannery', 'horticulturalist', 'entomologist')
    )
    banked_question = f"""
<div class="undefined" data-question-id="q-banked">
  <div class="answerView_wrapper__+4YLo">
    <div class="answerView_preview-options-container__5Zvrd">{chips}</div>
    <div class="answerView_subQuestionText__K0dqu">
      <div class="i-question-number_i-question-number__jTLaR"
           data-ipublish-question-type="fill_blanks_choice"></div>
      <div class="editor-content" contenteditable="false"><p>
        <span>Plans to renovate an abandoned </span>{blank('b3', 1)}
        <span> are underway.</span>
      </p></div>
    </div>
  </div>
</div>"""

    text_question = """
<div class="undefined" data-question-id="q-text">
  <div class="q-question-stem_questionTitle__dF+ck">
    <div class="i-question-number_i-question-number__jTLaR"
         data-ipublish-question-type="short_answer"></div>
    <div class="i-rich-text-editor_i-rich-text-editor-container__f8DG7">
      <div class="editor-content" contenteditable="false">Provide your answer here.</div>
    </div>
  </div>
  <div class="answersView_answerWrap__H-bXo">
    <div class="i-print_input-container__RQB5v answersView_replyInputWrapper__PTDNm">
      <textarea class="ant-input css-1qe4ilw" placeholder="请在此处作答"></textarea>
    </div>
  </div>
</div>"""

    dropdown_question = f"""
<div class="undefined" data-question-id="q-drop">
  <div class="i-question-number_i-question-number__jTLaR"
       data-ipublish-question-type="fill_blanks_dropdown"></div>
  <div class="editor-content" contenteditable="false"><p>
    <span>The Belt and Road Initiative was </span>{blank('b4', 1)}
    <span> by China in 2013, and it becomes </span>{blank('b5', 2)}
    <span> international public good.</span>
  </p></div>
</div>"""

    passage = ('The surprising history of America\'s pick-your-own farms. '
               'In the early twentieth century, farmers near Los Angeles recruited idle city '
               'workers to harvest surplus oranges, and the idea spread across the country.')
    reading_group = f"""
<div class="i-question_i-question-container__2po-e" id="reading"
     data-ipublish-question-group-type="rich_text_read_group">
  <div class="view-parsing-setting_rich-text-read-wrapper__m0xVq">
    <div class="editor-content" contenteditable="false">{passage}</div>
  </div>
</div>"""

    oral_group = """
<div class="i-question_i-question-container__2po-e" id="oral"
     data-ipublish-question-group-type="oral_personal_state_group">
  <div class="undefined" data-question-id="q-oral">
    <div class="q-question-stem_questionTitle__dF+ck">
      <div class="i-question-number_i-question-number__jTLaR"
           data-ipublish-question-type="oral_personal_state"></div>
      <div class="editor-content" contenteditable="false">Gastronomy tourism preserves heritage.</div>
    </div>
    <div class="answer_oral__KgkZw"><div class="i-record-count-down_title__DmpXC">点击开始</div></div>
  </div>
  <div class="i-button_default-container__cnPj6"
       data-ipublish-event-type="question-submit" onclick="gradeGroup(this)">
    <span class="i-button_btn-text__jiMYT">提交</span>
  </div>
</div>"""

    groups = "".join([
        # 已提交的组：应被跳过
        group('g-done', 'single_choice_group',
              choice_question('q-done', 1, 'Already answered?',
                              [('A', 'yes'), ('B', 'no')]), submitted_button),
        reading_group,
        group('g-choice', 'single_choice_group',
              choice_question('q1', 1, 'Why did the author choose the orchard?',
                              [('A', 'Because it was cheap.'), ('B', 'Because of the soil.')])
              + choice_question('q2', 2, 'What made the author take responsibility?',
                                [('A', 'A drought.'), ('B', 'A clay-rich slope.')])),
        group('g-tf', 'true_false_group',
              true_false_question('q3', '1. The pick-your-own model grew quickly.')
              + true_false_question('q4', '2. Farmers stopped harvesting surplus oranges.')),
        group('g-fill', 'fill_blanks_group', fill_question),
        group('g-banked', 'fill_blanks_choice_group', banked_question),
        group('g-dropdown', 'fill_blanks_dropdown_group', dropdown_question),
        group('g-text', 'short_answer_group', text_question),
        oral_group,
    ])

    content = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="app">{groups}</div>
<script>
function gradeGroup(button) {{
  var group = button.closest('[data-ipublish-question-group-type]');
  var label = button.querySelector('.i-button_btn-text__jiMYT');
  if (label) {{ label.textContent = '重新作答'; }}
  var marker = document.createElement('div');
  marker.className = 'answerPreview_marker';
  group.appendChild(marker);
}}
function pickOption(el) {{
  var siblings = el.parentElement.parentElement.querySelectorAll('.chioceView_strongItem__iIqBK');
  siblings.forEach(function (item) {{ item.classList.remove('chioceView_selected'); }});
  el.classList.add('chioceView_selected');
}}
</script>
</body></html>"""

    iframe = ('<iframe id="ipublish-pc-book-easy-iframe" style="width:1200px;height:900px" '
              f'srcdoc="{html.escape(content, quote=True)}"></iframe>')
    shell = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="shell-header">播放器外壳</div>
{iframe}
</body></html>"""
    return shell


_PREVIOUS_TEMP = {}


def _make_driver():
    """启动无头 Edge，并把 TEMP 指到可写目录（脏环境里系统 TEMP 可能不可写）。"""
    from selenium import webdriver

    global _PREVIOUS_TEMP
    work_dir = Path(tempfile.mkdtemp(prefix='ucampus_course_v3_'))
    _PREVIOUS_TEMP = {key: os.environ.get(key) for key in ('TEMP', 'TMP')}
    os.environ['TEMP'] = str(work_dir)
    os.environ['TMP'] = str(work_dir)
    profile = work_dir / 'profile'
    profile.mkdir(exist_ok=True)

    options = webdriver.EdgeOptions()
    for argument in ('--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
                     '--window-size=1600,1200',
                     '--disable-dev-shm-usage', f'--user-data-dir={profile}'):
        options.add_argument(argument)
    try:
        driver = webdriver.Edge(options=options)
    except Exception as exc:  # 没有 Edge/驱动时跳过浏览器测试
        shutil.rmtree(work_dir, ignore_errors=True)
        _restore_temp()
        raise BrowserUnavailable(str(exc)) from exc

    page_url = 'data:text/html;charset=utf-8,' + urllib.parse.quote(_fixture_html())
    driver.get(page_url)
    return driver, work_dir


def _restore_temp():
    for key, value in _PREVIOUS_TEMP.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _quit(driver, work_dir):
    try:
        driver.quit()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        _restore_temp()


def test_publish_page_detection_and_frame_switch():
    driver, work_dir = _make_driver()
    try:
        assert app.PublishPlayer.is_publish_page(driver) is True
        assert app.PublishPlayer.has_content_frame(driver) is True

        with app.PublishFrameContext(driver) as context:
            assert context.entered is True
            groups = app.PublishQuestionParser(driver).collect_groups()
            assert len(groups) == 9, [state['type'] for state in groups]
            kinds = [state['kind'] for state in groups]
            assert kinds.count('choice') == 2
            assert 'true_false' in kinds and 'fill' in kinds and 'text' in kinds
            assert 'dropdown' in kinds

        # 退出上下文后应回到顶层文档
        assert len(driver.find_elements(app.By.CSS_SELECTOR, '#shell-header')) == 1
    finally:
        _quit(driver, work_dir)


def test_publish_parser_reads_each_question_type():
    driver, work_dir = _make_driver()
    try:
        with app.PublishFrameContext(driver) as context:
            assert context.entered
            parser = app.PublishQuestionParser(driver)
            groups = {state['type']: state for state in parser.collect_groups()}

            choice = parser.parse_group(groups['single_choice_group']['element'],
                                        'single_choice_group')
            assert len(choice) == 2
            assert choice[0].q_type == app.QuestionType.SINGLE_CHOICE
            assert [option.letter for option in choice[0].options] == ['A', 'B']
            assert 'orchard' in choice[0].text
            assert choice[0].options[0].text == 'Because it was cheap.'

            tf = parser.parse_group(groups['true_false_group']['element'], 'true_false_group')
            assert len(tf) == 2
            assert tf[0].q_type == app.QuestionType.TRUE_FALSE
            assert [option.letter for option in tf[0].options] == ['T', 'F']
            assert tf[0].inputs == []  # 「请输入」理由框不应被当作答题框

            fill = parser.parse_group(groups['fill_blanks_group']['element'],
                                      'fill_blanks_group')
            assert len(fill) == 1
            assert fill[0].q_type == app.QuestionType.FILL_IN
            assert len(fill[0].inputs) == 2
            assert '(1)' in fill[0].text and '(2)' in fill[0].text

            banked = parser.parse_group(groups['fill_blanks_choice_group']['element'],
                                        'fill_blanks_choice_group')
            assert banked[0].banked_options == ['cannery', 'horticulturalist', 'entomologist']
            assert len(banked[0].inputs) == 1

            dropdown = parser.parse_group(groups['fill_blanks_dropdown_group']['element'],
                                          'fill_blanks_dropdown_group')
            assert dropdown[0].q_type == app.QuestionType.DROPDOWN_SELECT
            assert len(dropdown[0].inputs) == 2

            text = parser.parse_group(groups['short_answer_group']['element'],
                                      'short_answer_group')
            assert text[0].q_type == app.QuestionType.TEXT
            assert len(text[0].inputs) == 1

            passages = parser.collect_passages(list(groups.values()))
            assert passages and 'pick-your-own' in passages[0]
    finally:
        _quit(driver, work_dir)


def test_publish_executor_fills_and_submits():
    driver, work_dir = _make_driver()
    try:
        with app.PublishFrameContext(driver) as context:
            assert context.entered
            parser = app.PublishQuestionParser(driver)
            executor = app.PublishAnswerExecutor(driver)
            groups = {state['type']: state for state in parser.collect_groups()}

            choice = parser.parse_group(groups['single_choice_group']['element'],
                                        'single_choice_group')
            assert executor.execute(choice[0], 'A') is True

            tf = parser.parse_group(groups['true_false_group']['element'], 'true_false_group')
            assert executor.execute(tf[0], 'True') is True
            assert executor.execute(tf[1], 'F') is True
            assert executor.execute(tf[0], '这不是判断答案') is False

            fill = parser.parse_group(groups['fill_blanks_group']['element'],
                                      'fill_blanks_group')
            assert executor.execute(fill[0], '1. barn 2. market') is True
            values = [element.get_attribute('value') for element in fill[0].inputs]
            assert values == ['barn', 'market']

            text = parser.parse_group(groups['short_answer_group']['element'],
                                      'short_answer_group')
            assert executor.execute(text[0], '1. My sample answer') is True
            assert 'sample answer' in text[0].inputs[0].get_attribute('value')

            group_element = groups['single_choice_group']['element']
            assert executor.submit(group_element) is True
            assert executor.button_is_submitted(executor.find_submit_button(group_element))
    finally:
        _quit(driver, work_dir)


def test_publish_flow_end_to_end_with_fake_ai():
    driver, work_dir = _make_driver()
    try:
        solver = app.AISolver.__new__(app.AISolver)
        solver.driver = driver
        solver.stop_requested = Event()
        solver.ai_client = FakeAIClient()
        solver.prompt_builder = app.PromptBuilder(solver.ai_client)
        solver.publish_parser = app.PublishQuestionParser(driver)
        solver.publish_executor = app.PublishAnswerExecutor(driver)
        solver._publish_processed_groups = set()

        assert solver._process_publish_content() is True

        # 所有待作答组都应被提交；已提交组不应被重复处理
        with app.PublishFrameContext(driver) as context:
            assert context.entered
            submitted = driver.execute_script("""
                return Array.from(document.querySelectorAll('[data-ipublish-question-group-type]'))
                    .filter(function (group) {
                        var btn = group.querySelector('[data-ipublish-event-type="question-submit"]');
                        if (!btn) { return false; }
                        return (btn.innerText || '').indexOf('重新作答') >= 0;
                    }).map(function (group) {
                        return group.getAttribute('data-ipublish-question-group-type');
                    });
            """)
        assert submitted.count('single_choice_group') == 2  # 原有 1 个 + 新提交 1 个
        assert submitted.count('true_false_group') == 1
        assert submitted.count('fill_blanks_group') == 1
        assert submitted.count('fill_blanks_choice_group') == 1
        assert submitted.count('fill_blanks_dropdown_group') == 1
        assert submitted.count('short_answer_group') == 1
        assert 'oral_personal_state_group' not in submitted

        # 阅读材料应作为上下文累积
        assert solver.ai_client.accumulated_passages
        assert 'pick-your-own' in solver.ai_client.accumulated_passages[0]
        # 题组按组单独提问，不应把整页混在一个 prompt 里
        assert len(solver.ai_client.prompts) >= 5
    finally:
        _quit(driver, work_dir)


def test_publish_delayed_grade_and_local_blank_numbers():
    driver, work_dir = _make_driver()
    try:
        with app.PublishFrameContext(driver) as context:
            assert context.entered
            parser = app.PublishQuestionParser(driver)
            executor = app.PublishAnswerExecutor(driver)
            group = driver.find_element(app.By.ID, 'g-fill')
            driver.execute_script("""
                const intro = document.createElement('div');
                intro.className = 'editor-content';
                intro.textContent = 'Use each word once. Keep the original tense.';
                arguments[0].prepend(intro);
                arguments[0].querySelector('[class*="title-content"]').textContent =
                    '自动判题 计分 得分：86.3 分 小题1 小题2';
            """, group)
            assert parser.group_direction(group) == \
                'Use each word once. Keep the original tense.'
            # 作者原始空号可能跨小题递增；题面应使用各小题的局部编号。
            driver.execute_script("""
                const blanks = arguments[0].querySelectorAll('[data-dig-empty-id]');
                blanks.forEach((blank, i) => {
                    const label = document.createElement('span');
                    label.className = 'digEmptyIndexIcon_test';
                    label.textContent = '(' + (i + 4) + ')';
                    blank.prepend(label);
                });
            """, group)
            question = parser.parse_group(group, 'fill_blanks_group')[0]
            assert question.blank_count == 2
            assert '(1)' in question.text and '(2)' in question.text
            assert '(4)' not in question.text and '(5)' not in question.text

            solver = app.AISolver.__new__(app.AISolver)
            solver.driver = driver
            solver.stop_requested = Event()
            solver.publish_parser = parser
            solver.publish_executor = executor
            solver.ai_client = FakeAIClient()
            solver.ai_client.ask = lambda *_, **__: '1. alpha'
            solver.prompt_builder = app.PromptBuilder(solver.ai_client)
            assert solver._publish_answer_group(
                group, 'fill_blanks_group', parser.group_key(group)) is False
            assert not parser.group_submitted(group)

            # 不能只提交解析成功的那一道，留下同组另一道未作答。
            choice = driver.find_element(app.By.ID, 'g-choice')
            option_parent = driver.find_element(app.By.CSS_SELECTOR,
                                               '#g-choice [data-question-id="q2"] [class*="options"]')
            driver.execute_script('arguments[0].remove();', option_parent)
            solver.ai_client.ask = Mock(return_value='1. A')
            assert solver._publish_answer_group(
                choice, 'single_choice_group', parser.group_key(choice)) is False
            solver.ai_client.ask.assert_not_called()
            assert not parser.group_submitted(choice)

            # 没有 toast 时检查外层也必须回到 iframe，后续才能重新定位题组。
            assert executor.rejection_message() == ''
            assert driver.execute_script('return window !== window.top;') is True
            choice = driver.find_element(app.By.ID, 'g-choice')
            driver.execute_script("""
                setTimeout(() => {
                    const old = document.getElementById('g-choice');
                    const fresh = old.cloneNode(true);
                    fresh.querySelector('[data-ipublish-event-type="question-submit"]')
                        .textContent = '重新作答';
                    old.replaceWith(fresh);
                }, 900);
            """)
            assert executor.wait_graded(choice, timeout=4, resolver=lambda:
                                        driver.find_element(app.By.ID, 'g-choice')) is True
            assert driver.execute_script('return window !== window.top;') is True

        driver.execute_script("""
            const toast = document.createElement('div');
            toast.textContent = '当前作答客观题正确率未达到60%，请检查作答内容';
            document.body.prepend(toast);
        """)
        with app.PublishFrameContext(driver):
            assert '正确率未达到60%' in executor.rejection_message()
            assert driver.execute_script('return window !== window.top;') is True
    finally:
        _quit(driver, work_dir)


def test_rules_popup_lookup_is_scoped_and_verifies_close():
    driver, work_dir = _make_driver()
    try:
        # 规则按钮排在 450 个其他按钮之后；还有同名按钮与隐藏规则弹窗。
        driver.execute_script("""
            const unrelated = document.createElement('div');
            unrelated.setAttribute('role', 'dialog');
            unrelated.innerHTML = '<p>其他提示</p><button onclick="this.dataset.clicked=1">确定</button>';
            document.body.prepend(unrelated);
            for (let i = 0; i < 450; i++) {
                const button = document.createElement('button');
                button.textContent = i === 0 ? '我知道了' : '其他按钮';
                button.onclick = () => { button.dataset.clicked = 1; };
                document.body.prepend(button);
            }
            const hidden = document.createElement('div');
            hidden.className = 'ant-modal';
            hidden.style.display = 'none';
            hidden.innerHTML = '<p>本单元学习规则</p><button>我知道了</button>';
            document.body.prepend(hidden);
            const rules = document.createElement('div');
            rules.id = 'test-rules';
            rules.className = 'ant-modal ipublish-modal';
            rules.style.cssText = 'position:fixed;top:0;left:0;background:white;z-index:9999';
            rules.innerHTML = '<p>本单元学习规则</p><button>我知道了</button>';
            rules.querySelector('button').onclick = () => rules.remove();
            document.body.prepend(rules);
        """)
        executor = app.PublishAnswerExecutor(driver)
        assert executor.dismiss_rules_popup() is True
        assert not driver.find_elements(app.By.ID, 'test-rules')
        assert not driver.find_elements(app.By.CSS_SELECTOR, '[data-clicked]')
        assert executor.dismiss_rules_popup() is False
        # 点击无效时不能打印已关闭或返回成功。
        driver.execute_script("""
            const rules = document.createElement('div');
            rules.className = 'ipublish-modal';
            rules.style.cssText = 'position:fixed;top:0;left:0;background:white;z-index:9999';
            rules.innerHTML = '<p>本单元学习规则</p><button>我知道了</button>';
            document.body.prepend(rules);
        """)
        assert executor.dismiss_rules_popup() is False
    finally:
        _quit(driver, work_dir)


def test_materials_survive_history_pruning_and_reset():
    completions = Mock()
    completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='1. A'), finish_reason='stop')])
    config = SimpleNamespace(api_key='fake', base_url='https://example.invalid',
                             model='fake', temperature=0, max_tokens=100)
    with patch.object(app, 'OpenAI', return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=completions))):
        client = app.OpenAICompatibleClient(config)
    material_a = 'First reading material. ' * 5
    material_b = 'Second reading material. ' * 5
    transcript = 'Spoken video content. ' * 5
    assert client.add_passage_if_new(material_a)
    assert client.add_passage_if_new(material_b)
    for i in range(15):
        if i == 3:
            assert client.add_video_transcript_if_new(transcript)
        assert client.ask(f'Question {i}') == '1. A'
    assert len(client.conversation_history) == 20
    messages = completions.create.call_args.kwargs['messages']
    for material in (material_a, material_b, transcript):
        assert sum(material in msg['content'] for msg in messages) == 1
    assert not client.add_passage_if_new(material_b)
    assert not any(msg['content'] == 'Question 0' for msg in messages)
    client.force_reset('next unit')
    assert not client.material_messages and not client.conversation_history
    assert not client.accumulated_passages
    assert client.add_passage_if_new(material_b)


def test_new_run_allows_redone_or_rejected_groups():
    solver = app.AISolver.__new__(app.AISolver)
    solver.stop_requested = Event()
    solver.processed_hashes = set()
    solver._publish_processed_groups = {'previously rejected', 'previously submitted'}
    solver.ai_client = Mock()
    solver._generate_content_hash = Mock(return_value='page')
    def process(*args):
        assert not solver._publish_processed_groups
        solver._publish_processed_groups.add('processed in this run')
        return True
    solver._process_current_tab_content = process
    assert solver.solve_current_page()
    assert solver.solve_current_page()
    solver.driver = Mock()
    solver.process_selected_tabs([])
    assert not solver._publish_processed_groups


def test_publish_collapsed_prompts_tables_long_passages_and_stop():
    driver, work_dir = _make_driver()
    try:
        with app.PublishFrameContext(driver):
            driver.execute_script("""
                const group = document.getElementById('g-text');
                const outer = document.createElement('div');
                outer.innerHTML = `
                    <summary data-lexical-node-type="collapsible-head">Step 1</summary>
                    <div data-lexical-node-type="collapsible-content" hidden="until-found">
                        <summary data-lexical-node-type="collapsible-head">Details</summary>
                        <div data-lexical-node-type="collapsible-content" hidden="until-found">
                            <div data-lexical-node-type="question-block" id="older-question">
                                <p>Unrelated older question</p>
                            </div>
                            <p data-lexical-node-type="base-paragraph">Compare polyculture with monoculture.</p>
                            <div data-lexical-node-type="highlight-block"><p>Give two examples.</p><p>Explain climate resilience.</p></div>
                            <div data-lexical-node-type="question-block" id="target-block"></div>
                        </div>
                    </div>`;
                group.before(outer);
                outer.querySelector('#target-block').append(group);
                outer.querySelectorAll('summary').forEach(head => {
                    head.onclick = () => head.nextElementSibling.toggleAttribute('hidden');
                });
            """)
            parser = app.PublishQuestionParser(driver)
            executor = app.PublishAnswerExecutor(driver)
            group = driver.find_element(app.By.ID, 'g-text')
            assert not group.is_displayed()
            button = parser.submit_button(group)
            driver.execute_script("arguments[0].textContent = '重新作答';", button)
            assert parser.group_submitted(group)  # 折叠的已完成题不能误判为待答题
            driver.execute_script("arguments[0].textContent = '提交';", button)
            assert not parser.group_submitted(group)
            assert executor.reveal_group(group)
            assert group.is_displayed()
            assert executor.reveal_group(group)  # 再调用不能把已展开的面板折回去
            direction = parser.group_direction(group)
            assert 'Compare polyculture' in direction
            assert 'Give two examples.\n\nExplain climate resilience.' in direction
            assert 'Unrelated older question' not in direction

            fill_group = driver.find_element(app.By.ID, 'g-fill')
            node = parser._question_nodes(fill_group)[0]
            driver.execute_script("""
                const node = arguments[0], editor = node.querySelector('.editor-content');
                const extra = document.createElement('div');
                extra.className = 'editor-content';
                extra.innerHTML = '<p>Second stem paragraph.</p><table><tr><th>Country</th><th>Crop</th></tr><tr><td>China</td><td>Rice</td></tr></table>';
                editor.after(extra);
            """, node)
            stem = parser._numbered_stem_text(node)
            assert '(1)' in stem and '(2)' in stem
            assert 'Second stem paragraph.' in stem
            assert 'Country | Crop' in stem and 'China | Rice' in stem
            assert 'CountryCrop' not in stem

            driver.execute_script("""
                const read = document.createElement('div');
                read.dataset.ipublishQuestionGroupType = 'rich_text_read_group';
                read.id = 'long-reading';
                read.innerHTML = '<div class="editor-content"><p>' + 'A long article. '.repeat(500) +
                    '</p><p>Essential final evidence.</p></div>' +
                    '<button>Play audio</button><div data-ipublish-event-type="test">00:00</div>';
                document.body.append(read);
            """)
            read = driver.find_element(app.By.ID, 'long-reading')
            states = [{'type': 'rich_text_read_group', 'element': read}]
            passage = parser.collect_passages(states)[0]
            assert len(passage) > 6000 and passage.endswith('Essential final evidence.')
            driver.execute_script("arguments[0].querySelector('button').textContent = 'Pause audio';", read)
            assert parser.collect_passages(states) == [passage]

            # 停止恰好发生在最后一题填写完毕时，也不能继续点击提交。
            solver = app.AISolver.__new__(app.AISolver)
            solver.driver = driver
            solver.stop_requested = Event()
            solver._publish_processed_groups = set()
            solver.publish_parser = parser
            solver.publish_executor = executor
            solver.ai_client = FakeAIClient()
            solver.prompt_builder = app.PromptBuilder(solver.ai_client)
            solver.ai_client.ask = Mock(return_value='A complete written answer.')
            def fill_and_stop(*args):
                solver.stop_requested.set()
                return True
            executor.execute = fill_and_stop
            executor.submit = Mock(return_value=True)
            assert solver._publish_answer_group(group, 'short_answer_group', parser.group_key(group)) is False
            executor.submit.assert_not_called()
            assert not parser.group_submitted(group)
    finally:
        _quit(driver, work_dir)


def _make_scoped_pages(driver, work_dir):
    """复现一个 iframe 挂载整单元、目录任务仅靠 #anchor 区分的真实结构。"""
    with app.PublishFrameContext(driver):
        driver.execute_script("""
            const root = document.createElement('div');
            root.className = 'editor-content';
            document.body.append(root);
            const heading = (id, title, rank=2) => {
                const h = document.createElement('h' + rank);
                h.id = id; h.dataset.lexicalNodeAnchor = id;
                h.dataset.lexicalNodeType = 'heading'; h.textContent = title; root.append(h);
            };
            heading('unit3', 'Unit 3', 1);
            root.append(document.getElementById('g-choice'));
            heading('pre', 'Pre-viewing');
            const discussion = document.getElementById('oral').cloneNode(true);
            discussion.id = 'discussion'; discussion.dataset.ipublishQuestionGroupType = 'discussion_group';
            root.append(discussion);
            heading('synth', 'Viewing and synthesizing');
            root.append(document.getElementById('g-fill'));
            heading('discuss', 'Viewing and discussing');
            root.append(document.getElementById('oral'));
            heading('frontier', 'Exploring the frontier', 1);
            heading('thinking', 'Thinking globally');
            root.append(document.getElementById('g-text'));
            heading('repeat-synth', 'Viewing and synthesizing');
            root.append(document.getElementById('g-banked'));
            heading('last', 'Last task');
            root.append(document.getElementById('g-dropdown'));
            const next = document.createElement('button');
            next.textContent = '下一节'; next.id = 'forbidden-next';
            next.onclick = () => { next.dataset.clicked = '1'; };
            document.body.append(next);
        """)
        content = driver.page_source
    (work_dir / 'scope-content.html').write_text(content, encoding='utf-8')
    routes = {}
    for anchor in ('pre', 'synth', 'discuss', 'thinking', 'repeat-synth', 'last', 'unit3'):
        path = work_dir / f'scope-{anchor}.html'
        path.write_text('<!doctype html><meta charset="utf-8">'
                        '<iframe id="ipublish-pc-book-easy-iframe" style="width:1200px;height:900px" '
                        f'src="scope-content.html#{anchor}"></iframe>', encoding='utf-8')
        routes[anchor] = path.as_uri()
    directory = work_dir / 'scope-directory.html'
    items = []
    for anchor, title in [('pre', 'Pre-viewing'), ('synth', 'Viewing and synthesizing'),
                          ('discuss', 'Viewing and discussing'), ('thinking', 'Thinking globally')]:
        items.append(f'<div class="courses-unit_nodeItemInnerLayout__fixture">'
                     f'<span class="courses-unit_nodeTypeName__fixture" '
                     f'onclick="location.href=\'scope-{anchor}.html\'">{title}</span></div>')
    directory.write_text('<!doctype html><meta charset="utf-8">'
                         '<div class="unipus-tabs_unitTabScrollContainer__fXBxR"><button>Unit 3</button></div>'
                         + ''.join(items), encoding='utf-8')
    return directory.as_uri(), routes


def _scope_solver(driver):
    solver = app.AISolver.__new__(app.AISolver)
    solver.driver = driver
    solver.stop_requested = Event()
    solver.ai_client = FakeAIClient()
    solver.ai_client.current_chapter_id = 'scope fixture'
    solver.ai_client.start_new_chapter = lambda chapter: setattr(solver.ai_client, 'current_chapter_id', chapter)
    solver.ai_client.force_reset = solver.ai_client.start_new_chapter
    solver.prompt_builder = app.PromptBuilder(solver.ai_client)
    solver.publish_parser = app.PublishQuestionParser(driver)
    solver.publish_executor = app.PublishAnswerExecutor(driver)
    solver.processed_hashes = set()
    solver._publish_processed_groups = set()
    solver._publish_advance_page = Mock(side_effect=AssertionError('不能跳到未勾选任务'))
    return solver


def test_publish_selected_tasks_do_not_cross_into_thinking_globally():
    driver, work_dir = _make_driver()
    try:
        directory, _ = _make_scoped_pages(driver, work_dir)
        driver.get(directory)
        names = driver.find_elements(app.By.CSS_SELECTOR, '[class*="courses-unit_nodeTypeName__"]')
        selected = [{'_element': name, '_unit_idx': 0, 'l1_title': name.text, 'display': name.text}
                    for name in names[:3]]
        solver = _scope_solver(driver)
        snapshots = []
        process = solver._process_tab_with_accumulation
        def record(*args):
            assert process(*args)
            with app.PublishFrameContext(driver):
                groups = solver.publish_parser.collect_groups()
                snapshots.append((solver._publish_scope.title,
                    [s['element'].get_attribute('id') for s in groups
                     if solver.publish_parser.group_submitted(s['element'])]))
                assert not driver.find_elements(app.By.CSS_SELECTOR, '#forbidden-next[data-clicked]')
        solver._process_tab_with_accumulation = record
        # 离线页面同步加载，无需批量切换的网络等待。
        with patch.object(solver.stop_requested, 'wait', return_value=False):
            solver.process_selected_tabs(selected, 'Unit 3')
        assert [title for title, _ in snapshots] == [s['l1_title'] for s in selected]
        assert 'g-fill' in snapshots[1][1]
        assert all('g-text' not in done and 'g-banked' not in done and 'g-dropdown' not in done
                   and 'g-choice' not in done for _, done in snapshots)
        assert len(solver.ai_client.prompts) == 1  # 讨论/口语跳过，只有选中的填空题调用模型
        solver._publish_advance_page.assert_not_called()
    finally:
        _quit(driver, work_dir)


def test_publish_scope_survives_reload_and_refuses_unknown_boundaries():
    driver, work_dir = _make_driver()
    try:
        _, routes = _make_scoped_pages(driver, work_dir)
        solver = _scope_solver(driver)
        driver.get(routes['synth'])
        driver.execute_script("location.hash = '/course-v3:test/courseware/u3/synth';")
        assert solver._publish_prepare_scope('Viewing and synthesizing')
        driver.execute_script("location.hash += '/activity';")  # 平台自动追加题块路由
        with app.PublishFrameContext(driver):
            groups = solver.publish_parser.collect_groups()
            assert [s['element'].get_attribute('id') for s in solver._publish_scoped_groups(groups)] == ['g-fill']
            # 题目组重建后，范围由稳定的正文锚点重新定位。
            driver.execute_script("const g=document.getElementById('g-fill'); g.replaceWith(g.cloneNode(true));")
            assert [s['element'].get_attribute('id') for s in solver._publish_scoped_groups(
                solver.publish_parser.collect_groups())] == ['g-fill']
            outside = driver.find_element(app.By.ID, 'g-text')
            solver.ai_client.ask = Mock()
            assert solver._publish_answer_group(outside, 'short_answer_group', 'outside') is False
            solver.ai_client.ask.assert_not_called()
            # 选择父级标题应包含子任务，重复的同名标题则必须以锚点区分。
            parent = app.PublishTaskScope.capture(driver, 'unit3')
            assert set(s['element'].get_attribute('id') for s in parent.select(driver,
                solver.publish_parser.collect_groups())) == {'g-choice', 'discussion', 'g-fill', 'oral'}
            repeated = app.PublishTaskScope.capture(driver, 'repeat-synth')
            assert [s['element'].get_attribute('id') for s in repeated.select(driver,
                solver.publish_parser.collect_groups())] == ['g-banked']
            last = app.PublishTaskScope.capture(driver, 'last')
            assert [s['element'].get_attribute('id') for s in last.select(driver,
                solver.publish_parser.collect_groups())] == ['g-dropdown']
            # 模型请求期间边界发生变化，也必须在写入前重新检查。
            def move_boundary(*args, **kwargs):
                driver.execute_script("window.removedBoundary=document.getElementById('discuss'); removedBoundary.remove();")
                return '1. alpha 2. beta'
            solver.ai_client.ask = Mock(side_effect=move_boundary)
            solver.publish_executor.execute = Mock(return_value=True)
            current = driver.find_element(app.By.ID, 'g-fill')
            assert solver._publish_answer_group(current, 'fill_blanks_group',
                                               solver.publish_parser.group_key(current)) is False
            solver.publish_executor.execute.assert_not_called()
            driver.execute_script("document.getElementById('oral').before(window.removedBoundary);")
            # 正文标题之间可出现小标题和折叠内容，不应把它们误作下一任务。
            driver.execute_script("""
                const group=document.getElementById('g-fill'), wrap=document.createElement('div');
                wrap.dataset.lexicalNodeType='collapsible-content'; wrap.hidden=true;
                group.before(wrap); wrap.append(group);
                const sub=document.createElement('h3'); sub.textContent='Notes';
                document.getElementById('synth').after(sub);
            """)
            assert [s['element'].get_attribute('id') for s in solver._publish_scoped_groups(
                solver.publish_parser.collect_groups())] == ['g-fill']
            # 缺失定位信息必须拒绝，不能把任务范围扩大为整单元。
            driver.execute_script("document.getElementById('discuss').remove();")
            try:
                solver._publish_scoped_groups(solver.publish_parser.collect_groups())
            except ValueError:
                pass
            else:
                raise AssertionError('边界丢失时必须停止')
            for anchor in ('not-a-task', ''):
                if not anchor:
                    driver.execute_script("history.replaceState(null,'',location.pathname);")
                try:
                    app.PublishTaskScope.capture(driver, anchor)
                except ValueError:
                    pass
                else:
                    raise AssertionError('不能将未知范围退回整单元')
        driver.get(routes['thinking'])
        with app.PublishFrameContext(driver):
            # 同一正文的路由/可见位置变化不能把原任务扩大到 Thinking globally。
            assert [s['element'].get_attribute('id') for s in solver._publish_scoped_groups(
                solver.publish_parser.collect_groups())] == ['g-fill']
            driver.execute_script("history.replaceState(null,'',location.pathname+'?chapter=other#thinking');")
        assert solver._publish_process_next_group() == 'error'  # 切到其他单元文档则停止
        assert not solver._publish_prepare_scope('Viewing and synthesizing', timeout=0.1)
        solver._publish_advance_page.assert_not_called()
    finally:
        _quit(driver, work_dir)


def _run_browser_tests():
    tests = [
        test_publish_page_detection_and_frame_switch,
        test_publish_parser_reads_each_question_type,
        test_publish_executor_fills_and_submits,
        test_publish_flow_end_to_end_with_fake_ai,
        test_publish_delayed_grade_and_local_blank_numbers,
        test_rules_popup_lookup_is_scoped_and_verifies_close,
        test_publish_collapsed_prompts_tables_long_passages_and_stop,
        test_publish_selected_tasks_do_not_cross_into_thinking_globally,
        test_publish_scope_survives_reload_and_refuses_unknown_boundaries,
    ]
    failures = 0
    skipped = 0
    for test in tests:
        try:
            test()
        except BrowserUnavailable as exc:
            skipped += 1
            print(f'SKIP {test.__name__}: 本机无可用 Edge/{exc}'[:200])
        except AssertionError as exc:
            failures += 1
            print(f'FAIL {test.__name__}: {exc}')
        else:
            print(f'PASS {test.__name__}')
    print(f'\n浏览器测试: {len(tests) - failures - skipped} passed, '
          f'{failures} failed, {skipped} skipped')
    return failures


if __name__ == '__main__':
    test_group_classification()
    test_true_false_normalization()
    test_prompt_builder_true_false_and_banked_options()
    test_publish_answer_routing()
    test_dropdown_options_prompt_injection()
    test_publish_requires_every_answer_and_keyboard_write()
    test_publish_multi_question_dropdown_without_inputs()
    test_dropdown_requires_exact_option_and_nonempty_readback()
    test_publish_hash_does_not_wait_for_legacy_directions()
    test_ai_ask_retries_when_reasoning_exhausts_tokens()
    test_materials_survive_history_pruning_and_reset()
    test_new_run_allows_redone_or_rejected_groups()
    test_shift_blank_answer_for_multi_question_group()
    test_fill_answer_sentence_initial_capitalization()
    test_sanitize_fill_answer_nested_numbering()
    test_publish_player_button_text()
    test_select_native_option_matches_text()
    test_rejection_message_reads_accuracy_toast()
    print('PASS 纯逻辑测试')

    failures = _run_browser_tests()
    if failures:
        raise SystemExit(1)
