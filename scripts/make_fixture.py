"""生成离线 fixture：结构与真实作业页一致（7 节 / 40 下拉 / 10 填空 / 20 选择）。

真实快照在清理时被删，账号当前也没有待作答作业，无法重新抓取；
这里按已验证的真实 DOM 结构生成一份最小 fixture，供 CI 运行 e2e/dryrun。
若以后重新拿到真实快照，用 scripts/trim_fixture.py 裁剪后覆盖本文件即可。
"""

import pathlib

BANK = ["quantity", "concern", "ripped", "a chain of", "as well as", "interrelated",
        "in combination", "in a way", "switching", "absurd", "be extinguished",
        "harnessed", "perpetual", "narrowing", "baffled", "insulating", "spreads",
        "slice", "ideology", "supply"]


def cloze_section(number, title):
    blanks = []
    for i in range(20):
        options = "".join(f'<i>{word}</i>' for word in BANK)
        blanks.append(
            f'<span class="fe-scoop" data-scoop-index="{i}">'
            f'<span class="scoop-select-wrapper">'
            f'<div style="display: inline-block; height: 0px; visibility: hidden;'
            f' position: absolute; z-index: -9999; white-space: nowrap;">{options}</div>'
            f'<div class="user-answer-text"></div><div class="placeholder">点击选择</div>'
            f'</span></span>')
    paragraph = " ".join(
        f"The answer to the supply shortage is item {i} <u>{blanks[i]}</u>."
        for i in range(20))
    return f'''
<div class="question-basic question-abs-question ques-{number}">
  <div class="layout-container">
    <div class="layoutBody-container vertical has-material has-reply">
      <div class="layout-material-container"><div class="question-common-abs-material">
        <div class="text-material-wrapper"><div class="component-htmlview">{title}</div></div>
      </div></div>
      <div class="layout-reply-container"><div class="reply-wrap">
        <div class="question-common-abs-reply">
          <div class="question-common-abs-scoop comp-scoop-reply comp-scoop-reply-dropdown-s">
            <div class="item"><div class="component-htmlview"><p>{paragraph}</p></div></div>
          </div>
        </div>
      </div></div>
    </div>
  </div>
</div>'''


def paragraph_section(number):
    paragraphs = "".join(f"<div>{letter}) paragraph {letter} about financial aid.</div>"
                         for letter in "ABCDEFGHIJKLMN")
    replies = "".join(
        f'<div class="question-common-abs-reply"><div class="question-common-abs-scoop '
        f'comp-scoop-reply fill-blank-reply"><div class="item"><div class="component-htmlview">'
        f'Statement {i} about aid.'
        f'<span class="fe-scoop" data-scoop-index="0"><span class="scoop-input-wrapper">'
        f'<span class="question-number">{i}</span>'
        f'<div class="comp-abs-input input-user-answer"><div class="placeholder"></div>'
        f'<input value=""></div></span></span></div></div></div></div>'
        for i in range(1, 11))
    return f'''
<div class="question-basic question-abs-question ques-{number}">
  <div class="layout-container">
    <div class="layoutBody-container vertical has-material has-reply">
      <div class="layout-material-container"><div class="question-common-abs-material">
        <div class="text-material-wrapper"><div class="component-htmlview">
          <div style="text-align: center;"><b>How College Financial Aid Works</b></div>{paragraphs}
        </div></div>
      </div></div>
      <div class="layout-reply-container"><div class="reply-wrap">{replies}</div></div>
    </div>
  </div>
</div>'''


def choice_section(number, passage):
    replies = "".join(
        f'<div class="question-common-abs-reply"><div class="question-common-abs-choice">'
        f'<div class="component-htmlview ques-title"><span class="question-number">{i}.</span>'
        f'{passage} question {i}?</div><div class="option-wrap">'
        + "".join(f'<div class="option"><div class="caption">{letter}</div>'
                  f'<div class="component-htmlview content">option {letter} of {i}</div></div>'
                  for letter in "ABCD")
        + '</div></div></div>'
        for i in range(1, 6))
    return f'''
<div class="question-basic question-abs-question ques-{number}">
  <div class="layout-container">
    <div class="layoutBody-container vertical has-material has-reply">
      <div class="layout-material-container"><div class="question-common-abs-material">
        <div class="text-material-wrapper"><div class="component-htmlview">{passage}</div></div>
      </div></div>
      <div class="layout-reply-container"><div class="reply-wrap">{replies}</div></div>
    </div>
  </div>
</div>'''


def main():
    sections = [
        cloze_section(1, "The answer to the supply shortage of lithium (锂) is, quite literally, that there is no ONE answer."),
        cloze_section(2, "You've tried so hard to drop nearly all the extra fat that has crept into your diet."),
        paragraph_section(3),
        choice_section(4, "A spoonful of pickles can sometimes make a meal taste way better."),
        choice_section(5, "Foxes and farmers never get along well."),
        choice_section(6, "There's a common misconception that evolution leads animals to be selfish."),
        choice_section(7, "The advancements made in digitalization and AI have sparked a cognitive transformation."),
    ]
    page = ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>homework fixture</title></head><body>"
            + "".join(sections) + "</body></html>")
    out = pathlib.Path(__file__).resolve().parent / "fixtures" / "homework_page.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print("written:", out, round(len(page) / 1024, 1), "KB")


if __name__ == "__main__":
    main()