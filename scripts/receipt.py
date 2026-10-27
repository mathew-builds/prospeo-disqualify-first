#!/usr/bin/env python3
"""Recompute and check a disqualify-first run from the raw Prospeo responses it saved.

    python3 receipt.py <run_dir>           verify: recompute everything and compare it with receipt.md.
                                           Exit 0 only if every number matches and every check passes.
    python3 receipt.py --write <run_dir>   build receipt.md from raw/. Exit 0 on PASS, 1 on FAIL.

Every number comes from raw/ (the bytes Prospeo sent, saved before parsing) and from the request
bodies in filters/ (the bytes sent to Prospeo). Nothing the model typed is trusted. The manifest
pins a sha256 for each of those files, so a hand edit after the run is a FAIL.
"""
import copy
import hashlib
import itertools
import math
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

REAL_BASE_URL = "https://api.prospeo.io"
PAGE = 25
DATA_OPEN = "<!-- receipt-data"
DATA_CLOSE = "-->"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def root_of(company):
    """The root domain to send back in company.websites.exclude.

    Built from `website` (the filter is on websites), not `domain`, which can differ: Prospeo's own
    example has website intercom.com and domain intercom.io. `domain` is used only to strip a
    subdomain (app.foo.co.uk -> foo.co.uk) or when there is no website.
    """
    website = (company.get("website") or "").strip()
    host = ""
    if website:
        host = urlparse(website if "://" in website else "https://" + website).hostname or ""
        host = host.lower().removeprefix("www.")
    domain = (company.get("domain") or "").strip().lower().removeprefix("www.")
    if host and domain and host.endswith("." + domain):
        return domain
    return host or domain or None


def charge_of(endpoint, status, obj):
    """Credits one call cost, read from its response: a search page 1 unless `free`, an enrichment its `total_cost`,
    anything that failed 0. The engine's ledger and the receipt's recomputation both use this."""
    if not (status == 200 and isinstance(obj, dict) and obj.get("error") is False):
        return 0
    if endpoint.startswith("/search-company"):
        return 0 if obj.get("free") else 1
    if endpoint.startswith("/bulk-enrich-company"):
        return int(obj.get("total_cost", 0))
    return 0


def spot_rows(obj, icp):
    """The spot check's rows, compared with the ICP, from the bulk-enrich response."""
    techs = icp.get("company_technology", {}).get("include", [])
    bands = icp.get("company_headcount_range", [])
    model = icp.get("company_type", {}).get("business_model") if isinstance(icp.get("company_type"), dict) else None
    want_b2b = model == "b2b" or (isinstance(model, list) and model == ["b2b"])
    out = []
    for m in obj.get("matched", []):
        c = m["company"]
        tlist = {t.get("name"): t.get("category") for t in (c.get("technology") or {}).get("technology_list", [])}
        row = {"company_id": c.get("company_id"), "domain": root_of(c), "name": c.get("name"),
               "employee_range": c.get("employee_range"),
               "technology": {t: (f"on website, category '{tlist[t]}'" if t in tlist else "not detected")
                              for t in techs},
               "is_b2b": (c.get("attributes") or {}).get("is_b2b"), "disagreements": []}
        if bands and c.get("employee_range") not in bands:
            row["disagreements"].append(f"employee_range {c.get('employee_range')} is outside {bands}")
        for t in techs:
            if t not in tlist:
                row["disagreements"].append(f"{t} not in its technology list")
        if want_b2b and row["is_b2b"] is not True:
            row["disagreements"].append(f"attributes.is_b2b is {row['is_b2b']} although the search asked for b2b")
        out.append(row)
    return out


def parse_review(run):
    """company_ids in held.md's 'Held for review' table."""
    path = run / "held.md"
    if not path.exists() or "## Held for review" not in path.read_text():
        return []
    section = path.read_text().split("## Held for review", 1)[1]
    return [c[3].strip("`") for c in ([x.strip() for x in l.strip().strip("|").split("|")] for l in section.splitlines())
            if len(c) >= 4 and c[0] == "review" and c[3].startswith("`")]   # not the header row


def build_filters(icp, keeps, domains, names):
    """The filters of the current set: ICP, any range keeps passed in, every list stage's exclusions so far."""
    out = copy.deepcopy(icp)
    for k, v in keeps.items():
        out[k] = copy.deepcopy(v)
    if domains or names:
        company = out.setdefault("company", {})
        if domains:
            company["websites"] = {"exclude": sorted(domains)}
        if names:
            company["names"] = {"exclude": sorted(names)}
    return out


def load_run(run):
    problems = []
    brief = json.loads((run / "brief.json").read_text())
    meta = json.loads((run / "run_meta.json").read_text())
    lines = [json.loads(l) for l in (run / "manifest.jsonl").read_text().splitlines() if l.strip()]
    pins = {e["pin"]: e["sha256"] for e in lines if "pin" in e}
    entries = [e for e in lines if "pin" not in e]
    files = {}
    if pins.get("run_meta.json") != sha256_file(run / "run_meta.json"):
        problems.append("run_meta.json changed after the run started (or was never pinned)")
    if meta.get("brief_sha256") != sha256_file(run / "brief.json"):
        problems.append("brief.json changed after the run started")
    final = {}  # label -> (entry, parsed response, parsed request); the last attempt wins
    for e in entries:
        resp = run / "raw" / e["file"]
        if not resp.exists():
            problems.append(f"raw file missing: {e['file']}")
            continue
        files["raw/" + e["file"]] = sha256_file(resp)
        if files["raw/" + e["file"]] != e["sha256"]:
            problems.append(f"raw file changed after it was saved: raw/{e['file']}")
        req = None
        if e.get("request_file"):
            rp = run / "filters" / e["request_file"]
            if not rp.exists():
                problems.append(f"request file missing: filters/{e['request_file']}")
            else:
                files["filters/" + e["request_file"]] = sha256_file(rp)
                if files["filters/" + e["request_file"]] != e["request_sha256"]:
                    problems.append(f"request file changed after it was sent: filters/{e['request_file']}")
                req = json.loads(rp.read_bytes())
        try:
            obj = json.loads(resp.read_bytes())
        except ValueError:
            obj = None
        if charge_of(e["endpoint"], e["http_status"], obj) != e.get("charged"):
            problems.append(f"the charge recorded for raw/{e['file']} ({e.get('charged')}) is not what its response "
                            f"says ({charge_of(e['endpoint'], e['http_status'], obj)})")
        final[e["label"]] = (e, obj, req)
    return brief, meta, entries, final, files, problems


def total_of(item):
    """total_count of a search response; 0 for NO_RESULTS (HTTP 400, free); None if unusable."""
    if item is None:
        return None
    e, obj, _ = item
    if e["http_status"] == 200 and obj and obj.get("error") is False:
        return obj["pagination"]["total_count"]
    if obj and obj.get("error_code") == "NO_RESULTS":
        return 0
    return None


def rows_of(item):
    if item is None:
        return []
    _, obj, _ = item
    if not obj or obj.get("error") is not False:
        return []
    return [r["company"] for r in obj.get("results", [])]


def parse_held_md(run):
    """stage label -> company_ids listed in held.md's stage table."""
    out = {}
    path = run / "held.md"
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"s\d+_[a-z0-9_]+", cells[0]):
            out.setdefault(cells[0], []).append(cells[3].strip("`"))
    return out


MAX_PAGES = 1000  # Prospeo serves at most 1,000 pages of 25


def data_filter_for(held):
    """A range stage's `data` search: the same filter and sub-fields as `held`, over the filter's full documented range,
    so it counts every company with any value. Built here for the engine and the receipt alike, never typed by hand."""
    out = {}
    for key, value in held.items():
        value = copy.deepcopy(value)
        if key == "company_headcount_growth":
            value["min"], value["max"] = -100, 10000
        elif key == "company_website_traffic":
            change = value.get("visit_change")
            if not isinstance(change, dict) or set(value) - {"visit_change"}:
                raise ValueError("a traffic range stage may use visit_change only")
            change["min_change"], change["max_change"] = -100, 10000
        else:
            raise ValueError(f"{key} is not a supported range filter")
        out[key] = value
    return out


def venn_regions(counts):
    """counts: {frozenset of stage numbers: companies matching all of them}, for every non-empty subset. Returns the
    size of each exact region (in exactly those stages and no other), by Mobius inversion; every one must be >= 0."""
    regions = {}
    for region in counts:
        regions[region] = sum((-1) ** (len(t) - len(region)) * c for t, c in counts.items() if region <= t)
    return regions


def probe_filters(icp, st):
    """A list stage's coverage probe: its event filter with only the ICP's location, nothing else."""
    out = {"company_location_search": copy.deepcopy(icp["company_location_search"])} \
        if "company_location_search" in icp else {}
    out.update(copy.deepcopy(st["held"]))
    return out


def earlier_subsets(earlier):
    """Every non-empty subset of the earlier range stages' numbers, smallest first."""
    out = []
    for n in range(1, len(earlier) + 1):
        out.extend(itertools.combinations(earlier, n))
    return out


def and_label(sid, subset):
    return f"{sid}_and_" + "_".join(f"s{k}" for k in subset)


def range_filters(sl, stages, k, subset=()):
    """The set after list stages, plus stage k's signal, plus the signals of `subset` (stage numbers)."""
    out = copy.deepcopy(sl)
    out.update(copy.deepcopy(stages[k - 1]["held"]))
    for t in subset:
        out.update(copy.deepcopy(stages[t - 1]["held"]))
    return out


def country_outliers(rows, icp):
    """Pulled rows whose country is not in the location filter (only when every location value is a country name)."""
    inc = (icp.get("company_location_search") or {}).get("include") or []
    if not inc or any("," in v for v in inc):
        return None
    seen, out = set(), []
    for r in rows:
        cid = r.get("company_id")
        if cid in seen:
            continue
        seen.add(cid)
        country = (r.get("location") or {}).get("country")
        if country and country not in inc:
            out.append(cid)
    return {"pulled": len(seen), "outside": len(out)}


def compute(run):
    brief, meta, entries, final, files, problems = load_run(run)
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("integrity: every raw response and request body matches the sha256 pinned when it was saved",
          not problems, "; ".join(problems))
    held_md = parse_held_md(run)
    icp, stages = brief["icp_filters"], brief["stages"]
    mismatched = []

    def sent_is(label, page, filters):
        item = final.get(label)
        if item is None or item[2] != {"page": page, "filters": filters}:
            mismatched.append(label)

    def pages_of(prefix):
        return sorted((lbl for lbl in final if lbl.startswith(prefix) and lbl[len(prefix):].isdigit()),
                      key=lambda s: int(s[len(prefix):]))

    base = total_of(final.get("base"))
    check("base search returned a usable count", base is not None)
    sent_is("base", 1, icp)
    stages_out, all_rows = [], rows_of(final.get("base"))
    domains, names, list_ids = set(), set(), {}
    before, usable, sl_label = base, base is not None, "base"

    # list stages: excluded by passing the held companies' websites back to Prospeo
    for k, st in enumerate(stages, 1):
        if st["kind"] != "list" or not usable:
            continue
        sid = f"s{k}_{st['id']}"
        expected = build_filters(icp, {}, domains, names)
        expected.update(copy.deepcopy(st["held"]))
        hp = pages_of(f"{sid}_held_p")
        for lbl in hp:
            sent_is(lbl, int(lbl.rsplit("_p", 1)[1]), expected)
        held = total_of(final.get(f"{sid}_held_p1"))
        after = total_of(final.get(f"{sid}_apply"))
        if held is None or after is None:
            check(f"{sid}: held and apply searches returned usable counts", False, f"held={held} after={after}")
            usable = False
            continue
        probe = None
        if f"{sid}_probe" in final:
            sent_is(f"{sid}_probe", 1, probe_filters(icp, st))
            probe = total_of(final[f"{sid}_probe"])
        rows = [r for lbl in hp for r in rows_of(final[lbl])]
        all_rows += rows
        ids = [r.get("company_id") for r in rows]
        unique = set(ids)
        drop = before - after
        check(f"{sid}: removed == held ({drop} == {held})", drop == held)
        check(f"{sid}: held rows pulled == held ({len(unique)} == {held}), no duplicates",
              len(unique) == held and len(ids) == len(unique))
        check(f"{sid}: held.md lists exactly the held companies ({len(held_md.get(sid, []))} rows)",
              sorted(held_md.get(sid, [])) == sorted(unique))
        for r in rows:
            d = root_of(r)
            if d:
                domains.add(d)
            elif r.get("name"):
                names.add(r["name"])
        sent_is(f"{sid}_apply", 1, build_filters(icp, {}, domains, names))
        residual = None
        if held:
            # drop == held proves how many left; an empty residual proves they are the held companies
            res_f = build_filters(icp, {}, domains, names)
            res_f.update(copy.deepcopy(st["held"]))
            sent_is(f"{sid}_residual", 1, res_f)
            residual = total_of(final.get(f"{sid}_residual"))
            check(f"{sid}: no held company survived its exclusion (its filter, after the exclusions, found {residual})",
                  residual == 0)
        list_ids[sid] = unique
        stages_out.append({"stage": sid, "kind": "list", "before": before, "held": held, "drop": drop,
                           "after": after, "probe": probe, "residual": residual, "pages_read": len(hp),
                           "listed": len(unique)})
        before, sl_label = after, f"{sid}_apply"
    all_rows += rows_of(final.get(sl_label)) if sl_label != "base" else []

    # range stages: counted by intersection searches, so a company with no data is never removed
    sl_count = before
    sl = build_filters(icp, {}, domains, names)
    range_ks = [k for k, st in enumerate(stages, 1) if st["kind"] == "range"]
    held_of, ids_of, counts, stop = {}, {}, {}, {}
    list_held_ids = set().union(*list_ids.values()) if list_ids else set()
    for pos, k in enumerate(range_ks):
        if not usable:
            break
        st, sid, earlier = stages[k - 1], f"s{k}_{stages[k - 1]['id']}", range_ks[:pos]
        hp = pages_of(f"{sid}_held_p")
        for lbl in hp:
            sent_is(lbl, int(lbl.rsplit("_p", 1)[1]), range_filters(sl, stages, k))
        try:
            data_f = copy.deepcopy(sl)
            data_f.update(data_filter_for(st["held"]))
            sent_is(f"{sid}_data", 1, data_f)
        except (ValueError, AttributeError, TypeError):
            mismatched.append(f"{sid}_data (the brief's filter cannot be a range stage)")
        held = total_of(final.get(f"{sid}_held_p1"))
        data = total_of(final.get(f"{sid}_data"))
        inter = {}
        for sub in earlier_subsets(earlier):
            lbl = and_label(sid, sub)
            sent_is(lbl, 1, range_filters(sl, stages, k, sub))
            inter[lbl] = (sub, total_of(final.get(lbl)))
        if held is None or data is None or any(v is None for _, v in inter.values()):
            check(f"{sid}: its searches returned usable counts", False)
            usable = False
            break
        removed = held + sum((-1) ** len(sub) * c for sub, c in inter.values())
        counts[frozenset([k])] = held
        for sub, c in inter.values():
            counts[frozenset([k, *sub])] = c
        rows = [r for lbl in hp for r in rows_of(final[lbl])]
        all_rows += rows
        unique = {r.get("company_id") for r in rows}
        ids_of[k], held_of[k] = unique, held
        need = min(math.ceil(held / PAGE), MAX_PAGES)
        read_all = len(hp) >= need and held <= MAX_PAGES * PAGE
        check(f"{sid}: no company listed twice, and no more listed than the count ({len(unique)} of {held})",
              len(rows) == len(unique) and len(unique) <= held)
        if read_all:
            check(f"{sid}: every page was read, so the companies listed equal the count ({len(unique)} == {held})",
                  len(unique) == held)
        truncated = len(unique) < held
        if truncated:
            if len(hp) >= need and held > MAX_PAGES * PAGE:
                stop[sid] = f"Prospeo serves at most {MAX_PAGES:,} pages of {PAGE}"
            elif read_all:
                stop[sid] = "its pages held fewer distinct companies than its count (see the failed check)"
            elif brief.get("range_list_pages") is not None and len(hp) >= brief["range_list_pages"]:
                stop[sid] = "the range_list_pages limit"
            else:
                stop[sid] = "the cap"
        leaked = [r for r in rows if root_of(r) in domains or r.get("company_id") in list_held_ids]
        check(f"{sid}: no company it lists was removed by a list stage ({len(leaked)} found)", not leaked)
        check(f"{sid}: the counts fit inside the set after the list stages (signal {held}, data {data}, set "
              f"{sl_count})", held <= sl_count and data <= sl_count)
        check(f"{sid}: removed is between 0 and the companies with the signal ({removed} of {held})",
              0 <= removed <= held)
        check(f"{sid}: every company with the signal has data for it ({held} <= {data})", held <= data)
        check(f"{sid}: held.md lists the {len(unique)} companies pulled ({'truncated' if truncated else 'complete'})",
              sorted(held_md.get(sid, [])) == sorted(unique))
        stages_out.append({"stage": sid, "kind": "range", "before": before, "held": held, "drop": removed,
                           "after": before - removed, "overlap": held - removed, "data": data,
                           "no_data": sl_count - data, "intersections": {l: c for l, (_, c) in inter.items()},
                           "pages_read": len(hp), "listed": len(unique), "truncated": truncated})
        before -= removed
    cross = None
    if range_ks and usable:
        regions = venn_regions(counts)
        negative = {"+".join(f"s{k}" for k in sorted(r)): v for r, v in regions.items() if v < 0}
        check(f"the overlap counts describe a possible set of companies (every one of the {len(regions)} overlap "
              "regions is 0 or more)", not negative, ", ".join(f"{r}={v}" for r, v in negative.items()))
        if not stop:
            errors = []
            seen = set()
            for s in (x for x in stages_out if x["kind"] == "range"):
                k = int(s["stage"].split("_")[0][1:])
                first = ids_of[k] - seen
                seen |= ids_of[k]
                if len(first) != s["drop"]:
                    errors.append(f"{s['stage']} removed {s['drop']} by counting, {len(first)} by ids")
            for t_set, c in counts.items():
                if len(t_set) > 1 and len(set.intersection(*(ids_of[k] for k in t_set))) != c:
                    errors.append(f"overlap of {'+'.join(f's{k}' for k in sorted(t_set))} counted {c}")
            cross = (f"run: every range count matched the ids in the complete lists ({len(seen)} companies)"
                     if not errors else "run: the counts and the ids disagree (see the failed cross-check)")
            check("cross-check: every removal and every overlap counted by search equals the same count from the "
                  "ids in the complete lists (two methods agree)", not errors, "; ".join(errors))
        else:
            cross = "not run: " + "; ".join(f"{s} was not listed in full ({why})" for s, why in stop.items())

    check("every request body sent matches the brief (ICP, each stage's filter, exclusions so far)",
          not mismatched, ", ".join(mismatched))
    sids = list(list_ids)
    for i in range(len(sids)):
        for j in range(i + 1, len(sids)):
            overlap = list_ids[sids[i]] & list_ids[sids[j]]
            check(f"{sids[j]} holds no company already removed by {sids[i]}", not overlap, f"{len(overlap)} overlap")
    leaked = [root_of(r) for r in rows_of(final.get(sl_label)) if root_of(r) in domains]
    check("page 1 after the list stages contains no excluded website", not leaked, ", ".join(leaked))

    spot = None
    item = final.get("spotcheck_enrich")
    if item and item[1] and item[1].get("error") is False:
        rows = spot_rows(item[1], icp)
        disagree = sorted(r["company_id"] for r in rows if r["disagreements"])
        try:
            stored = json.loads((run / "spotcheck.json").read_text()).get("rows")
        except (OSError, ValueError):
            stored = None
        check(f"spot check: spotcheck.json and held.md's review table match the enrichment response ({len(rows)} "
              f"enriched, {len(disagree)} disagree)", stored == rows and sorted(parse_review(run)) == disagree)
        spot = {"enriched": len(rows), "disagree": len(disagree)}

    ledger = sum(e.get("charged", 0) for e in entries)
    acct_b = total_acct(final.get("account_before"))
    acct_a = total_acct(final.get("account_after"))
    spend = {"cap": meta.get("cap"), "ledger": ledger, "credits_before": acct_b, "credits_after": acct_a}
    if acct_b is not None and acct_a is not None:
        spend["account_difference"] = acct_b - acct_a
        check(f"spend: account difference == ledger ({acct_b - acct_a} == {ledger})", acct_b - acct_a == ledger)
    else:
        check("spend: account read before and after the run", False)
    check(f"spend: ledger <= cap ({ledger} <= {meta.get('cap')})", ledger <= (meta.get("cap") or 0))

    status = "PASS" if all(c["ok"] for c in checks) else "FAIL"
    return {
        "status": status,
        "source": meta.get("base_url"),
        "mock": meta.get("base_url") != REAL_BASE_URL,
        "started": meta.get("started"),
        "brief_sha256": sha256_file(run / "brief.json"),
        "base": base,
        "after_list_stages": sl_count,
        "stages": stages_out,
        "final": before if usable else None,
        "cross_check": cross,
        "country_outliers": country_outliers(all_rows, icp),
        "spot": spot,
        "probe_scope": (f"in {', '.join(icp['company_location_search'].get('include', []))}"
                        if "company_location_search" in icp else "with no other filter at all"),
        "spend": spend,
        "checks": checks,
        "files": dict(sorted(files.items())),
    }


def total_acct(item):
    if item is None:
        return None
    e, obj, _ = item
    if e["http_status"] == 200 and obj and obj.get("error") is False:
        return obj["response"]["remaining_credits"]
    return None


def render(data):
    out = ["# Disqualify-first receipt", ""]
    if data["mock"]:
        out += [f"> **TEST RUN against {data['source']}. These numbers are a mock server's, not Prospeo's.**", ""]
    out += [f"**Status: {data['status']}.** Every number below was recomputed by `receipt.py` from the raw "
            "responses in `raw/` and the request bodies in `filters/`.", "",
            f"Run started {data['started']} · source {data['source']} · `brief.json` sha256 `{data['brief_sha256']}`", "",
            "## Counts", "",
            "| # | Stage | Kind | Before | With the signal | Removed | Left | Notes |",
            "|---|---|---|---|---|---|---|---|",
            f"| 0 | ICP search (base) | | | | | {data['base']} | |"]
    for i, s in enumerate(data["stages"], 1):
        note = ""
        if s["kind"] == "list" and s.get("probe") is not None:
            note = f"coverage probe: {s['probe']} companies matched this filter {data['probe_scope']}"
        elif s["kind"] == "range":
            note = (f"{s['overlap']} also matched an earlier stage; {s['no_data']} of the {data['after_list_stages']} "
                    "after the list stages have no data for this filter")
        out.append(f"| {i} | {s['stage']} | {s['kind']} | {s['before']} | {s['held']} | {s.get('drop')} | "
                   f"{s.get('after')} | {note} |")
    removed = sum(s.get("drop") or 0 for s in data["stages"])
    out += ["", f"**Left after all stages: {data['final']} companies** ({removed} removed in all)."]
    for s in data["stages"]:
        if s["kind"] == "range" and data["final"] is not None:
            out.append(f"- Up to {min(s['no_data'], data['final'])} of the {data['final']} left have no data for "
                       f"`{s['stage']}`, so that stage never checked them.")
    co = data.get("country_outliers")
    if co:
        out.append(f"- Of the {co['pulled']} companies pulled, {co['outside']} report a country outside the location "
                   "filter.")
    if data.get("spot"):
        out.append(f"- Spot check: {data['spot']['enriched']} enriched, {data['spot']['disagree']} disagree with the "
                   "ICP (listed under 'Held for review' in held.md).")
    out += ["", "A company is removed only when Prospeo records the signal for it. A range stage is counted with "
            "intersection searches, so a company with no data stays in. Each removed company is credited to the "
            "first stage that removes it.",
            f"Cross-check: {data['cross_check'] or 'not applicable'}.", "", "## Checks", ""]
    for c in data["checks"]:
        out.append(f"- {'PASS' if c['ok'] else 'FAIL'} · {c['name']}" + (f" · {c['detail']}" if c["detail"] else ""))
    sp = data["spend"]
    out += ["", "## Spend", "",
            f"- Cap {sp['cap']} credits. Ledger (from each response): {sp['ledger']}. "
            f"Account before {sp['credits_before']}, after {sp['credits_after']}"
            + (f", difference {sp['account_difference']}." if "account_difference" in sp else "."),
            "", "## Files (sha256)", ""]
    for f, h in data["files"].items():
        out.append(f"- `{f}` `{h}`")
    out += ["", DATA_OPEN, json.dumps(data, indent=1, sort_keys=True), DATA_CLOSE, ""]
    return "\n".join(out)


def embedded(text):
    start = text.index(DATA_OPEN) + len(DATA_OPEN)
    end = text.index(DATA_CLOSE, start)
    return json.loads(text[start:end])


def main(argv):
    write = "--write" in argv
    args = [a for a in argv if a != "--write"]
    if len(args) != 1:
        print(__doc__)
        return 2
    run = Path(args[0])
    data = compute(run)
    if write:
        (run / "receipt.md").write_text(render(data))
        print(f"receipt.md written: {data['status']}, final {data['final']}")
        return 0 if data["status"] == "PASS" else 1
    path = run / "receipt.md"
    if not path.exists():
        print("FAIL: no receipt.md to verify")
        return 1
    text = path.read_text()
    stored = embedded(text)
    if stored == data and text != render(data):
        print("FAIL: receipt.md's visible text was edited (its data block still matches raw/)")
        return 1
    if stored != data:
        diffs = sorted(k for k in set(stored) | set(data) if stored.get(k) != data.get(k))
        print(f"FAIL: receipt.md does not match a recomputation from raw/ (differs in: {', '.join(diffs)})")
        for c in data["checks"]:
            if not c["ok"]:
                print(f"  FAIL · {c['name']} {c['detail']}")
        return 1
    if data["status"] != "PASS":
        print("FAIL: receipt.md reproduces, but its checks FAIL")
        return 1
    print(f"PASS: receipt.md reproduces from raw/ ({len(data['files'])} files); final {data['final']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
