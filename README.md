# disqualify-first: a Prospeo skill for Claude Code

A Claude Code skill that finds the companies in a Prospeo search that you should not contact right now, says why
each one was removed, and proves the removals add up.

## What it does

You describe an ICP in plain words. The skill turns it into a Prospeo company search, then runs removal stages over
the results: by default, layoffs or restructuring news, a sales or C-level departure, falling headcount and falling
website traffic. Each removed company is credited to the first stage that removes it. Every API response is saved
byte for byte before it is parsed, and `scripts/receipt.py` recomputes every count from those saved bytes. If the
counts do not add up, the receipt says FAIL and the skill does not present the counts as a result.

## Requirements

- Claude Code, and Python 3.9 or later (standard library only; nothing to install with pip).
- A Prospeo account and API key. A run spends Prospeo credits.
- **The key-executive and website-traffic filters need Prospeo's Pro plan** (per Prospeo's pricing page, Oct 2026). Two of the four default stages use them
  (sales leader departures, falling traffic). If your plan does not include them, ask the skill to drop those two
  stages; otherwise the run halts at the first of them.
- The macOS Keychain is optional. The `PROSPEO_API_KEY` environment variable works on any OS.

## Install

Clone it into your skills folder, then start a new Claude Code session:

```sh
git clone https://github.com/mathew-builds/prospeo-disqualify-first ~/.claude/skills/disqualify-first
```

That makes it available in all your projects. For one project only, clone it into that project's
`.claude/skills/disqualify-first` instead.

## Set the API key

Set it yourself in your own terminal, never in the chat. Use one of these:

```sh
export PROSPEO_API_KEY='your-key'                                        # macOS, Linux
security add-generic-password -U -s prospeo-api -a disqualify-first -w   # macOS Keychain; prompts for the key
```

On Windows PowerShell, use `$env:PROSPEO_API_KEY = 'your-key'`. Set the variable before you start Claude Code (or in
your shell profile): a variable exported in another terminal is not seen by a session that is already running. The
Keychain entry works for every session, and `-U` updates it if it already exists.

The script reads the environment variable first, then the Keychain, and sends the key only to
`https://api.prospeo.io`.

## Use it

Ask in plain words, for example:

- "Find UK B2B SaaS companies with 21-200 staff that use HubSpot, and take out any that shouldn't be called right now."
- "How many US B2B companies with 51-500 staff are left after removing the ones with layoffs news or a sales
  leader departure? Spend at most 30 credits."
- "Same search for Germany, with a 180-day window for the news stage."

The skill reads your Prospeo subscription and credits (free), writes a `brief.json`, quotes the cost and asks for a
cap, runs the search, checks the receipt and reports. Each run writes one folder, by default
`./prospeo-runs/<YYYY-MM-DD>_disqualify-first/`. It holds Prospeo output data, so keep it out of version control (for
example, add `prospeo-runs/` to `.gitignore`).

## Cost

`scripts/prospeo.py plan` quotes the cost offline before anything is spent. The minimum is 1 credit for the base
search, 3 per list stage (the third search checks that no held company survived the exclusion, and costs nothing when
it finds none), 2 for the first range stage and 3 for the second (it also counts its overlap with the first; the
engine supports two range filters, headcount growth and website traffic). With the four default stages that is 12
credits. The cap must cover it. That is what the required searches cost when each list stage fits on one page of 25;
a run often spends less, because a search that finds nothing is free and so is a page paid for in the last 30 days.
On top of that: 1 credit per extra page of 25 that a list stage holds, plus optional extras bought only when the cap
leaves room (a coverage probe for a list stage that holds nobody, and a spot check of up to 5 enriched rows).

Naming the companies the range stages remove is all or nothing by default: those pages are bought only if the cap
covers every one of them. To get names within a tighter cap, ask for a page limit per range stage
(`range_list_pages`); a list cut short by it skips the receipt's cross-check.

You set a hard cap in credits. The script refuses a cap below the minimum and checks the cap before every paid call,
halting rather than going over. Prospeo's API docs say an identical search page re-run within 30 days is free (the response carries
`"free": true`). Searches are paced for the Pro plan's rate limit; on a lower plan Prospeo answers some calls with a
rate-limit error, and the engine waits for Prospeo's reset time and retries. If a run stops for a missing key, start
the next run in a new folder.

## Default stages

From `assets/default_stages.json`. These are starting points; change any threshold or window in the brief.

| Stage | Kind | Removes companies that |
|---|---|---|
| `layoffs` | list | matched Prospeo's "Layoffs & Restructuring" news category in the last 90 days |
| `sales_leader_departed` | list | matched a CRO, VP of Sales or any C-level departure event in the last 90 days (Pro) |
| `headcount_down` | range | have Prospeo headcount growth of -10% or lower over 6 months |
| `traffic_down` | range | have a Prospeo quarterly website visit change of -20% or lower (Pro) |

List stages run first and exclude the companies they hold by website. Range stages are counted with intersection
searches, so a company with no data for a filter is never removed by it; the receipt reports those as "no data".
A news or executive-event stage can hold nobody for a given ICP. When the cap allows, the receipt then shows a
coverage probe (the same filter with only your ICP's location), which tells you whether that stage had anything to
check.

## What the receipt proves, and what it does not

A PASS means every count, every charge and the spot check's findings were recomputed from the saved responses and
request bodies (pinned by sha256 in `manifest.jsonl`, so an accidental edit after the run is a FAIL); each list stage
removed as many companies as it held, `held.md` lists exactly those, and the stage's filter searched again after the
exclusions found nobody, so the removed companies are the held ones; each range stage's counts are consistent, and
when every list is complete a second method (distinct ids) agrees with the counting; and the recorded spend matches
the account's own before and after difference and stays within the cap. Before each paid batch the engine also reads
the account and halts if it shows more spend than the responses reported.

It does not prove that a removed company had the event: held means it matched Prospeo's filter, and the skill
web-checks a sample of 3. It shows no dates (the search results it reads carry none, so "matched in the last 90 days"
is the most it says). Technology is detected on the website, not proof of what a company runs inside, and headcount
is the employee range band. It cannot stop someone who edits the run folder and the manifest together on purpose.
Outputs are internal working data, not a do-not-contact list for anyone else. Not built yet: calibration against
named companies, local suppression of a list, and a full export of the final set.

## Two refusals

Read Prospeo's terms yourself; this is not legal advice. A run's output is Prospeo Output Data, which ToS 1.1-1.2
limit to your own internal use: never commit a run folder or publish its rows.

1. **A customer list never goes into a Prospeo search.** Prospeo's ToS 3.2 grants Prospeo a "perpetual and
   irrevocable" licence to ingest, use and retain Customer Uploads, and a list sent inside a filter plausibly counts as one; ToS 1.1 limits Output Data to the
   account holder's own internal use. The engine refuses a brief that uses a filter taking company names, domains or
   customers (`company`, `company_key_customers`, `company_lookalike`, `company_website_search`), any value shaped
   like a website, URL or email (outside the technology and integration filters, which take product names), or any
   list of more than 25 values (outside locations). It cannot recognise a few company names typed into the keyword
   filter (`company_keywords`), so do not put them there. Client work runs on the client's own Prospeo account.
2. **No accuracy, "safe to call", "safe to email" or eligibility claims** based on Prospeo's data (Prospeo's
   ToS §5, Acceptable Use Policy). The skill offers a line about the method instead.

## Tests

`python3 -I tests/test_engine.py`, run from the skill's folder, runs the engine and the receipt end to end against a
local mock of Prospeo. It spends nothing and needs no key. A pass shows the arithmetic and the controls work on
synthetic data, not how Prospeo behaves.

## License

MIT. See `LICENSE`.

Built by Mathew Joseph. Not made or endorsed by Prospeo.
