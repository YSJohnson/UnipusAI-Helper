"""把整页快照裁成最小 fixture：只保留 .question-abs-question 子树（真实 DOM 用）。

用法：python scripts/trim_fixture.py <page_main.html> [out.html]
默认输出 scripts/fixtures/homework_page.html，供 e2e/dryrun 使用。
"""

import pathlib
import re
import sys
from html.parser import HTMLParser

VOID = {"br", "img", "input", "meta", "link", "hr", "source", "col", "area", "base", "wbr"}


class SectionExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth = 0
        self.start_depth = -1
        self.capturing = False
        self.buf = []
        self.out = []

    def handle_starttag(self, tag, attrs):
        if not self.capturing:
            cls = dict(attrs).get("class", "")
            if "question-abs-question" in cls and tag == "div":
                self.capturing = True
                self.start_depth = self.depth
                self.buf = []
        if self.capturing:
            self.buf.append(self.get_starttag_text())
        if tag not in VOID:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.capturing:
            self.buf.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.capturing:
            self.buf.append(f"</{tag}>")
        if tag not in VOID:
            self.depth -= 1
            if self.capturing and self.depth == self.start_depth:
                self.out.append("".join(self.buf))
                self.capturing = False
                self.buf = []

    def handle_data(self, data):
        if self.capturing:
            self.buf.append(data)

    def handle_entityref(self, name):
        if self.capturing:
            self.buf.append(f"&{name};")

    def handle_charref(self, name):
        if self.capturing:
            self.buf.append(f"&#{name};")


def main():
    src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not src or not src.exists():
        print("用法: python scripts/trim_fixture.py <page_main.html> [out.html]")
        return 1
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else \
        pathlib.Path(__file__).resolve().parent / "fixtures" / "homework_page.html"
    parser = SectionExtractor()
    parser.feed(src.read_text(encoding="utf-8", errors="ignore"))
    parser.close()
    sections = parser.out
    print("sections:", len(sections), "sizes:", [len(s) for s in sections][:10])
    body = re.sub(r">\s+<", "><", "\n".join(sections))
    page = ('<!doctype html><html><head><meta charset="utf-8">'
            "<title>homework fixture</title></head>"
            f"<body>{body}</body></html>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print("written:", out, round(len(page) / 1024, 1), "KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())