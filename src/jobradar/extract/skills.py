"""Skill extraction from job description text.

The taxonomy is code, not data. Each row carries an explicit ``match_pattern``
and an optional ``negative_context``, because naive substring matching fails
hardest on exactly the skills that matter most:

============  ==================================  ==================================
Skill         Naive failure                       Guard
============  ==================================  ==================================
``R``         matches every letter R              requires a co-signal (R Studio,
                                                  "in R,", "R and Python")
``Go``        "go the extra mile", "go-getter"    idiom veto list
``C``         "C-level", "C-suite"                requires programming context
``Swift``     "swift response", SWIFT banking     adjective and finance veto
``Excel``     "excel at", "excellent"             suffix veto
``Rust``      corrosion in manufacturing ads      rust-treatment veto
``Spark``     "spark joy", "sparked interest"     idiom veto
============  ==================================  ==================================

**Negative context is scoped to the sentence containing the match**, not the
whole document. A posting can legitimately say "we move fast, so you must go the
extra mile" in one sentence and "experience with Go" in another; a
document-level veto would throw away the real mention.

The extractor never counts a skill it cannot defend. When a pattern is
ambiguous, the taxonomy demands a co-signal rather than guessing — undercounting
a skill is recoverable, but a phantom trend is not.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..parse.text import sentences


@dataclass(frozen=True)
class Skill:
    """One taxonomy row, with its patterns pre-compiled."""

    skill: str
    canonical_name: str
    category: str
    zindua_track: str
    pattern: re.Pattern[str]
    negative: re.Pattern[str] | None
    is_diffusion_baseline: bool
    notes: str = ""


@dataclass
class SkillMatch:
    skill: str
    canonical_name: str
    category: str
    zindua_track: str
    n_mentions: int
    matched_in: str  # "title", "description", or "both"
    evidence: list[str] = field(default_factory=list)


def load_taxonomy(path: str | Path) -> list[Skill]:
    """Read and compile the taxonomy CSV.

    A malformed regex is raised immediately rather than skipped: a silently
    dropped skill produces a zero that is indistinguishable from real absence,
    which is the failure mode this whole project is built to avoid.
    """
    skills: list[Skill] = []
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for line_no, row in enumerate(csv.DictReader(fh), start=2):
            name = (row.get("skill") or "").strip()
            pattern = (row.get("match_pattern") or "").strip()
            if not name or not pattern:
                continue
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(
                    f"{path}:{line_no}: bad match_pattern for {name!r}: {exc}"
                ) from exc

            negative_raw = (row.get("negative_context") or "").strip()
            negative = None
            if negative_raw:
                try:
                    negative = re.compile(negative_raw, re.IGNORECASE)
                except re.error as exc:
                    raise ValueError(
                        f"{path}:{line_no}: bad negative_context for {name!r}: {exc}"
                    ) from exc

            skills.append(
                Skill(
                    skill=name,
                    canonical_name=(row.get("canonical_name") or name).strip().lower(),
                    category=(row.get("category") or "").strip(),
                    zindua_track=(row.get("zindua_track") or "").strip(),
                    pattern=compiled,
                    negative=negative,
                    is_diffusion_baseline=str(row.get("is_diffusion_baseline") or "0").strip()
                    in {"1", "true", "yes"},
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return skills


class SkillExtractor:
    """Finds taxonomy skills in job text."""

    def __init__(self, skills: list[Skill]) -> None:
        self.skills = skills

    @classmethod
    def from_csv(cls, path: str | Path) -> SkillExtractor:
        return cls(load_taxonomy(path))

    def _count_in(self, skill: Skill, text: str) -> tuple[int, list[str]]:
        """Count defensible mentions of ``skill`` in ``text``.

        Sentence-scoped: each sentence is tested independently so a veto in one
        clause cannot suppress a genuine mention in another.
        """
        if not text:
            return 0, []
        # Cheap pre-filter: skip the sentence split entirely when the skill
        # cannot possibly appear. Over ~150 skills and 40k documents this is the
        # difference between minutes and hours.
        if not skill.pattern.search(text):
            return 0, []

        count = 0
        evidence: list[str] = []
        for sentence in sentences(text):
            hits = skill.pattern.findall(sentence)
            if not hits:
                continue
            if skill.negative is not None and skill.negative.search(sentence):
                continue  # vetoed in this sentence only
            count += len(hits)
            if len(evidence) < 3:
                evidence.append(sentence.strip()[:200])
        return count, evidence

    def extract(self, title: str | None, description: str | None) -> list[SkillMatch]:
        """Return every skill found, with where it was found and how often."""
        title = title or ""
        description = description or ""
        matches: list[SkillMatch] = []

        for skill in self.skills:
            title_count, title_evidence = self._count_in(skill, title)
            body_count, body_evidence = self._count_in(skill, description)
            total = title_count + body_count
            if total == 0:
                continue

            if title_count and body_count:
                where = "both"
            elif title_count:
                where = "title"
            else:
                where = "description"

            matches.append(
                SkillMatch(
                    skill=skill.skill,
                    canonical_name=skill.canonical_name,
                    category=skill.category,
                    zindua_track=skill.zindua_track,
                    n_mentions=total,
                    matched_in=where,
                    evidence=(title_evidence + body_evidence)[:3],
                )
            )
        return matches

    def skill_names(self) -> list[str]:
        return [s.skill for s in self.skills]

    def baseline_skills(self) -> list[str]:
        """Skills flagged as fully diffused, used to calibrate the diffusion gap.

        These are the reference set for PLAN.md section 6: the median gap across
        them estimates the structural bias of our global sources, which is then
        subtracted from every skill's raw gap.
        """
        return [s.skill for s in self.skills if s.is_diffusion_baseline]

    def tracks(self) -> dict[str, list[str]]:
        """Skills grouped by Zindua programme — the curriculum roll-up."""
        out: dict[str, list[str]] = {}
        for skill in self.skills:
            out.setdefault(skill.zindua_track, []).append(skill.skill)
        return out
