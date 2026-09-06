"""Turning job description markup into plain text for skill extraction.

Two things matter here, and both affect the counts downstream.

**Structure must survive as whitespace.** ``<li>Python</li><li>Go</li>`` naively
stripped becomes ``PythonGo``, which matches neither skill. Block-level elements
therefore become newlines before tags are removed.

**Sentence boundaries must survive.** The extractor's negative-context guards
work per sentence — "we move fast, so you must be able to go the extra mile" has
to stay separable from a neighbouring sentence mentioning Go the language.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

# Elements whose boundaries are semantic; each becomes a line break.
_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "ul",
    "ol",
    "tr",
    "td",
    "th",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
    "hr",
}

# \xa0 is a non-breaking space: ubiquitous in scraped descriptions, and it
# breaks the \b word boundaries the skill patterns rely on.
_WHITESPACE = re.compile("[ \\t\\xa0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(markup: str | None) -> str:
    """Convert description markup to plain text, preserving block boundaries."""
    if not markup:
        return ""

    # Greenhouse and several JSON-LD sources double-encode: the payload contains
    # &lt;p&gt; rather than <p>. Unescape first so the parser sees real tags.
    text = markup
    if "&lt;" in text and "<" not in text:
        text = html.unescape(text)

    soup = BeautifulSoup(text, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")

    out = soup.get_text()
    out = html.unescape(out)
    # Zero-width space and BOM: invisible, but they break  word
    # boundaries in the skill patterns, so a skill next to one stops matching.
    # Zero-width space and BOM are invisible but still break word boundaries,
    # so a skill sitting next to one silently stops matching.
    out = out.replace("\u200b", "").replace("\ufeff", "")
    out = _WHITESPACE.sub(" ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    out = _BLANK_LINES.sub("\n\n", out)
    return out.strip()


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str) -> list[str]:
    """Split text into rough sentences.

    Deliberately simple. The extractor uses these only to scope negative-context
    guards, so an occasional bad split costs one guard check, not a wrong count.
    """
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


_REMOTE_PATTERNS = re.compile(
    r"\b(fully[- ]remote|100% remote|work from home|remote[- ]first|"
    r"remote \(|remote,|remote$|anywhere in the world|distributed team)\b",
    re.IGNORECASE,
)
_ONSITE_PATTERNS = re.compile(r"\b(on[- ]?site|in[- ]office|hybrid)\b", re.IGNORECASE)


def infer_remote(*fields: str | None) -> bool | None:
    """Best-effort remote flag from location and title text.

    Returns None rather than False when nothing is stated — "we could not tell"
    and "definitely not remote" are different facts, and collapsing them would
    quietly bias any remote-share analysis downward.
    """
    blob = " ".join(f for f in fields if f)
    if not blob.strip():
        return None
    if _REMOTE_PATTERNS.search(blob):
        return True
    if _ONSITE_PATTERNS.search(blob):
        return False
    return None


_SENIORITY = [
    ("intern", re.compile(r"\b(intern|internship|graduate trainee|attach[eé])\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|associate|graduate|trainee)\b", re.I)),
    ("lead", re.compile(r"\b(lead|principal|staff|head of|director|vp|chief|architect)\b", re.I)),
    ("senior", re.compile(r"\b(senior|snr\.?|sr\.?|experienced)\b", re.I)),
]


def infer_seniority(title: str | None) -> str | None:
    """Infer seniority from the job title.

    Order matters: "Senior Staff Engineer" is a lead-track role, so ``lead`` is
    tested before ``senior``. Titles with no marker return None rather than being
    defaulted to mid — inventing a level would distort any seniority breakdown.
    """
    if not title:
        return None
    for level, pattern in _SENIORITY:
        if pattern.search(title):
            return level
    return None
