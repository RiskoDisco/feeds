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
    """Collects every link on the page with the text chunks inside it.

    Each chunk is (text, in_heading): in_heading is True for text inside h1-h6,
    which is usually the article title.
    """

    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []      # [(href, [(text, in_heading)])]
        self._stack = []     # currently open <a> tags
        self._heading = 0    # depth of open heading tags

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._stack.append((dict(attrs).get("href"), []))
        elif tag in self.HEADINGS:
            self._heading += 1

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, chunks = self._stack.pop()
            if href:
                self.links.append((href, chunks))
                # text of a nested link also belongs to the outer one
                if self._stack:
                    self._stack[-1][1].extend(chunks)
        elif tag in self.HEADINGS and self._heading:
            self._heading -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and self._stack:
            self._stack[-1][1].append((text, self._heading > 0))


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


def is_meta(text):
    """True for chunks holding only a category and/or a date."""
    return bool(DATE_RE.search(text)) and len(DATE_RE.sub("", text).strip(" \u2022|-")) < 25


def pick_title_and_summary(chunks, skip):
    """Pick the title and a short summary from the text inside a link.

    Title priority: text in a heading tag, then a chunk repeated on the card,
    then the first chunk after the date, then the longest chunk.
    """
    cands = [(t, h) for t, h in chunks
             if len(t) >= 4 and t.strip().lower() not in skip and not is_meta(t)]
    if not cands:
        return None, None
    texts = [t for t, _ in cands]

    title = next((t for t, h in cands if h), None)
    if not title:
        repeated = [t for t in texts if texts.count(t) > 1]
        title = repeated[0] if repeated else None
    if not title:
        date_idx = next((i for i, (t, _) in enumerate(chunks) if DATE_RE.search(t)), None)
        if date_idx is not None:
            after = [t for t, _ in chunks[date_idx + 1:] if t in texts]
            title = after[0] if after else None
    if not title:
        title = max(texts, key=len)

    rest = [t for t in texts if t != title and len(t) > 40]
    summary = max(rest, key=len) if rest else None
    return title, summary


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
        date = parse_date(" ".join(t for t, _ in chunks))
        title, summary = pick_title_and_summary(chunks, source.get("skip_text", set()))
        score = (date is not None, title is not None, summary is not None)
        if key not in best or score > best[key][0]:
            best[key] = (score, {"title": title or title_from_slug(url), "link": url,
                                 "date": date, "summary": summary})

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
        if it.get("summary"):
            out.append(f"<description>{escape(it['summary'])}</description>")
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
        "title": "Arena Physica - Publications",
        "description": "Publications and papers from Arena Physica",
        "page": "https://www.arenaphysica.com/publications",
        "article_paths": ["arenaphysica.com/publications/", "arxiv.org/abs/"],
        "skip_text": {"arena physica", "publication", "publications", "paper", "papers",
                      "all", "read more"},
        "require_date": True,
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
