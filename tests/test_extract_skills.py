"""Skill extraction tests.

Most of these are adversarial: sentences drawn from the shapes real job ads
actually use, aimed at the skills where naive substring matching fails. A false
positive here does not look like a bug downstream — it looks like a trend.
"""

from __future__ import annotations

import pytest

from jobradar.extract.skills import SkillExtractor, load_taxonomy

TAXONOMY = "taxonomy/skills.csv"


@pytest.fixture(scope="module")
def extractor():
    return SkillExtractor.from_csv(TAXONOMY)


def found(extractor, text, title=""):
    return {m.skill for m in extractor.extract(title, text)}


# ------------------------------------------------------------------ taxonomy


def test_taxonomy_loads_and_is_substantial(extractor):
    assert len(extractor.skills) >= 140


def test_every_skill_has_a_zindua_track(extractor):
    """The track mapping is the deliverable — a skill without one cannot roll up
    onto a programme and is invisible to the curriculum brief."""
    missing = [s.skill for s in extractor.skills if not s.zindua_track]
    assert missing == []


def test_tracks_match_the_programme_list(extractor):
    known = {
        "Software Engineering Core",
        "Data Science Core",
        "Cybersecurity Core",
        "Data Analytics",
        "Frontend Development",
        "DevOps Engineering",
        "Data Engineering",
        "AI Engineering",
        "AI Workflow Automation",
        "Data Storytelling",
        "Product Management",
        "Data Structures & Algorithms",
    }
    assert set(extractor.tracks()) <= known


def test_canonical_names_are_unique(extractor):
    names = [s.canonical_name for s in extractor.skills]
    assert len(names) == len(set(names)), "duplicate canonical names double-count"


def test_diffusion_baseline_set_exists(extractor):
    """The calibration set from PLAN.md section 6. Without it the watchlist would
    rank how American a technology is, not whether it is arriving in Kenya."""
    baseline = extractor.baseline_skills()
    assert len(baseline) >= 5
    assert {"SQL", "Git", "Java", "Excel"} <= set(baseline)


def test_bad_regex_is_raised_not_skipped(tmp_path):
    """A silently dropped skill produces a zero indistinguishable from real
    absence — the exact failure this project is built to avoid."""
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "skill,canonical_name,category,zindua_track,match_pattern,negative_context,"
        "is_diffusion_baseline,notes\n"
        "Broken,broken,x,Data Analytics,[unclosed,,0,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bad match_pattern"):
        load_taxonomy(bad)


# ------------------------------------------------- ambiguous skills: R, Go, C


@pytest.mark.parametrize(
    "text",
    [
        "Strong programming skills in R and Python required.",
        "Experience with R Studio and statistical modelling.",
        "Analysis is done in R, with reporting in Power BI.",
        "Proficiency in Python & R for data analysis.",
    ],
)
def test_r_is_detected_with_a_co_signal(extractor, text):
    assert "R" in found(extractor, text)


@pytest.mark.parametrize(
    "text",
    [
        "Reporting to the R&D manager for this role.",
        "The candidate should have a Bachelor's degree.",
        "Our HR team will contact successful applicants.",
        "Must be registered with the relevant board.",
    ],
)
def test_r_is_not_matched_from_stray_letters(extractor, text):
    assert "R" not in found(extractor, text)


@pytest.mark.parametrize(
    "text",
    [
        "Backend services are written in Go.",
        "Experience with Golang and Kubernetes.",
        "We are hiring a Go developer for our platform team.",
    ],
)
def test_go_the_language_is_detected(extractor, text):
    assert "Go" in found(extractor, text)


@pytest.mark.parametrize(
    "text",
    [
        "You must be willing to go the extra mile for clients.",
        "We need a go-getter who thrives under pressure.",
        "Ready to go live with the product next quarter.",
        "She is the go-to person for escalations.",
        "Candidates must be able to go above and beyond.",
    ],
)
def test_go_idioms_are_vetoed(extractor, text):
    assert "Go" not in found(extractor, text)


def test_go_survives_an_idiom_in_a_neighbouring_sentence(extractor):
    """Negative context is scoped to the sentence. A document-level veto would
    discard the real mention because of unrelated boilerplate."""
    text = "We move fast and you must go the extra mile. Our backend is written in Go."
    assert "Go" in found(extractor, text)


@pytest.mark.parametrize(
    "text",
    [
        "Reports directly to C-level executives.",
        "Engagement with the C-suite is expected.",
        "Vitamin C supplementation programmes.",
    ],
)
def test_c_is_not_matched_from_business_jargon(extractor, text):
    assert "C" not in found(extractor, text)


def test_c_is_detected_in_programming_context(extractor):
    assert "C" in found(extractor, "Embedded work in C programming and assembly.")


# --------------------------------------------- ambiguous: Swift, Excel, Rust


def test_swift_the_language_is_detected(extractor):
    assert "Swift" in found(extractor, "iOS development using Swift and UIKit.")


@pytest.mark.parametrize(
    "text",
    [
        "We expect swift resolution of customer issues.",
        "Provide a swift response to all enquiries.",
        "Process SWIFT payment messages accurately.",
        "Ensure swift turnaround on all deliverables.",
    ],
)
def test_swift_non_language_uses_are_vetoed(extractor, text):
    assert "Swift" not in found(extractor, text)


def test_excel_the_tool_is_detected(extractor):
    assert "Excel" in found(extractor, "Advanced Microsoft Excel including pivot tables.")


@pytest.mark.parametrize(
    "text",
    [
        "The successful candidate will excel at stakeholder management.",
        "We are looking for someone with excellent communication skills.",
        "You will excel in a fast-paced environment.",
    ],
)
def test_excel_the_verb_is_vetoed(extractor, text):
    assert "Excel" not in found(extractor, text)


def test_rust_the_language_is_detected(extractor):
    assert "Rust" in found(extractor, "Systems programming in Rust and C++.")


@pytest.mark.parametrize(
    "text",
    [
        "Apply rust proofing to all metal surfaces.",
        "Experience with corrosion and rust treatment.",
        "Inspect for rust resistant coatings.",
    ],
)
def test_rust_the_corrosion_is_vetoed(extractor, text):
    assert "Rust" not in found(extractor, text)


# -------------------------------------------------- other tricky boundaries


def test_java_and_javascript_do_not_bleed(extractor):
    assert found(extractor, "Strong JavaScript skills required.") & {"Java", "JavaScript"} == {
        "JavaScript"
    }
    assert "Java" in found(extractor, "Backend development in Java and Spring Boot.")


def test_git_does_not_match_github(extractor):
    hits = found(extractor, "Use GitHub for version control.")
    assert "GitHub" in hits and "Git" not in hits


def test_react_the_verb_is_vetoed(extractor):
    assert "React" not in found(extractor, "You must react to incidents quickly.")
    assert "React" in found(extractor, "Frontend built with React and Redux.")


def test_spark_idioms_are_vetoed(extractor):
    assert "Apache Spark" not in found(extractor, "This role will spark interest across teams.")
    assert "Apache Spark" in found(extractor, "Big data processing with Apache Spark.")


def test_express_the_verb_is_vetoed(extractor):
    assert "Express" not in found(extractor, "Please express interest by Friday.")


def test_sql_does_not_match_nosql(extractor):
    assert "SQL" not in found(extractor, "Experience with NoSQL databases preferred.")


# ------------------------------------------------------- aliases and variants


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Experience with PostgreSQL clusters.", "PostgreSQL"),
        ("We run Postgres in production.", "PostgreSQL"),
        ("Deep psql knowledge required.", "PostgreSQL"),
        ("Node.js backend services.", "Node.js"),
        ("Strong nodejs experience.", "Node.js"),
        ("Familiarity with scikit-learn.", "scikit-learn"),
        ("Uses sklearn for modelling.", "scikit-learn"),
        ("Container orchestration with k8s.", "Kubernetes"),
        ("Deploy on Kubernetes clusters.", "Kubernetes"),
        ("Building with LangGraph agents.", "LangGraph"),
        ("Familiar with lang chain pipelines.", "LangChain"),
    ],
)
def test_aliases_map_to_one_canonical_skill(extractor, text, expected):
    assert expected in found(extractor, text)


# ------------------------------------------------------------ Kenya-specific


def test_kenyan_fintech_skills_are_captured(extractor):
    """These should surface as kenya_specific in the diffusion engine — real
    local demand that the global signal is structurally blind to."""
    text = "Integrate M-Pesa via the Daraja API and build USSD menus with Africa's Talking."
    hits = found(extractor, text)
    assert {"M-Pesa", "USSD", "Africa's Talking"} <= hits


# ------------------------------------------------------------- match metadata


def test_title_matches_are_labelled(extractor):
    matches = extractor.extract("Senior Python Engineer", "We use Django and Postgres.")
    by_skill = {m.skill: m for m in matches}
    assert by_skill["Python"].matched_in == "title"
    assert by_skill["Django"].matched_in == "description"


def test_skill_in_both_title_and_body(extractor):
    matches = extractor.extract("Python Developer", "Strong Python and Flask experience.")
    assert next(m for m in matches if m.skill == "Python").matched_in == "both"


def test_mentions_are_counted(extractor):
    matches = extractor.extract("", "Docker is required. We use Docker daily. Docker Compose too.")
    assert next(m for m in matches if m.skill == "Docker").n_mentions == 3


def test_evidence_is_captured_for_auditing(extractor):
    """Being able to see why a skill was counted is what makes the numbers
    defensible in front of a curriculum committee."""
    matches = extractor.extract("", "We build data pipelines with Apache Airflow.")
    airflow = next(m for m in matches if m.skill == "Apache Airflow")
    assert airflow.evidence and "Airflow" in airflow.evidence[0]


def test_empty_input_is_safe(extractor):
    assert extractor.extract(None, None) == []
    assert extractor.extract("", "") == []


def test_a_realistic_posting_extracts_a_sensible_stack(extractor):
    text = """
    We are looking for a Senior Backend Engineer in Nairobi.
    You will build REST APIs in Python using Django and PostgreSQL,
    deploy with Docker and Kubernetes on AWS, and help maintain our CI/CD
    pipelines. Experience with Redis and unit testing is required.
    The successful candidate will excel at collaboration and go the extra mile.
    """
    hits = found(extractor, text, title="Senior Backend Engineer")
    assert {
        "Python",
        "Django",
        "PostgreSQL",
        "Docker",
        "Kubernetes",
        "AWS",
        "CI/CD",
        "Redis",
        "REST API",
        "Testing",
    } <= hits
    # The boilerplate at the end must not produce phantom skills.
    assert "Excel" not in hits
    assert "Go" not in hits


# ------------------------------ regressions found by auditing the live corpus


@pytest.mark.parametrize(
    "text",
    [
        "Work with C++, TypeScript within an AWS environment.",
        "Strong proficiency in C# and .NET required.",
    ],
)
def test_c_does_not_swallow_cplusplus_or_csharp(extractor, text):
    """\bC\b matches the C in "C++" -- the boundary sits between C and + -- so
    the bare-C skill was inflated by every C++ and C# posting."""
    assert "C" not in found(extractor, text)


def test_c_still_matches_a_real_language_list(extractor):
    hits = found(extractor, "Projects using C, C++, C#, Java and Python.")
    assert {"C", "C++", "C#"} <= hits


@pytest.mark.parametrize(
    "text",
    [
        "We foster a culture where everyone can excel.",
        "A place where you will excel and grow.",
        "Helping our people to excel every day.",
    ],
)
def test_excel_the_verb_is_vetoed_by_grammar(extractor, text):
    """This exact boilerplate appeared three times in one company's postings."""
    assert "Excel" not in found(extractor, text)


@pytest.mark.parametrize(
    "text",
    [
        "Strong Excel skills and an interest in automation.",
        "Advanced Microsoft Excel including pivot tables.",
    ],
)
def test_excel_the_tool_still_matches(extractor, text):
    assert "Excel" in found(extractor, text)


def test_express_the_verb_does_not_match(extractor):
    """The old lookahead ended in '|\b', which is always true, so the framework
    requirement was inert and 'express ideas effectively' counted."""
    assert "Express" not in found(
        extractor, "The ability to articulate thoughts and express ideas effectively."
    )


def test_express_the_framework_still_matches(extractor):
    assert "Express" in found(extractor, "Backend built on Express framework and Node.")


def test_swift_the_adjective_is_vetoed(extractor):
    assert "Swift" not in found(
        extractor, "Swift deployment of charging infrastructure is critical."
    )


def test_swift_the_language_still_matches(extractor):
    assert "Swift" in found(extractor, "Our stack: Swift and SwiftUI on mobile.")


def test_r_does_not_match_research_and_development(extractor):
    """'with R&D hubs' satisfied the '(with) R' co-signal, because the word
    boundary sits between R and &."""
    assert "R" not in found(extractor, "We have R&D hubs across Europe and the UK.")
    assert "R" not in found(extractor, "Reporting to the R & D director.")


def test_r_still_matches_the_language(extractor):
    assert "R" in found(extractor, "Hands-on experience with Python/R and SQL is essential.")


@pytest.mark.parametrize(
    "text",
    [
        "We craft experiences that spark engagement and shape perceptions.",
        "Recommend the mix of Colo and Spark modular deployments.",
        "This project sparked interest across the business.",
    ],
)
def test_spark_marketing_copy_is_vetoed(extractor, text):
    assert "Apache Spark" not in found(extractor, text)


def test_spark_the_engine_still_matches(extractor):
    assert "Apache Spark" in found(
        extractor, "Deep experience with distributed computing with Apache Spark."
    )
