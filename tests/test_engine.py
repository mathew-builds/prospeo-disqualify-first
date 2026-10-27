#!/usr/bin/env python3
"""End-to-end tests of prospeo.py + receipt.py against the local mock. Spends nothing.

    python3 -I tests/test_engine.py        (from the skill folder; exits non-zero on any failure)

Expected counts come from the synthetic records directly, not from the engine's filter JSON. They share
the mock's assumptions (see mock_prospeo.py), so a pass proves the arithmetic and the controls, not
Prospeo's behaviour.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import mock_prospeo as mp  # noqa: E402

ENGINE = SKILL / "scripts" / "prospeo.py"
RECEIPT = SKILL / "scripts" / "receipt.py"
STAGES = json.loads((SKILL / "assets" / "default_stages.json").read_text())["stages"]
ICP = {"company_location_search": {"include": ["United Kingdom"]},
       "company_headcount_range": ["21-50", "51-100", "101-200"],
       "company_technology": {"include": ["HubSpot"]},
       "company_type": {"business_model": "b2b"}}
MIN_DEFAULT = 1 + 3 * 2 + (2 + 3)   # base, two list stages (held, apply, residual), two range stages (+ overlap)
FAILS = []


def ok(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def new_run(root, name, icp=ICP, stages=STAGES, **extra):
    run = Path(root) / name
    run.mkdir()
    (run / "brief.json").write_text(json.dumps({"icp_words": "test", "icp_filters": icp, "stages": stages,
                                                "defaults_used": True, **extra}, indent=1))
    return run


def engine(base_url, *args):
    env = dict(os.environ, PROSPEO_BASE_URL=base_url)
    p = subprocess.run([sys.executable, "-I", str(ENGINE), *map(str, args)], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def verify(run, write=False):
    p = subprocess.run([sys.executable, "-I", str(RECEIPT), *(["--write"] if write else []), str(run)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def receipt_data(run):
    text = (run / "receipt.md").read_text()
    start = text.index("<!-- receipt-data") + len("<!-- receipt-data")
    return json.loads(text[start:text.index("-->", start)])


def in_icp(c):
    return (c["location"] == "United Kingdom" and c["employee_range"] in ICP["company_headcount_range"]
            and "HubSpot" in c["tech"] and c["business_model"] == "b2b")


def truth(data):
    """A company is removed only when it has the signal; no data means it stays."""
    base = [c for c in data if in_icp(c)]
    h1 = [c for c in base if "Layoffs & Restructuring" in c["news"]]
    s1 = [c for c in base if c not in h1]
    h2 = [c for c in s1 if "CRO Departed" in c["execs"]]
    sl = [c for c in s1 if c not in h2]
    h3 = [c for c in sl if c["growth6"] is not None and c["growth6"] <= -10]
    h4 = [c for c in sl if c["traffic_q"] is not None and c["traffic_q"] <= -20]
    r4 = [c for c in h4 if c not in h3]
    return {"base": len(base), "held": [len(h1), len(h2), len(h3), len(h4)],
            "removed": [len(h1), len(h2), len(h3), len(r4)], "final": len(sl) - len(h3) - len(r4),
            "no_data": [sum(c["growth6"] is None for c in sl), sum(c["traffic_q"] is None for c in sl)]}


def main():
    root = tempfile.mkdtemp(prefix="dq-test-")
    print(f"test folder: {root}")

    print("T1 happy path: four default stages, spot check, cap 40")
    data = mp.make_dataset()
    mock = mp.Mock(data)
    server, url = mp.start(mock)
    run = new_run(root, "t1")
    code, out = engine(url, "run", run, "--cap", 40)
    ok(code == 0, f"engine exits 0 (got {code})" + ("" if code == 0 else "\n" + out))
    d = receipt_data(run)
    t = truth(data)
    ok(d["base"] == t["base"], f"base {d['base']} == truth {t['base']}")
    ok([s["held"] for s in d["stages"]] == t["held"], f"with the signal {[s['held'] for s in d['stages']]} == {t['held']}")
    ok([s["drop"] for s in d["stages"]] == t["removed"], f"removed {[s['drop'] for s in d['stages']]} == {t['removed']}")
    ok(d["final"] == t["final"], f"left {d['final']} == truth {t['final']} (companies with no data stay in)")
    ok([s["no_data"] for s in d["stages"] if s["kind"] == "range"] == t["no_data"],
       f"no-data counts {[s['no_data'] for s in d['stages'] if s['kind'] == 'range']} == {t['no_data']}")
    ok((d["cross_check"] or "").startswith("run:"), f"the two-method cross-check ran ({d['cross_check']})")
    ok(d["mock"] is True and "TEST RUN" in (run / "receipt.md").read_text(), "receipt marks a mock run as a test")
    ok(d["spend"]["ledger"] == mock.start_credits - mock.credits,
       f"ledger {d['spend']['ledger']} == mock's charge {mock.start_credits - mock.credits}")
    ok(d["spend"]["ledger"] <= 40, "spend within cap")
    ok((run / "spotcheck.json").exists(), "spot check ran")
    marked = [l for l in (run / "held.md").read_text().splitlines()
              if l.startswith("| s4_") and "removed it" in l]
    s4 = next(s for s in d["stages"] if s["stage"].startswith("s4"))
    ok(len(marked) == s4["overlap"], f"held.md marks the {s4['overlap']} traffic rows an earlier stage removed ({len(marked)})")
    code, out = verify(run)
    ok(code == 0, f"receipt.py verify exits 0 ({out.strip()})")
    n_raw = len(list((run / "raw").iterdir()))
    n_manifest = sum(1 for l in (run / "manifest.jsonl").read_text().splitlines() if l.strip() and '"pin"' not in l)
    ok(n_raw == n_manifest, f"one raw file per call ({n_raw} == {n_manifest})")
    code, out = engine(url, "run", run, "--cap", 40)
    ok(code == 3, "a used run folder is refused")
    server.shutdown()

    print("T2 mutation: change one count in raw/ by hand")
    m = Path(root) / "t2"
    shutil.copytree(run, m)
    f = next(p for p in sorted((m / "raw").iterdir()) if p.name.endswith("_base.json"))
    obj = json.loads(f.read_bytes())
    obj["pagination"]["total_count"] += 1
    f.write_text(json.dumps(obj))
    code, out = verify(m)
    ok(code != 0, f"verify exits non-zero ({out.splitlines()[0]})")
    code, out = verify(m, write=True)
    ok(code != 0, "rebuilding the receipt from the mutated raw/ also FAILs")
    ok("changed after it was saved" in (m / "receipt.md").read_text(), "the FAIL names the changed file")
    m = Path(root) / "t2b"
    shutil.copytree(run, m)
    f = next(p for p in sorted((m / "filters").iterdir()) if p.name.endswith("_base.json"))
    f.write_bytes(f.read_bytes().replace(b"United Kingdom", b"United Kingdon"))
    code, out = verify(m, write=True)
    ok(code != 0 and "request file changed after it was sent" in (m / "receipt.md").read_text(),
       "an edited request body FAILs and is named")

    print("T3 mutation: delete one row from held.md")
    m = Path(root) / "t3"
    shutil.copytree(run, m)
    lines = (m / "held.md").read_text().splitlines()
    idx = next(i for i, l in enumerate(lines) if re.match(r"\| s\d+_", l))
    del lines[idx]
    (m / "held.md").write_text("\n".join(lines) + "\n")
    code, out = verify(m)
    ok(code != 0, f"verify exits non-zero ({out.splitlines()[0]})")
    for kind, pattern in (("list", r"\| s[12]_"), ("range", r"\| s[34]_")):
        m = Path(root) / f"t3_{kind}"
        shutil.copytree(run, m)
        lines = (m / "held.md").read_text().splitlines()
        del lines[next(i for i, l in enumerate(lines) if re.match(pattern, l))]
        (m / "held.md").write_text("\n".join(lines) + "\n")
        code, out = verify(m, write=True)
        failed = [c["name"] for c in receipt_data(m)["checks"] if not c["ok"]]
        ok(code != 0 and any("held.md lists" in n for n in failed),
           f"a {kind} row deleted: rebuilding the receipt FAILs on the held.md check ({failed[:1]})")

    print("T4 Prospeo ignores one exclusion: the count check must catch it")
    held1 = [c for c in data if in_icp(c) and c["news"]]
    ok(bool(held1), f"dataset has stage-1 held companies ({len(held1)})")
    bad = mp.Mock(data, ignore_exclusion_of=next(iter(mp.website_keys(held1[0]))))
    server, url = mp.start(bad)
    run4 = new_run(root, "t4")
    code, out = engine(url, "run", run4, "--cap", 40)
    ok(code == 1, f"engine exits 1 (receipt FAIL) (got {code})")
    failed = [c["name"] for c in receipt_data(run4)["checks"] if not c["ok"]]
    ok(any(n.startswith("s1_layoffs: removed == held") for n in failed), f"the failed check is stage 1's count: {failed[:2]}")
    server.shutdown()

    print("T5 caps")
    server, url = mp.start(mp.Mock(data))
    code, out = engine(url, "run", new_run(root, "t5a"), "--cap", MIN_DEFAULT)
    ok(code == 0, f"cap {MIN_DEFAULT} (the minimum) passes, extras skipped (got {code})")
    d5 = receipt_data(Path(root) / "t5a")
    ok(d5["spend"]["ledger"] <= MIN_DEFAULT, f"spend {d5['spend']['ledger']} <= {MIN_DEFAULT}")
    server.shutdown()
    tight = mp.Mock(data)
    server, url = mp.start(tight)
    code, out = engine(url, "run", new_run(root, "t5b"), "--cap", MIN_DEFAULT - 1)
    ok(code == 3 and len(tight.requests) == 0, f"cap {MIN_DEFAULT - 1} is refused before any request (code {code}, "
       f"{len(tight.requests)} requests)")
    server.shutdown()
    many = mp.Mock(mp.make_dataset(seed=3, n=400, layoffs=0.9))
    server, url = mp.start(many)
    run5c = new_run(root, "t5c")
    code, out = engine(url, "run", run5c, "--cap", MIN_DEFAULT)
    spent = many.start_credits - many.credits
    ok(code == 2 and (run5c / "HALT.md").exists(), f"a list needing extra pages halts under cap {MIN_DEFAULT} (code {code})")
    ok(spent <= MIN_DEFAULT, f"and spends no more than the cap ({spent})")
    server.shutdown()

    print("T6 a client list in the brief is refused, and nothing reaches Prospeo")
    n1 = mp.Mock(data)
    server, url = mp.start(n1)
    icp = dict(ICP, company={"websites": {"exclude": [f"client{i}.example" for i in range(40)]}})
    code, out = engine(url, "run", new_run(root, "t6", icp=icp), "--cap", 40)
    ok(code == 3 and "never sent to Prospeo" in out, f"refused (code {code})")
    ok(len(n1.requests) == 0, f"zero requests reached the server ({len(n1.requests)})")
    code, out = engine(url, "plan", Path(root) / "t6")
    ok(code == 3, "plan refuses it too")
    server.shutdown()

    print("T7 a list stage that holds nothing gets a coverage probe")
    server, url = mp.start(mp.Mock(data))
    ipo = [{"id": "ipo", "kind": "list", "reason": "IPO news", "held": {"company_news": {"categories": ["IPO"],
                                                                                          "timeframe_days": 90}}}]
    run7 = new_run(root, "t7", stages=ipo, spotcheck=0)
    code, out = engine(url, "run", run7, "--cap", 5)
    d7 = receipt_data(run7)
    ok(code == 0 and d7["stages"][0]["held"] == 0 and d7["final"] == d7["base"], f"PASS with held 0 (code {code})")
    ok(d7["stages"][0].get("probe") == 0, f"the probe is on the receipt (probe {d7['stages'][0].get('probe')})")
    server.shutdown()

    print("T8 the per-call cap guard on its own (no projection in front of it)")
    guard = mp.Mock(data)
    server, url = mp.start(guard)
    os.environ["PROSPEO_BASE_URL"] = url
    sys.path.insert(0, str(SKILL / "scripts"))
    import prospeo  # noqa: E402  (reads PROSPEO_BASE_URL at import)
    client = prospeo.Client(Path(root) / "t8", cap=3)
    client.spent = 3
    try:
        client.call("POST", "/search-company", "over_cap", {"page": 1, "filters": ICP}, max_cost=1)
        halted = False
    except prospeo.Halt:
        halted = True
    ok(halted and len(guard.requests) == 0, f"a call past the cap halts before sending ({len(guard.requests)} sent)")
    server.shutdown()

    print("T9 a list stage longer than one page is paged in full")
    big = mp.Mock(mp.make_dataset(seed=3, n=400, layoffs=0.9))
    server, url = mp.start(big)
    run9 = new_run(root, "t9")
    code, out = engine(url, "run", run9, "--cap", 60)
    d9 = receipt_data(run9)
    ok(code == 0 and d9["stages"][0]["held"] > 25 and d9["stages"][0]["pages_read"] >= 2,
       f"PASS with stage 1 held {d9['stages'][0]['held']} over {d9['stages'][0]['pages_read']} pages (code {code})")
    server.shutdown()

    print("T10 Prospeo misreports an overlap: the two-method cross-check must catch it")
    server, url = mp.start(mp.Mock(data, undercount_overlap=True))
    run10 = new_run(root, "t10")
    code, out = engine(url, "run", run10, "--cap", 40)
    failed = [c["name"] for c in receipt_data(run10)["checks"] if not c["ok"]]
    ok(code == 1 and any(n.startswith("cross-check") for n in failed), f"FAIL on the cross-check (code {code}): {failed[:1]}")
    cross = receipt_data(run10)["cross_check"] or ""
    ok("disagree" in cross and "matched" not in cross, f"the receipt's cross-check line does not claim a match ({cross})")
    server.shutdown()

    print("T11 pins: a brief or run_meta edited after the run cannot pass")
    for name, edit in (("t11a", "kind"), ("t11b", "base_url")):
        m = Path(root) / name
        shutil.copytree(run, m)
        if edit == "kind":
            b = json.loads((m / "brief.json").read_text())
            b["stages"][1]["kind"] = "range"
            (m / "brief.json").write_text(json.dumps(b, indent=1))
        else:
            meta = json.loads((m / "run_meta.json").read_text())
            meta["base_url"] = "https://api.prospeo.io"
            (m / "run_meta.json").write_text(json.dumps(meta, indent=1))
        ok(verify(m)[0] != 0, f"{edit} edited: verify FAILs")
        ok(verify(m, write=True)[0] != 0, f"{edit} edited: rebuilding the receipt FAILs too")
        named = "brief.json changed" if edit == "kind" else "run_meta.json changed"
        ok(named in (m / "receipt.md").read_text(), f"{edit} edited: the FAIL says '{named}'")
    m = Path(root) / "t11c"
    shutil.copytree(run, m)
    b = json.loads((m / "brief.json").read_text())
    b["stages"][2]["held"]["company_headcount_growth"]["max"] = -5
    (m / "brief.json").write_text(json.dumps(b, indent=1))
    meta = json.loads((m / "run_meta.json").read_text())
    meta["brief_sha256"] = hashlib.sha256((m / "brief.json").read_bytes()).hexdigest()
    (m / "run_meta.json").write_text(json.dumps(meta, indent=1))
    pin = hashlib.sha256((m / "run_meta.json").read_bytes()).hexdigest()
    lines = [json.dumps({"pin": "run_meta.json", "sha256": pin}) if '"pin"' in l else l
             for l in (m / "manifest.jsonl").read_text().splitlines()]
    (m / "manifest.jsonl").write_text("\n".join(lines) + "\n")
    code, out = verify(m, write=True)
    body = [c["name"] for c in receipt_data(m)["checks"] if not c["ok"]]
    ok(code != 0 and any("request body" in n for n in body),
       f"brief edited and re-pinned: the request-body check still FAILs it ({body[:1]})")
    m = Path(root) / "t11d"
    shutil.copytree(run, m)
    b = json.loads((m / "brief.json").read_text())
    b["stages"][2]["held"]["company_headcount_growth"]["timeframe_month"] = 12
    (m / "brief.json").write_text(json.dumps(b, indent=1))
    meta = json.loads((m / "run_meta.json").read_text())
    meta["brief_sha256"] = hashlib.sha256((m / "brief.json").read_bytes()).hexdigest()
    (m / "run_meta.json").write_text(json.dumps(meta, indent=1))
    pin = hashlib.sha256((m / "run_meta.json").read_bytes()).hexdigest()
    lines = [json.dumps({"pin": "run_meta.json", "sha256": pin}) if '"pin"' in l else l
             for l in (m / "manifest.jsonl").read_text().splitlines()]
    (m / "manifest.jsonl").write_text("\n".join(lines) + "\n")
    verify(m, write=True)
    detail = " ".join(c["detail"] for c in receipt_data(m)["checks"] if not c["ok"])
    ok("s3_headcount_down_data" in detail, f"a timeframe edit is caught on the data search too ({detail[:90]})")

    print("T12 the visible receipt text is checked, not only its data block")
    m = Path(root) / "t12"
    shutil.copytree(run, m)
    text = (m / "receipt.md").read_text()
    text = re.sub(r"Left after all stages: (\d+)", lambda g: f"Left after all stages: {int(g.group(1)) + 22}", text,
                  count=1)
    (m / "receipt.md").write_text(text)
    code, out = verify(m)
    ok(code != 0, f"verify FAILs ({out.splitlines()[0]})")

    print("T13 a redirect is refused and the key does not follow it")
    target = mp.Mock(data)
    tserver, turl = mp.start(target)
    redir = mp.Mock(data, redirect_to=turl)
    server, url = mp.start(redir)
    code, out = engine(url, "run", new_run(root, "t13"), "--cap", 40)
    ok(code == 2 and len(target.requests) == 0, f"HALT, and the redirect target saw {len(target.requests)} requests")
    server.shutdown()
    tserver.shutdown()

    print("T14 a dropped connection after a charged search halts with the true spend")
    drop = mp.Mock(data, drop_after=3)
    server, url = mp.start(drop)
    run14 = new_run(root, "t14")
    code, out = engine(url, "run", run14, "--cap", 40)
    halt = (run14 / "HALT.md").read_text() if (run14 / "HALT.md").exists() else ""
    charged = drop.start_credits - drop.credits
    ok(code == 2 and f"Spent by this run: {charged} credits" in halt, f"HALT.md states {charged} credits (code {code})")
    server.shutdown()

    print("T15 filters that can carry a company list are refused")
    sneaky = mp.Mock(data)
    server, url = mp.start(sneaky)
    icp = dict(ICP, company_key_customers={"include": [f"client{i}.example" for i in range(40)]})
    code, out = engine(url, "run", new_run(root, "t15", icp=icp), "--cap", 40)
    ok(code == 3 and len(sneaky.requests) == 0, f"refused before any request (code {code}, {len(sneaky.requests)} sent)")
    icp = dict(ICP, company_lookalike={"include": ["Acme"]})   # short, no domain: only the allow-list stops it
    code, out = engine(url, "run", new_run(root, "t15b", icp=icp), "--cap", 40)
    ok(code == 3 and len(sneaky.requests) == 0, f"a lookalike filter is refused by name alone (code {code})")
    server.shutdown()

    print("T16 range listing pages are budgeted, and a truncated list says so")
    wide = [{"id": "g", "kind": "range", "reason": "growth <= 10",
             "held": {"company_headcount_growth": {"timeframe_month": 6, "max": 10}}},
            {"id": "t", "kind": "range", "reason": "traffic <= 0",
             "held": {"company_website_traffic": {"visit_change": {"period": "quarterly", "max_change": 0}}}}]
    minimum = 1 + 2 + 3
    for name, cap in (("t16a", minimum), ("t16b", minimum + 3)):
        wmock = mp.Mock(mp.make_dataset(seed=3, n=400))
        server, url = mp.start(wmock)
        r16 = new_run(root, name, stages=wide, spotcheck=0)
        code, out = engine(url, "run", r16, "--cap", cap)
        d16 = receipt_data(r16) if (r16 / "receipt.md").exists() else {"stages": [{}, {}], "spend": {"ledger": None}}
        spent = wmock.start_credits - wmock.credits
        ok(code == 0 and spent <= cap, f"cap {cap}: PASS within the cap (code {code}, spent {spent})")
        ok(d16["stages"][0].get("held", 0) > 25, f"cap {cap}: stage 1 holds more than a page ({d16['stages'][0].get('held')})")
        if cap == minimum:
            ok("not run" in (d16.get("cross_check") or "") and d16["stages"][0].get("pages_read") == 1,
               f"cap {cap}: listing every company does not fit, so none is bought and the receipt says why "
               f"({d16.get('cross_check')})")
        else:
            ok(d16["stages"][0].get("pages_read", 0) >= 2 and (d16.get("cross_check") or "").startswith("run"),
               f"cap {cap}: every list completed and the cross-check ran ({d16['stages'][0].get('pages_read')} pages)")
        server.shutdown()
    server, url = mp.start(mp.Mock(mp.make_dataset(seed=3, n=400)))
    r16 = new_run(root, "t16c", stages=wide, spotcheck=0, range_list_pages=1)
    code, out = engine(url, "run", r16, "--cap", minimum + 3)
    d16 = receipt_data(r16)
    ok(code == 0 and d16["stages"][0]["pages_read"] == 1 and "range_list_pages" in (d16["cross_check"] or ""),
       f"range_list_pages 1 is honoured and named ({d16['stages'][0]['pages_read']} page; {d16['cross_check']})")
    server.shutdown()
    server, url = mp.start(mp.Mock(mp.make_dataset(seed=3, n=400)))
    r16 = new_run(root, "t16d", stages=wide, spotcheck=5)
    code, out = engine(url, "run", r16, "--cap", minimum + 3 + 5 - 1)
    d16 = receipt_data(r16)
    spot = json.loads((r16 / "spotcheck.json").read_text()) if (r16 / "spotcheck.json").exists() else {"rows": []}
    ok(code == 0 and d16["stages"][0]["pages_read"] == 1 and len(spot["rows"]) == 5,
       f"the spot check is reserved before listing: listing skipped, 5 rows enriched "
       f"({d16['stages'][0]['pages_read']} page, {len(spot['rows'])} rows)")
    server.shutdown()

    print("T17 stage ids are checked")
    server, url = mp.start(mp.Mock(data))
    bad_id = json.loads(json.dumps(STAGES))
    bad_id[0]["id"] = "Layoffs-90d"
    code, out = engine(url, "plan", new_run(root, "t17", stages=bad_id))
    ok(code == 3, f"refused (code {code})")
    server.shutdown()

    print("T18 a pulled row whose country is outside the location filter is counted")
    odd = mp.make_dataset()
    first = next(c for c in odd if in_icp(c))
    first["reported_country"] = "United States"
    server, url = mp.start(mp.Mock(odd))
    run18 = new_run(root, "t18")
    code, out = engine(url, "run", run18, "--cap", 40)
    co = receipt_data(run18).get("country_outliers") or {}
    ok(code == 0 and co.get("outside", 0) >= 1, f"country outliers reported ({co})")
    server.shutdown()

    print("T19 range stages with `keep` and range-before-list orders are refused")
    server, url = mp.start(mp.Mock(data))
    v1 = json.loads(json.dumps(STAGES))
    v1[2]["keep"] = {"company_headcount_growth": {"timeframe_month": 6, "min": -9}}
    ok(engine(url, "plan", new_run(root, "t19a", stages=v1))[0] == 3, "a `keep` filter is refused")
    flipped = json.loads(json.dumps(STAGES))
    flipped = [flipped[2], flipped[0], flipped[1], flipped[3]]
    ok(engine(url, "plan", new_run(root, "t19b", stages=flipped))[0] == 3, "a range stage before a list stage is refused")
    clash = json.loads(json.dumps(STAGES))
    clash[0]["held"] = {"company_headcount_range": ["21-50"]}
    ok(engine(url, "plan", new_run(root, "t19c", stages=clash))[0] == 3, "a stage reusing an ICP filter is refused")
    server.shutdown()

    print("T20 exclusions that leak into the range searches are caught")
    leaky = mp.Mock(mp.make_dataset(seed=3, n=400, layoffs=0.9), leak_in_range=True)
    server, url = mp.start(leaky)
    run20 = new_run(root, "t20")
    code, out = engine(url, "run", run20, "--cap", 60)
    failed = [c["name"] for c in receipt_data(run20)["checks"] if not c["ok"]] if (run20 / "receipt.md").exists() else []
    ok(code == 1 and any("removed by a list stage" in n for n in failed) and any("fit inside" in n for n in failed),
       f"FAIL on both the leak and the fit-inside checks (code {code}): {failed[:2]}")
    server.shutdown()

    print("T21 data filters are built by the engine; impossible overlaps are caught")
    sys.path.insert(0, str(SKILL / "scripts"))
    import receipt as rc  # noqa: E402
    df = rc.data_filter_for({"company_headcount_growth": {"timeframe_month": 12, "max": -10}})
    ok(df == {"company_headcount_growth": {"timeframe_month": 12, "min": -100, "max": 10000}},
       f"data keeps the stage's timeframe and spans the full range ({df})")
    good = {frozenset([3]): 20, frozenset([4]): 15, frozenset([5]): 10, frozenset([3, 4]): 5, frozenset([3, 5]): 4,
            frozenset([4, 5]): 3, frozenset([3, 4, 5]): 2}
    bad = dict(good)
    bad[frozenset([3, 4, 5])] = 8
    ok(all(v >= 0 for v in rc.venn_regions(good).values()), "a consistent set of overlaps passes")
    ok(any(v < 0 for v in rc.venn_regions(bad).values()), "a triple overlap larger than its pairs is caught")
    server, url = mp.start(mp.Mock(data))
    with_data = json.loads(json.dumps(STAGES))
    with_data[2]["data"] = with_data[2]["held"]
    ok(engine(url, "plan", new_run(root, "t21a", stages=with_data))[0] == 3, "a hand-written `data` filter is refused")
    founded = json.loads(json.dumps(STAGES))
    founded[3] = {"id": "old", "kind": "range", "reason": "founded", "held": {"company_founded": {"max": 2000}}}
    ok(engine(url, "plan", new_run(root, "t21b", stages=founded))[0] == 3, "an unsupported range filter is refused")
    server.shutdown()

    print("T22 a range stage past Prospeo's 1,000-page limit: never page 1001, and the receipt names the limit")

    class Huge(mp.Mock):
        """The growth signal search reports 30,000 companies and serves made-up rows; page 1001 is an error."""
        def handle(self, method, path, body, key):
            f = body.get("filters", {}) if path == "/search-company" else {}
            if (f.get("company_headcount_growth") or {}).get("max") == -10 and "company_website_traffic" not in f:
                self.requests.append((method, path, body))
                page = int(body.get("page", 1))
                if page > 1000:
                    return 400, {"error": True, "error_code": "INVALID_REQUEST"}
                if f"huge{page}" not in self.paid_pages:
                    self.paid_pages.add(f"huge{page}")
                    self.credits -= 1
                rows = [{"company": {"company_id": f"ff{page:06d}{j:016x}", "name": f"Huge {page}-{j}",
                                     "website": f"https://huge{page}x{j}.com", "domain": f"huge{page}x{j}.com"}}
                        for j in range(25)]
                return 200, {"error": False, "free": False, "results": rows,
                             "pagination": {"current_page": page, "per_page": 25, "total_page": 1200,
                                            "total_count": 30000}}
            return super().handle(method, path, body, key)
    huge = Huge(data)
    server, url = mp.start(huge)
    run22 = new_run(root, "t22", icp={"company_location_search": {"include": ["United Kingdom"]}}, spotcheck=0)
    engine(url, "run", run22, "--cap", 1100)
    pages = [int(b.get("page", 1)) for _, p, b in huge.requests if p == "/search-company"
             and (b["filters"].get("company_headcount_growth") or {}).get("max") == -10
             and "company_website_traffic" not in b["filters"]]
    ok(max(pages) == 1000, f"the highest page asked is 1000 ({max(pages)})")
    cross = (receipt_data(run22)["cross_check"] or "") if (run22 / "receipt.md").exists() else ""
    ok("1,000 pages" in cross and "distinct" not in cross, f"the receipt names Prospeo's page limit ({cross})")
    server.shutdown()

    class Tweak(mp.Mock):
        """Rewrites the page-1 count of the searches `match` picks: a misbehaving Prospeo, for one check at a time."""
        def __init__(self, data, match, new_total):
            super().__init__(data)
            self.match, self.new_total = match, new_total

        def handle(self, method, path, body, key):
            status, obj = super().handle(method, path, body, key)
            if path == "/search-company" and status == 200 and int(body.get("page", 1)) == 1 \
                    and self.match(body["filters"]):
                obj["pagination"]["total_count"] = self.new_total(obj["pagination"]["total_count"])
            return status, obj

    def growth(f):
        return (f.get("company_headcount_growth") or {}).get("max")

    print("T23 impossible range counts: an overlap larger than a signal, and a signal larger than its data")
    pair = Tweak(data, lambda f: growth(f) == -10 and "company_website_traffic" in f, lambda t: t + 60)
    server, url = mp.start(pair)
    run23 = new_run(root, "t23a")
    code, out = engine(url, "run", run23, "--cap", 40)
    failed = [c["name"] for c in receipt_data(run23)["checks"] if not c["ok"]]
    ok(code == 1 and any("overlap regions" in n for n in failed) and any("removed is between 0" in n for n in failed),
       f"FAIL on the overlap-region and removal-bound checks (code {code}): {failed[:2]}")
    server.shutdown()
    thin = Tweak(data, lambda f: growth(f) == 10000, lambda t: 1)
    server, url = mp.start(thin)
    run23 = new_run(root, "t23b")
    code, out = engine(url, "run", run23, "--cap", 40)
    failed = [c["name"] for c in receipt_data(run23)["checks"] if not c["ok"]]
    ok(code == 1 and any("has data for it" in n for n in failed), f"FAIL on the signal-within-data check: {failed[:1]}")
    server.shutdown()

    print("T24 a list stage's pages repeat a company: the row count check names it")

    class Repeat(mp.Mock):
        def handle(self, method, path, body, key):
            status, obj = super().handle(method, path, body, key)
            f = body.get("filters", {}) if path == "/search-company" else {}
            if status == 200 and "company_news" in f and int(body.get("page", 1)) == 2:
                first = mp.Mock.handle(self, method, path, dict(body, page=1), key)[1]
                obj["results"][-1] = first["results"][0]
            return status, obj
    server, url = mp.start(Repeat(mp.make_dataset(seed=3, n=400, layoffs=0.9)))
    run24 = new_run(root, "t24")
    code, out = engine(url, "run", run24, "--cap", 60)
    failed = [c["name"] for c in receipt_data(run24)["checks"] if not c["ok"]]
    ok(code == 1 and any("held rows pulled" in n for n in failed), f"FAIL on the held-rows check: {failed[:2]}")
    server.shutdown()

    print("T25 an exclusion Prospeo ignores across both list stages: overlap and page-1 leak are named")
    dense = mp.make_dataset(seed=3, n=400, layoffs=0.9)
    both = [c for c in dense if in_icp(c) and c["news"] and c["execs"]]
    ok(bool(both), f"dataset has a company in both list stages ({len(both)})")
    server, url = mp.start(mp.Mock(dense, ignore_exclusion_of=next(iter(mp.website_keys(both[0])))))
    run25 = new_run(root, "t25")
    code, out = engine(url, "run", run25, "--cap", 60)
    failed = [c["name"] for c in receipt_data(run25)["checks"] if not c["ok"]]
    ok(code == 1 and any("already removed by" in n for n in failed) and any("excluded website" in n for n in failed),
       f"FAIL on the list-overlap and page-1 leak checks: {[n for n in failed if 'removed by' in n or 'website' in n][:2]}")
    server.shutdown()

    print("T26 more than 500 exclusions halts before Prospeo's filter limit")
    server, url = mp.start(mp.Mock(mp.make_dataset(seed=5, n=3000, layoffs=0.9)))
    run26 = new_run(root, "t26")
    code, out = engine(url, "run", run26, "--cap", 60)
    ok(code == 2 and "limit of 500" in out, f"HALT at the 500 limit (code {code}): {out.strip().splitlines()[-1][:90]}")
    server.shutdown()

    print("T27 receipt wording: the no-data line is bounded by what is left; a probe with no location says so")
    sparse = mp.make_dataset(seed=2, n=160)
    for i, c in enumerate(sparse):
        c["growth6"] = None if i % 5 < 3 else c["growth6"]
        c["traffic_q"] = -30.0 if i % 5 else c["traffic_q"]
    server, url = mp.start(mp.Mock(sparse))
    run27 = new_run(root, "t27a")
    code, out = engine(url, "run", run27, "--cap", 60)
    d27 = receipt_data(run27)
    lines = re.findall(r"Up to (\d+) of the (\d+) left", (run27 / "receipt.md").read_text())
    s3 = next(s for s in d27["stages"] if s["stage"].startswith("s3"))
    ok(code == 0 and s3["no_data"] > d27["final"] and lines and all(int(a) <= int(b) for a, b in lines),
       f"no data {s3['no_data']} > left {d27['final']}, and every 'up to' line is bounded: {lines}")
    server.shutdown()
    server, url = mp.start(mp.Mock(data))
    run27 = new_run(root, "t27b", icp={"company_headcount_range": ["21-50", "51-100"]}, stages=ipo, spotcheck=0)
    code, out = engine(url, "run", run27, "--cap", 5)
    note = [l for l in (run27 / "receipt.md").read_text().splitlines() if l.startswith("| 1 |")]
    ok(code == 0 and note and "with no other filter at all" in note[0], f"probe note: {note[0][-70:] if note else None}")
    server.shutdown()

    print("T28 spend: a silent extra charge, and a ledger over the cap, each FAIL their own check")

    class Overcharge(mp.Mock):
        """Charges more than the response reports: on the enrichment (after the last account check), or on searches."""
        def __init__(self, data, on):
            super().__init__(data)
            self.on = on

        def handle(self, method, path, body, key):
            status, obj = super().handle(method, path, body, key)
            if status == 200 and path == self.on and (path != "/search-company" or growth(body["filters"]) == -10):
                self.credits -= 1   # charged, and not said in the response
            return status, obj
    server, url = mp.start(Overcharge(data, "/bulk-enrich-company"))
    run28 = new_run(root, "t28a")
    code, out = engine(url, "run", run28, "--cap", 40)
    failed = [c["name"] for c in receipt_data(run28)["checks"] if not c["ok"]]
    ok(code == 1 and any("account difference == ledger" in n for n in failed), f"FAIL on the account check: {failed[:1]}")
    server.shutdown()
    hidden = Overcharge(data, "/search-company")
    server, url = mp.start(hidden)
    code, out = engine(url, "run", new_run(root, "t28c"), "--cap", 40)
    ok(code == 2 and "account shows" in out, f"a charge the responses hide halts at the next account check (code {code}): "
       f"{out.strip().splitlines()[-1][:100]}")
    server.shutdown()
    m = Path(root) / "t28b"
    shutil.copytree(run, m)
    lines = [json.loads(l) for l in (m / "manifest.jsonl").read_text().splitlines() if l.strip()]
    for e in lines:
        if e.get("label") == "base":
            e["charged"] = 100
    (m / "manifest.jsonl").write_text("\n".join(json.dumps(e, sort_keys=True) for e in lines) + "\n")
    verify(m, write=True)
    failed = [c["name"] for c in receipt_data(m)["checks"] if not c["ok"]]
    ok(any("ledger <= cap" in n for n in failed), f"a ledger over the cap FAILs its own check: {failed}")
    ok("charge recorded" in (m / "receipt.md").read_text(), "and the edited charge is named against its response")

    print("T29 range pages that disagree with their count FAIL")

    class RangeRows(mp.Mock):
        """repeat: page 2 of the growth list repeats a page-1 company. extra: page 1 of the traffic list carries one
        company more than Prospeo's count, one with the growth signal and no traffic signal."""
        def __init__(self, data, repeat=False, extra=False):
            super().__init__(data)
            self.repeat, self.extra = repeat, extra

        def handle(self, method, path, body, key):
            status, obj = super().handle(method, path, body, key)
            if path != "/search-company" or status != 200:
                return status, obj
            f, page = body.get("filters", {}), int(body.get("page", 1))
            t = (f.get("company_website_traffic") or {}).get("visit_change") or {}
            if self.repeat and growth(f) == 10 and "company_website_traffic" not in f and page == 2:
                obj["results"][-1] = mp.Mock.handle(self, method, path, dict(body, page=1), key)[1]["results"][0]
            if self.extra and t.get("max_change") == 0 and "company_headcount_growth" not in f and page == 1:
                listed = {r["company"]["company_id"] for r in obj["results"]}
                rest = {k: v for k, v in f.items() if k != "company_website_traffic"}
                more = next(c for c in self.data if mp.matches(c, rest) and c["growth6"] is not None
                            and c["growth6"] <= 10 and (c["traffic_q"] is None or c["traffic_q"] > 0)
                            and c["company_id"] not in listed)
                obj["results"].append({"company": mp.public(more)})
            return status, obj
    for name, mock, want in (("t29a", RangeRows(mp.make_dataset(seed=3, n=400), repeat=True), "every page was read"),
                             ("t29b", RangeRows(mp.make_dataset(seed=3, n=400), extra=True), "no more listed than")):
        server, url = mp.start(mock)
        r29 = new_run(root, name, stages=wide, spotcheck=0)
        code, out = engine(url, "run", r29, "--cap", minimum + 3)
        failed = [c["name"] for c in receipt_data(r29)["checks"] if not c["ok"]]
        ok(code == 1 and any(want in n for n in failed), f"{name}: FAIL on '{want}' (code {code}): {failed[:2]}")
        server.shutdown()

    print("T30 a held company that survives its exclusion while a clean one goes in its place FAILs")
    swap = mp.make_dataset()
    like = dict(location="United Kingdom", employee_range="51-100", tech=["HubSpot"], business_model="b2b",
                is_b2b_flag=None, execs=set(), growth6=None, traffic_q=None, reported_country=None)
    swap += [dict(like, company_id="e" * 24, name="Clean Root", website="https://zz.co.uk", domain="zz.co.uk",
                  news=set()),
             dict(like, company_id="f" * 24, name="Held Sub", website="https://app.zz.co.uk", domain="zz.co.uk",
                  news={"Layoffs & Restructuring"})]
    keys = mp.website_keys
    mp.website_keys = lambda c: {"app.zz.co.uk"} if c["name"] == "Held Sub" else keys(c)   # Prospeo matching hosts
    try:
        server, url = mp.start(mp.Mock(swap))
        run30 = new_run(root, "t30", spotcheck=0)
        code, out = engine(url, "run", run30, "--cap", 40)
        failed = [c["name"] for c in receipt_data(run30)["checks"] if not c["ok"]]
        ok(code == 1 and any("survived its exclusion" in n for n in failed), f"FAIL on the residual check: {failed[:2]}")
        server.shutdown()
    finally:
        mp.website_keys = keys

    print("T31 values shaped like a website, URL or email are refused outside product-name filters")
    for name, icp_x in (("t31a", dict(ICP, company_keywords={"include": ["acme.com", "globex.io"]})),
                        ("t31b", dict(ICP, company_location_search={"include": ["United Kingdom"],
                                                                    "exclude": ["https://initech.co.uk"]})),
                        ("t31c", dict(ICP, company_industry={"include": ["ceo@hooli.com"]})),
                        ("t31e", dict(ICP, company_keywords={"include": [f"client{i}.example" for i in range(40)]})),
                        ("t31f", dict(ICP, company_technology={"include": ["HubSpot"],
                                                               "exclude": [f"client{i}.example" for i in range(40)]})),
                        ("t31g", dict(ICP, company_type={"business_model": "b2b", "customers": ["acme.example"]})),
                        ("t31h", dict(ICP, company_keywords={"include": [f"Client Name {i}" for i in range(40)]}))):
        ok(engine("http://127.0.0.1:9", "plan", new_run(root, name, icp=icp_x))[0] == 3, f"{name}: refused")
    tech = dict(ICP, company_technology={"include": ["HubSpot", "Customer.io"]})
    ok(engine("http://127.0.0.1:9", "plan", new_run(root, "t31d", icp=tech))[0] == 0,
       "a product name such as Customer.io in company_technology is allowed")

    print("T32 spotcheck and range_list_pages are type-checked before any credit is spent")
    for name, extra in (("t32a", {"spotcheck": -1}), ("t32b", {"spotcheck": "5"}), ("t32c", {"spotcheck": True}),
                        ("t32d", {"range_list_pages": 0}), ("t32e", {"range_list_pages": "2"})):
        ok(engine("http://127.0.0.1:9", "plan", new_run(root, name, **extra))[0] == 3, f"{name} {extra}: refused")

    print("T33 the spot check is recomputed from the enrichment response")
    spot1 = json.loads((run / "spotcheck.json").read_text())
    ok(any("is_b2b" in x for r in spot1["rows"] for x in r["disagreements"]),
       f"the mock's is_b2b mismatch is flagged ({[r['name'] for r in spot1['rows'] if r['disagreements']]})")

    class Shrunk(mp.Mock):
        def handle(self, method, path, body, key):
            status, obj = super().handle(method, path, body, key)
            if path == "/bulk-enrich-company" and status == 200 and obj["matched"]:
                obj["matched"][0]["company"]["employee_range"] = "1-10"
            return status, obj
    server, url = mp.start(Shrunk(data))
    run33 = new_run(root, "t33a")
    code, out = engine(url, "run", run33, "--cap", 40)
    spot33 = json.loads((run33 / "spotcheck.json").read_text())
    ok(code == 0 and any("employee_range 1-10" in x for x in spot33["rows"][0]["disagreements"]),
       f"a band outside the ICP is flagged (code {code})")
    server.shutdown()
    m = Path(root) / "t33b"
    shutil.copytree(run, m)
    s = json.loads((m / "spotcheck.json").read_text())
    for r in s["rows"]:
        r["disagreements"] = []
    (m / "spotcheck.json").write_text(json.dumps(s, indent=1))
    code, out = verify(m, write=True)
    failed = [c["name"] for c in receipt_data(m)["checks"] if not c["ok"]]
    ok(code != 0 and any(n.startswith("spot check") for n in failed), f"an edited spotcheck.json FAILs: {failed[:1]}")

    print("T34 a rate-limited search is retried and still adds up")

    class Busy(mp.Mock):
        def handle(self, method, path, body, key):
            if path == "/search-company":
                self.n = getattr(self, "n", 0) + 1
                if self.n == 2:
                    self.requests.append((method, path, body))
                    return 429, {"error": True, "error_code": "RATE_LIMITED"}
            return super().handle(method, path, body, key)
    busy = Busy(data)
    server, url = mp.start(busy)
    run34 = new_run(root, "t34")
    code, out = engine(url, "run", run34, "--cap", 40)
    retried = [json.loads(l) for l in (run34 / "manifest.jsonl").read_text().splitlines() if '"attempt": 2' in l]
    ok(code == 0 and retried and receipt_data(run34)["spend"]["ledger"] == busy.start_credits - busy.credits,
       f"PASS after a 429 with the ledger equal to the mock's charge (code {code}, {len(retried)} retried)")
    server.shutdown()

    print("T35 a company name with a | in it does not break held.md")
    piped = mp.make_dataset()
    next(c for c in piped if in_icp(c) and c["news"])["name"] = "Pipe | Co"
    server, url = mp.start(mp.Mock(piped))
    run35 = new_run(root, "t35")
    code, out = engine(url, "run", run35, "--cap", 40)
    ok(code == 0 and "Pipe / Co" in (run35 / "held.md").read_text(), f"PASS, name escaped (code {code})")
    server.shutdown()

    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'}")
    if FAILS:
        print(f"test folder kept for inspection: {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
