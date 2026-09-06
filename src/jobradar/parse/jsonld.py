"""JSON-LD extraction for the Kenyan job boards.

All three Kenyan sources embed a schema.org ``JobPosting``, which is far more
stable than CSS selectors — a site redesign rarely changes its structured data,
because that data is what feeds Google Jobs.

The catch, found while smoke-testing BrighterMonday in Phase 1: their JSON-LD is
a ``@graph`` and ``hiringOrganization`` is an **``@id`` reference into it**, not
an inline object::

    {"@graph": [
        {"@type": "JobPosting",
         "hiringOrganization": {"@id": ".../schema/Organization/1182963"}},
        {"@type": "Organization",
         "@id": ".../schema/Organization/1182963",
         "name": "Med Bill, L.L.C"}
    ]}

A naive ``posting["hiringOrganization"]["name"]`` returns None. That matters more
than a missing field usually would, because ``company`` feeds the cross-board
dedupe key — silently losing it means the same job on BrighterMonday and
MyJobMag stops collapsing, and both get counted.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def iter_blocks(html: str) -> list[Any]:
    """Return every parseable JSON-LD block in a page.

    Parsing is deliberately lenient, because real sites ship JSON that is not
    quite JSON. MyJobMag embeds **raw CR/LF control characters inside string
    values**, which the spec forbids and ``json.loads`` rejects outright. Strict
    parsing there returns zero blocks for every single posting — losing the whole
    source silently, with no error and no empty-result signal, just a table that
    never gains rows.

    So: try strict first, then permit control characters, then strip the ones
    that are still illegal. A block that survives none of that is skipped rather
    than raised on, since a page often ships one broken block beside good ones.
    """
    blocks: list[Any] = []
    for raw in _SCRIPT_RE.findall(html or ""):
        parsed = _loads_lenient(raw.strip())
        if parsed is not None:
            blocks.append(parsed)
    return blocks


# Control characters that are illegal unescaped inside a JSON string, excluding
# the ones strict=False already tolerates.
_ILLEGAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _loads_lenient(raw: str) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        # strict=False permits literal control characters inside strings, which
        # is exactly MyJobMag's defect.
        return json.loads(raw, strict=False)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return json.loads(_ILLEGAL_CONTROL.sub("", raw), strict=False)
    except (json.JSONDecodeError, ValueError):
        return None


def _walk(node: Any):
    """Yield every dict in a nested JSON-LD structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def build_index(blocks: list[Any]) -> dict[str, dict]:
    """Map ``@id`` to the node that defines it, across all blocks."""
    index: dict[str, dict] = {}
    for block in blocks:
        for node in _walk(block):
            node_id = node.get("@id")
            # A node that carries only @id is a reference, not a definition;
            # indexing it would overwrite the real definition with a stub.
            if isinstance(node_id, str) and len(node) > 1:
                index.setdefault(node_id, node)
    return index


def resolve(value: Any, index: dict[str, dict], _depth: int = 0) -> Any:
    """Follow an ``@id`` reference to its definition, if there is one.

    Returns the value unchanged when it is already inline or the target is
    missing. Depth-limited because a graph may legitimately contain cycles
    (an Organization referencing a WebSite that references it back).
    """
    if _depth > 4 or not isinstance(value, dict):
        return value
    node_id = value.get("@id")
    if isinstance(node_id, str) and len(value) == 1 and node_id in index:
        return resolve(index[node_id], index, _depth + 1)
    return value


def find_job_posting(html: str) -> tuple[dict | None, dict[str, dict]]:
    """Return ``(JobPosting node, @id index)`` for a job detail page.

    The index is returned alongside so callers can resolve the posting's own
    references — see :func:`organization_name`.
    """
    blocks = iter_blocks(html)
    if not blocks:
        return None, {}
    index = build_index(blocks)
    for block in blocks:
        for node in _walk(block):
            if node.get("@type") == "JobPosting":
                return node, index
    return None, index


def organization_name(posting: dict, index: dict[str, dict]) -> str | None:
    """Hiring organisation name, resolving an ``@id`` reference if needed."""
    org = resolve(posting.get("hiringOrganization"), index)
    if isinstance(org, dict):
        name = org.get("name") or org.get("legalName")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(org, str) and org.strip():
        return org.strip()
    return None


def location_text(posting: dict, index: dict[str, dict]) -> str | None:
    """Flatten ``jobLocation`` into readable text.

    schema.org allows a single object or a list, with the address nested one or
    two levels down. We take the most specific parts available and join them.
    """
    raw = posting.get("jobLocation")
    if raw is None:
        return None
    entries = raw if isinstance(raw, list) else [raw]

    parts: list[str] = []
    for entry in entries:
        node = resolve(entry, index)
        if isinstance(node, str):
            parts.append(node)
            continue
        if not isinstance(node, dict):
            continue
        address = resolve(node.get("address"), index)
        if isinstance(address, str):
            parts.append(address)
        elif isinstance(address, dict):
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                value = address.get(key)
                if isinstance(value, dict):
                    value = value.get("name")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        elif isinstance(node.get("name"), str):
            parts.append(node["name"])

    # Preserve order while dropping repeats ("Nairobi, Nairobi, Kenya").
    seen: set[str] = set()
    unique = [p for p in parts if not (p.lower() in seen or seen.add(p.lower()))]
    return ", ".join(unique) if unique else None


def remote_flag(posting: dict) -> bool | None:
    """Whether the posting is remote, per schema.org's convention.

    Remote roles set ``jobLocationType: TELECOMMUTE`` and often omit
    ``jobLocation`` entirely — so an empty location is not evidence of an
    on-site role, and treating it as one would bias the remote share downward.
    Returns None when nothing is stated either way.
    """
    location_type = posting.get("jobLocationType")
    if isinstance(location_type, list):
        location_type = next((v for v in location_type if isinstance(v, str)), None)
    if isinstance(location_type, str) and "telecommute" in location_type.lower():
        return True
    if posting.get("jobLocation"):
        return False
    return None


def applicant_location(posting: dict, index: dict[str, dict]) -> str | None:
    """Country or region a remote posting will accept applicants from.

    For a TELECOMMUTE role this is the only geography on offer, and it is what
    tells us a remote job is actually open to Kenya.
    """
    raw = posting.get("applicantLocationRequirements")
    if raw is None:
        return None
    entries = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    for entry in entries:
        node = resolve(entry, index)
        if isinstance(node, str) and node.strip():
            names.append(node.strip())
        elif isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return ", ".join(names) if names else None


def employment_type(posting: dict) -> str | None:
    value = posting.get("employmentType")
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str)), None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def salary(posting: dict, index: dict[str, dict]) -> tuple[float | None, float | None, str | None]:
    """Extract ``(min, max, currency)`` from ``baseSalary``.

    Kenyan postings rarely state salary, so this is usually a null — but when it
    is present it is worth having, and the shape is a nested MonetaryAmount.
    """
    base = resolve(posting.get("baseSalary"), index)
    if not isinstance(base, dict):
        return None, None, None

    currency = base.get("currency") or base.get("priceCurrency")
    value = resolve(base.get("value"), index)

    def _num(x):
        try:
            return float(x) if x not in (None, "") else None
        except (TypeError, ValueError):
            return None

    if isinstance(value, dict):
        low = _num(value.get("minValue"))
        high = _num(value.get("maxValue"))
        exact = _num(value.get("value"))
        if low is None and high is None and exact is not None:
            low = high = exact
        currency = currency or value.get("currency")
    else:
        low = high = _num(value)

    if isinstance(currency, str):
        currency = currency.strip() or None
    else:
        currency = None
    return low, high, currency
