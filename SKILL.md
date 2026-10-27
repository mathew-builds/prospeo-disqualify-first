---
name: disqualify-first
description: Run a Prospeo company search for an ICP, then take out the companies not to contact right now (layoffs or restructuring news, a CRO / VP of Sales / C-level departure, falling headcount, falling website traffic), give the reason each one was removed, and prove the removals add up with a receipt recomputed from Prospeo's raw responses. Use whenever someone asks to find companies in Prospeo and exclude, remove, disqualify, suppress or filter out accounts with negative signals, asks "who should I not call" or "how many are left after removing...", or wants a list cleaned of companies that are shrinking or losing sales leaders, even if they never say "disqualify". Also use when someone asks to exclude a client's or employer's customer list inside a Prospeo search, or to claim how accurate a Prospeo signal was: this skill handles both safely. Not for building a positive list with no removals, finding emails, or enriching a list already in hand.
---

# Disqualify first: who not to call, with a receipt

Take an ICP company search in Prospeo and remove the companies not to contact now. Say exactly why each one was
removed, and prove the removals add up. The arithmetic is done by scripts, never by you: `scripts/prospeo.py` calls
Prospeo's REST API and saves every response byte for byte; `scripts/receipt.py` recomputes every count from those
bytes and fails loudly when they do not add up. Your job is the brief, the cost conversation, and an honest report.

## What it touches

- **Reads** Prospeo (search, account and suggestion endpoints; one bulk enrichment for the spot check).
- **Writes** only its run folder: the folder the user names, or else
  `./prospeo-runs/<YYYY-MM-DD>_disqualify-first/` (add `_2`, `_3` for more runs that day). One folder holds one
  run. It holds Prospeo output data, so tell the user once to keep it out of version control (for example, add
  `prospeo-runs/` to `.gitignore`).
- **Sends nothing anywhere else.** No CRM push, no list upload, no email. Say this once at the start.
- **Paths:** call the scripts by absolute path (`<this skill's folder>/scripts/...`) and give the run folder as an
  absolute path, so the working directory never matters.
- **Key:** read by the script from `PROSPEO_API_KEY`, or else the macOS Keychain (service `prospeo-api`, account `disqualify-first`).
  Never ask for the key in chat, never print it, never write it to a file. If it is missing, the script says how the
  user sets it themselves.

## Two refusals, and why

1. **A client's, employer's or anyone else's company list never goes into a Prospeo search from this account.**
   Prospeo's ToS 3.2 grants Prospeo a "perpetual and irrevocable" licence to ingest, use and retain Customer Uploads, and a list sent inside a filter
   plausibly counts as one; ToS 1.1 limits this account's Output Data to the user's own internal use. So:
   - say that plainly, without naming any employer;
   - client work runs on the client's own Prospeo account;
   - offer local suppression instead: after a run, compare the client's list with the rows already pulled, on disk,
     and drop the matches (a short script on `held.md` and the pulled rows; there is no command for it yet). The
     list itself never leaves the machine.

   The engine enforces this too: a brief may use only an allow-list of filters, which leaves out every filter that
   takes company names, domains or customers (`company`, `company_key_customers`, `company_lookalike`,
   `company_website_search`), and it refuses any value shaped like a website, URL or email (outside the
   technology and integration filters, which take product names) and any list of more than 25 values (outside
   locations). It cannot recognise a few company names typed into a keyword filter, so never put one there.
   Exclusions come only from Prospeo's own results. Make no Prospeo call that contains any domain or name from the
   user's list.
2. **No accuracy, "safe to call", "safe to email" or eligibility claims to anyone else based on Prospeo's data**
   (Prospeo ToS §5, Acceptable Use Policy). A held reason is "matched Prospeo's Layoffs & Restructuring category in the last 90 days",
   never "had layoffs". If asked for an accuracy line, decline, cite the clause, and offer a line about the method
   (for example: "A technology filter tells you what is on the website, not what is in the CRM.").

## Inputs

| Input | Default | If missing |
|---|---|---|
| ICP in plain words | none | Ask |
| Removal stages, thresholds, order | `assets/default_stages.json` (the four signals above) | Use the defaults and say so |
| Spend cap (credits) | none | Ask once at the cost gate. If the user already named a ceiling, that is the cap: "at most 25" is 25, "under 25" is 24 |
| Spot check size | 5 enriched rows (1 credit each) | The engine shrinks or skips it to stay inside the cap |
| `range_list_pages` | none: name every company the range stages remove, but only if the cap covers all of them (all or nothing) | A number N lists up to N pages of 25 per range stage instead: names within the cap, but no cross-check |

Stage order matters: each removed company is credited to the first stage that removes it. List stages (events) come
first, then range stages (headcount, traffic); the engine refuses any other order.

## Steps

**0. Posture and account (free).** Say: "This run reads Prospeo, writes only its run folder, and sends nothing
anywhere." Then `python3 -I <this skill's folder>/scripts/prospeo.py account <run_dir>`, and report plan and credits
left (without a run folder it saves its response in a temporary folder).
The key-execs and traffic stages need the Pro plan: on any other plan, drop those stages from the brief before the
cost gate and say so, rather than paying for the first stages and halting on `PLAN_REQUIRED`.

**1. Write `brief.json`** in a new run folder:

```json
{
  "icp_words": "UK B2B SaaS, 21-200 staff, HubSpot on the website",
  "icp_filters": {
    "company_location_search": {"include": ["United Kingdom"]},
    "company_headcount_range": ["21-50", "51-100", "101-200"],
    "company_technology": {"include": ["HubSpot"]},
    "company_type": {"business_model": "b2b", "subtypes": {"include": ["SaaS"]}}
  },
  "stages": [ ...copied from assets/default_stages.json, or adjusted to what the user asked... ],
  "defaults_used": true,
  "spotcheck": 5
}
```

`defaults_used` is true when the stages are the defaults, even if the user also named the same thresholds.

- Resolve every location, technology and industry value with `python3 -I <this skill's folder>/scripts/prospeo.py suggest <run_dir>
  --location "united kingdom"` (also `--technology`, `--industry`; free, saved to the run folder) and copy the
  returned value exactly. Other values come from `references/filters.md`.
- If the user's thresholds differ from the defaults, edit the stage's `held` filter. If their words are narrower than a
  default stage (for example, sales-leader departures but not every C-level one), narrow its filter to match, using
  values from `references/filters.md`, and set `defaults_used` to false. Never give a range stage a
  `data` filter: the engine builds it from `held` (the same filter over its full range) to count the companies that
  have any value, and refuses a brief that carries one. Headcount growth takes integers; traffic change takes floats.
- Never put a company list in the brief (refusal 1).

**2. Cost gate.** `python3 -I <this skill's folder>/scripts/prospeo.py plan <run_dir>` validates the brief offline and prints the quote.
Tell the user, in one or two lines: the minimum (1 for the base search, 3 per list stage, 2 for the first range stage and 3 for the second, which
also counts its overlap with the first), that each list stage costs 1 more credit per
extra page of 25 it holds, the optional extras (a coverage probe per empty list stage, pages that name every company a
range stage removes, the spot check), that identical pages re-run within 30 days are free, and the cap. Get a yes unless the user already gave a ceiling.
If the user wants each removed company named and the cap is tight, set `range_list_pages` before the run, to
`1 + (cap - minimum - spot check - 1 per list stage) // number of range stages` (at least 1), so the spare credits buy
names instead of going unspent: a range list that cannot be completed is otherwise not bought at all. These REST
calls do not pass through any MCP permission prompt, so the cap is the control: the engine refuses a cap
below the minimum and halts before any call that would break it.

**3. Run.** `python3 -I <this skill's folder>/scripts/prospeo.py run <run_dir> --cap <N>`. Exit codes: 0 PASS, 1 receipt FAIL, 2 HALT,
3 refused, 4 engine crash (a bug; report it verbatim). What it does, in order:
- reads credits; runs the base ICP search;
- **list stages** (news, exec events): pages through every held company, excludes their websites, and checks the
  removal count exactly; then searches the stage's filter again after the exclusions, which must find nobody (free
  when it does), so the receipt proves which companies were removed, not only how many. A list stage that holds nobody gets a **coverage probe** (its filter with only the ICP's
  location), which tells "none in this ICP" apart from "this filter holds almost nothing";
- **range stages** (headcount, traffic): counted inside what the list stages leave, with intersection searches, so a
  company with **no data** for a filter is never removed by it. A `data` search counts those companies. Spare credits
  name every removed company only when they cover all of them (or up to `range_list_pages` pages each, if set); when
  every list is complete, a second method (distinct ids) must agree with the counting. In `held.md` a range stage
  lists the companies with its signal; a row is marked when an earlier stage removed that company;
- enriches up to 5 rows from the set after the list stages and compares technology, headcount band and `is_b2b` with
  the ICP (sampled before the range stages, so a spot-checked company may then be removed by one); counts pulled rows
  whose country is outside the location filter;
- reads credits again, writes `held.md`, `spotcheck.json`, `brief.md`, then `receipt.md`.

**4. Verify independently.** `python3 -I <this skill's folder>/scripts/receipt.py <run_dir>` must print PASS and exit 0. If it does not,
the run failed: report the failing checks verbatim, and do not present the counts as a result.

**5. Web check (no Prospeo credits).** Pick 3 held companies from the list stages and look for the news or the
departure on the open web with a web search tool you have (snippets only for LinkedIn; never fetch prospeo.io
pages: Prospeo's ToS 1.3 allows automation only through its public API). With no web search tool, skip this step and say so. Record each as confirmed,
not found, or contradicted in
`<run_dir>/webcheck.md`, with the URL. Leave `held.md` untouched; the receipt checks it. If the list stages removed
nobody, there is nothing to look up (range signals are numbers, not events): write that in `webcheck.md` and skip it.

**6. Deliver.** Every number comes from `receipt.md`; copy it, never retype it from memory. Give:
- the final count and the base count;
- one line per stage: with the signal, removed (first match), and for range stages how many have no data;
- for an empty list stage, the coverage probe, said plainly: "the filter matched N companies in <location> overall",
  so the user knows the stage barely checked anything; never as a fact about Prospeo's data in general. You may offer
  a second probe with a wider window (180 or 365 days): it costs nothing if it finds nothing, 1 credit if it does;
- "up to N of the companies left were never checked by <stage>", for each range stage;
- PASS or FAIL, and any failed check verbatim;
- held companies summarised by reason (counts; names only if the user asks, and say they are in `held.md`). A
  request for why each company was removed counts as asking. If the range lists are incomplete, say how many are
  named, what naming the rest costs (1 credit per extra page of 25, from the counts with the signal), and offer a
  rerun in a new folder with `range_list_pages` (pages already paid are free for 30 days);
- spend: the account difference against the cap;
- spot-check disagreements and web-check results;
- the run folder path.

## Reading the receipt

- **List stage:** removed must equal held, exactly, `held.md` must list exactly those companies, and the stage's
  filter searched again after the exclusions must find nobody. Together these prove the removed companies are the held
  ones. A mismatch means the exclusion did not do what the run assumed (for example, Prospeo matched websites
  differently).
- **Range stage:** "with the signal" is every company Prospeo records it for; "removed" is the ones no earlier range
  stage already removed (counted by intersection searches); the difference is the overlap. Companies with no data for
  the filter are never removed and are reported as "no data". The receipt FAILs if a removal count falls outside 0 to
  the signal count, if a signal count exceeds its data count, or if an overlap exceeds any stage in it. Its listed
  companies may not repeat or outnumber the count, and when every page was read they must equal it.
- **Cross-check:** when every range list is complete, the distinct companies across them must equal the counted
  removals. Two methods agree, or the receipt FAILs. A truncated list skips it, and the receipt says so.
- **A test run** against the mock says so at the top of the receipt. Its numbers are not Prospeo's.

## Halts

| Halt | What to tell the user |
|---|---|
| No API key | They set it themselves in their own terminal (the script prints how) |
| Cap below the minimum, or a list stage needs more pages than the cap allows | The numbers from the message; offer a higher cap or a narrower ICP/window |
| Held exclusions over 500 | Prospeo's filter limit; narrow the ICP or shorten the window |
| `INVALID_FILTERS`, `PLAN_REQUIRED`, `INSUFFICIENT_CREDITS` | The `filter_error` text verbatim; fix the brief, nothing was charged for the failed call |
| Network error mid-run | `HALT.md` states the spend from the account itself; a call can be charged with no response saved |
| Engine crash (exit 4) | The error verbatim; it is a bug in the engine, not a Prospeo answer |
| Receipt FAIL | The failed checks verbatim; the counts are not a result |

After a halt, a rerun goes in a new folder with a cap of the user's ceiling minus what the halted run spent (from
`HALT.md`). Pages already paid for are free on the rerun for 30 days.

## What this does not claim

- Prospeo's search rows carry no event dates, so no date is shown for a news item or departure, and any date in a
  report would be invented. "Matched in the last 90 days" is the most it can say.
- Technology is detected on the website (a script), not proof of what the company runs inside. Headcount uses the
  `employee_range` band; `employee_count_on_prospeo` counts contacts in Prospeo, not staff, and is never used.
- A removed company is not proven to have had the event; the web check samples 3, it does not confirm them all.
- Outputs are internal working data: not a do-not-contact list for anyone else, and never published with names.
- The receipt catches accidental edits and anything the model mistyped: every response, every request body, the brief
  and the run settings are pinned by sha256, every request body is rebuilt from the brief, and each charge and the spot
  check's findings are recomputed from the responses. The cap is enforced on what the responses say they cost; before
  each paid batch the engine also reads the account and halts if it shows more spend than that. It cannot stop someone
  who edits the run folder and the manifest together on purpose; the manifest is the only anchor.
- Not built yet: calibration against named companies (it needs `company.websites.include`, which the engine refuses),
  a built-in command for local suppression, and a full export of the final set (only held rows and the spot-check rows are pulled).

## For a public post about a run

No company names, no result rows and no screenshots of results. No accuracy, "safe to call" or eligibility claims
(refusal 2), and no claim that a held company had the event: held means it matched Prospeo's filter. Counts come
only from a PASS `receipt.md`, copied exactly. An empty stage describes one search, never Prospeo's data in
general. No post states a result that has not run.

## Files

`scripts/prospeo.py` (engine) · `scripts/receipt.py` (checks) · `assets/default_stages.json` · `references/filters.md`
(values) · `tests/` (mock Prospeo and end-to-end tests; `python3 -I tests/test_engine.py`, spends nothing) · `README.md` ·
`LICENSE` (MIT).
