#!/usr/bin/env python3
"""
RSS feed factory for sites that don't publish RSS.

Fetches a listing page, extracts articles (title, date, link, summary),
optionally fetches each article for its full text, and writes an RSS feed
into docs/, which GitHub Pages serves.

A second kind of source ("preprints") reads another site's RSS feed of
paywalled papers and keeps only the papers whose authors posted a free
preprint on arXiv, linking to that preprint.

To add a site, append it to SOURCES at the bottom of this file.
Standard library only, nothing to install.
"""

import gzip
import html as html_lib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
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
        self.spans = []      # [(start, end)] for each entry of self.links, as positions in self.texts
        self.texts = []      # every text chunk on the page, in order (also outside links)
        self._stack = []     # currently open <a> tags: (href, chunks, start)
        self._heading = 0    # depth of open heading tags
        self._skip = 0       # depth of <script>/<style>, whose text is not page text

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._stack.append((dict(attrs).get("href"), [], len(self.texts)))
        elif tag in self.HEADINGS:
            self._heading += 1
        elif tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, chunks, start = self._stack.pop()
            if href:
                self.links.append((href, chunks))
                self.spans.append((start, len(self.texts)))
                # text of a nested link also belongs to the outer one
                if self._stack:
                    self._stack[-1][1].extend(chunks)
        elif tag in self.HEADINGS and self._heading:
            self._heading -= 1
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and self._stack:
            self._stack[-1][1].append((text, self._heading > 0))
        if text and not self._skip:
            self.texts.append(text)


def decode_body(raw, content_encoding, charset):
    """Unpack a compressed response (some servers send gzip even unasked) and decode it."""
    encoding = (content_encoding or "").lower()
    if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif "deflate" in encoding:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)  # raw deflate without a header
    return raw.decode(charset or "utf-8", errors="replace")


def fetch(url, timeout=30, user_agent=USER_AGENT):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept-Language": "en",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return decode_body(resp.read(), resp.headers.get("Content-Encoding"),
                           resp.headers.get_content_charset())


def fetch_xml(url, **kwargs):
    """Fetch and parse an XML document (RSS feed, arXiv API answer).

    If the site sends something else, the error says what it sent, so the
    Actions log shows the real cause.
    """
    return parse_xml(fetch(url, **kwargs), url)


def parse_xml(text, url):
    text = text.lstrip("\ufeff \t\r\n")
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError(f"{url} did not return XML ({e}); it starts with {text[:80]!r}") from None


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


def nearby_date(texts, spans, i, article_spans, position, window=12):
    """Date printed next to link number i instead of inside it.

    position "before": the date is in the text just above the link (back to the
    previous article link); "after": just below it (up to the next article link).
    """
    start, end = spans[i]
    if position == "after":
        stop = min((s for s, _ in article_spans if s >= end), default=len(texts))
        chunk = texts[end:min(stop, end + window)]
    else:
        stop = max((e for _, e in article_spans if e <= start), default=0)
        chunk = list(reversed(texts[max(stop, start - window):start]))
    for text in chunk:
        date = parse_date(text)
        if date:
            return date
    return None


def extract_items(source):
    html = fetch(source["page"])
    parser = LinkCollector()
    parser.feed(html)

    # article links are recognised by URL fragments ("article_paths") or by a
    # regular expression ("article_pattern")
    pattern = re.compile(source["article_pattern"]) if source.get("article_pattern") else None

    def is_article(url):
        return bool(pattern.search(url)) if pattern else any(p in url for p in source["article_paths"])

    position = source.get("date_position")  # where the date is printed, if not inside the link
    links = [(i, href, chunks, urljoin(source["page"], href).split("#")[0])
             for i, (href, chunks) in enumerate(parser.links)]
    links = [x for x in links if is_article(x[3])]
    article_spans = [parser.spans[i] for i, *_ in links]

    # The same article can appear several times (banner, card).
    # Keep the best version: one with a date and a real title.
    best = {}
    for i, href, chunks, url in links:
        p = urlparse(url)
        key = p.netloc.removeprefix("www.") + p.path.rstrip("/")
        url = "https://" + p.netloc + p.path.rstrip("/")
        date = parse_date(" ".join(t for t, _ in chunks))
        if date is None and position:
            date = nearby_date(parser.texts, parser.spans, i, article_spans, position)
        title, summary = pick_title_and_summary(chunks, source.get("skip_text", set()))
        score = (date is not None, title is not None, summary is not None)
        if key not in best or score > best[key][0]:
            best[key] = (score, {"title": title or title_from_slug(url), "link": url,
                                 "date": date, "summary": summary})

    items = [v for _, v in best.values()
             if v["date"] or not source.get("require_date")]
    items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    if source.get("max_items"):
        items = items[:source["max_items"]]
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


CONTENT_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"


def previous_full_text(source):
    """Full texts already in the current feed file, by link (for "reuse_full_text")."""
    try:
        root = ET.parse(OUT_DIR / source["file"]).getroot()
    except (OSError, ET.ParseError):
        return {}
    return {(it.findtext("link") or "").strip(): it.findtext(CONTENT_TAG)
            for it in root.iter("item") if it.findtext(CONTENT_TAG)}


def add_full_text(items, source):
    """Fetch every article and attach its full text. Failures keep the summary.

    With "reuse_full_text", articles already in the feed keep the text fetched
    before, so only new articles are downloaded.
    """
    page_host = urlparse(source["page"]).netloc.removeprefix("www.")
    known = previous_full_text(source) if source.get("reuse_full_text") else {}
    for it in items:
        if known.get(it["link"]):
            it["content"] = known[it["link"]]
            continue
        host = urlparse(it["link"]).netloc.removeprefix("www.")
        try:
            if host == "arxiv.org":
                it["content"] = arxiv_content(it["link"])
            elif host == page_host:
                it["content"] = extract_full_text(fetch(it["link"]), it["link"])
        except Exception as e:  # noqa: BLE001
            print(f"  could not fetch {it['link']}: {e}", file=sys.stderr)
        time.sleep(1)  # be polite to the site


# ---------------------------------------------------------------------------
# Free preprints for papers listed by a paywalled site or journal
# ---------------------------------------------------------------------------
#
# A "preprints" source takes a list of paywalled papers (an RSS feed, or the
# newest articles of journals looked up by ISSN in Crossref) and publishes only
# the papers whose authors posted a free version on arXiv or SSRN.

ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works"
SSRN_DOI_PREFIX = "10.2139"   # every SSRN paper with a full text gets a DOI starting with this
ATOM = "{http://www.w3.org/2005/Atom}"
# Step 1: search by title.
TITLE_MATCH = 0.85        # how similar the titles must be (1.0 = identical)
# Step 2 (when the preprint has a different title): list papers by the same
# co-authors and pick the one about the same topic.
TOPIC_MATCH = 0.5         # share of the paper's keywords the preprint must contain
TOPIC_MARGIN = 0.15       # lead the best preprint needs over the next best one
YEARS_BEFORE = 2          # preprint posted at most this long before the paper (per source: "years_before")
YEARS_AFTER = 1           # ...or at most this long after it
RETRY_DAYS = 7            # look again for a paper with no preprint after this many days
RETRY_MAX_AGE_DAYS = 180  # stop looking for papers older than this
MAX_PAPERS_PER_RUN = 10   # papers searched per run, all preprint feeds together; the rest wait
SCRIPT_USER_AGENT = "RiskoDisco-feeds/1.0 (personal RSS feeds; https://github.com/RiskoDisco/feeds)"
# Being polite to the search services: arXiv refuses requests ("HTTP 429 Too
# Many Requests") when a script asks too often, and GitHub's servers are shared
# by many scripts. After a refusal or a timeout we wait and try again; if the
# service still refuses, searching stops until the next run.
SERVICES = {
    "arxiv": {"name": "arXiv", "pause": 5, "last": 0.0},
    "crossref": {"name": "Crossref", "pause": 1, "last": 0.0},
}
SERVICE_TIMEOUT = 60      # seconds to wait for an answer
SERVICE_RETRIES = 2       # extra tries after a refusal or a timeout...
SERVICE_BACKOFF = 30      # ...waiting this many seconds, doubled each time
STOPWORDS = {"with", "from", "into", "under", "over", "between", "their", "them", "this",
             "that", "these", "those", "what", "when", "where", "which", "while", "about",
             "beyond", "using", "towards", "toward", "through", "within", "without",
             "the", "and", "for", "are", "has", "have", "can", "its", "was", "were", "been",
             "our", "how", "why", "not", "all", "but", "also", "more", "most", "such", "than",
             "then", "there", "they", "both", "each", "new", "novel", "paper", "show", "shows",
             "shown", "present", "presented", "presents", "introduce", "introduced",
             "introduces", "propose", "proposed", "proposes", "based", "approach",
             "approaches", "method", "methods"}
# journal entries that are not research papers
NOT_A_PAPER = re.compile(r"^(issue information|front matter|back matter|masthead|editorial board|"
                         r"erratum|errata|corrigendum|correction|retraction|report of the|"
                         r"announcement|call for papers|index to|table of contents|"
                         r"list of referees|acknowledg|in memoriam|obituary)", re.I)
RUN = {"searched": 0, "busy": None}   # shared by all preprint feeds during one run


class ServiceBusy(Exception):
    """A search service keeps refusing requests; stop asking until the next run."""


def ascii_fold(text):
    """'Guéant' -> 'Gueant'."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def norm_text(text):
    """Lowercase ASCII words separated by single spaces, for comparing titles."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_fold(text).lower()).split())


def surname(name):
    """'Shaun (Xiaoyuan) Li' -> 'li', 'Marcos  López de Prado' -> 'prado'."""
    words = norm_text(re.sub(r"\(.*?\)", " ", name)).split()
    return words[-1] if words else ""


def surnames(names):
    return {s for s in (surname(n) for n in names) if len(s) > 1}


def keywords(text):
    """Rough word stems for comparing topics: 'calibrated', 'calibration' -> 'calib'."""
    return {w[:5] for w in norm_text(text).split()
            if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()}


def markup_to_text(text):
    """Plain text from the little HTML/JATS markup Crossref uses in titles and abstracts."""
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = " ".join(text.split())
    return re.sub(r"^(abstract|summary)\s*[:.]?\s+", "", text, flags=re.I)


def shorten(text, limit=300):
    if not text or len(text) <= limit:
        return text or None
    return text[:limit].rsplit(" ", 1)[0] + "…"


def clean_paper_title(title, authors):
    """Drop a trailing 'by <author>' that some sites append to the title."""
    for name in authors:
        m = re.search(r",?\s+by\s+(.+)$", title)
        if m and norm_text(m.group(1)) == norm_text(name):
            return title[:m.start()].strip()
    return title


def service_get(service, url):
    """GET a search service politely (see SERVICES). Raises ServiceBusy if it keeps refusing."""
    s = SERVICES[service]
    for attempt in range(SERVICE_RETRIES + 1):
        wait = s["pause"] - (time.time() - s["last"])
        if wait > 0:
            time.sleep(wait)
        try:
            return fetch(url, timeout=SERVICE_TIMEOUT, user_agent=SCRIPT_USER_AGENT)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            reason = f"HTTP {e.code}"
            retry_after = e.headers.get("Retry-After", "") if e.headers else ""
        except (TimeoutError, urllib.error.URLError) as e:
            if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, TimeoutError):
                raise
            reason, retry_after = "timed out", ""
        finally:
            s["last"] = time.time()
        if attempt == SERVICE_RETRIES:
            raise ServiceBusy(f"{s['name']}: {reason}")
        delay = int(retry_after) if retry_after.isdigit() else SERVICE_BACKOFF * 2 ** attempt
        time.sleep(min(delay, 300))


# --- arXiv -----------------------------------------------------------------

def arxiv_query(query, max_results, **extra):
    """Run one arXiv API search and return the papers it found."""
    url = ARXIV_API + "?" + urlencode({"search_query": query, "max_results": max_results, **extra})
    root = parse_xml(service_get("arxiv", url), url)
    found = []
    for e in root.findall(f"{ATOM}entry"):
        abs_url = (e.findtext(f"{ATOM}id") or "").strip()
        if "/abs/" not in abs_url:
            continue  # arXiv reports query errors as an entry without /abs/
        published = None
        try:
            published = datetime.fromisoformat((e.findtext(f"{ATOM}published") or "")
                                               .replace("Z", "+00:00"))
        except ValueError:
            pass
        found.append({
            "title": " ".join((e.findtext(f"{ATOM}title") or "").split()),
            "authors": [" ".join((a.findtext(f"{ATOM}name") or "").split())
                        for a in e.findall(f"{ATOM}author")],
            "abstract": " ".join((e.findtext(f"{ATOM}summary") or "").split()),
            "url": re.sub(r"v\d+$", "", abs_url.replace("http://", "https://")),
            "published": published,
        })
    return found


def arxiv_by_title(title, names):
    words = [w for w in norm_text(title).split() if len(w) > 3 and w not in STOPWORDS][:6]
    if len(words) < 2:
        return []
    query = " AND ".join(f"ti:{w}" for w in words)
    query += " AND (" + " OR ".join(f"au:{n}" for n in sorted(names)) + ")"
    return arxiv_query(query, 10)


def arxiv_by_authors(names, date, years_before):
    query = " AND ".join(f"au:{n}" for n in sorted(names))
    return arxiv_query(query, 50, sortBy="submittedDate", sortOrder="descending")


# --- Crossref (journal contents, and SSRN preprints) ------------------------

def crossref_query(params):
    url = CROSSREF_API + "?" + urlencode(params)
    text = service_get("crossref", url)
    try:
        return json.loads(text)["message"]["items"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"{url} did not return Crossref results; it starts with {text[:80]!r}") from None


def crossref_date(item):
    """Earliest publication date of a Crossref record (online first, then print)."""
    dates = []
    for key in ("published-online", "published-print", "posted", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            y, m, d = (list(parts) + [1, 1])[:3]
            try:
                dates.append(datetime(int(y), int(m or 1), int(d or 1), 12, tzinfo=timezone.utc))
            except (TypeError, ValueError):
                pass
    return min(dates) if dates else None


def crossref_record(item):
    title = markup_to_text(" ".join(item.get("title") or []))
    authors = [" ".join(f"{a.get('given', '')} {a['family']}".split())
               for a in item.get("author") or [] if a.get("family")]
    return title, authors, markup_to_text(item.get("abstract")), crossref_date(item)


def ssrn_candidate(item):
    title, authors, abstract, date = crossref_record(item)
    doi = item.get("DOI", "")
    m = re.search(r"ssrn\.(\d+)", doi, re.I)
    url = (f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={m.group(1)}" if m
           else f"https://doi.org/{doi}")
    return {"title": title, "authors": authors, "abstract": abstract, "url": url, "published": date}


def ssrn_by_title(title, names):
    items = crossref_query({"query.bibliographic": title, "query.author": " ".join(sorted(names)),
                            "filter": f"prefix:{SSRN_DOI_PREFIX}", "rows": 10})
    return [ssrn_candidate(i) for i in items]


def ssrn_by_authors(names, date, years_before):
    filters = [f"prefix:{SSRN_DOI_PREFIX}"]
    if date:
        start = date.replace(year=date.year - years_before, month=1, day=1)
        end = date.replace(year=date.year + YEARS_AFTER, month=12, day=31)
        filters += [f"from-pub-date:{start:%Y-%m-%d}", f"until-pub-date:{end:%Y-%m-%d}"]
    items = crossref_query({"query.author": " ".join(sorted(names)), "filter": ",".join(filters),
                            "rows": 40})
    return [ssrn_candidate(i) for i in items]


def read_journal_items(source):
    """Newest research articles of the journals in source['issn'], from Crossref."""
    filters = ",".join([f"issn:{i}" for i in source["issn"]] + ["type:journal-article"])
    items = crossref_query({"filter": filters, "sort": "published", "order": "desc",
                            "rows": source.get("max_papers", 25)})
    papers = []
    for item in items:
        title, authors, abstract, date = crossref_record(item)
        if not title or not authors or NOT_A_PAPER.match(title):
            continue
        papers.append({"title": title, "link": f"https://doi.org/{item['DOI']}", "date": date,
                       "description": shorten(abstract), "abstract": abstract, "authors": authors})
    return papers


# --- RSS feeds of papers ------------------------------------------------------

def read_rss_items(url):
    """Items of an RSS 2.0 feed: title, link, date, description, authors."""
    root = fetch_xml(url)
    items = []
    for it in root.iter("item"):
        date = None
        if it.findtext("pubDate"):
            try:
                date = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
        items.append({
            "title": " ".join((it.findtext("title") or "").split()),
            "link": (it.findtext("link") or "").strip(),
            "date": date,
            "description": " ".join((it.findtext("description") or "").split()) or None,
            "authors": [" ".join(a.text.split()) for a in it.iter("author") if (a.text or "").strip()],
        })
    return items


# --- matching -----------------------------------------------------------------

TITLE_SEARCH = {"arxiv": arxiv_by_title, "ssrn": ssrn_by_title}
AUTHOR_SEARCH = {"arxiv": arxiv_by_authors, "ssrn": ssrn_by_authors}


def pick_by_title(title, names, candidates):
    """Step 1: a preprint with a nearly identical title and at least one shared author."""
    want, best = norm_text(title), None
    for c in candidates:
        score = SequenceMatcher(None, want, norm_text(c["title"])).ratio()
        if score >= TITLE_MATCH and names & surnames(c["authors"]):
            if best is None or score > best[0]:
                best = (score, c)
    return best[1] if best else None


def pick_by_topic(text, names, date, years_before, candidates):
    """Step 2: among preprints by all the same co-authors, the one on the same topic."""
    want = keywords(text)
    if not want:
        return None
    scored, seen = [], set()
    for c in candidates:
        if c["url"] in seen or not names <= surnames(c["authors"]):
            continue
        seen.add(c["url"])
        if date and c["published"]:
            years = (c["published"] - date).days / 365.25
            if years < -years_before or years > YEARS_AFTER:
                continue
        have = keywords(f"{c['title']} {c['abstract']}")
        scored.append((len(want & have) / len(want), c))
    scored.sort(key=lambda s: s[0], reverse=True)
    if not scored or scored[0][0] < TOPIC_MATCH:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < TOPIC_MARGIN:
        return None  # two preprints look equally likely: better to skip than to guess
    return scored[0][1]


def find_preprint(paper, order, years_before):
    """A free preprint of a paper on arXiv or SSRN, or None. Raises on network errors.

    Title search on every service first, then the co-author search, which is
    only done for papers with two or more authors: a single prolific author has
    too many unrelated preprints to tell them apart safely.
    """
    title = clean_paper_title(paper["title"], paper["authors"])
    names = surnames(paper["authors"])
    if not names:
        return None
    for service in order:
        match = pick_by_title(title, names, TITLE_SEARCH[service](title, names))
        if match:
            break
    else:
        match = None
        if len(names) >= 2:
            text = f"{title} {paper.get('abstract') or paper.get('description') or ''}"
            for service in order:
                found = AUTHOR_SEARCH[service](names, paper["date"], years_before)
                match = pick_by_topic(text, names, paper["date"], years_before, found)
                if match:
                    break
    if match:
        match = {k: v for k, v in match.items() if k != "published"}
    return match


def preprint_html(match, original_link, original_name):
    links = []
    if "arxiv.org" in match["url"]:
        links += [f'<a href="{escape(match["url"].replace("/abs/", "/pdf/"))}">PDF</a>',
                  f'<a href="{escape(match["url"])}">arXiv page</a>']
    else:
        links.append(f'<a href="{escape(match["url"])}">SSRN page (free PDF download)</a>')
    links.append(f'<a href="{escape(original_link)}">{escape(original_name)}</a>')
    abstract = f"<p>{escape(match['abstract'])}</p>" if match.get("abstract") else ""
    sep = " · "
    return (f"<p><strong>Authors:</strong> {escape(', '.join(match['authors']))}</p>"
            f"{abstract}<p>{sep.join(links)}</p>")


def preprint_items(source):
    """Papers from the source that have a free preprint on arXiv or SSRN.

    Results are remembered in docs/<file>.cache.json so each paper is searched
    once; papers without a preprint are searched again every RETRY_DAYS days.
    Returns (items, note for the log).
    """
    cache_path = OUT_DIR / (Path(source["file"]).stem + ".cache.json")
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    before = json.dumps(cache, sort_keys=True)

    if source.get("issn"):
        papers = read_journal_items(source)
    else:
        papers = [p for p in read_rss_items(source["rss"])
                  if any(x in p["link"] for x in source["paper_paths"])
                  and not any(x in p["link"] for x in source.get("skip_paths", []))]
        if not papers:
            raise RuntimeError("no papers in the source feed, its layout may have changed")

    order = source.get("search", ["arxiv", "ssrn"])
    years_before = source.get("years_before", YEARS_BEFORE)
    today = datetime.now(timezone.utc).date()
    searches = waiting = 0
    for p in papers:
        entry = cache.get(p["link"])
        if entry and entry.get("match"):
            continue
        if entry:
            checked = datetime.strptime(entry["checked"], "%Y-%m-%d").date()
            age = (today - p["date"].date()).days if p["date"] else 0
            if (today - checked).days < RETRY_DAYS or age > RETRY_MAX_AGE_DAYS:
                continue
        if RUN["busy"] or RUN["searched"] >= MAX_PAPERS_PER_RUN:
            waiting += 1  # not remembered, so it is searched in a later run
            continue
        RUN["searched"] += 1
        searches += 1
        try:
            match = find_preprint(p, order, years_before)
        except ServiceBusy as e:
            print(f"  {e} is refusing requests, the rest waits for the next run", file=sys.stderr)
            RUN["busy"] = str(e)
            RUN["searched"] -= 1
            searches, waiting = searches - 1, waiting + 1
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  could not search preprints for {p['link']}: {e}", file=sys.stderr)
            continue  # not remembered, so it is tried again next run
        entry = {"checked": today.isoformat(), "match": match}
        if match:
            entry["paper"] = {"title": p["title"], "description": p["description"],
                              "date": p["date"].isoformat() if p["date"] else None}
        cache[p["link"]] = entry

    # keep every found preprint; forget misses that dropped out of the source list
    current = {p["link"] for p in papers}
    cache = {k: v for k, v in cache.items() if v.get("match") or k in current}

    items, seen = [], set()
    for link, entry in cache.items():
        m = entry.get("match")
        if not m or m["url"] in seen:
            continue
        seen.add(m["url"])
        paper = entry["paper"]
        items.append({
            "title": m["title"],
            "link": m["url"],
            "date": datetime.fromisoformat(paper["date"]) if paper.get("date") else None,
            "summary": paper.get("description"),
            "content": preprint_html(m, link, source.get("original_name", "Original")),
        })
    items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    items = items[:source.get("max_items", 50)]

    if json.dumps(cache, sort_keys=True) != before:
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    return items, (f" ({len(papers)} papers in the source, {searches} searched this run,"
                   f" {waiting} waiting for the next runs)")


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
#   file            - feed file name (served at .../feeds/<file>)
#   page            - page that lists the articles
#   article_paths   - URL fragments that identify article links
#   article_pattern - or instead a regular expression for article links
#   date_position   - "before"/"after": the date is printed above/below the
#                     article link rather than inside it
#   require_date    - only articles with a date
#   max_items       - keep only this many newest articles
#   full_text       - fetch each article and put its full text into the feed
#   reuse_full_text - fetch the full text only once per article
#
# "kind": "preprints" sources take a list of paywalled papers, either the RSS
# feed in "rss" (links matching "paper_paths", minus "skip_paths") or the
# newest articles of the journals with the ISSNs in "issn", and publish only
# the papers that have a free preprint. "search" sets where to look first.
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

    # --- free preprints of paywalled papers ----------------------------------
    {
        "kind": "preprints",
        "file": "risk-cutting-edge.xml",
        "title": "Risk.net Cutting Edge - Free Preprints",
        "description": "Risk.net Cutting Edge papers that have a free preprint on arXiv or SSRN",
        "page": "https://www.risk.net/cutting-edge",
        "rss": "https://www.risk.net/feeds/rss/category/cutting-edge",
        "paper_paths": ["risk.net/cutting-edge/"],
        "skip_paths": ["/cutting-edge/views/"],   # podcasts and opinion columns
        "search": ["arxiv", "ssrn"],
        "original_name": "Risk.net Cutting Edge",
    },
    {
        "kind": "preprints",
        "file": "journal-of-finance.xml",
        "title": "Journal of Finance - Free Preprints",
        "description": "Journal of Finance articles that have a free preprint on SSRN or arXiv",
        "page": "https://onlinelibrary.wiley.com/journal/15406261",
        "issn": ["0022-1082", "1540-6261"],
        "search": ["ssrn", "arxiv"],
        "years_before": 5,   # finance papers often circulate on SSRN for years
        "original_name": "Journal of Finance",
    },
    {
        "kind": "preprints",
        "file": "journal-of-portfolio-management.xml",
        "title": "Journal of Portfolio Management - Free Preprints",
        "description": "Journal of Portfolio Management articles that have a free preprint on SSRN or arXiv",
        "page": "https://www.pm-research.com/content/iijpormgmt",
        "issn": ["0095-4918", "2168-8656"],
        "search": ["ssrn", "arxiv"],
        "years_before": 3,
        "original_name": "Journal of Portfolio Management",
    },
    {
        "kind": "preprints",
        "file": "quantitative-finance.xml",
        "title": "Quantitative Finance - Free Preprints",
        "description": "Quantitative Finance (journal) articles that have a free preprint on arXiv or SSRN",
        "page": "https://www.tandfonline.com/journals/rquf20",
        "issn": ["1469-7688", "1469-7696"],
        "search": ["arxiv", "ssrn"],
        "years_before": 3,
        "original_name": "Quantitative Finance",
    },

    # --- research published by investment firms ------------------------------
    {
        "file": "oaktree-memos.xml",
        "title": "Oaktree - Howard Marks Memos",
        "description": "Memos from Howard Marks, co-chairman of Oaktree Capital",
        "page": "https://www.oaktreecapital.com/insights/memos",
        "article_pattern": r"oaktreecapital\.com/insights/memo/[^/?#]+$",
        "date_position": "before",
        "require_date": True,
        "max_items": 15,
        "full_text": True,
        "reuse_full_text": True,
    },
    {
        "file": "aqr-research.xml",
        "title": "AQR - Research",
        "description": "Research papers and articles from AQR Capital Management",
        "page": "https://www.aqr.com/Insights/Research",
        "article_pattern": r"aqr\.com/Insights/Research/[^/?#]+/[^/?#]+$",
        "date_position": "after",
        "require_date": True,
        "max_items": 15,
        "full_text": True,
        "reuse_full_text": True,
    },
    {
        "file": "bridgewater-research.xml",
        "title": "Bridgewater - Research & Insights",
        "description": "Research and insights from Bridgewater Associates",
        "page": "https://www.bridgewater.com/research-and-insights",
        "article_pattern": r"bridgewater\.com/research-and-insights/[^/?#]+$",
        "date_position": "after",
        "require_date": True,
        "max_items": 15,
        "full_text": True,
        "reuse_full_text": True,
    },
    {
        "file": "two-sigma-insights.xml",
        "title": "Two Sigma - Insights",
        "description": "Articles and research from Two Sigma",
        "page": "https://www.twosigma.com/insights/",
        "article_pattern": r"twosigma\.com/articles/[^/?#]+/?$",
        "date_position": "after",
        "require_date": True,
        "max_items": 15,
        "full_text": True,
        "reuse_full_text": True,
    },
    {
        "file": "apollo-daily-spark.xml",
        "title": "Apollo - The Daily Spark",
        "description": "Torsten Slok's daily chart and commentary from Apollo",
        "page": "https://www.apollo.com/wealth/insights-news/insights/daily-spark",
        "article_pattern": r"/insights/daily-spark/[^/?#]+$",
        "date_position": "before",
        "require_date": True,
        "max_items": 15,
        "full_text": True,
        "reuse_full_text": True,
    },
]


def main():
    OUT_DIR.mkdir(exist_ok=True)
    failed = False
    for source in SOURCES:
        preprints = source.get("kind") == "preprints"
        note = ""
        try:
            if preprints:
                items, note = preprint_items(source)
            else:
                items = extract_items(source)
        except Exception as e:  # noqa: BLE001
            print(f"[{source['file']}] fetch failed: {e}", file=sys.stderr)
            failed = True
            continue
        if not items and not preprints:
            # don't overwrite the old feed with an empty one; let Actions show a red X
            print(f"[{source['file']}] no articles found, the site layout may have changed", file=sys.stderr)
            failed = True
            continue
        if source.get("full_text"):
            add_full_text(items, source)
        (OUT_DIR / source["file"]).write_text(build_rss(source, items), encoding="utf-8")
        full = sum(1 for i in items if i.get("content"))
        print(f"[{source['file']}] {len(items)} articles, {full} with full text{note}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
