#!/usr/bin/env python3
"""
RSS feed factory for sites that don't publish RSS.

Fetches a listing page, extracts articles (title, date, link, summary),
optionally fetches each article for its full text, and writes an RSS feed
into docs/, which GitHub Pages serves.

To add a site, append it to SOURCES at the bottom of this file.
Standard library only, nothing to install.
"""

import re
import sys
import time
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


# ---------------------------------------------------------------------------
# Full-text extraction
# ---------------------------------------------------------------------------

class ContentExtractor(HTMLParser):
    """Copies the inside of the first <root> element as clean, reader-friendly HTML.

    Keeps text and basic formatting (paragraphs, headings, lists, links, images,
    quotes, code, tables). Drops scripts, navigation, forms and other page chrome.
    Unknown wrapper tags (div, span, section...) are removed but their text is kept.
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "source", "track", "wbr"}
    DROP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside",
            "form", "button", "iframe", "template", "canvas", "video", "audio",
            "select", "textarea", "dialog", "h1", "picture"}
    KEEP = {"p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre",
            "code", "em", "strong", "b", "i", "figure", "figcaption", "table", "thead",
            "tbody", "tr", "th", "td", "sup", "sub", "a"}

    def __init__(self, base_url, root):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.root = root
        self.out = []
        self.text_len = 0
        self._depth = 0       # >0 while inside the root element
        self._done = False
        self._drop = 0        # >0 while inside a dropped element
        self._pre = 0

    def _url(self, value):
        return urljoin(self.base, value) if value else None

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if self._depth == 0:
            if tag == self.root:
                self._depth = 1
            return
        if tag not in self.VOID:
            self._depth += 1
        if self._drop:
            if tag not in self.VOID:
                self._drop += 1
            return
        if tag in self.DROP:
            if tag not in self.VOID:
                self._drop = 1
            return
        a = dict(attrs)
        if tag == "img":
            src = self._url(a.get("src") or a.get("data-src"))
            if src and not src.startswith("data:"):
                alt = escape(a.get("alt") or "", {'"': "&quot;"})
                self.out.append(f'<img src="{escape(src, {chr(34): "&quot;"})}" alt="{alt}">')
        elif tag in ("br", "hr"):
            self.out.append(f"<{tag}>")
        elif tag == "a":
            href = self._url(a.get("href"))
            if href and not href.startswith("javascript:"):
                self.out.append(f'<a href="{escape(href, {chr(34): "&quot;"})}">')
            else:
                self.out.append("<a>")
        elif tag in self.KEEP:
            if tag == "pre":
                self._pre += 1
            self.out.append(f"<{tag}>")
        else:
            self.out.append(" ")  # unwrapped tag: keep words from sticking together

    def handle_endtag(self, tag):
        if self._done or self._depth == 0 or tag in self.VOID:
            return
        self._depth -= 1
        if self._depth == 0:
            self._done = True
            return
        if self._drop:
            self._drop -= 1
            return
        if tag in self.KEEP:
            if tag == "pre" and self._pre:
                self._pre -= 1
            self.out.append(f"</{tag}>")
        else:
            self.out.append(" ")

    def handle_data(self, data):
        if self._done or self._depth == 0 or self._drop:
            return
        text = data if self._pre else re.sub(r"\s+", " ", data)
        if text.strip():
            self.text_len += len(text.strip())
        self.out.append(escape(text))

    def html(self):
        out = "".join(self.out)
        out = re.sub(r"<(p|li|h[2-6]|figcaption|blockquote)>\s*</\1>", "", out)  # empty tags
        out = re.sub(r"<a>(.*?)</a>", r"\1", out, flags=re.S)                      # links without href
        out = re.sub(r"[ \t]{2,}", " ", out).strip()
        # drop loose text before the first block (usually the category and date line)
        m = re.match(r"([^<]*)(<.*)", out, re.S)
        if m and len(m.group(1)) < 120:
            out = m.group(2)
        return out


def extract_full_text(html, url):
    """Return the article body as HTML, or None if nothing substantial was found."""
    for root in ("article", "main", "body"):
        ex = ContentExtractor(url, root)
        ex.feed(html)
        if ex.text_len >= 300:
            return ex.html()
    return None


ARXIV_ABSTRACT_RE = re.compile(r'<blockquote[^>]*class="[^"]*abstract[^"]*"[^>]*>(.*?)</blockquote>', re.S)


def arxiv_content(url):
    """Full abstract plus links to the PDF and the abstract page for an arXiv paper."""
    html = fetch(url)
    m = ARXIV_ABSTRACT_RE.search(html)
    if not m:
        return None
    ex = ContentExtractor(url, "div")
    ex.feed("<div>" + m.group(1) + "</div>")
    abstract = re.sub(r"^\s*Abstract:\s*", "", ex.html())
    pdf = url.replace("/abs/", "/pdf/")
    return (f"<p>{abstract}</p>"
            f'<p><a href="{escape(pdf)}">PDF</a> \u00b7 <a href="{escape(url)}">arXiv page</a></p>')


def add_full_text(items, source):
    """Fetch every article and attach its full text. Failures keep the summary."""
    page_host = urlparse(source["page"]).netloc.removeprefix("www.")
    for it in items:
        host = urlparse(it["link"]).netloc.removeprefix("www.")
        try:
            if host == "arxiv.org":
                it["content"] = arxiv_content(it["link"])
            elif host == page_host:
                it["content"] = extract_full_text(fetch(it["link"]), it["link"])
        except Exception as e:  # noqa: BLE001
            print(f"  could not fetch {it['link']}: {e}", file=sys.stderr)
        time.sleep(1)  # be polite to the site


def build_rss(source, items):
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
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
        if it.get("content"):
            out.append(f"<content:encoded>{escape(it['content'])}</content:encoded>")
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
#   full_text     - fetch each article and put its full text into the feed
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
        "full_text": True,
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
        if source.get("full_text"):
            add_full_text(items, source)
        (OUT_DIR / source["file"]).write_text(build_rss(source, items), encoding="utf-8")
        full = sum(1 for i in items if i.get("content"))
        print(f"[{source['file']}] {len(items)} articles, {full} with full text")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
