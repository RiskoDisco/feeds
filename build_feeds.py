#!/usr/bin/env python3
"""
RSS feed factory for sites that don't publish RSS.

Fetches a listing page, extracts articles (title, date, link) and writes
an RSS feed into docs/, which GitHub Pages serves.

To add a site, append it to SOURCES at the bottom of this file.
Standard library only, nothing to install.
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
    """Collects every link on the page together with the text chunks inside it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []      # [(href, [text chunks])]
        self._stack = []     # currently open <a> tags

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            self._stack.append((href, []))

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, chunks = self._stack.pop()
            if href:
                self.links.append((href, chunks))
                # text of a nested link also belongs to the outer one
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
    """Title = the most repeated (usually shown twice), then longest, text chunk."""
    candidates = []
    for c in chunks:
        if DATE_RE.search(c) and len(DATE_RE.sub("", c).strip(" •|-")) < 25:
            continue                      # chunk holding only category and date
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

    # The same article can appear several times (banner, card).
    # Keep the best version: one with a date and a real title.
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
# Sources. To add a site, copy a block and change the fields.
#   file          - feed file name (served at .../feeds/<file>)
#   page          - page that lists the articles
#   article_paths - URL fragments that identify article links
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "file": "arenaphysica.xml",
        "title": "Arena Physica - News",
        "description": "News and publications from Arena Physica",
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
            print(f"[{source['file']}] fetch failed: {e}", file=sys.stderr)
            failed = True
            continue
        if not items:
            # don't overwrite the old feed with an empty one; let Actions show a red X
            print(f"[{source['file']}] no articles found, the site layout may have changed", file=sys.stderr)
            failed = True
            continue
        (OUT_DIR / source["file"]).write_text(build_rss(source, items), encoding="utf-8")
        print(f"[{source['file']}] {len(items)} articles")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
