"""Description text extraction tests.

These exist because the failure mode is silent. Text that loses its block
boundaries still looks like text; it just quietly stops matching skills, and the
resulting counts look plausible.
"""

from __future__ import annotations

import pytest

from jobradar.parse.text import html_to_text, infer_remote, infer_seniority, sentences


def test_list_items_do_not_run_together():
    """The core reason this module exists.

    ``<li>Python</li><li>Go</li>`` naively stripped becomes "PythonGo", which
    matches neither skill — a silent undercount of exactly the list-formatted
    requirements sections where skills actually live.
    """
    text = html_to_text("<ul><li>Python</li><li>Go</li><li>PostgreSQL</li></ul>")
    assert "PythonGo" not in text
    assert "Python" in text and "Go" in text and "PostgreSQL" in text


def test_paragraphs_become_separate_lines():
    text = html_to_text("<p>We use React.</p><p>Go experience is a plus.</p>")
    assert "React.Go" not in text
    assert len(text.splitlines()) >= 2


def test_double_encoded_html_is_unescaped():
    """Greenhouse returns &lt;p&gt; rather than <p> in its `content` field."""
    text = html_to_text(
        "&lt;p&gt;Experience with &lt;strong&gt;Kubernetes&lt;/strong&gt;&lt;/p&gt;"
    )
    assert "Kubernetes" in text
    assert "<" not in text and "&lt;" not in text


def test_entities_are_decoded():
    assert "R&D" in html_to_text("<p>R&amp;D team</p>")


def test_scripts_and_styles_are_dropped():
    text = html_to_text("<div>Python<script>var Java = 1;</script><style>.Go{}</style></div>")
    assert "Python" in text
    assert "var Java" not in text


def test_empty_input_is_safe():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_unicode_survives():
    """Job text is full of curly quotes and accents; mangling them would corrupt
    company names used in the dedupe key."""
    assert "Africa\u2019s" in html_to_text("<p>Africa\u2019s platform</p>")


def test_excess_blank_lines_collapse():
    assert "\n\n\n" not in html_to_text("<p>a</p><br><br><br><p>b</p>")


# ------------------------------------------------------------------ sentences


def test_sentences_split_on_terminators_and_newlines():
    parts = sentences("We move fast. You must go the extra mile.\nWe use Go daily.")
    assert len(parts) == 3
    assert any("extra mile" in p for p in parts)
    assert any("use Go daily" in p for p in parts)


# --------------------------------------------------------------------- remote


@pytest.mark.parametrize(
    "fields",
    [("Remote (Work From Home)",), ("Nairobi", "fully remote role"), ("100% remote",)],
)
def test_remote_is_detected(fields):
    assert infer_remote(*fields) is True


@pytest.mark.parametrize("fields", [("On-site, Nairobi",), ("Hybrid - Westlands",)])
def test_onsite_is_detected(fields):
    assert infer_remote(*fields) is False


def test_unknown_remote_status_is_none_not_false():
    """'We could not tell' and 'definitely not remote' are different facts.
    Collapsing them biases any remote-share analysis downward."""
    assert infer_remote("Nairobi, Kenya") is None
    assert infer_remote(None, "") is None


# ------------------------------------------------------------------ seniority


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Software Engineering Intern", "intern"),
        ("Junior Data Analyst", "junior"),
        ("Senior Backend Engineer", "senior"),
        ("Staff Software Engineer", "lead"),
        ("Head of Data", "lead"),
        ("Principal Architect", "lead"),
    ],
)
def test_seniority_inference(title, expected):
    assert infer_seniority(title) == expected


def test_lead_beats_senior_when_both_present():
    """'Senior Staff Engineer' is a lead-track role, not a senior one."""
    assert infer_seniority("Senior Staff Engineer") == "lead"


def test_unmarked_titles_return_none_not_mid():
    """Defaulting to 'mid' would invent a level and distort the breakdown."""
    assert infer_seniority("Software Engineer") is None
    assert infer_seniority(None) is None
