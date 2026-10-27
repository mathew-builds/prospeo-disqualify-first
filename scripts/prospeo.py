#!/usr/bin/env python3
"""Prospeo REST engine for the disqualify-first skill.

    python3 prospeo.py account [<run_dir>]                     free: plan and credits left
    python3 prospeo.py suggest <run_dir> --location "united kingdom"   free (also --technology, --industry,
                                                                 --naics, --sic, --job-title)
    python3 prospeo.py plan <run_dir>                           offline: check brief.json, quote credits
    python3 prospeo.py run <run_dir> --cap N                    PAID: run the stages, then write receipt.md

Every HTTP response is written to <run_dir>/raw/ byte for byte before it is parsed, every request
body to <run_dir>/filters/, and both are pinned by sha256 in <run_dir>/manifest.jsonl. The credit cap
is enforced here, before each paid call, not by the model.

The API key comes from the PROSPEO_API_KEY environment variable, or else from the macOS Keychain
(service prospeo-api, account disqualify-first), read at call time and sent only to https://api.prospeo.io.
With PROSPEO_BASE_URL set to anything else (the test mock), no key is read at all.

Exit codes: 0 PASS, 1 receipt FAIL, 2 HALT (stopped before breaking the cap, or on an API or network
error), 3 refused input or setup problem, 4 engine crash (a bug: no receipt; read the account for spend).
"""
import argparse
import copy
import hashlib
import http.client
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import receipt  # noqa: E402

REAL_BASE_URL = receipt.REAL_BASE_URL
BASE_URL = os.environ.get("PROSPEO_BASE_URL", REAL_BASE_URL).rstrip("/")
SEARCH_INTERVAL = 0.45 if BASE_URL == REAL_BASE_URL else 0.0   # Search API on Pro: 5/s, 180/min (Prospeo pricing page, Oct 2026); the mock has no limit
PAGE = 25
MAX_EXCLUDE = 500        # Prospeo: names + websites in one filter


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Halt(f"Prospeo answered {code} with a redirect to {newurl}; not followed, so the key stays put")


OPENER = urllib.request.build_opener(NoRedirect)

# Filters a brief may use. Anything that can carry a list of companies (company, company_key_customers,
# company_lookalike, company_website_search) is left out on purpose: see load_brief.
ALLOWED_FILTERS = {
    "company_location_search", "company_headcount_range", "company_headcount_custom", "company_industry",
    "company_keywords", "company_attributes", "company_naics", "company_sics", "company_revenue", "company_type",
    "company_funding", "company_technology", "company_founded", "company_headcount_growth",
    "company_job_posting_hiring_for", "company_job_posting_quantity", "company_headcount_by_department",
    "company_email_provider", "company_headcount_by_location", "company_news", "company_intent",
    "company_key_execs", "company_website_traffic", "company_icp", "company_products_services",
    "company_integrations", "company_awards", "company_operating_languages"}


class Halt(Exception):
    pass


class Refused(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def api_key():
    if BASE_URL != REAL_BASE_URL:
        return "mock-key"  # the real key never goes anywhere but Prospeo
    key = os.environ.get("PROSPEO_API_KEY", "").strip()
    if key:
        return key
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", "prospeo-api", "-a", "disqualify-first", "-w"],
                             capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise Refused("No Prospeo API key. Set PROSPEO_API_KEY in your own shell, or on macOS store it in the "
                      "Keychain yourself: security add-generic-password -s prospeo-api -a disqualify-first -w")
    return out.stdout.strip()


class Client:
    def __init__(self, run_dir, cap=None):
        self.run = Path(run_dir)
        (self.run / "raw").mkdir(parents=True, exist_ok=True)
        (self.run / "filters").mkdir(parents=True, exist_ok=True)
        self.manifest = self.run / "manifest.jsonl"
        self.seq = len(self.manifest.read_text().splitlines()) if self.manifest.exists() else 0
        self.cap = cap
        self.spent = 0
        self.key = None
        self.last_search = 0.0

    def call(self, method, path, label, body=None, max_cost=0):
        if max_cost and self.spent + max_cost > self.cap:
            raise Halt(f"{label}: would exceed the cap ({self.spent} spent + up to {max_cost} > {self.cap})")
        if self.key is None:
            self.key = api_key()
        req_bytes = None if body is None else json.dumps(body, indent=1, sort_keys=True).encode()
        for attempt in range(1, 5):
            if path.startswith("/search-company"):
                wait = self.last_search + SEARCH_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.last_search = time.monotonic()
            headers = {"X-KEY": self.key}
            if req_bytes is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(BASE_URL + path, data=req_bytes, method=method, headers=headers)
            try:
                with OPENER.open(req, timeout=90) as resp:
                    status, data, hdrs = resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as e:
                status, data, hdrs = e.code, e.read(), e.headers
            except (OSError, http.client.HTTPException) as e:
                # the request may have been charged with no response to show for it
                raise Halt(f"{label}: network error, no response saved ({e}); the call may still have been "
                           "charged, so the account difference is the true spend")
            self.seq += 1
            name = f"{self.seq:03d}_{label}.json"
            (self.run / "raw" / name).write_bytes(data)  # saved before anything parses it
            entry = {"seq": self.seq, "label": label, "attempt": attempt, "ts": now(), "method": method,
                     "endpoint": path, "file": name, "sha256": hashlib.sha256(data).hexdigest(),
                     "http_status": status}
            if req_bytes is not None:
                (self.run / "filters" / name).write_bytes(req_bytes)
                entry["request_file"] = name
                entry["request_sha256"] = hashlib.sha256(req_bytes).hexdigest()
            try:
                obj = json.loads(data)
            except ValueError:
                obj = None
            ok = status == 200 and isinstance(obj, dict) and obj.get("error") is False
            entry["error_code"] = None if ok else (obj or {}).get("error_code") if isinstance(obj, dict) else None
            entry["free"] = bool(ok and obj.get("free"))
            charged = receipt.charge_of(path, status, obj)
            entry["charged"] = charged
            self.spent += charged
            with self.manifest.open("a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
            retry = status == 429 or entry["error_code"] == "SERVICE_TEMPORARILY_UNAVAILABLE"
            if retry and attempt < 4:
                reset = hdrs.get("x-minute-reset-seconds") if hdrs else None
                time.sleep(min(30.0, float(reset) + 0.5) if reset else 2.0 * attempt)
                continue
            return status, obj
        raise Halt(f"{label}: still rate-limited or unavailable after 4 attempts")


# ---------------------------------------------------------------- brief

def load_brief(run):
    path = Path(run) / "brief.json"
    if not path.exists():
        raise Refused(f"no brief.json in {run}")
    brief = json.loads(path.read_text())
    icp = brief.get("icp_filters")
    if not isinstance(icp, dict) or not icp:
        raise Refused("brief.json needs a non-empty icp_filters object")
    stages = brief.get("stages")
    if not isinstance(stages, list) or not stages:
        raise Refused("brief.json needs a non-empty stages list (copy assets/default_stages.json)")
    # A company list in a brief would be uploaded to Prospeo, and ToS 3.2 gives Prospeo a "perpetual and
    # irrevocable" licence over uploads. Exclusions in this engine come only from Prospeo's own results.
    used = set(icp) | {k for st in stages for k in (*st.get("held", {}), *st.get("data", {}), *st.get("keep", {}))}
    if "company" in used:
        raise Refused("brief.json may not set the `company` filter (names or websites). A client's, employer's or "
                      "anyone's company list is never sent to Prospeo from this account; suppress it locally "
                      "on rows already pulled instead.")
    if used - ALLOWED_FILTERS:
        raise Refused(f"brief.json uses filters this engine does not allow: {sorted(used - ALLOWED_FILTERS)}. "
                      "Filters that take company names, domains or customer lists are never sent from this account.")
    seen_keys = set(icp)
    ids = set()
    for st in stages:
        if not re.fullmatch(r"[a-z0-9_]+", str(st.get("id"))) or st["id"] in ids:
            raise Refused(f"stage ids must be unique, lowercase letters, digits and underscores: {st.get('id')!r}")
        ids.add(st["id"])
        if st.get("kind") not in ("list", "range"):
            raise Refused(f"stage {st['id']}: kind must be 'list' or 'range'")
        if not st.get("held") or not st.get("reason"):
            raise Refused(f"stage {st['id']}: needs held filters and a reason")
        if st["kind"] == "range":
            if "keep" in st:
                raise Refused(f"stage {st['id']}: `keep` is not a stage field (range stages are counted by intersection, "
                              "so a company with no data stays in); remove it, the engine builds the data search itself")
            if "data" in st:
                raise Refused(f"stage {st['id']}: leave out `data`; the engine builds it from `held` (same filter, "
                              "full range) so the two cannot drift apart")
            try:
                receipt.data_filter_for(st["held"])
            except ValueError as e:
                raise Refused(f"stage {st['id']}: {e} (range stages support company_headcount_growth and "
                              "company_website_traffic.visit_change)")
        keys = set(st["held"])
        clash = keys & seen_keys
        if clash:
            raise Refused(f"stage {st['id']}: filter {sorted(clash)} is already set by the ICP or an earlier "
                          "stage; one search cannot hold the same filter twice")
        seen_keys |= keys
    kinds = [st["kind"] for st in stages]
    if "range" in kinds and "list" in kinds[kinds.index("range"):]:
        raise Refused("list stages must come before range stages (range stages are counted inside the set the "
                      "list stages leave)")
    brief.setdefault("range_list_pages", None)   # None: name every removed company only if the cap covers them all
    brief.setdefault("spotcheck", 5)
    spot, rlp = brief["spotcheck"], brief["range_list_pages"]
    if type(spot) is not int or spot < 0:
        raise Refused(f"spotcheck must be a whole number, 0 or more (got {spot!r})")
    if rlp is not None and (type(rlp) is not int or rlp < 1):
        raise Refused(f"range_list_pages must be null or a whole number, 1 or more (got {rlp!r})")
    for key, value in [*icp.items(), *((k, v) for st in stages for k, v in st["held"].items())]:
        bad = [s for s in strings_in(value) if LISTLIKE.search(s)] if key not in NAME_FILTERS else []
        if bad:
            raise Refused(f"{key} holds a website, URL or email ({bad[0]!r}). Company lists never go to Prospeo from "
                          "this account; suppress them locally on rows already pulled instead. (A technology name such "
                          "as Node.js belongs in company_technology.)")
        long = [n for n in lists_in(value) if n > MAX_VALUES] if key != "company_location_search" else []
        if long:
            raise Refused(f"{key} holds a list of {long[0]} values (more than {MAX_VALUES}), which reads as a company "
                          "list; those never go to Prospeo from this account. Suppress it locally instead.")
    return brief


# A value shaped like a website, URL or email, in any filter but the ones that take product names (Customer.io),
# and a list long enough to be a pasted customer list, in any filter but locations.
LISTLIKE = re.compile(r"://|@|\bwww\.|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", re.I)
NAME_FILTERS = {"company_technology", "company_integrations"}
MAX_VALUES = 25


def lists_in(value):
    """Lengths of every list inside a filter value."""
    if isinstance(value, list):
        yield len(value)
        for v in value:
            yield from lists_in(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from lists_in(v)


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings_in(v)


def range_cost(pos):
    """Mandatory searches for the range stage at position pos (0-based): signal, data, and one per subset of the
    earlier range stages."""
    return 1 + 1 + (2 ** pos - 1)


def quote(brief):
    lists = sum(1 for s in brief["stages"] if s["kind"] == "list")
    ranges = sum(1 for s in brief["stages"] if s["kind"] == "range")
    minimum = 1 + 3 * lists + sum(range_cost(p) for p in range(ranges))
    return {"minimum": minimum,
            "list_paging": "plus 1 credit per extra page of 25 that a list stage holds (each list stage's third search, "
                           "the check that no held company survived its exclusion, is free when it finds none)",
            "optional": f"a coverage probe per list stage that holds nothing (<= {lists}); a spot check of up to "
                        f"{brief['spotcheck']} (reserved first); then, only if the cap covers ALL of it, 1 credit per "
                        "extra page of 25 to name every company the range stages remove (otherwise none is bought)",
            "note": "identical pages re-run within 30 days are free"}


build_filters = receipt.build_filters


# ---------------------------------------------------------------- run

def search(client, filters, page, label):
    status, obj = client.call("POST", "/search-company", label, {"page": page, "filters": filters}, max_cost=1)
    if status == 200 and obj and obj.get("error") is False:
        return obj["pagination"]["total_count"], [r["company"] for r in obj.get("results", [])]
    code = (obj or {}).get("error_code")
    if code == "NO_RESULTS":
        return 0, []
    raise Halt(f"{label}: HTTP {status} {code} {(obj or {}).get('filter_error', '')}".strip())


def account(client, label):
    status, obj = client.call("GET", "/account-information", label)
    if status != 200 or not obj or obj.get("error") is not False:
        raise Halt(f"{label}: account check failed, HTTP {status} {(obj or {}).get('error_code')}")
    return obj["response"]


def reconcile(client, before, label):
    """The cap is enforced on what the responses say they cost. Before each paid batch, read the account (free) and
    halt if it shows more spend than that: a charge the responses did not report."""
    now = account(client, label).get("remaining_credits")
    if isinstance(before, int) and isinstance(now, int) and before - now > client.spent:
        raise Halt(f"{label}: the account shows {before - now} credits spent, the responses {client.spent}; stopped "
                   "before spending more")


def md_cell(s):
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


def run_stages(run, cap):
    brief = load_brief(run)
    q = quote(brief)
    if cap < q["minimum"]:
        raise Refused(f"cap {cap} is below the minimum this brief needs ({q['minimum']} credits)")
    if (run / "run_meta.json").exists():
        raise Refused(f"{run} already holds a run; use a new folder so one receipt covers one run")
    meta = json.dumps({"started": now(), "cap": cap, "base_url": BASE_URL,
                       "brief_sha256": receipt.sha256_file(run / "brief.json")}, indent=1).encode()
    (run / "run_meta.json").write_bytes(meta)
    with (run / "manifest.jsonl").open("a") as f:
        f.write(json.dumps({"pin": "run_meta.json", "sha256": hashlib.sha256(meta).hexdigest()}) + "\n")
    client = Client(run, cap)
    print(f"This run reads Prospeo, writes only {run}, and sends nothing anywhere else. Cap: {cap} credits.")
    acct = account(client, "account_before")
    print(f"Plan {acct.get('current_plan')}, {acct.get('remaining_credits')} credits before the run.")

    icp, stages = brief["icp_filters"], brief["stages"]
    lists = [(k, st) for k, st in enumerate(stages, 1) if st["kind"] == "list"]
    ranges = [(k, st) for k, st in enumerate(stages, 1) if st["kind"] == "range"]

    def still_needed(lists_done, ranges_done):
        return 3 * (len(lists) - lists_done) + sum(range_cost(p) for p in range(ranges_done, len(ranges)))

    total, sl_rows = search(client, icp, 1, "base")
    print(f"base: {total}")
    domains, names, held_md = set(), set(), []
    for i, (k, st) in enumerate(lists):
        sid = f"s{k}_{st['id']}"
        later = still_needed(i + 1, 0)
        held_filters = build_filters(icp, {}, domains, names)
        held_filters.update(copy.deepcopy(st["held"]))
        held, hrows = search(client, held_filters, 1, f"{sid}_held_p1")
        pages = math.ceil(held / PAGE)
        if client.spent + (pages - 1) + 2 + later > cap:
            raise Halt(f"{sid}: {held} held needs {max(pages - 1, 0)} more pages; with {later + 2} calls still to "
                       f"come that breaks the cap ({client.spent} spent of {cap})")
        if len(domains) + len(names) + held > MAX_EXCLUDE:
            raise Halt(f"{sid}: {len(domains) + len(names) + held} exclusions would pass Prospeo's limit of "
                       f"{MAX_EXCLUDE}; narrow the ICP or the window")
        for p in range(2, pages + 1):
            _, more = search(client, held_filters, p, f"{sid}_held_p{p}")
            hrows.extend(more)
        if held == 0 and client.spent + 1 + 1 + later <= cap:
            # none in the ICP: is that the ICP, or does this filter hold almost nothing at all?
            probe, _ = search(client, receipt.probe_filters(icp, st), 1, f"{sid}_probe")
            print(f"{sid}: coverage probe {probe}")
        for r in hrows:
            d = receipt.root_of(r)
            held_md.append((sid, d or "", r.get("name"), r.get("company_id"), st["reason"], "list"))
            if d:
                domains.add(d)
            elif r.get("name"):
                names.add(r["name"])
            else:
                raise Halt(f"{sid}: a held company has neither a website nor a name, so it cannot be excluded")
        after, sl_rows = search(client, build_filters(icp, {}, domains, names), 1, f"{sid}_apply")
        if held:
            # the stage's own filter after its exclusions must find nobody (free when it does): that proves the
            # companies removed are the held ones, not just as many of them
            residual = build_filters(icp, {}, domains, names)
            residual.update(copy.deepcopy(st["held"]))
            left, _ = search(client, residual, 1, f"{sid}_residual")
            if left:
                print(f"{sid}: {left} held companies survived their exclusion; the receipt will FAIL")
        print(f"{sid}: held {held}, after {after}")

    reconcile(client, acct.get("remaining_credits"), "account_check_ranges")
    sl = build_filters(icp, {}, domains, names)
    done, counted = [], []
    for pos, (k, st) in enumerate(ranges):
        sid = f"s{k}_{st['id']}"
        signal = receipt.range_filters(sl, stages, k)
        held, hrows = search(client, signal, 1, f"{sid}_held_p1")
        data_filters = copy.deepcopy(sl)
        data_filters.update(receipt.data_filter_for(st["held"]))
        search(client, data_filters, 1, f"{sid}_data")
        earlier = set()   # companies known to match an earlier range stage too, so they were removed there
        for subset in receipt.earlier_subsets(done):
            _, arows = search(client, receipt.range_filters(sl, stages, k, subset), 1, receipt.and_label(sid, subset))
            earlier |= {r.get("company_id") for r in arows}
        done.append(k)
        counted.append((k, st, sid, signal, held, hrows, earlier))
        print(f"{sid}: with the signal {held}")

    # Naming every removed company: all lists complete, or none extended. A partial list buys no cross-check, so the
    # spot check is reserved first and listing only spends what finishes every list.
    def pages_for(held):
        n = min(math.ceil(held / PAGE), receipt.MAX_PAGES)
        return n if brief["range_list_pages"] is None else min(n, brief["range_list_pages"])
    extra = sum(pages_for(held) - 1 for *_, held, _, _ in counted if held > 0)
    reserve = brief["spotcheck"]
    if extra and client.spent + extra + reserve > cap:
        print(f"naming every removed company needs {extra} more credits; skipped, so the cap holds and the spot "
              f"check runs (cap {cap}, spent {client.spent})")
        extra = 0
    if extra:
        reconcile(client, acct.get("remaining_credits"), "account_check_listing")
    listed_before = set()
    for k, st, sid, signal, held, hrows, earlier in counted:
        if extra:
            for p in range(2, pages_for(held) + 1):
                _, more = search(client, signal, p, f"{sid}_held_p{p}")
                hrows.extend(more)
        seen = set()
        for r in hrows:
            cid = r.get("company_id")
            if cid in seen:
                continue
            seen.add(cid)
            kind = "range_earlier" if cid in earlier or cid in listed_before else "range"
            held_md.append((sid, receipt.root_of(r) or "", r.get("name"), cid, st["reason"], kind))
        listed_before |= seen

    reconcile(client, acct.get("remaining_credits"), "account_check_spot")
    spot = spotcheck(client, brief, sl_rows, cap)
    account(client, "account_after")
    write_held(run, held_md, spot)
    write_brief_md(run, brief, cap, q)
    return receipt.main(["--write", str(run)])


def spotcheck(client, brief, rows, cap):
    n = min(brief["spotcheck"], len(rows), cap - client.spent)
    if n <= 0:
        why = "no rows left" if not rows else "spotcheck is 0" if brief["spotcheck"] <= 0 else "no budget left"
        return {"skipped": why, "rows": []}
    pick = rows[:n]  # page 1 of the final set, not a random sample
    data = [{k: v for k, v in (("identifier", str(i)), ("company_id", r.get("company_id")),
                                ("company_website", receipt.root_of(r))) if v}
            for i, r in enumerate(pick)]
    status, obj = client.call("POST", "/bulk-enrich-company", "spotcheck_enrich", {"data": data}, max_cost=n)
    if status != 200 or not obj or obj.get("error") is not False:
        return {"skipped": f"enrich failed: HTTP {status} {(obj or {}).get('error_code')}", "rows": []}
    out = receipt.spot_rows(obj, brief["icp_filters"])
    result = {"note": "Technology is what Prospeo detects on the website (a script), not proof of what the "
                      "company runs internally. Headcount is the employee_range band, never "
                      "employee_count_on_prospeo (that counts contacts in Prospeo, not staff).",
              "not_matched": obj.get("not_matched", []), "rows": out}
    (client.run / "spotcheck.json").write_text(json.dumps(result, indent=1))
    return result


def write_held(run, rows, spot):
    lines = ["# Held companies", "",
             "Reasons are Prospeo filter matches, not verified events. The API gives no event date, so none is "
             "shown. Internal working file: never publish names, and never present this as a do-not-contact or "
             "safe-to-call list to a third party (Prospeo ToS §5, Acceptable Use Policy).", "",
             "A range stage lists companies with its signal. One that also matched an earlier stage was removed "
             "there, and its row says so when the run saw the overlap (an incomplete list can miss some).", "",
             "| stage | website | name | company_id | reason |", "|---|---|---|---|---|"]
    for sid, d, name, cid, reason, kind in rows:
        r = reason + {"list": ", per Prospeo (no event date in the API)", "range": ", per Prospeo",
                      "range_earlier": ", per Prospeo; also matched an earlier stage, which removed it"}[kind]
        lines.append(f"| {sid} | {md_cell(d)} | {md_cell(name)} | `{md_cell(cid)}` | {md_cell(r)} |")
    review = [r for r in spot.get("rows", []) if r["disagreements"]]
    lines += ["", "## Held for review (spot check)", ""]
    if spot.get("skipped"):
        lines.append(f"Spot check skipped: {spot['skipped']}.")
    elif not review:
        lines.append(f"No disagreements in the {len(spot['rows'])} rows enriched.")
    else:
        lines += ["| review | website | name | company_id | reason |", "|---|---|---|---|---|"]
        for r in review:
            lines.append(f"| review | {md_cell(r['domain'])} | {md_cell(r['name'])} | `{md_cell(r['company_id'])}` | "
                         f"{md_cell('; '.join(r['disagreements']))} |")
    (run / "held.md").write_text("\n".join(lines) + "\n")


def write_brief_md(run, brief, cap, q):
    lines = ["# Brief", "", f"ICP in words: {brief.get('icp_words', '(not given)')}", "",
             f"Stages, in order (a company is credited to the first stage that removes it); defaults used: "
             f"{brief.get('defaults_used', 'not stated')}.", ""]
    for k, st in enumerate(brief["stages"], 1):
        lines.append(f"{k}. `{st['id']}` ({st['kind']}): {st['reason']}")
    lines += ["", f"Spend cap: {cap} credits. Quote: minimum {q['minimum']}, {q['list_paging']}; optional: "
              f"{q['optional']} ({q['note']}).", "", "ICP filters as sent:", "", "```json",
              json.dumps(brief["icp_filters"], indent=1, sort_keys=True), "```", ""]
    (run / "brief.md").write_text("\n".join(lines))


def write_halt(run, err):
    """Best effort: say what was spent, from the account itself, so a rerun's cap can subtract it."""
    before = after = None
    try:
        entries = [json.loads(l) for l in (run / "manifest.jsonl").read_text().splitlines() if l.strip()]
        for e in entries:
            if e.get("label") == "account_before" and e.get("http_status") == 200:
                before = json.loads((run / "raw" / e["file"]).read_bytes())["response"]["remaining_credits"]
        after = account(Client(run), "account_after_halt").get("remaining_credits")
    except Exception:
        pass
    spent = f"{before - after} credits (account {before} before, {after} after)" if None not in (before, after) \
        else "unknown: read the account"
    (run / "HALT.md").write_text(f"# Halted\n\n{err}\n\nNo receipt was written. Spent by this run: {spent}. A rerun "
                                 "in a new folder needs a cap of the user's ceiling minus this spend.\n")


# ---------------------------------------------------------------- cli

SUGGEST = {"location": "location_search", "technology": "technology_search", "industry": "industry_search",
           "naics": "naics_search", "sic": "sic_search", "job_title": "job_title_search"}


def main(argv):
    ap = argparse.ArgumentParser(description="Prospeo REST engine for disqualify-first")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("account")
    a.add_argument("run_dir", nargs="?")
    s = sub.add_parser("suggest")
    s.add_argument("run_dir")
    for k in SUGGEST:
        s.add_argument("--" + k.replace("_", "-"), dest=k)
    p = sub.add_parser("plan")
    p.add_argument("run_dir")
    r = sub.add_parser("run")
    r.add_argument("run_dir")
    r.add_argument("--cap", type=int, required=True, help="hard credit ceiling for this run")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "account":
            import tempfile
            run = Path(args.run_dir) if args.run_dir else Path(tempfile.mkdtemp(prefix="prospeo-acct-"))
            info = account(Client(run), "account_check")
            print(json.dumps({k: info.get(k) for k in ("current_plan", "remaining_credits", "used_credits",
                                                         "next_quota_renewal_date")}, indent=1))
            return 0
        if args.cmd == "suggest":
            chosen = [(k, v) for k, v in vars(args).items() if k in SUGGEST and v]
            if len(chosen) != 1:
                raise Refused("give exactly one of --" + ", --".join(k.replace("_", "-") for k in SUGGEST))
            k, v = chosen[0]
            status, obj = Client(args.run_dir).call("POST", "/search-suggestions", f"suggest_{k}", {SUGGEST[k]: v})
            print(json.dumps(obj, indent=1))
            return 0 if status == 200 else 2
        if args.cmd == "plan":
            brief = load_brief(args.run_dir)
            print(json.dumps(quote(brief), indent=1))
            return 0
        if args.cmd == "run":
            return run_stages(Path(args.run_dir), args.cap)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    except Halt as e:
        print(f"HALT: {e}", file=sys.stderr)
        if args.cmd == "run":
            write_halt(Path(args.run_dir), e)
        return 2
    except Exception as e:  # a bug, not a Prospeo answer
        print(f"ENGINE CRASH: {type(e).__name__}: {e}", file=sys.stderr)
        if args.cmd == "run":
            write_halt(Path(args.run_dir), e)
        return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
