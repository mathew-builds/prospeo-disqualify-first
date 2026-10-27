# Prospeo company-search values this skill uses

From Prospeo's public filters documentation (saved 23 Oct 2026). Enums are checked by Prospeo: a wrong value
returns HTTP 400 `INVALID_FILTERS`, which is free, and the engine halts on it.

## Resolve with `prospeo.py suggest` (free), never type them

| Filter | Flag | Notes |
|---|---|---|
| `company_location_search.include` | `--location "united kingdom"` | Copy the returned `name` exactly ("United Kingdom", not "UK") |
| `company_technology.include` | `--technology "hubspot"` | Max 20 values. Detected on the website, not proof of a CRM |
| `company_industry.include` | `--industry "software"` | 256 values |
| `company_naics`, `company_sics` | `--naics`, `--sic` | Code or text |

## Fixed values (copy exactly)

- `company_headcount_range`: `1-10`, `11-20`, `21-50`, `51-100`, `101-200`, `201-500`, `501-1000`, `1001-2000`,
  `2001-5000`, `5001-10000`, `10000+`. "21-200 staff" is `["21-50","51-100","101-200"]`. Cannot be combined with
  `company_headcount_custom`.
- `company_type.business_model`: `b2b`, `b2c`, `b2b2c`, `d2c`, `marketplace`, `franchise`, `non_profit`, `government`.
- `company_type.subtypes.include`: `SaaS`, `Platform`, `FinTech`, `HealthTech`, `AI/ML`, `Data/Analytics`, `Agency`,
  `Consulting`, `Marketplace`, `E-commerce`, and others. "B2B SaaS" is
  `{"business_model": "b2b", "subtypes": {"include": ["SaaS"]}}`.
- `company_news.categories`: `Funding & Investment`, `Mergers & Acquisitions`, `Product Launch`, `Partnership`,
  `Expansion`, `Layoffs & Restructuring`, `IPO`, `Leadership Change`, `Legal & Regulatory`, `Awards & Recognition`.
  `timeframe_days`: 60, 90, 180 or 365.
- `company_key_execs.event_types` (Pro plan): `CEO|CTO|CFO|COO|CMO|CRO Departed|Appointed`,
  `VP of Sales|VP of Marketing|VP of Engineering Departed|Appointed`, `Any C-Level|Any VP|Any Director
  Departed|Appointed`. `timeframe_days`: 60, 90, 180 or 365.
- `company_headcount_growth`: `timeframe_month` 3, 6, 12 or 24; `min`/`max` are INTEGERS (-100 to 10,000).
- `company_website_traffic.visit_change` (Pro plan): `period` monthly, quarterly or yearly; `min_change`/`max_change`
  floats (-100 to 10,000).

## Undocumented, so measured by the receipt instead

- Whether a company with no growth or traffic data matches a range (no `include_unknown` flag exists for these two).
- Whether range ends are inclusive.
- Whether `company.websites.exclude` matches on the website's root or on `domain` (they can differ).

## Constraints the engine enforces

- No search with only exclude filters (Prospeo rejects it).
- `company.websites.exclude` + `company.names.exclude`: at most 500 in all. Only the engine sets them, from Prospeo's
  own results.
- 25 rows a page, 1 credit a page that returns rows, free if the team paid for the same page in the last 30 days.
