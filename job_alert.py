#!/usr/bin/env python3
"""Alert on new Staff/Principal Security Engineer and Security Architect jobs (India/remote, 8+ yrs).

Reads official public job feeds (Greenhouse, Lever, Ashby, Workday, Amazon) for the companies in
companies.json and sends a push notification through ntfy.sh for every new matching job.

Env vars:
  NTFY_TOPIC      ntfy topic to publish to (required to get phone alerts)
  MIN_YEARS       minimum years the job should ask for (default 8); Staff/Principal/Architect titles
                  are also kept when they ask 5+, since that is usually the posted floor for those levels
  INCLUDE_UNSTATED  "1" to also alert on matching titles whose description states no years (default 1)
  LOOP_MINUTES    if set, run forever and check every N minutes (for laptop use)
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "seen_jobs.json")
COMPANIES_FILE = os.path.join(HERE, "companies.json")
MIN_YEARS = int(os.getenv("MIN_YEARS", "8"))
INCLUDE_UNSTATED = os.getenv("INCLUDE_UNSTATED", "1") == "1"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (job-alert personal script)", "Accept": "application/json"}

TITLE_SECURITY = re.compile(r"\b(security|appsec|infosec|cyber|cybersecurity)\b", re.I)
TITLE_LEVEL = re.compile(r"\b(staff|principal|lead|architect|architecture|distinguished|senior\s+staff)\b", re.I)
SENIOR_TITLE = re.compile(r"\b(staff|principal|architect|architecture|distinguished)\b", re.I)
SENIOR_TITLE_MIN_YEARS = 5  # Staff/Principal/Architect titles often list "5+ years" as the bare minimum
TITLE_EXCLUDE = re.compile(
    r"\b(physical|sales|pre-?sales|account|customer|solutions?|consultant|analyst|intern|recruit\w*|"
    r"counsel|legal|compliance|grc|audit\w*|guard|officer|program\s+manager|project\s+manager|"
    r"product\s+manager|marketing|investigator|technician|operator|specialist|admin\w*|developer|"
    r"support|trainer|instructor)\b",
    re.I,
)
INDIA = re.compile(
    r"india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|delhi|"
    r"kolkata|ahmedabad|kochi|trivandrum|thiruvananthapuram|coimbatore|jaipur|indore|chandigarh|apac|asia",
    re.I,
)
GENERIC_REMOTE = re.compile(r"^\W*(remote|anywhere|global|worldwide|work from home|wfh)(\W+(remote|anywhere|global|worldwide|work from home|wfh))*\W*$", re.I)
YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?)?\s*(?:years|yrs|year)",
    re.I,
)


def get(url, data=None, timeout=25):
    headers = dict(UA)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s))


def title_ok(title):
    return bool(TITLE_SECURITY.search(title) and TITLE_LEVEL.search(title) and not TITLE_EXCLUDE.search(title))


def location_ok(loc):
    loc = (loc or "").strip()
    if INDIA.search(loc):
        return True
    return bool(GENERIC_REMOTE.match(loc))


def years_asked(text):
    """Highest 'N years ... experience' figure in the description (ignores e.g. '25 years of history')."""
    text = text or ""
    nums = []
    for m in YEARS.finditer(text):
        near = text[max(0, m.start() - 60): m.end() + 80].lower()
        if "experien" not in near:
            continue
        for g in m.groups():
            if g and 1 <= int(g) <= 20:
                nums.append(int(g))
    return max(nums) if nums else None


# ---- fetchers: each returns list of candidate dicts with a lazy "desc" loader ----

def fetch_greenhouse(company, slug):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": f"gh:{slug}:{j['id']}", "company": company, "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""), "url": j.get("absolute_url", ""),
            "desc": lambda s=slug, i=j["id"]: strip_html(
                get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs/{i}").get("content", "")),
        })
    return out


def fetch_lever(company, slug):
    data = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        loc = ", ".join(x for x in locs if x)
        if (j.get("workplaceType") == "remote") and not loc:
            loc = "Remote"
        text = " ".join([j.get("descriptionPlain", ""), j.get("additionalPlain", "")] +
                        [strip_html(x.get("content", "")) for x in j.get("lists", [])])
        out.append({"id": f"lever:{slug}:{j['id']}", "company": company, "title": j.get("text", ""),
                    "location": loc, "url": j.get("hostedUrl", ""), "desc": lambda t=text: t})
    return out


def fetch_ashby(company, slug):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        loc = ", ".join(x for x in locs if x)
        if j.get("isRemote") and not INDIA.search(loc):
            loc = f"Remote ({loc})" if loc else "Remote"
        out.append({"id": f"ashby:{slug}:{j['id']}", "company": company, "title": j.get("title", ""),
                    "location": loc, "url": j.get("jobUrl", ""),
                    "desc": lambda t=j.get("descriptionPlain", ""): t})
    return out


WORKDAY_SEARCHES = ["security architect", "staff security", "principal security", "lead security"]


def fetch_workday(company, path):
    host, tenant, site = path.split("/")
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    seen, out = set(), []
    for q in WORKDAY_SEARCHES:
        for offset in (0, 20):
            data = get(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
            posts = data.get("jobPostings", [])
            for p in posts:
                ep = p.get("externalPath")
                if not ep or ep in seen:
                    continue
                seen.add(ep)

                def detail(ep=ep):
                    info = get(f"{api}{ep}").get("jobPostingInfo", {})
                    return info

                loc = p.get("locationsText", "")
                out.append({"id": f"wd:{tenant}:{ep.rsplit('_', 1)[-1]}", "company": company,
                            "title": p.get("title", ""), "location": loc,
                            "url": f"https://{host}/{site}{ep}", "_detail": detail,
                            "desc": None})
            if len(posts) < 20:
                break
    return out


def fetch_amazon(company="Amazon", _=None):
    out = []
    for q in ("security architect", "principal security engineer", "security engineer"):
        url = ("https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(
            {"base_query": q, "normalized_country_code[]": "IND", "result_limit": 100, "sort": "recent"}))
        for j in get(url).get("jobs", []):
            text = strip_html(" ".join([j.get("basic_qualifications", ""), j.get("preferred_qualifications", ""),
                                        j.get("description", "")]))
            out.append({"id": f"amazon:{j.get('id_icims')}", "company": company, "title": j.get("title", ""),
                        "location": f"{j.get('location', '')} India", "url": "https://www.amazon.jobs" + j.get("job_path", ""),
                        "desc": lambda t=text: t})
    return out


INSTAHYRE_SKILLS = ["Security Architecture", "Application Security", "Product Security", "Cloud Security",
                    "Information Security", "Cyber Security"]


def fetch_instahyre(company="Instahyre", exclude=None):
    """Instahyre (India). Its years filter already limits results to jobs suitable for MIN_YEARS."""
    bad = re.compile("|".join(re.escape(x) for x in exclude), re.I) if exclude else None
    out, seen = [], set()
    for n, skill in enumerate(INSTAHYRE_SKILLS):
        if n:
            time.sleep(2)  # Instahyre rate-limits bursts
        q = urllib.parse.urlencode({"skills": skill, "years": MIN_YEARS, "limit": 35, "offset": 0})
        try:
            objs = get(f"https://www.instahyre.com/api/v1/job_search?{q}").get("objects", [])
        except urllib.error.HTTPError as e:
            if e.code == 429 and out:
                break  # keep what we have; the rest is picked up next run
            raise
        for j in objs:
            emp = (j.get("employer") or {}).get("company_name", "")
            if j["id"] in seen or (bad and bad.search(emp)):
                continue
            seen.add(j["id"])
            out.append({"id": f"instahyre:{j['id']}", "company": f"{emp} (Instahyre)",
                        "title": j.get("title", ""), "location": j.get("locations", ""),
                        "url": j.get("public_url", ""), "desc": lambda: "", "instahyre": True})
    return out


def collect():
    cfg = json.load(open(COMPANIES_FILE))
    tasks = []
    for name, slug in cfg.get("greenhouse", {}).items():
        tasks.append((fetch_greenhouse, name, slug))
    for name, slug in cfg.get("lever", {}).items():
        tasks.append((fetch_lever, name, slug))
    for name, slug in cfg.get("ashby", {}).items():
        tasks.append((fetch_ashby, name, slug))
    for name, path in cfg.get("workday", {}).items():
        tasks.append((fetch_workday, name, path))
    if cfg.get("instahyre"):
        tasks.append((fetch_instahyre, "Instahyre", cfg.get("exclude_companies", [])))
    if cfg.get("amazon"):
        tasks.append((fetch_amazon, "Amazon", None))

    def run(t):
        fn, name, arg = t
        try:
            return fn(name, arg), None
        except Exception as e:  # one broken board must not stop the rest
            return [], f"{name}: {type(e).__name__}: {e}"

    jobs, errors = [], []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for res, err in ex.map(run, tasks):
            jobs.extend(res)
            if err:
                errors.append(err)
    return jobs, errors


def evaluate(job):
    """Fill location/desc for Workday, then decide. Returns years (int|None) or False if rejected."""
    if job.get("_detail"):
        info = job["_detail"]()
        locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
        country = (info.get("country") or {}).get("descriptor", "")
        parts = [x for x in locs if x]
        if country and country.lower() not in " ".join(parts).lower():
            parts.append(country)
        job["location"] = ", ".join(parts)
        job["url"] = info.get("externalUrl") or job["url"]
        text = strip_html(info.get("jobDescription", ""))
    else:
        text = job["desc"]()
    if not location_ok(job["location"]):
        return False
    if job.get("instahyre"):
        return MIN_YEARS  # filtered server-side by Instahyre's years parameter
    yrs = years_asked(text)
    if yrs is None:
        return None if INCLUDE_UNSTATED else False
    floor = min(MIN_YEARS, SENIOR_TITLE_MIN_YEARS) if SENIOR_TITLE.search(job["title"]) else MIN_YEARS
    return yrs if yrs >= floor else False


def notify(title, message, url=None, priority="high"):
    print(f"\n[ALERT] {title}\n{message}\n{url or ''}")
    if not NTFY_TOPIC:
        return
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": priority, "Tags": "briefcase"}
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, Apply, {url}"
    req = urllib.request.Request(f"https://ntfy.sh/{urllib.parse.quote(NTFY_TOPIC)}", data=message.encode(),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"ntfy send failed: {e}", file=sys.stderr)


def check_once():
    first_run = not os.path.exists(STATE_FILE)
    seen = {} if first_run else json.load(open(STATE_FILE))
    known_sources = set(seen.get("_sources", []))
    jobs, errors = collect()
    source = lambda j: j["id"].rsplit(":", 1)[0]
    new_sources = {source(j) for j in jobs} - known_sources
    candidates = [j for j in jobs if j["id"] not in seen and title_ok(j["title"])]

    matches = []

    def judge(j):
        try:
            return j, evaluate(j), None
        except Exception as e:
            return j, False, f"{j['company']} {j['title']}: {e}"

    with ThreadPoolExecutor(max_workers=8) as ex:
        for j, verdict, err in ex.map(judge, candidates):
            if err:
                errors.append(err)
                continue  # retry next run
            seen[j["id"]] = int(time.time())
            if verdict is not False:
                matches.append((j, verdict))

    for j, yrs in matches:
        exp = f"{yrs}+ yrs" if yrs else "years not stated"
        body = f"{j['company']} | {j['location']} | {exp}"
        if first_run or source(j) in new_sources:
            print(f"[existing] {j['title']} — {body}\n  {j['url']}")
        else:
            notify(f"{j['title']} @ {j['company']}", body, j["url"])
    if first_run:
        lines = "\n".join(f"- {j['title']} @ {j['company']}" for j, _ in matches[:15])
        notify("Job alert is live", f"Watching {len(jobs)} jobs. {len(matches)} current matches:\n{lines}",
               priority="default")

    seen["_sources"] = sorted(known_sources | new_sources)
    json.dump(seen, open(STATE_FILE, "w"), separators=(",", ":"))
    print(f"\nChecked {len(jobs)} jobs, {len(candidates)} new title matches, {len(matches)} alerts. "
          f"Errors: {len(errors)}")
    for e in errors:
        print("  ! " + e, file=sys.stderr)


if __name__ == "__main__":
    loop = os.getenv("LOOP_MINUTES")
    if not loop:
        check_once()
    else:
        while True:
            check_once()
            time.sleep(float(loop) * 60)
