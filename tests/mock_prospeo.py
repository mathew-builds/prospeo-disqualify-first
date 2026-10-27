"""A local stand-in for Prospeo's REST API, so the engine and receipt can be tested without spending credits.

What it copies from Prospeo's docs (saved 23-24 Oct 2026): the endpoints, the response shapes, NO_RESULTS
as HTTP 400, INVALID_FILTERS for an unknown filter, 25 rows a page, `free: true` on a page already paid
for, and credits falling by 1 per paid search page and 1 per matched enrichment.

What it ASSUMES, because Prospeo does not document it: a company with no growth or traffic data matches
no range; range ends are inclusive; `company.websites` matches the website's root (and `domain` only
when the website is a subdomain of it). The engine makes the same assumption about websites, so this
mock cannot catch that assumption being wrong. Only a real run's receipt can.
"""
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

KNOWN = {"company_location_search", "company_headcount_range", "company_technology", "company_type", "company",
         "company_news", "company_key_execs", "company_headcount_growth", "company_website_traffic"}


def make_dataset(seed=7, n=160, layoffs=0.08):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        website, domain = f"https://www.co{i:03d}.com", f"co{i:03d}.com"
        if i % 11 == 0:
            domain = f"co{i:03d}.io"            # website and domain differ, as in Prospeo's own example
        if i % 13 == 0:
            website, domain = f"https://app.co{i:03d}.co.uk", f"co{i:03d}.co.uk"   # a subdomain website
        out.append({
            "company_id": f"{i:024x}", "name": f"Co {i:03d}", "website": website, "domain": domain,
            "location": "United Kingdom" if i % 4 else "Germany",
            "employee_range": rnd.choice(["11-20", "21-50", "51-100", "101-200", "201-500"]),
            "tech": [t for t in ("HubSpot", "Salesforce", "Intercom") if rnd.random() < 0.6],
            "business_model": "b2b" if rnd.random() < 0.85 else "b2c",
            "is_b2b_flag": None,  # set below for one deliberate disagreement
            "news": {"Layoffs & Restructuring"} if rnd.random() < layoffs else set(),
            "execs": {"CRO Departed"} if rnd.random() < 0.1 else set(),
            "growth6": None if rnd.random() < 0.12 else round(rnd.uniform(-30, 30), 1),
            "traffic_q": None if rnd.random() < 0.12 else round(rnd.uniform(-50, 50), 2),
            "reported_country": None,  # what the record's location.country says, when it disagrees with the HQ
        })
    if n > 5:
        out[5]["reported_country"] = "United States"   # an HQ-filter match whose record says otherwise
    return out


def website_keys(c):
    host = (urlparse(c["website"]).hostname or "").lower().removeprefix("www.")
    keys = {host}
    if host.endswith("." + c["domain"]):
        keys.add(c["domain"])
    return keys


def in_range(v, lo, hi):
    if v is None:
        return False
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def matches(c, f, ignore=None, leak_in_range=False):
    for k in f:
        if k not in KNOWN:
            raise ValueError(f"Unknown filter: {k}")
    loc = f.get("company_location_search", {})
    if loc.get("include") and c["location"] not in loc["include"]:
        return False
    if c["location"] in loc.get("exclude", []):
        return False
    if f.get("company_headcount_range") and c["employee_range"] not in f["company_headcount_range"]:
        return False
    tech = f.get("company_technology", {})
    if tech.get("include") and not set(tech["include"]) & set(c["tech"]):
        return False
    bm = (f.get("company_type") or {}).get("business_model")
    if bm and c["business_model"] not in ([bm] if isinstance(bm, str) else bm):
        return False
    comp = f.get("company", {})
    keys = website_keys(c)
    inc = comp.get("websites", {}).get("include")
    if inc and not keys & set(inc):
        return False
    exc = set(comp.get("websites", {}).get("exclude", [])) - ({ignore} if ignore else set())
    if leak_in_range and ("company_headcount_growth" in f or "company_website_traffic" in f):
        exc = set()   # misbehaviour: exclusions ignored whenever a range filter is present
    if keys & exc:
        return False
    if c["name"] in comp.get("names", {}).get("exclude", []):
        return False
    news = f.get("company_news")
    if news and not set(news.get("categories") or []) & c["news"]:
        return False
    ex = f.get("company_key_execs")
    if ex and not set(ex.get("event_types") or []) & c["execs"]:
        return False
    g = f.get("company_headcount_growth")
    if g and not in_range(c["growth6"], g.get("min"), g.get("max")):
        return False
    t = (f.get("company_website_traffic") or {}).get("visit_change")
    if t and not in_range(c["traffic_q"], t.get("min_change"), t.get("max_change")):
        return False
    return True


def public(c, b2b_mismatch_ids=()):
    cats = {"HubSpot": "Marketing automation", "Salesforce": "CRM", "Intercom": "Live chat"}
    return {"company_id": c["company_id"], "name": c["name"], "website": c["website"], "domain": c["domain"],
            "employee_range": c["employee_range"],
            "location": {"country": c.get("reported_country") or c["location"]},
            "technology": {"technology_names": list(c["tech"]),
                           "technology_list": [{"name": t, "category": cats[t]} for t in c["tech"]]},
            "attributes": {"is_b2b": (c["business_model"] == "b2b") != (c["company_id"] in b2b_mismatch_ids)}}


class Mock:
    def __init__(self, data, ignore_exclusion_of=None, credits=35000, redirect_to=None, drop_after=None,
                 undercount_overlap=False, leak_in_range=False):
        self.data, self.ignore, self.credits = data, ignore_exclusion_of, credits
        self.undercount_overlap = undercount_overlap
        self.leak_in_range = leak_in_range
        self.redirect_to, self.drop_after, self.searches, self.keys = redirect_to, drop_after, 0, []
        self.start_credits = credits
        self.paid_pages, self.enriched, self.requests = set(), set(), []
        self.b2b_mismatch = {data[1]["company_id"]} if len(data) > 1 else set()
        self.lock = threading.Lock()

    def handle(self, method, path, body, key):
        self.requests.append((method, path, body))
        self.keys.append(key)
        if key != "mock-key":
            return 400, {"error": True, "error_code": "INVALID_API_KEY"}
        if method == "GET" and path == "/account-information":
            return 200, {"error": False, "response": {"current_plan": "PRO", "current_team_members": 1,
                                                       "remaining_credits": self.credits,
                                                       "used_credits": self.start_credits - self.credits,
                                                       "next_quota_renewal_days": 30,
                                                       "next_quota_renewal_date": "2026-11-23 00:00:00+00:00"}}
        if method == "POST" and path == "/search-suggestions":
            q = next(iter(body.values()), "").lower()
            if "location_search" in body:
                return 200, {"error": False, "location_suggestions":
                             [{"name": n, "type": "COUNTRY"} for n in ("United Kingdom", "Germany") if q in n.lower()]}
            if "technology_search" in body:
                return 200, {"error": False, "technology_suggestions":
                             [t for t in ("HubSpot", "Salesforce", "Intercom") if q in t.lower()]}
            return 200, {"error": False}
        if method == "POST" and path == "/search-company":
            f, page = body.get("filters", {}), int(body.get("page", 1))
            try:
                hits = [c for c in self.data if matches(c, f, self.ignore, self.leak_in_range)]
            except ValueError as e:
                return 400, {"error": True, "error_code": "INVALID_FILTERS", "filter_error": str(e)}
            if self.undercount_overlap and "company_headcount_growth" in f and "company_website_traffic" in f \
                    and hits and "max" in f["company_headcount_growth"]:
                hits = hits[1:]   # misreport an intersection by one company
            if not hits:
                return 400, {"error": True, "error_code": "NO_RESULTS"}
            rows = hits[(page - 1) * 25: page * 25]
            sig = json.dumps([sorted(c["company_id"] for c in rows), page])
            free = sig in self.paid_pages
            if not free:
                self.paid_pages.add(sig)
                self.credits -= 1
            return 200, {"error": False, "free": free, "results": [{"company": public(c, self.b2b_mismatch)} for c in rows],
                         "pagination": {"current_page": page, "per_page": 25, "total_page": -(-len(hits) // 25),
                                        "total_count": len(hits)}}
        if method == "POST" and path == "/bulk-enrich-company":
            by_id = {c["company_id"]: c for c in self.data}
            matched, not_matched, cost = [], [], 0
            for d in body.get("data", []):
                c = by_id.get(d.get("company_id"))
                if not c:
                    not_matched.append(d["identifier"])
                    continue
                if c["company_id"] not in self.enriched:
                    self.enriched.add(c["company_id"])
                    cost += 1
                matched.append({"identifier": d["identifier"], "company": public(c, self.b2b_mismatch)})
            self.credits -= cost
            return 200, {"error": False, "total_cost": cost, "matched": matched, "not_matched": not_matched,
                         "invalid_datapoints": []}
        return 400, {"error": True, "error_code": "INVALID_REQUEST"}


def start(mock):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else {}
            if mock.redirect_to and self.path == "/search-company":
                self.send_response(302)
                self.send_header("Location", mock.redirect_to + self.path)
                self.end_headers()
                return
            with mock.lock:
                status, obj = mock.handle(method, self.path, body, self.headers.get("X-KEY"))
                if self.path == "/search-company":
                    mock.searches += 1
            if mock.drop_after and self.path == "/search-company" and mock.searches >= mock.drop_after:
                self.close_connection = True   # charged, then no answer: the client sees a dropped connection
                return
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
