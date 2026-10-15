"""Build the local, bilingual HTML edition of the MCP documentation.

Requires Python 3.10+ and mistune 3.x. Run from any working directory.
The font split, heading anchors, and literal-code handling reuse the
wmw-abaqus publication builder. No fonts or remote assets are downloaded.
"""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import mistune


DEFAULT_REPO = Path(__file__).resolve().parents[1]
SOURCES = {
    "README.md": ("index.html", "中文说明", "zh-CN"),
    "README.en.md": ("english.html", "English", "en"),
    "CHANGELOG.md": ("changelog.html", "版本记录 / Changelog", "zh-CN"),
    "docs/validation.md": ("validation.html", "验证范围 / Validation", "zh-CN"),
    "docs/publication_style.md": ("publication-style.html", "排版 / Typography", "zh-CN"),
}
HAN = re.compile(r"([\u2e80-\ua4cf\uf900-\ufaff\ufe30-\ufe4f\uff01-\uff60\U00020000-\U0002ffff]+)")


def mixed_text(value: str) -> str:
    """Apply the requested CJK font without changing visible text."""
    return "".join(
        '<span class="han">' + html.escape(part) + "</span>"
        if index % 2 else html.escape(part)
        for index, part in enumerate(HAN.split(value)) if part
    )


class HeadingRenderer(mistune.HTMLRenderer):
    """Give Markdown headings usable local anchors, including Chinese ones."""

    def __init__(self) -> None:
        super().__init__(escape=True)
        self.heading_counts: dict[str, int] = {}

    def heading(self, text: str, level: int, **attrs: object) -> str:
        plain = html.unescape(re.sub(r"<[^>]*>", "", text)).lower()
        slug = re.sub(r"[^\w\- ]", "", plain, flags=re.UNICODE).replace(" ", "-")
        count = self.heading_counts.get(slug, 0)
        self.heading_counts[slug] = count + 1
        anchor = slug + ("-" + str(count) if count else "")
        return f'<h{level} id="{html.escape(anchor, quote=True)}">{text}</h{level}>\n'


class PresentationHTML(HTMLParser):
    """Rewrite local links and wrap CJK prose; keep code text literal."""

    def __init__(self, source: Path, destination: Path, mapping: dict[Path, Path]) -> None:
        super().__init__(convert_charrefs=False)
        self.source, self.destination, self.mapping = source, destination, mapping
        self.output: list[str] = []
        self.literal_depth = 0
        self.code_blocks = 0

    def link(self, value: str) -> str:
        url = urlsplit(value)
        if url.scheme or url.netloc or not url.path:
            return value
        target = (self.source.parent / unquote(url.path)).resolve()
        target = self.mapping.get(target, target)
        relative = os.path.relpath(target, self.destination.parent).replace("\\", "/")
        return urlunsplit(("", "", relative, url.query, url.fragment))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("pre", "code"):
            self.literal_depth += 1
        if tag == "pre":
            self.code_blocks += 1
        values = []
        for key, value in attrs:
            if value is not None and key in ("href", "src"):
                value = self.link(value)
            values.append(key if value is None else key + '="' + html.escape(value, quote=True) + '"')
        self.output.append("<" + tag + (" " + " ".join(values) if values else "") + ">")

    def handle_endtag(self, tag: str) -> None:
        self.output.append("</" + tag + ">")
        if tag in ("pre", "code"):
            self.literal_depth -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.output.append(html.escape(data, quote=False) if self.literal_depth else mixed_text(data))

    def handle_entityref(self, name: str) -> None:
        self.output.append("&" + name + ";")

    def handle_charref(self, name: str) -> None:
        self.output.append("&#" + name + ";")


def publication_version(repo: Path, override: str | None) -> str:
    value = (override or (repo / "VERSION").read_text(encoding="utf-8-sig").strip()).removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", value):
        raise ValueError("Use a semantic publication version, for example 5.2.0")
    return value


def render_callouts(body: str, language: str) -> str:
    labels = ({"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important",
               "WARNING": "Conditions", "CAUTION": "Caution"} if language == "en" else
              {"NOTE": "说明", "TIP": "使用建议", "IMPORTANT": "关键要求",
               "WARNING": "适用条件", "CAUTION": "注意"})

    def alert(match: re.Match[str]) -> str:
        kind = match.group(1)
        return ('<blockquote class="callout ' + kind.lower() + '"><p>'
                '<span class="callout-label">' + labels[kind] + "</span>")

    return re.sub(r"<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", alert, body)


def build(repo: Path, override: str | None = None) -> dict[str, object]:
    repo = repo.resolve()
    missing = [name for name in [*SOURCES, "VERSION", "docs/assets/publication.css"]
               if not (repo / name).is_file()]
    if missing:
        raise FileNotFoundError("Write the publication sources before building: " + ", ".join(missing))
    version = publication_version(repo, override)
    output = repo / "docs/site"
    mapping = {(repo / source).resolve(): (output / target).resolve()
               for source, (target, _, _) in SOURCES.items()}
    links = dict(mapping)
    links[(repo / "README.zh-CN.md").resolve()] = output / "index.html"
    pages: dict[Path, str] = {}
    counts: dict[str, int] = {}
    for name, (_, _, language) in SOURCES.items():
        source = (repo / name).resolve()
        destination = mapping[source]
        text = source.read_text(encoding="utf-8-sig")
        title = next((line[2:].replace("**", "") for line in text.splitlines()
                      if line.startswith("# ")), source.stem)
        markdown = mistune.create_markdown(renderer=HeadingRenderer(),
                                          plugins=["table", "strikethrough", "task_lists"])
        body = render_callouts(markdown(text), language)
        presentation = PresentationHTML(source, destination, links)
        presentation.feed(body)
        presentation.close()
        nav = "".join('<a ' + ('class="active" aria-current="page" ' if item == name else '')
                      + 'href="' + target + '">' + mixed_text(label) + "</a>"
                      for item, (target, label, _) in SOURCES.items())
        original = os.path.relpath(source, destination.parent).replace("\\", "/")
        footer = ("Generated from repository Markdown. GitHub controls native Markdown fonts."
                  if language == "en" else
                  "由仓库 Markdown 生成本地 HTML；GitHub 原生 Markdown 字体由平台控制。")
        source_label = "View source" if language == "en" else "查看源文档"
        pages[destination] = (
            '<!doctype html>\n<html lang="' + language + '"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>'
            + html.escape(title) + '</title><link rel="stylesheet" href="../assets/publication.css">'
            '</head>\n<body><div class="layout"><nav aria-label="Documentation">'
            '<div class="brand latin">wmw-abaqus-mcp<br><span class="version">v'
            + html.escape(version) + '</span></div>' + nav + '</nav>\n<main>'
            '<div class="font-samples"><span id="font-zh-sample" class="han">中文：微软雅黑</span>'
            ' · <span id="font-en-sample" class="latin">English: Times New Roman</span></div>'
            + "".join(presentation.output) + '\n<footer>' + mixed_text(footer) + '<br><a href="'
            + html.escape(original, quote=True) + '">' + mixed_text(source_label)
            + '</a></footer></main></div></body></html>\n'
        )
        counts[name] = presentation.code_blocks
    output.mkdir(parents=True, exist_ok=True)
    for destination, document in pages.items():
        destination.write_text(document, encoding="utf-8", newline="\n")
    return {
        "version": version,
        "outputs": ["docs/site/" + target for target, _, _ in SOURCES.values()],
        "literal_code_blocks": counts,
        "font_contract": {"chinese": "Microsoft YaHei", "english_numbers_units": "Times New Roman"},
        "remote_runtime_dependencies": [],
        "font_binaries_distributed": False,
        "markdown_fonts_are_platform_controlled": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--version", help="Override the version read from VERSION")
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.version), ensure_ascii=False, indent=2))
