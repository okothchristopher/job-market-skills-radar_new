"""Real markup captured from the live Kenyan boards.

Shared by the JSON-LD tests and the adapter tests, which assert against the
same pages from different angles.
"""

from __future__ import annotations

# BrighterMonday's real shape: a @graph where hiringOrganization is an @id
# reference rather than an inline object.
BRIGHTERMONDAY_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"WebPage","@id":"https://x/listings/abc","name":"page"},
  {"@type":"Organization","@id":"https://x/#/schema/Organization/1",
   "name":"Med Bill, L.L.C"},
  {"@type":"JobPosting","@id":"https://x/#/schema/JobPosting/listing-1",
   "title":"Senior QA Automation Engineer",
   "datePosted":"2026-07-08T00:00:00.000000Z",
   "validThrough":"2026-10-06T00:00:00.000000Z",
   "employmentType":"CONTRACTOR",
   "industry":"Healthcare",
   "jobLocationType":"TELECOMMUTE",
   "applicantLocationRequirements":{"@type":"Country","name":"KE"},
   "baseSalary":{"@type":"MonetaryAmount","currency":"KES",
     "value":{"@type":"QuantitativeValue","minValue":250000,"maxValue":300000}},
   "hiringOrganization":{"@id":"https://x/#/schema/Organization/1"},
   "description":"<p>Selenium and Python</p>"}
]}
</script>
"""

# MyJobMag's real defect: raw CR/LF control characters inside a string value.
MYJOBMAG_HTML = (
    '<script type="application/ld+json">\n'
    "{\n"
    '  "@context": "http://schema.org",\n'
    '  "@type": "JobPosting",\n'
    '  "title": "Account Manager",\n'
    '  "datePosted": "2026-09-01T13:54:43+01:00",\n'
    '  "hiringOrganization": {"@type": "Organization", "name": "Tugende"},\n'
    '  "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",\n'
    '     "addressLocality": "Mombasa", "addressCountry": "KE"}},\n'
    '  "description": "&lt;p&gt;About the role&lt;/p&gt;\r\n\r\n&lt;ul&gt;\r\n\t'
    '&lt;li&gt;End-to-end client management&lt;/li&gt;&lt;/ul&gt;"\n'
    "}\n"
    "</script>"
)
