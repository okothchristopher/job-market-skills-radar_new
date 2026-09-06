"""JSON-LD parsing tests.

Both regressions pinned here caused total, silent data loss when first hit: not
an error, not an empty result to notice, just a source that never produced rows.
"""

from __future__ import annotations

from fixtures_html import BRIGHTERMONDAY_HTML, MYJOBMAG_HTML

from jobradar.parse import jsonld

# ------------------------------------------------- regression: @id references


def test_hiring_organization_resolves_through_an_id_reference():
    """A naive posting["hiringOrganization"]["name"] returns None here.

    That matters more than a normal missing field: company feeds the cross-board
    dedupe key, so losing it stops the same job on two boards from collapsing
    and both get counted.
    """
    posting, index = jsonld.find_job_posting(BRIGHTERMONDAY_HTML)
    assert posting is not None
    assert posting["hiringOrganization"].get("name") is None  # the trap
    assert jsonld.organization_name(posting, index) == "Med Bill, L.L.C"


def test_reference_only_nodes_do_not_shadow_definitions():
    """A node carrying only @id is a pointer, not a definition; indexing it
    would overwrite the real record with a stub."""
    _, index = jsonld.find_job_posting(BRIGHTERMONDAY_HTML)
    assert index["https://x/#/schema/Organization/1"]["name"] == "Med Bill, L.L.C"


def test_resolve_is_cycle_safe():
    """Graphs legitimately contain cycles (Organization <-> WebSite)."""
    index = {"a": {"@id": "b"}, "b": {"@id": "a"}}
    assert jsonld.resolve({"@id": "a"}, index) is not None


def test_resolve_passes_through_inline_objects():
    assert jsonld.resolve({"name": "Acme"}, {}) == {"name": "Acme"}


# ------------------------------- regression: control characters in JSON strings


def test_control_characters_inside_strings_still_parse():
    """MyJobMag embeds raw CR/LF inside description strings, which the JSON spec
    forbids. Strict parsing returns zero blocks for every posting on the site --
    the whole source lost with no error and no empty-result signal."""
    posting, index = jsonld.find_job_posting(MYJOBMAG_HTML)
    assert posting is not None
    assert posting["title"] == "Account Manager"
    assert jsonld.organization_name(posting, index) == "Tugende"


def test_malformed_block_is_skipped_not_raised():
    html = '<script type="application/ld+json">{not json at all}</script>'
    assert jsonld.iter_blocks(html) == []
    assert jsonld.find_job_posting(html) == (None, {})


def test_good_block_survives_a_broken_sibling():
    html = (
        '<script type="application/ld+json">{oops</script>'
        '<script type="application/ld+json">{"@type":"JobPosting","title":"Dev"}</script>'
    )
    posting, _ = jsonld.find_job_posting(html)
    assert posting is not None and posting["title"] == "Dev"


def test_page_with_no_jsonld():
    assert jsonld.find_job_posting("<html><body>nothing</body></html>") == (None, {})


# --------------------------------------------------------------- field mapping


def test_location_flattens_nested_address():
    posting, index = jsonld.find_job_posting(MYJOBMAG_HTML)
    assert jsonld.location_text(posting, index) == "Mombasa, KE"


def test_location_deduplicates_repeated_parts():
    html = (
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"x","jobLocation":{"address":'
        '{"addressLocality":"Nairobi","addressRegion":"Nairobi","addressCountry":"KE"}}}'
        "</script>"
    )
    posting, index = jsonld.find_job_posting(html)
    assert jsonld.location_text(posting, index) == "Nairobi, KE"


def test_remote_flag_from_telecommute():
    """Remote postings often omit jobLocation entirely. Reading an absent
    location as 'on-site' would bias the remote share downward."""
    posting, _ = jsonld.find_job_posting(BRIGHTERMONDAY_HTML)
    assert posting.get("jobLocation") is None
    assert jsonld.remote_flag(posting) is True


def test_remote_flag_false_when_a_physical_location_is_given():
    posting, _ = jsonld.find_job_posting(MYJOBMAG_HTML)
    assert jsonld.remote_flag(posting) is False


def test_remote_flag_unknown_returns_none():
    html = '<script type="application/ld+json">{"@type":"JobPosting","title":"x"}</script>'
    posting, _ = jsonld.find_job_posting(html)
    assert jsonld.remote_flag(posting) is None


def test_applicant_location_is_read_for_remote_roles():
    posting, index = jsonld.find_job_posting(BRIGHTERMONDAY_HTML)
    assert jsonld.applicant_location(posting, index) == "KE"


def test_salary_range_is_extracted():
    """Kenyan salary data is scarce enough to be worth capturing when present."""
    posting, index = jsonld.find_job_posting(BRIGHTERMONDAY_HTML)
    assert jsonld.salary(posting, index) == (250000.0, 300000.0, "KES")


def test_salary_absent_returns_nulls():
    posting, index = jsonld.find_job_posting(MYJOBMAG_HTML)
    assert jsonld.salary(posting, index) == (None, None, None)


def test_employment_type_handles_a_list():
    html = (
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"x","employmentType":["FULL_TIME","CONTRACTOR"]}'
        "</script>"
    )
    posting, _ = jsonld.find_job_posting(html)
    assert jsonld.employment_type(posting) == "FULL_TIME"
