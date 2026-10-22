#!/usr/bin/env python3
"""
Фабрика RSS-лент для сайтов без RSS.

Скрипт открывает страницу сайта, находит статьи (заголовок, дата, ссылка)
и записывает RSS-ленту в папку docs/, откуда её отдаёт GitHub Pages.

Чтобы добавить новый сайт, допишите его в список SOURCES внизу файла.
Только стандартная библиотека Python, ничего устанавливать не нужно.
"""

import re
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
OUT_DIR = Path(__file__).resolve().parent / "docs"

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
DATE_RE = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(\d{1,2}),\s*(\d{4})\b",
                     re.IGNORECASE)


class LinkCollector(HTMLParser):
    """Собирает все ссылки страницы вместе с кусочками текста внутри них."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []      # [(href, [text chunks])]
        self._stack = []     # открытые <a>

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            self._stack.append((href, []))

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, chunks = self._stack.pop()
            if href:
                self.links.append((href, chunks))
                # текст вложенной ссылки относится и к внешней
                if self._stack:
                    self._stack[-1][1].extend(chunks)

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and self._stack:
            self._stack[-1][1].append(text)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def parse_date(text):
    m = DATE_RE.search(text)
    if not m:
        return None
    month = MONTHS[m.group(1)[:3].upper()]
    try:
        return datetime(int(m.group(3)), month, int(m.group(2)), 12, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


def title_from_slug(url):
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().capitalize() or url


def pick_title(chunks, skip):
    """Заголовок — самый частый (обычно повторяется дважды) и самый длинный кусок текста."""
    candidates = []
    for c in chunks:
        if DATE_RE.search(c) and len(DATE_RE.sub("", c).strip(" •|-")) < 25:
            continue                      # кусок, где только категория и дата
        if c.strip().lower() in skip:
            continue
        if len(c) < 4:
            continue
        candidates.append(c)
    if not candidates:
        return None
    return max(candidates, key=lambda c: (candidates.count(c), len(c)))


def extract_items(source):
    html = fetch(source["page"])
    parser = LinkCollector()
    parser.feed(html)

    # Одна статья может встречаться на странице несколько раз (баннер, карточка).
    # Оставляем лучший вариант: с датой и с нормальным заголовком.
    best = {}
    for href, chunks in parser.links:
        url = urljoin(source["page"], href).split("#")[0]
        if not any(p in url for p in source["article_paths"]):
            continue
        p = urlparse(url)
        key = p.netloc.removeprefix("www.") + p.path.rstrip("/")
        url = "https://" + p.netloc + p.path.rstrip("/")
        date = parse_date(" ".join(chunks))
        title = pick_title(chunks, source.get("skip_text", set()))
        score = (date is not None, title is not None)
        if key not in best or score > best[key][0]:
            best[key] = (score, {"title": title or title_from_slug(url), "link": url, "date": date})

    items = [v for _, v in best.values()
             if v["date"] or not source.get("require_date")]
    items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items


def build_rss(source, items):
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{escape(source['title'])}</title>",
        f"<link>{escape(source['page'])}</link>",
        f"<description>{escape(source.get('description', source['title']))}</description>",
    ]
    for it in items:
        out.append("<item>")
        out.append(f"<title>{escape(it['title'])}</title>")
        out.append(f"<link>{escape(it['link'])}</link>")
        out.append(f'<guid isPermaLink="true">{escape(it["link"])}</guid>')
        if it["date"]:
            out.append(f"<pubDate>{format_datetime(it['date'])}</pubDate>")
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Список источников. Чтобы добавить сайт — скопируйте блок и поменяйте поля.
#   file          — имя файла ленты (адрес будет .../feeds/<file>)
#   page          — страница, где перечислены статьи
#   article_paths — кусочки адреса, по которым узнаются ссылки на статьи
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "file": "arenaphysica.xml",
        "title": "Arena Physica — News",
        "description": "Новости и публикации Arena Physica",
        "page": "https://www.arenaphysica.com/news",
        "article_paths": ["arenaphysica.com/articles/", "arenaphysica.com/publications/"],
        "skip_text": {"arena physica", "media", "news", "research", "read the article"},
    },
]


def main():
    OUT_DIR.mkdir(exist_ok=True)
    failed = False
    for source in SOURCES:
        try:
            items = extract_items(source)
        except Exception as e:  # noqa: BLE001
            print(f"[{source['file']}] ошибка загрузки: {e}", file=sys.stderr)
            failed = True
            continue
        if not items:
            # не перезаписываем старую ленту пустой — пусть Actions покажет красный крестик
            print(f"[{source['file']}] статьи не найдены — возможно, сайт поменял вёрстку", file=sys.stderr)
            failed = True
            continue
        (OUT_DIR / source["file"]).write_text(build_rss(source, items), encoding="utf-8")
        print(f"[{source['file']}] {len(items)} статей")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
