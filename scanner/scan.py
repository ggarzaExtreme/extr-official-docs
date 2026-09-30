"""Extreme Docs Atlas scanner. Reads Extreme Networks' public documentation sites into a catalog, writes a
report for each scan, builds the explorer page, and keeps a library's downloaded copies current.

Needs Python 3 with PyYAML and lxml (PyMuPDF only for a library's details). Run from anywhere:

  python scanner/scan.py scan [--if-due] [--only docs-site|main-site] [--allow-shrink]
                                           read the sites, write results/, rebuild index.html
  python scanner/scan.py due               is a scan due? (exit 0 yes, 1 no)
  python scanner/scan.py refresh           rebuild the latest report, the README's Last Scan and index.html
  python scanner/scan.py compare REF REF   what changed between two scans (git tags scan-<date>, or commits)
  python scanner/scan.py update --library DIR [--profile FILE] [--dry-run]
                                           download what DIR's profile takes, record it in DIR/.docsync/
  python scanner/scan.py check --library DIR [--remote]
                                           verify DIR's files against its lock; --remote asks the servers too

What each file holds is in the README; how each site is read, in scanner/NOTES.md.
"""
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SCANNER_VERSION = "2"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(ROOT, "config")
CATALOG = os.path.join(ROOT, "results", "catalog")
REPORTS = os.path.join(ROOT, "results", "reports")
CACHE = os.path.join(ROOT, ".cache", "pages")  # saved pages, so a stopped scan resumes; cleared when a scan finishes
TEMPLATE = os.path.join(HERE, "template.html")
EXPLORER = os.path.join(ROOT, "index.html")
SITES = ("docs-site", "main-site")
UA = {"User-Agent": "Mozilla/5.0 (Extreme Docs Atlas scanner)"}
WORKERS = 3  # the docs site slows down under heavier load
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


# ---------- config ----------

def load_yaml(path, default=None):
    import yaml
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def sites_config():
    c = load_yaml(os.path.join(CONFIG, "sites.yaml"))
    if not c:
        raise SystemExit("config/sites.yaml is missing: it names the sites to scan")
    return c


SITES_CFG = sites_config()
DOCS = SITES_CFG["sites"]["docs-site"]["url"].rstrip("/") + "/"
DOCS_INDEX = DOCS + "support/documentation/"
MAIN_CFG = SITES_CFG["sites"]["main-site"]
WWW = MAIN_CFG["url"].rstrip("/")
WWW_AREAS = tuple(MAIN_CFG["areas"])
WWW_RESOURCE_TYPES = tuple(MAIN_CFG["resource_types"])
WWW_SNAPSHOT_PAGES = tuple(p.rstrip("/") for p in MAIN_CFG["snapshot_pages"])


def rescan_after_days():
    return int(SITES_CFG.get("rescan_after_days") or 30)


TYPES_CFG = load_yaml(os.path.join(CONFIG, "types.yaml"), {})
TYPE_RULES = [(t, re.compile(rx, re.I)) for t, rx in TYPES_CFG.get("rules") or []]
TYPE_BY_FORMAT = TYPES_CFG.get("by_format") or {}
TRANSLATION = re.compile(TYPES_CFG.get("translation") or r"$^", re.I)
QUICK_REF = re.compile(r"quick (reference|start)", re.I)
TYPE_ORDER = [t["name"] for t in TYPES_CFG.get("types") or []]
VALUE = {t["name"]: t["value"] for t in TYPES_CFG.get("types") or []}


def doc_type(title, fmt):
    """A document's type: its format first (API, Video, Doc Collection), then the first title rule that
    matches (config/types.yaml), else Other."""
    if fmt in TYPE_BY_FORMAT:
        return TYPE_BY_FORMAT[fmt]
    if re.search(r"document collection", title or "", re.I):
        return "Doc Collection"
    return next((t for t, rx in TYPE_RULES if rx.search(title or "")), "Other")


def starter_profiles():
    """{name: profile} from config/profiles/*.yaml."""
    d = os.path.join(CONFIG, "profiles")
    out = {}
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if n.endswith(".yaml"):
            out[n[:-5]] = load_yaml(os.path.join(d, n), {})
    return out


# ---------- network ----------

class Pace:
    """A delay shared by all workers: doubles on a throttle or connection error, and after every run of
    successes shrinks again, so a scan slows down when a site pushes back and speeds up when it recovers."""
    def __init__(self):
        self.lock, self.delay, self.ok = threading.Lock(), 0.0, 0

    def wait(self):
        if self.delay:
            time.sleep(self.delay)

    def good(self):
        with self.lock:
            self.ok += 1
            if self.ok >= 20 and self.delay:
                self.delay = self.delay * 0.7 if self.delay > 0.1 else 0.0
                self.ok = 0

    def bad(self, floor=0.0):
        with self.lock:
            self.delay = min(15.0, max(floor, 0.5, self.delay * 2))
            self.ok = 0


PACE = Pace()
STATS = {"live": 0, "saved": 0, "throttled": 0, "retried": 0, "failed": 0}  # counted as the scan runs
STATS_LOCK = threading.Lock()


def count(k, n=1):
    with STATS_LOCK:
        STATS[k] += n


def http(url, method="GET", tries=4):
    """(status, final_url, headers, body bytes). Never raises for HTTP errors; body is b'' on failure.
    Backs off on 429/5xx and connection errors, honouring Retry-After."""
    url = quote_url(url)
    for i in range(tries):
        wait = 5 * 2 ** i
        PACE.wait()
        if i:
            count("retried")
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA, method=method), timeout=45)
            out = r.status, r.geturl(), dict(r.headers), (r.read() if method == "GET" else b"")
            PACE.good()
            count("live")
            return out
        except urllib.error.HTTPError as e:
            if not (e.code == 429 or e.code >= 500):
                PACE.good()  # a real 404 is an answer, not push-back
                count("live")
                return e.code, url, dict(e.headers or {}), b""
            count("throttled")
            ra = (e.headers or {}).get("Retry-After", "")
            wait = int(ra) if ra.isdigit() else wait
            PACE.bad(float(wait) / 4)
            if i == tries - 1:
                count("failed")
                return e.code, url, dict(e.headers or {}), b""
        except Exception as e:  # connection reset, timeout, DNS
            PACE.bad()
            if i == tries - 1:
                count("failed")
                return 0, url, {"error": "%s: %s" % (type(e).__name__, e)}, b""
        time.sleep(min(wait, 120))


def quote_url(url):
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(urllib.parse.unquote(p.path), safe="/%:@!$&'()*+,;="),
                                    p.query, ""))


def fetch_all(urls, fn, stall=120, label="pages"):
    """Apply fn(url, status, final_url, headers, body) to every url; results keyed by url. Threads only
    download, parsing stays on this thread. If nothing finishes for `stall` seconds, the unfinished urls get
    one more try in a fresh pool, then are recorded as status 0 ("stalled") so the scan always ends.
    Prints progress with a rate and an estimate of the time left."""
    out, failed, todo, t0 = {}, {}, list(urls), time.time()
    for attempt in (1, 2):
        ex = cf.ThreadPoolExecutor(WORKERS)
        futures = {ex.submit(cached_http, u): u for u in todo}
        pending = set(futures)
        while pending:
            done, pending = cf.wait(pending, timeout=stall, return_when=cf.FIRST_COMPLETED)
            if not done:
                break
            for f in done:
                u = futures[f]
                res = f.result()
                if res[0] != 200:
                    failed[u] = res
                    print("  failed %s %s %s" % (res[0], res[2].get("error", ""), u), flush=True)
                    continue
                out[u] = fn(u, *res)
                if len(out) % 100 == 0:
                    secs = max(0.001, time.time() - t0)
                    rate = len(out) / secs
                    left = (len(urls) - len(out)) / rate if rate else 0
                    print("  %s %s/%s   %.1f/s   about %d min left   live %d, saved %d   throttled %d   slowdown %.1f s" % (
                        label, format(len(out), ","), format(len(urls), ","), rate, left // 60 + 1, STATS["live"], STATS["saved"],
                        STATS["throttled"], PACE.delay), flush=True)
        ex.shutdown(wait=False, cancel_futures=True)
        todo = [futures[f] for f in pending]
        if not todo:
            break
        print("  %d stalled (attempt %d)" % (len(todo), attempt), flush=True)
    for u in todo:
        failed[u] = (0, u, {"error": "stalled"}, b"")
    if failed:  # one slow, sequential pass over whatever failed under load
        print("  retrying %d failed one at a time" % len(failed), flush=True)
        for u in sorted(failed):
            time.sleep(2)
            res = cached_http(u)
            out[u] = fn(u, *(res if res[0] == 200 else failed[u]))
    return out


def cached_http(url):
    """http() for scan pages, through a disk cache that lasts until the scan finishes, so a stopped scan
    resumes after any gap and a finished one never feeds the next."""
    key = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(CACHE, key)
    if os.path.exists(path) and os.path.exists(path + ".json"):
        with open(path + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        with open(path, "rb") as f:
            count("saved")
            return meta["status"], meta["final"], meta["headers"], f.read()
    status, final, headers, body = http(url)
    if status == 200 and body:
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
        with open(path + ".json", "w", encoding="utf-8") as f:
            json.dump({"url": url, "status": status, "final": final, "headers": headers}, f)
    return status, final, headers, body


def sitemap(url):
    """Every <loc> in a sitemap. A sitemap that fails to load stops the scan: a partial page list would
    look like the site dropping hundreds of pages."""
    status, _, _, body = http(url)
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", body.decode("utf-8", "replace")) if status == 200 else []
    if not locs:
        raise SystemExit("sitemap %s: status %s, no entries; scan stopped, catalog unchanged" % (url, status))
    return locs


# ---------- timing: one row per step of a scan ----------

TIMING = []


class Step:
    """with Step("Docs-site pages") as s: ...; s.pages = n. Records time, rate and what the network did."""
    def __init__(self, name):
        self.name, self.pages = name, 0

    def __enter__(self):
        self.t0, self.s0 = time.time(), dict(STATS)
        print(self.name.lower(), flush=True)
        return self

    def __exit__(self, *exc):
        secs = time.time() - self.t0
        d = {k: STATS[k] - self.s0[k] for k in STATS}
        TIMING.append({"step": self.name, "pages": self.pages, "seconds": round(secs, 1),
                       "rate": round(self.pages / secs, 2) if secs > 0 else 0, **d})
        return False


# ---------- parsing (pure, tested) ----------

def html(body):
    import lxml.html
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")  # both sites serve UTF-8; lxml's byte sniffing guesses wrong
        except UnicodeDecodeError:
            pass
    if isinstance(body, str):
        body = re.sub(r"^\s*<\?xml[^>]*\?>", "", body)
    return lxml.html.fromstring(body)


def clean(s):
    return re.sub(r"\s+", " ", re.sub("[​‌‍﻿™®]", "", s or "")).strip()


def norm(url):
    return url.split("#")[0].rstrip("/") + "/" if url else url


def month_iso(text):
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.? (\d{4})\b", text or "")
    if m and m.group(1).lower() in MONTHS:
        return "%s-%02d" % (m.group(2), MONTHS[m.group(1).lower()])
    return ""


def kind(url, title=""):
    """What a link points at, from its url: pdf, portal (an HTML guide), archive, sheet, api, video, hub
    (the main site's content hub), page, external, other. format_via() turns it into the catalog's words."""
    u = (url or "").lower()
    path = urllib.parse.urlsplit(u).path
    host = urllib.parse.urlsplit(u).netloc
    ext = os.path.splitext(urllib.parse.unquote(path))[1]
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):  # "v9.4 User Guide" has no extension
        ext = ""
    if "youtu" in host or "vimeo" in host:
        return "video"
    if "sitecorecontenthub" in host:
        return "hub"
    if ext == ".pdf":
        return "pdf"
    if ext in (".zip", ".tar", ".gz", ".tgz"):
        return "archive"
    if ext in (".xlsx", ".xls", ".csv"):
        return "sheet"
    if host.startswith("developer.") or "api" in path.lower() or re.search(r"\bAPI\b", title or ""):
        return "api"
    if host == "documentation.extremenetworks.com":
        return "portal" if ext in ("", ".html", ".htm", ".shtml") else "other"
    if "extremenetworks.com" in host:
        return "page"
    return "external"


FORMAT_VIA = {"pdf": ("pdf", "direct"), "portal": ("pdf", "html_guide"), "archive": ("zip", "direct"), "sheet": ("xlsx", "direct"),
              "api": ("api", "direct"), "video": ("video", "direct"), "hub": ("file", "content_hub"), "page": ("page", "direct"),
              "external": ("page", "direct"), "other": ("other", "direct")}


def format_via(url, title=""):
    """(format, via): what the file is, and how it is reached. An HTML guide is a PDF reached through its guide."""
    return FORMAT_VIA[kind(url, title)]


def parse_docs_index(body):
    """[(heading, label, url)] for every entry on the documentation index, under its heading."""
    d = html(body)
    out = []
    for sec in d.xpath("//section"):
        heads = [clean(h.text_content()) for h in sec.xpath(".//*[self::h2 or self::h3 or self::h4]")]
        heads = [h for h in heads if h]
        for a in sec.xpath(".//a[@href]"):
            href = a.get("href")
            if "/support/documentation/" in href and norm(href) != DOCS_INDEX and clean(a.text_content()):
                out.append((heads[0] if heads else "", clean(a.text_content()), norm(urllib.parse.urljoin(DOCS_INDEX, href))))
    seen, uniq = set(), []
    for row in out:
        if row[2] not in seen:
            seen.add(row[2])
            uniq.append(row)
    return uniq


def parse_docs_page(body, url):
    """An entry's page or a release page: title, release menu options, the selected release, document items."""
    d = html(body)
    title = clean(d.findtext(".//title") or "").replace(" | Extreme Networks Support Documentation", "")
    options = []
    for s in d.xpath("//select"):
        opts = [(clean(o.text_content()), o.get("value") or "") for o in s.xpath(".//option")]
        opts = [(re.sub(r"^Version\s*", "", t), norm(v)) for t, v in opts if "/support/documentation/" in v]
        if opts:
            options = opts
            break
    selected = next((t for t, v in options if v == norm(url)), "")
    items = []
    for a in d.xpath("//li//a[@href][.//h1 or .//h2 or .//h3 or .//h4 or .//h5]"):
        h = clean(a.xpath("string((.//h1|.//h2|.//h3|.//h4|.//h5)[1])"))
        rest = [clean(t) for t in a.xpath(".//text()") if clean(t) and clean(t) != h]
        date = next((r for r in rest if month_iso(r)), "")
        items.append({"title": h, "date": date, "url": urllib.parse.urljoin(url, a.get("href").strip())})
    return {"title": title, "options": options, "selected": selected, "items": items, "health": health(d, url, items)}


DOCLIKE = re.compile(r"documentation\.extremenetworks\.com|sitecorecontenthub|youtu|vimeo|\.(pdf|zip|xlsx?)(\?|$)", re.I)


def health(d, url, items):
    """What the parser may be missing on a page: document-like links outside the parsed items, more
    than one release menu, and structures that load or hide content (iframes, tabs, load-more)."""
    got = {i["url"] for i in items}
    links = d.xpath("//body//a[@href][not(ancestor::header or ancestor::footer or ancestor::nav)]/@href")
    missed = sorted({urllib.parse.urljoin(url, h.strip()) for h in links if DOCLIKE.search(h)} - got)
    selects = sum(1 for s in d.xpath("//select") if len(s.xpath(".//option")) > 1)
    signals = []
    if d.xpath("//iframe[@src]"):
        signals.append("iframe")
    if d.xpath("//*[@role='tab' or @role='tablist']"):
        signals.append("tabs")
    if any(re.search(r"(load|show|view) (more|all)", clean(e.text_content()), re.I) for e in d.xpath("//button|//a")):
        signals.append("load-more")
    if d.xpath("//*[@data-ajax or @data-url or @data-src][not(self::img)]"):
        signals.append("data-url")
    return {"missed": missed, "selects": selects, "signals": signals}


def latest_line(options):
    """Release pages in the newest release line: every option sharing the first numbered option's
    major.minor. A patch page (9.4.1.0) often lists only its release notes while the guides sit on the
    line's first page (9.4.0), so 'latest' is the line, not one page."""
    nums = [(t, v) for t, v in options if re.match(r"^v?\d+\.\d+", t)]
    if not nums:
        return set()
    line = re.match(r"^v?(\d+\.\d+)", nums[0][0]).group(1)
    return {v for t, v in nums if re.match(r"^v?(\d+\.\d+)", t).group(1) == line}


def portal_target(body, base):
    """Where an HTML guide's root page redirects (its meta refresh), as an absolute url; '' if it does not."""
    m = re.search(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["'][^"']*?url=([^"'>]+)""",
                  body.decode("utf-8", "replace") if isinstance(body, bytes) else body, re.I)
    return urllib.parse.urljoin(base if base.endswith("/") else base + "/", m.group(1).strip()) if m else ""


def portal_revision(target):
    """The revision an HTML guide's redirect names: 'fe_9.4_ug_revacdev/...' is Rev AC. '' when it names none."""
    m = re.search(r"_rev([a-z]{2})(?:dev|[_/.-]|$)", urllib.parse.unquote(target or ""), re.I)
    return m.group(1).upper() if m else ""


def parse_links(body, base, context=False):
    """[(label, absolute url)] of every link in the page's main content; with context, a third item:
    the nearest heading before the link (what a generic label like 'Course Outline' belongs to)."""
    d = html(body)
    scope = (d.xpath("//main") or d.xpath("//*[@id='content']") or [d])[0]
    out, heading = [], ""
    for el in scope.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in ("h1", "h2", "h3", "h4", "h5") and el.getparent() is not None and el.xpath("not(ancestor::a)"):
            heading = re.sub(r"^Toggle\s+", "", clean(el.text_content())) or heading  # accordion button text
        if el.tag != "a" or not el.get("href"):
            continue
        href = el.get("href").strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        label = clean(el.text_content()) or clean(el.get("title") or el.get("aria-label") or "")
        url = urllib.parse.urljoin(base, href)
        out.append((label, url, heading) if context else (label, url))
    return out


def cell_text(c):
    return clean(c.text_content())


def table_rows(t):
    """(header, rows, links) of one <table>. The site writes headers as <thead><th> without a <tr>, so the
    header is every th in thead, else a first row made only of th cells."""
    head = [cell_text(c) for c in t.xpath("./thead//th|./thead//td")]
    trs = [tr for tr in t.xpath(".//tr") if not tr.xpath("ancestor::thead")]
    if not head and trs and trs[0].xpath("./th") and not trs[0].xpath("./td"):
        head, trs = [cell_text(c) for c in trs[0].xpath("./th")], trs[1:]
    rows, links = [], []
    for tr in trs:
        cells = tr.xpath("./th|./td")
        rows.append([cell_text(c) for c in cells])
        links.append(" ".join(a.get("href") for c in cells for a in c.xpath(".//a[@href]")))
    return head, rows, links


MD_TABLE_ROWS = 300


def snapshot_page(body, url):
    """A table-bearing page as (title, markdown, tables): headings, text, tab labels and tables in page order.
    tables: [{"caption", "header", "rows", "links"}]."""
    d = html(body)
    for e in d.xpath("//*[contains(translate(@style,' ',''),'display:none')]"):  # hidden sort keys in date cells
        e.drop_tree()
    title = clean(d.findtext(".//title") or "").split(" | ")[0]
    scope = (d.xpath("//main") or d.xpath("//*[@id='content']") or [d])[0]
    for e in scope.xpath(".//script|.//style|.//nav|.//header|.//footer|.//form|.//button[not(starts-with(@id,'button-tab'))]"):
        e.drop_tree()
    tab_names = {b.get("id"): cell_text(b) for b in d.xpath("//*[starts-with(@id,'button-tab')]")}
    md, tables, heading, seen_text = ["# %s" % title, "", "Source: <%s>" % url, ""], [], "", set()

    def walk(el):
        nonlocal heading
        for ch in el:
            if not isinstance(ch.tag, str):
                continue
            tab = tab_names.get(ch.get("aria-labelledby") or "")
            if tab:
                md.extend(["### %s" % tab, ""])
                heading = tab
            if ch.tag in ("h1", "h2", "h3", "h4", "h5"):
                t = re.sub(r"^Toggle\s+", "", cell_text(ch))
                if t and t != title:
                    md.extend(["%s %s" % ("#" * min(int(ch.tag[1]) + 1, 4), t), ""])
                    heading = t
            elif ch.tag == "table":
                head, rows, links = table_rows(ch)
                tables.append({"caption": heading, "header": head, "rows": rows, "links": links})
                width = max([len(head)] + [len(r) for r in rows] or [1])
                hdr = head + [""] * (width - len(head)) if head else ["col %d" % (i + 1) for i in range(width)]
                esc = lambda x: x.replace("|", "\\|")
                md.append("| " + " | ".join(esc(h) for h in hdr) + " |")
                md.append("|" + "---|" * width)
                for r in rows[:MD_TABLE_ROWS]:
                    md.append("| " + " | ".join(esc(c) for c in r + [""] * (width - len(r))) + " |")
                if len(rows) > MD_TABLE_ROWS:
                    md.append("")
                    md.append("*%d more rows in the CSV.*" % (len(rows) - MD_TABLE_ROWS))
                md.append("")
            elif ch.tag in ("p", "li") and not ch.xpath(".//table"):
                t = cell_text(ch)
                if t and t not in seen_text:
                    seen_text.add(t)
                    md.extend(["%s%s" % ("- " if ch.tag == "li" else "", t), ""])
            else:
                walk(ch)
    walk(scope)
    return title, "\n".join(md).rstrip() + "\n", tables


def slug(text, n=80):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].rstrip("-") or "untitled"


# ---------- catalog files ----------

def write_csv(name, header, rows):
    path = os.path.join(CATALOG, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for r in sorted(rows, key=lambda r: [str(x) for x in r]):
            w.writerow(r)
    print("  wrote %s (%d rows)" % (os.path.relpath(path, ROOT), len(rows)))


def write_ledger(name, header, rows, key):
    """Like write_csv, but a ledger: every row ever seen stays, with first_seen and last_seen. A row the
    site no longer lists keeps its last values, and its last_seen stops moving. Returns how many rows stopped."""
    today = dt.date.today().isoformat()
    idx = [header.index(k) for k in key]
    old = {tuple(r[k] for k in key): r for r in read_csv(name)}
    out, seen = [], set()
    for r in rows:
        k = tuple(str(r[i]) for i in idx)
        if k in seen:
            continue
        seen.add(k)
        out.append(list(r) + [(old.get(k) or {}).get("first_seen") or today, today])
    for k, r in old.items():
        if k not in seen:
            out.append([r.get(h, "") for h in header] + [r.get("first_seen", ""), r.get("last_seen", "")])
    write_csv(name, header + ["first_seen", "last_seen"], out)
    return sum(1 for k in old if k not in seen)


def read_csv(name, ref=None):
    """A catalog CSV as dicts; with ref, as committed at that git ref (an earlier scan)."""
    if ref:
        text = git_show(ref, "results/catalog/" + name)
        return list(csv.DictReader(text.splitlines())) if text else []
    path = os.path.join(CATALOG, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def current(rows):
    """The rows of a ledger that its last scan still listed."""
    last = max((r.get("last_seen", "") for r in rows), default="")
    return [r for r in rows if r.get("last_seen", "") == last]


def git(*args):
    import subprocess
    try:
        out = subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, timeout=60)
        return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None
    except Exception:
        return None


def git_show(ref, path):
    return git("show", "%s:%s" % (ref, path))


# ---------- scan: the docs site ----------

SHRINK_FLOOR = 0.9   # a scan that finds fewer than this share of the previous scan's pages stops
FAIL_CEILING = 0.02  # ... and so does one where more than this share of pages failed to load
ALLOW_SHRINK = False  # --allow-shrink: the site really did get smaller


def guard(what, now, before, failed=0):
    """Stop the scan, before anything is written, when it looks partial: far fewer rows than the previous
    scan, or many failed pages. A partial scan written to the ledgers reads as the site dropping pages."""
    problems = []
    if before and now < before * SHRINK_FLOOR:
        problems.append("%d, previous scan %d" % (now, before))
    if now and failed > now * FAIL_CEILING:
        problems.append("%d of %d failed" % (failed, now))
    if problems and not ALLOW_SHRINK:
        raise SystemExit("%s: %s; scan stopped before writing that part of the catalog. Retry later, or pass "
                         "--allow-shrink if the site really shrank." % (what, "; ".join(problems)))
    for p in problems:
        print("  %s: %s (allowed by --allow-shrink)" % (what, p))


def scan_docs(summary, prev):
    with Step("Docs-site pages") as st:
        s, _, _, body = http(DOCS_INDEX)
        index = parse_docs_index(body)
        if not index:
            raise SystemExit("docs index: status %s, no entries; scan stopped, catalog unchanged" % s)
        pages = {u for _, _, u in index}
        for sm in ("s_product-sitemap.xml", "s_product_version-sitemap.xml", "s_product_version-sitemap2.xml"):
            pages |= {norm(u) for u in sitemap(DOCS + sm) if norm(u) != DOCS_INDEX}
        parsed = fetch_all(sorted(pages), lambda u, s, fu, h, b: (s, parse_docs_page(b, u) if s == 200 and b else None), label="docs pages")
        # release menus may name pages the sitemaps miss
        extra = {v for s, p in parsed.values() if p for _, v in p["options"]} - set(parsed)
        if extra:
            parsed.update(fetch_all(sorted(extra), lambda u, s, fu, h, b: (s, parse_docs_page(b, u) if s == 200 and b else None), label="docs pages"))
        st.pages = len(parsed)

    entry_of = {}  # page url -> (heading, entry, the page the index links to)
    for heading, label, u in index:
        entry_of[u] = (heading, label, u)
    for heading, label, u in index:
        p = (parsed.get(u) or (0, None))[1]
        for _, v in (p["options"] if p else []):
            entry_of.setdefault(v, (heading, label, u))
    page_rows, listing_rows = [], []
    latest = {}
    for heading, label, u in index:
        p = (parsed.get(u) or (0, None))[1]
        latest[u] = latest_line(p["options"] if p else [])
    for u, (status, p) in sorted(parsed.items()):
        heading, label, root = entry_of.get(u, ("", "", ""))
        role = "entry" if u == root else ("release" if root else "orphan")
        release = p["selected"] if p else ""
        # latest: the most recently published release line, not Extreme's recommendation (see the
        # recommendation snapshots). The entry's page counts when it is a "Latest Documentation" page or the
        # entry has no numbered releases
        line = latest.get(root, set())
        is_latest = "yes" if root and (u in line or (u == root and (not line or "latest" in release.lower()))) else ""
        page_rows.append([heading, label, role, is_latest, release, p["title"] if p else "", status,
                          len(p["items"]) if p else 0, len(p["options"]) if p else 0, u])
        for it in (p["items"] if p else []):
            f, v = format_via(it["url"], it["title"])
            listing_rows.append([heading, label, release, is_latest, it["title"], it["date"], month_iso(it["date"]),
                                 f, v, urllib.parse.urlsplit(it["url"]).netloc, it["url"], u])
    before = prev.get("docs-site") or {}
    guard("docs-site pages", len(page_rows), before.get("pages"), sum(1 for r in page_rows if r[6] != 200))
    guard("docs-site listings", len(listing_rows), before.get("listings"))
    write_ledger("docs-site/pages.csv", ["heading", "entry", "role", "latest", "release", "page_title", "status", "items", "releases", "page"], page_rows, ["page"])
    health_rows = []
    for u, (status, p) in sorted(parsed.items()):
        if status != 200 or not p:
            health_rows.append(["fetch-failed", status, "", u])
            continue
        h = p["health"]
        for m in h["missed"]:
            health_rows.append(["missed-link", "", m, u])
        if h["selects"] > 1:
            health_rows.append(["extra-dropdown", h["selects"], "", u])
        for s_ in h["signals"]:
            health_rows.append(["structure", s_, "", u])
        if not p["items"] and not p["options"]:
            health_rows.append(["empty-page", "", "", u])
    for r in page_rows:
        if r[2] == "orphan":
            health_rows.append(["orphan-page", "in the sitemap, reachable from neither the index nor a release menu", "", r[9]])
    write_csv("docs-site/health.csv", ["check", "detail", "url", "page"], health_rows)
    gone = write_ledger("docs-site/listings.csv", ["heading", "entry", "release", "latest", "title", "date", "month", "format", "via", "host", "url", "page"],
                        listing_rows, ["url", "page"])
    summary["docs-site"] = {"available": len(pages | extra), "entries": len(index), "pages": len(page_rows),
                            "pages_failed": sum(1 for r in page_rows if r[6] != 200), "listings": len(listing_rows),
                            "documents": len({r[10] for r in listing_rows}), "latest_documents": len({r[10] for r in listing_rows if r[3] == "yes"}),
                            "no_longer_listed": gone,
                            "health": {k: sum(1 for r in health_rows if r[0] == k) for k in sorted({r[0] for r in health_rows})}}

    with Step("HTML guide redirects") as st:
        guides = sorted({r[10] for r in listing_rows if r[3] == "yes" and r[8] == "html_guide"})
        got = fetch_all(guides, lambda u, s, fu, h, b: portal_target(b, fu) if s == 200 and b else "", label="guides")
        st.pages = len(guides)
    summary["docs-site"]["html_guides_checked"] = len(guides)
    REDIRECTS.update({u: t for u, t in got.items() if t})

    with Step("Release-notes archive") as st:
        s, _, _, body = http(DOCS + "support/release-notes/")
        rn_pages = sorted({v for t, v in ((clean(o.text_content()), o.get("value") or "") for o in html(body).xpath("//select//option"))
                           if "/release-notes/product/" in v}) if s == 200 else []
        got = fetch_all(rn_pages, lambda u, s, fu, h, b: (s, parse_links(b, u) if s == 200 and b else []), label="release-note pages")
        st.pages = len(got)
    rn_rows = []
    for u, (s, links) in got.items():
        for label, href in links:
            if urllib.parse.urlsplit(href).netloc.endswith("extremenetworks.com") and "supportdocs" in href:
                continue
            f, v = format_via(href, label)
            rn_rows.append([u.rstrip("/").split("/")[-1], label, f, v, href])
    before = prev.get("docs-site", {}).get("release_notes") or {}
    guard("release-note pages", len(got), before.get("archive_pages"), sum(1 for s, _ in got.values() if s != 200))
    guard("release notes", len(rn_rows), before.get("entries"))
    write_ledger("docs-site/release-notes.csv", ["archive_page", "title", "format", "via", "url"], rn_rows, ["archive_page", "url"])
    summary["docs-site"]["release_notes"] = {"archive_pages": len(rn_pages), "entries": len(rn_rows)}

    with Step("Docs-site snapshots") as st:
        cm = sorted({norm(u) for u in sitemap(DOCS + "s_comp_matrix-sitemap.xml")} - {norm(DOCS + "s_comp_matrices/")})
        write_snapshot_index("docs-site", scan_snapshots(cm, "docs-site", summary))
        st.pages = len(cm)


REDIRECTS = {}  # HTML guide url -> where its root redirects, from this scan


SNAPSHOT_COLS = ["site", "name", "title", "status", "tables", "rows", "url"]


def write_snapshot_index(site, rows):
    """snapshots.csv lists both sites; replace only this scan's site."""
    keep = [[r[k] for k in SNAPSHOT_COLS] for r in read_csv("snapshots.csv") if r["site"] != site]
    write_csv("snapshots.csv", SNAPSHOT_COLS, keep + rows)


def scan_snapshots(urls, site, summary):
    """Save table-bearing pages whole: <site>/snapshots/<name>.md (text, headings, tables) and one CSV per
    table in <site>/snapshots/<name>/, with a header row and a links column."""
    got = fetch_all(urls, lambda u, s, fu, h, b: (s, snapshot_page(b, u) if s == 200 and b else None), label="snapshots")
    rows = []
    for u, (s, snap) in sorted(got.items()):
        name = slug(u.rstrip("/").split("/")[-1])
        if not snap:
            rows.append([site, "", "", s, 0, 0, u])
            continue
        title, md, tables = snap
        base = os.path.join(CATALOG, site, "snapshots", name)
        os.makedirs(os.path.dirname(base), exist_ok=True)
        with open(base + ".md", "w", encoding="utf-8", newline="\n") as f:
            f.write("<!-- Generated by scanner/scan.py scan. Do not edit. -->\n" + md)
        if os.path.isdir(base):
            shutil.rmtree(base)
        for i, t in enumerate(tables, 1):
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "%02d-%s.csv" % (i, slug(t["caption"] or "table", 50))), "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow((t["header"] or ["col %d" % (j + 1) for j in range(max((len(r) for r in t["rows"]), default=0))]) + ["links"])
                for r, l in zip(t["rows"], t["links"]):
                    w.writerow(r + [l])
        rows.append([site, site + "/" + name, title, s, len(tables), sum(len(t["rows"]) for t in tables), u])
    summary.setdefault(site, {})["snapshots"] = {"pages": len(rows), "tables": sum(r[4] for r in rows)}
    return rows


# ---------- scan: the main site ----------

def main_targets():
    locs = sitemap(WWW + "/sitemap.xml")
    keep = []
    for u in locs:
        path = urllib.parse.urlsplit(u).path.strip("/").split("/")
        if not path or not path[0]:
            continue
        if path[0] in WWW_AREAS or (path[0] == "resources" and len(path) > 2 and path[1] in WWW_RESOURCE_TYPES):
            keep.append(u.rstrip("/"))
    return sorted(set(keep)), len(locs)


def area_of(url):
    p = urllib.parse.urlsplit(url).path.strip("/").split("/")
    return p[0] + ("/" + p[1] if p[0] in ("resources", "support") and len(p) > 1 else "")


def is_snapshot_page(url, prefixes=None):
    """A main-site page config/sites.yaml names under snapshot_pages: that page, or with '/*' every page under it."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    for p in (WWW_SNAPSHOT_PAGES if prefixes is None else prefixes):
        if path == p or (p.endswith("/*") and path.startswith(p[:-1])):
            return True
    return False


def scan_main(summary, prev):
    with Step("Main-site pages") as st:
        targets, total = main_targets()
        print("  %d of %d sitemap pages in scope" % (len(targets), total))
        got = fetch_all(targets, lambda u, s, fu, h, b: (s, clean(html(b).findtext(".//title") or "") if b else "",
                                                         parse_links(b, u, context=True) if s == 200 and b else []), label="main-site pages")
        st.pages = len(got)
    before = prev.get("main-site") or {}
    guard("main-site pages", len(got), before.get("pages_in_scope"), sum(1 for s, _, _ in got.values() if s != 200))
    # site chrome: links on a large share of all pages, or of the pages in one area, are navigation
    overall, by_area, area_size = {}, {}, {}
    for u, (s, title, links) in got.items():
        a = area_of(u)
        area_size[a] = area_size.get(a, 0) + 1
        for href in {h for _, h, _ in links}:
            overall[href] = overall.get(href, 0) + 1
            by_area[(a, href)] = by_area.get((a, href), 0) + 1
    chrome = {h for h, c in overall.items() if c >= max(8, len(got) * 0.2)}
    chrome_in = {k for k, c in by_area.items() if area_size[k[0]] >= 5 and c >= area_size[k[0]] * 0.6 and kind(k[1]) != "hub"}
    page_rows, link_rows, ds_rows = [], [], []
    for u, (s, title, links) in sorted(got.items()):
        a = area_of(u)
        uniq = {}
        for l, h, ctx in links:
            if h not in chrome and (a, h) not in chrome_in and norm(h) != norm(u):
                uniq.setdefault(h, (l, ctx))
        hub = [(l, h) for h, (l, _) in uniq.items() if kind(h) == "hub"]
        page_rows.append([a, title.replace(" | Extreme Networks", ""), s, len(uniq), len(hub), u])
        for h, (l, ctx) in uniq.items():
            f, v = format_via(h, l)
            link_rows.append([a, u, ctx, l, f, v, urllib.parse.urlsplit(h).netloc, h])
        if a in ("products", "platform-one"):
            for l, h in hub:
                if re.search(r"data ?sheet", l, re.I) or l.lower().startswith(("view", "download")):
                    ds_rows.append([title.split(" | ")[0], l, u, h])
    guard("main-site listings", len(link_rows), before.get("listings"))
    health_rows = [["fetch-failed", s, "", u] for u, (s, _, _) in sorted(got.items()) if s != 200]
    had = {r["page"] for r in current(read_csv("main-site/datasheets.csv"))}
    has = {r[2] for r in ds_rows}
    health_rows += [["datasheet-missing", "had a datasheet link at the last scan", "", u]
                    for u in sorted(had - has) if u in got and got[u][0] == 200]
    write_csv("main-site/health.csv", ["check", "detail", "url", "page"], health_rows)
    write_ledger("main-site/pages.csv", ["area", "title", "status", "links", "hub_files", "url"], page_rows, ["url"])
    write_ledger("main-site/listings.csv", ["area", "page", "heading", "label", "format", "via", "host", "url"], link_rows, ["page", "url"])
    write_ledger("main-site/datasheets.csv", ["product", "label", "page", "url"], ds_rows, ["page", "url"])
    summary["main-site"] = {"available": total, "pages_in_scope": len(page_rows), "pages_failed": sum(1 for r in page_rows if r[2] != 200),
                            "listings": len(link_rows), "hub_files": len({r[7] for r in link_rows if r[5] == "content_hub"}),
                            "datasheet_links": len(ds_rows),
                            "health": {k: sum(1 for r in health_rows if r[0] == k) for k in sorted({r[0] for r in health_rows})}}
    with Step("Main-site snapshots") as st:
        cm = [u for u in targets if is_snapshot_page(u)]
        write_snapshot_index("main-site", scan_snapshots(cm, "main-site", summary))
        st.pages = len(cm)


def write_documents():
    """documents.csv: every unique file either site lists, with an HTML guide's redirect (a new redirect
    means the guide was re-published)."""
    old = {r["url"]: r for r in read_csv("documents.csv")}
    rows = {}
    for r in current(read_csv("docs-site/listings.csv")):
        rows.setdefault(r["url"], [r["url"], r["title"], r["format"], r["via"], r["host"]])
    for r in current(read_csv("main-site/listings.csv")):
        if r["via"] == "content_hub" or r["format"] in ("pdf", "zip", "xlsx"):
            rows.setdefault(r["url"], [r["url"], r["label"], r["format"], r["via"], r["host"]])
    out = []
    for u, r in rows.items():
        target = REDIRECTS.get(u, (old.get(u) or {}).get("redirect", ""))
        out.append(r + [target, portal_revision(target)])
    write_ledger("documents.csv", ["url", "title", "format", "via", "host", "redirect", "revision"], out, ["url"])


# ---------- last-scan.yaml: dates, counts, timing ----------

def load_meta():
    return load_yaml(os.path.join(CATALOG, "last-scan.yaml"), {})


def merge_meta(old, summary, ran, today, timing):
    """last-scan.yaml after scanning the sites in `ran`: the other site's counts and dates are kept."""
    counts = dict(old.get("counts") or {})
    counts.update({k: v for k, v in summary.items() if k in SITES})
    scanned = {k: v for k, v in (old.get("scanned") or {}).items() if k in SITES}
    scanned.update({s: today for s in ran})
    return {"scanner_version": SCANNER_VERSION, "scanned": scanned, "counts": counts, "timing": timing}


def scan_due(today=None, meta=None, after=None):
    """(due, message): whether the oldest site's last scan is at least rescan_after_days old."""
    after = after if after is not None else rescan_after_days()
    today = today or dt.date.today()
    scanned = ((meta if meta is not None else load_meta()).get("scanned") or {})
    dates = [scanned.get(s) for s in SITES]
    if not all(dates):
        return True, "scan due: %s never scanned" % ", ".join(s for s, d in zip(SITES, dates) if not d)
    last = min(dt.date.fromisoformat(str(d)) for d in dates)
    nxt = last + dt.timedelta(days=after)
    if today >= nxt:
        return True, "scan due: last scan %s, rescan_after_days %d" % (last, after)
    return False, "scan not due: last scan %s, next on or after %s (rescan_after_days %d)" % (last, nxt, after)


def cmd_scan(only=None):
    import yaml
    ran = (only,) if only else SITES
    if only and only not in SITES:
        raise SystemExit("scan --only what? docs-site or main-site")
    today = dt.date.today().isoformat()
    old = load_meta()
    prev = old.get("counts") or {}
    base = "HEAD" if git("rev-parse", "--verify", "--quiet", "HEAD") else None  # the previous scan, as committed
    before, snaps_before = snapshot(base), snapshot_hashes(base)
    summary, t0 = {}, time.time()
    if "docs-site" in ran:
        scan_docs(summary, prev)
    if "main-site" in ran:
        scan_main(summary, prev)
    write_documents()
    timing = {"total_seconds": round(time.time() - t0, 1), "steps": TIMING, "from_saved_pages": STATS["saved"]}
    meta = merge_meta(old, summary, ran, today, timing)
    with open(os.path.join(CATALOG, "last-scan.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Generated by scanner/scan.py scan. When each site was last scanned, what it held, how long it took.\n")
        yaml.safe_dump(meta, f, sort_keys=True, allow_unicode=True)
    doc = write_changes(today, base, before, snaps_before, prev, meta, old)
    refresh()
    if not only and os.path.isdir(CACHE):
        shutil.rmtree(CACHE)  # the next scan reads the live sites
    print("to check:" if doc["to_check"] else "to check: nothing flagged")
    for r in doc["to_check"]:
        print("  - " + r)
    return 0


# ---------- what changed ----------

SNAPSHOT_KEYS = {"docs-site/listings.csv": (("url", "page"), ("entry", "release", "latest", "title", "date")),
                 "docs-site/release-notes.csv": (("archive_page", "url"), ("archive_page", "title")),
                 "main-site/listings.csv": (("page", "url"), ("page", "heading", "label", "format")),
                 "main-site/datasheets.csv": (("page", "url"), ("product", "label")),
                 "documents.csv": (("url",), ("redirect", "revision"))}
CHANGE_LABELS = {"docs-site/listings.csv": "Docs-site listings", "docs-site/release-notes.csv": "Release notes",
                 "main-site/listings.csv": "Main-site listings", "main-site/datasheets.csv": "Datasheets",
                 "documents.csv": "HTML guide redirects"}


def snapshot(ref=None):
    """Catalog rows by key, for comparing a scan with the one before it; with ref, as committed there."""
    return {name: {tuple(r.get(k, "") for k in key): r for r in read_csv(name, ref)} for name, (key, _) in SNAPSHOT_KEYS.items()}


def page_hash(text):
    """A snapshot's content hash, without its generated header line (which names the tool, not the page)."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[0].startswith("<!-- Generated by"):
        lines = lines[1:]
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def snapshot_hashes(ref=None):
    """{<site>/<name>: hash} for the snapshots, in the working tree or as committed at ref."""
    out = {}
    for site in SITES:
        if ref:
            for path in (git("ls-tree", "-r", "--name-only", ref, "results/catalog/%s/snapshots" % site) or "").splitlines():
                if path.endswith(".md"):
                    out[site + "/" + os.path.basename(path)[:-3]] = page_hash(git_show(ref, path) or "")
            continue
        base = os.path.join(CATALOG, site, "snapshots")
        for n in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            if n.endswith(".md"):
                with open(os.path.join(base, n), encoding="utf-8") as f:
                    out[site + "/" + n[:-3]] = page_hash(f.read())
    return out


def snapshot_changes(before, after):
    return {"new": sorted(set(after) - set(before)), "gone": sorted(set(before) - set(after)),
            "changed": sorted(p for p in set(before) & set(after) if before[p] != after[p])}


def catalog_changes(before, after):
    """new: not in the earlier catalog. no_longer_listed: listed by the earlier scan, not by this one.
    changed: listed by both, with a different value in a shown field."""
    out = {}
    for name, (_, fields) in SNAPSHOT_KEYS.items():
        before.setdefault(name, {})
        after.setdefault(name, {})
        prev_scan = max((r.get("last_seen", "") for r in before[name].values()), default="")
        this_scan = max((r.get("last_seen", "") for r in after[name].values()), default="")
        show = lambda r: dict({f: r.get(f, "") for f in fields}, url=r.get("url", ""))
        new, gone, changed = [], [], []
        for k, r in after[name].items():
            if k not in before[name]:
                new.append(show(r))
                continue
            diff = {f: [before[name][k].get(f, ""), r.get(f, "")] for f in fields if before[name][k].get(f, "") != r.get(f, "")}
            if diff:
                changed.append(dict(show(r), changed=diff))
        if this_scan > prev_scan:
            for k, r in before[name].items():
                if r.get("last_seen") == prev_scan and after[name].get(k, {}).get("last_seen") != this_scan:
                    gone.append(show(r))
        out[name] = {"new": sorted(new, key=str), "no_longer_listed": sorted(gone, key=str), "changed": sorted(changed, key=str)}
    return out


def health_delta(prev, now):
    out = []
    for site in SITES:
        a, b = (prev.get(site) or {}).get("health") or {}, (now.get(site) or {}).get("health") or {}
        for k in sorted(set(a) | set(b)):
            if a.get(k, 0) != b.get(k, 0):
                out.append("%s %s %d -> %d" % (site, k, a.get(k, 0), b.get(k, 0)))
    return out


REC_SNAPSHOT = re.compile(r"recommend|sw-release|compatibility-matrices")
NOT_RELEASES = re.compile(r"^(latest|previous) documentation$|^document collection$|^tcg/tsg$|^sd-wan appliances$", re.I)


def to_check(cat, snaps, health, meta, old_meta):
    """What a person should look at after this scan, in plain words (the report's To Check)."""
    out = []
    if (old_meta.get("scanner_version") or SCANNER_VERSION) != SCANNER_VERSION:
        out.append("The scanner changed since the last scan (version %s to %s): some changes below may come from the scanner, not the sites."
                   % (old_meta.get("scanner_version"), SCANNER_VERSION))
    rec = [p for p in snaps["changed"] + snaps["new"] + snaps["gone"] if REC_SNAPSHOT.search(p)]
    if rec:
        out.append("Release-recommendation snapshots changed (%s): check the notes against them." % ", ".join(rec))
    other = [p for p in snaps["changed"] + snaps["new"] + snaps["gone"] if p not in rec]
    if other:
        out.append("%d other snapshots changed: git diff results/catalog/*/snapshots" % len(other))
    new_latest = [r for r in cat.get("docs-site/listings.csv", {}).get("new", []) if r.get("latest") == "yes"]
    if new_latest:
        ents = sorted({r["entry"] for r in new_latest})
        out.append("%d new documents in latest release lines (%s): libraries run `update`."
                   % (len(new_latest), ", ".join(ents[:12]) + (", ..." if len(ents) > 12 else "")))
    gone = sum(len(v.get("no_longer_listed", [])) for v in cat.values())
    if gone:
        out.append("%d catalog rows no longer listed: spot-check a few before trusting it." % gone)
    redirects = cat.get("documents.csv", {}).get("changed", [])
    if redirects:
        out.append("%d HTML guides now redirect somewhere new (re-published): libraries run `update`." % len(redirects))
    ds = cat.get("main-site/datasheets.csv", {})
    if ds.get("new") or ds.get("no_longer_listed"):
        out.append("Datasheet links changed (%d new, %d gone): libraries run `update`." % (len(ds.get("new", [])), len(ds.get("no_longer_listed", []))))
    for h in health:
        out.append("Scan health changed: %s (results/catalog/*/health.csv)." % h)
    # entries: new ones, ones in no starter profile, names profiles use that the catalog lacks
    pages = current(read_csv("docs-site/pages.csv"))
    entries = {r["entry"] for r in pages if r["role"] == "entry"}
    named = {e for p in starter_profiles().values() for f in p.get("filters") or [] for e in f.get("entries") or []}
    today = max((r["last_seen"] for r in pages), default="")
    new_entries = sorted(r["entry"] for r in pages if r["role"] == "entry" and r["first_seen"] == today and today != min((x["first_seen"] for x in pages), default=today))
    if new_entries:
        out.append("New docs-site entries: %s. %s" % (", ".join(new_entries), "Not in any starter profile yet: " + ", ".join(e for e in new_entries if e not in named) if any(e not in named for e in new_entries) else "Each is in a starter profile."))
    missing = sorted(named - entries - {r["n"] for r in main_rows()})
    if missing:
        out.append("Starter profiles name entries the catalog doesn't have: %s." % ", ".join(missing))
    # release menus whose labels the latest rule can't read
    unread = sorted({r["entry"] for r in pages if r["role"] in ("entry", "release") and r["release"] and not re.match(r"^v?\d+\.\d+", r["release"])
                     and not NOT_RELEASES.search(r["release"])})
    if unread:
        out.append("%d entries have release labels the latest rule can't read (e.g. %s): check their latest marks." % (len(unread), ", ".join(unread[:4])))
    # a step that got much slower than last time: the site may be throttling
    prev_rate = {s["step"]: s.get("rate", 0) for s in ((old_meta.get("timing") or {}).get("steps") or [])}
    for s in (meta.get("timing") or {}).get("steps") or []:
        if prev_rate.get(s["step"]) and s["rate"] and s["rate"] < prev_rate[s["step"]] * 0.5:
            out.append("%s ran at %.1f/s, under half the last scan's %.1f/s: the site may be throttling." % (s["step"], s["rate"], prev_rate[s["step"]]))
    for c in compat_unmapped():
        out.append(c)
    return out


def write_changes(today, base, before, snaps_before, prev, meta, old_meta):
    """results/reports/<date>/changes.yaml: what this scan found compared with the previous one (the
    committed catalog). A second scan on the same day compares with the same commit."""
    import yaml
    cat = catalog_changes(before, snapshot())
    snaps = snapshot_changes(snaps_before, snapshot_hashes())
    summary = {CHANGE_LABELS[k]: {x: len(v[x]) for x in ("new", "no_longer_listed", "changed")} for k, v in cat.items()}
    summary["Snapshots"] = {"new": len(snaps["new"]), "no_longer_listed": len(snaps["gone"]), "changed": len(snaps["changed"])}
    health = health_delta(prev, meta["counts"])
    doc = {"date": today, "scanner_version": SCANNER_VERSION, "compared_with": (git("rev-parse", base) or "").strip() or None if base else None,
           "first_scan": not any(before[k] for k in before), "summary": summary,
           "to_check": to_check(cat, snaps, health, meta, old_meta), "health": health, "snapshots": snaps, "catalog": cat}
    d = os.path.join(REPORTS, today)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "changes.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Generated by scanner/scan.py scan: what this scan found, row by row; report.md is the readable version.\n")
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, width=150)
    return doc


# ---------- the report: report.md (the scanner's) and notes.md (a person's) ----------

def fmt(n):
    return "{:,}".format(n)


def latest_report():
    names = sorted(n for n in os.listdir(REPORTS) if re.match(r"\d{4}-\d{2}-\d{2}$", n)) if os.path.isdir(REPORTS) else []
    if not names:
        return None
    return load_yaml(os.path.join(REPORTS, names[-1], "changes.yaml"), None)


def carry_notes(date):
    """notes.md for a new report: the latest earlier report's notes, marked as carried forward so the
    check at this scan re-confirms them (and then replaces the mark with '*Checked at this scan.*')."""
    path = os.path.join(REPORTS, date, "notes.md")
    if os.path.exists(path):
        return
    names = sorted(n for n in os.listdir(REPORTS) if n < date and os.path.exists(os.path.join(REPORTS, n, "notes.md")))
    body = "*No notes yet.*\n"
    if names:
        with open(os.path.join(REPORTS, names[-1], "notes.md"), encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r"^# [^\n]*\n+", "", text)
        text = re.sub(r"^\*(Carried forward|Checked at this scan)[^\n]*\n(?:[^\n]+\n)*\n", "", text)
        body = "*Carried forward from [%s](../%s/notes.md); not yet re-checked at this scan.* Check each fact against this scan's snapshots, fix what changed, then replace this line with \"*Checked at this scan.*\"\n\n%s" % (names[-1], names[-1], text)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Notes, Scan %s\n\n%s" % (date, body))


def render_report(doc, meta):
    c = meta.get("counts") or {}
    d, w = c.get("docs-site") or {}, c.get("main-site") or {}
    sc = meta.get("scanned") or {}
    tm = meta.get("timing") or {}
    L = ["# Scan %s" % doc["date"], "",
         "*Generated by `scanner/scan.py scan`; do not edit. Notes by hand are in [notes.md](notes.md); row-by-row detail in [changes.yaml](changes.yaml). "
         "Scanner version %s. Compared with commit `%s`%s.*" % (doc.get("scanner_version", "?"), (doc.get("compared_with") or "none")[:7],
                                                              ", the first scan in this layout" if doc.get("first_scan") else ""), "",
         "## Coverage", "", "| Site | Last scan | Available | Scanned | Read | Not read |", "|---|---|---|---|---|---|"]
    nr = lambda site: "; ".join(SITES_CFG["sites"][site].get("not_read") or [])
    L.append("| docs site | %s | %s pages | all | %s | %s |" % (sc.get("docs-site", "never"), fmt(d.get("available", d.get("pages", 0))),
                                                                   fmt(d.get("pages", 0) - d.get("pages_failed", 0)), nr("docs-site")))
    L.append("| main site | %s | %s pages | %s | %s | %s |" % (sc.get("main-site", "never"), fmt(w.get("available", w.get("sitemap_pages", 0))),
                                                                 fmt(w.get("pages_in_scope", 0)), fmt(w.get("pages_in_scope", 0) - w.get("pages_failed", 0)), nr("main-site")))
    for n, s in SITES_CFG["sites"].items():
        if n not in SITES:
            L.append("| %s | never | | | | known, not read yet |" % s["url"].split("//")[-1])
    L += ["", "## Timing", ""]
    if tm.get("steps"):
        L += ["| Step | Pages | Time | Rate | Live | Saved | Throttled | Retried | Failed |", "|---|---|---|---|---|---|---|---|---|"]
        for s in tm["steps"]:
            L.append("| %s | %s | %d min %02d s | %.1f/s | %s | %s | %s | %s | %s |" % (s["step"], fmt(s["pages"]), s["seconds"] // 60, s["seconds"] % 60, s["rate"],
                                                                                    fmt(s["live"]), fmt(s["saved"]), s["throttled"], s["retried"], s["failed"]))
        L += ["", "Total %d min. %s" % (tm.get("total_seconds", 0) // 60 + 1, "%s pages came from saved copies (a resumed scan)." % fmt(tm["from_saved_pages"]) if tm.get("from_saved_pages") else "Every page was read live.")]
    else:
        L.append("Not recorded (this scan ran before the scanner recorded timing).")
    L += ["", "## To Check", ""]
    L += ["- " + r for r in doc.get("to_check") or []] or ["Nothing flagged."]
    L += ["", "## Changes", "", "| | New | No longer listed | Changed |", "|---|---|---|---|"]
    for k, v in (doc.get("summary") or {}).items():
        L.append("| %s | %d | %d | %d |" % (k, v["new"], v["no_longer_listed"], v["changed"]))
    if doc.get("first_scan"):
        L += ["", "Everything is new at a first scan: these counts are a baseline, not changes."]
    new_latest = [r for r in (doc.get("catalog") or {}).get("docs-site/listings.csv", {}).get("new", []) if r.get("latest") == "yes"]
    if new_latest and not doc.get("first_scan"):
        L += ["", "### New Documents in Latest Release Lines", ""]
        for r in sorted(new_latest, key=lambda r: (r["entry"], r["title"]))[:25]:
            L.append("- %s: [%s](%s) (%s)" % (r["entry"], r["title"], r["url"], r.get("date") or "no date"))
        if len(new_latest) > 25:
            L.append("- ... %d more in changes.yaml" % (len(new_latest) - 25))
    L += ["", "## Numbers", "", render_numbers(catalog_numbers(), meta)]
    return "\n".join(L) + "\n"


def catalog_numbers():
    from collections import Counter
    n = {}
    pages = current(read_csv("docs-site/pages.csv"))
    ents = [r for r in pages if r["role"] == "entry"]
    n["headings"] = Counter(r["heading"] for r in ents).most_common()
    n["entries"] = len(ents)
    n["roles"] = Counter(r["role"] for r in pages)
    orph = [r for r in pages if r["role"] == "orphan" and int(r["items"] or 0) > 0]
    n["orphans_with_docs"] = (len(orph), sum(int(r["items"]) for r in orph))
    n["menus"] = [(r["entry"], int(r["releases"] or 0)) for r in sorted(ents, key=lambda r: -int(r["releases"] or 0))[:6]]
    listings = current(read_csv("docs-site/listings.csv"))
    n["listings"] = len(listings)
    by_url = {}
    for r in listings:
        by_url.setdefault(r["url"], r)
    n["documents"] = len(by_url)
    n["formats"] = Counter("%s via %s" % (r["format"], r["via"]) if r["via"] != "direct" else r["format"] for r in by_url.values()).most_common()
    latest = {}
    for r in listings:
        if r["latest"] == "yes":
            latest.setdefault(r["url"], r)
    n["latest"] = len(latest)
    n["latest_quick"] = sum(1 for r in latest.values() if QUICK_REF.search(r["title"]))
    n["latest_translations"] = sum(1 for r in latest.values() if TRANSLATION.search(r["title"]))
    types = Counter(doc_type(r["title"], r["format"]) for r in by_url.values())
    n["other"] = types.get("Other", 0)
    rn = current(read_csv("docs-site/release-notes.csv"))
    n["release_notes"] = (len({r["archive_page"] for r in rn}), len(rn))
    n["health"] = {s: Counter(r["check"] for r in read_csv(s + "/health.csv")).most_common() for s in SITES}
    main = current(read_csv("main-site/pages.csv"))
    n["main_in_scope"] = len(main)
    n["areas"] = Counter(r["area"] for r in main).most_common()
    links = current(read_csv("main-site/listings.csv"))
    n["hub_files"] = len({r["url"] for r in links if r["via"] == "content_hub"})
    n["other_hosts"] = Counter(r["host"] for r in links if r["host"] and "www.extremenetworks.com" not in r["host"] and r["via"] != "content_hub").most_common(8)
    n["datasheets"] = len({r["url"].split("?")[0] for r in current(read_csv("main-site/datasheets.csv"))})
    snaps = read_csv("snapshots.csv")
    n["snapshots"] = {s: (sum(1 for r in snaps if r["site"] == s), sum(int(r["tables"] or 0) for r in snaps if r["site"] == s)) for s in SITES}
    docs = current(read_csv("documents.csv"))
    n["guides"] = (sum(1 for r in docs if r["redirect"]), sum(1 for r in docs if r["revision"]))
    n["recommendations"] = recommendation_dates(snaps)
    return n


# the main site states the Site Engine recommendation on its landing page, the docs site on its own page
REC_ALIAS = {"compatibility-matrices": "software-release-recommendation-for-extremecloud-iq-site-engine"}
STATED = re.compile(r"(?:current as of|as of|updated on)\s+(\d{1,2} [A-Z][a-z]+ \d{4}|[A-Z][a-z]+ \d{4})", re.I)


def recommendation_dates(snaps):
    """[(page title, docs-site date, main-site date)] as each release-recommendation snapshot states it."""
    by = {}
    for r in snaps:
        if not r["name"] or not REC_SNAPSHOT.search(r["name"]):
            continue
        site, name = r["name"].split("/", 1)
        key = REC_ALIAS.get(name, name)
        path = os.path.join(CATALOG, site, "snapshots", name + ".md")
        stated = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                m = STATED.search(f.read())
            stated = m.group(1) if m else "(no date stated)"
        e = by.setdefault(key, {"title": r["title"]})
        e[site] = stated
        if site == "docs-site":  # the docs page's title names the product; the main site's landing page's does not
            e["title"] = r["title"]
    return [(v["title"], v.get("docs-site", "–"), v.get("main-site", "–")) for k, v in sorted(by.items())]


def render_numbers(n, meta):
    L = ["From the catalog's current rows.", ""]
    L.append("**Docs index.** %s entries under %d headings: %s." % (fmt(n["entries"]), len(n["headings"]), ", ".join("%s %d" % g for g in n["headings"])))
    L.append("")
    r = n["roles"]
    L.append("**Docs pages.** %s pages: %s entry pages, %s release pages, %s orphans (in the sitemap, reachable from neither the "
             "index nor a release menu); %d orphans still list %s documents. Largest release menus: %s." % (
                 fmt(sum(r.values())), fmt(r.get("entry", 0)), fmt(r.get("release", 0)), fmt(r.get("orphan", 0)),
                 n["orphans_with_docs"][0], fmt(n["orphans_with_docs"][1]), ", ".join("%s %d" % m for m in n["menus"])))
    L.append("")
    L.append("**Documents.** %s documents (unique files), in %s listings (a document on two pages is two listings). By format: %s. "
             "In a latest release line: %s documents, of which %d quick references and %d translations. %s titles no type rule "
             "recognises (Other)." % (fmt(n["documents"]), fmt(n["listings"]), ", ".join("%s %s" % (k, fmt(v)) for k, v in n["formats"]),
                                      fmt(n["latest"]), n["latest_quick"], n["latest_translations"], fmt(n["other"])))
    L.append("")
    L.append("**HTML guides.** %s latest guides redirect to a landing page, whose path changes when a guide is re-published; %d name "
             "a revision in it (`documents.csv`)." % (fmt(n["guides"][0]), n["guides"][1]))
    L.append("")
    L.append("**Release notes.** %d archive pages list %s release notes (`docs-site/release-notes.csv`)." % (n["release_notes"][0], fmt(n["release_notes"][1])))
    L.append("")
    w = (meta.get("counts") or {}).get("main-site") or {}
    res = [(s.split("/", 1)[1], k) for s, k in n["areas"] if s.startswith("resources/")]
    L.append("**Main site.** %s sitemap pages, %s in scope. Resources by type: %s. %s content-hub files; %d unique datasheets. Other hosts "
             "linked most: %s." % (fmt(w.get("available", w.get("sitemap_pages", 0))), fmt(n["main_in_scope"]), ", ".join("%s %d" % x for x in res),
                                   fmt(n["hub_files"]), n["datasheets"], ", ".join("%s %d" % h for h in n["other_hosts"])))
    L.append("")
    L.append("**Snapshots.** %d docs-site pages (%d tables), %d main-site pages (%d tables) (`snapshots.csv`)." % (n["snapshots"]["docs-site"] + n["snapshots"]["main-site"]))
    L.append("")
    L.append("**Scan health.** Docs site: %s. Main site: %s." % (", ".join("%s %d" % h for h in n["health"]["docs-site"]) or "clean",
                                                             ", ".join("%s %d" % h for h in n["health"]["main-site"]) or "clean"))
    L += ["", "**Release recommendations, the date each page states:**", "", "| Page | Docs site | Main site |", "|---|---|---|"]
    for t, d, wv in n["recommendations"]:
        L.append("| %s | %s | %s |" % (t, d, wv))
    return "\n".join(L)


def render_last_scan(meta, doc):
    sc = meta.get("scanned") or {}
    c = meta.get("counts") or {}
    d, w = c.get("docs-site") or {}, c.get("main-site") or {}
    last = min(sc.values()) if sc else None
    due = (dt.date.fromisoformat(last) + dt.timedelta(days=rescan_after_days())).isoformat() if last else "now"
    tm = meta.get("timing") or {}
    L = ["*Generated by `scanner/scan.py`; do not edit.*", "",
         "**%s** · next due from %s (every %d days, `config/sites.yaml`)" % (last or "never", due, rescan_after_days()), "",
         "- Docs site: %s of %s pages read · Main site: %s of %s in scope%s" % (
             fmt(d.get("pages", 0) - d.get("pages_failed", 0)), fmt(d.get("available", d.get("pages", 0))),
             fmt(w.get("pages_in_scope", 0) - w.get("pages_failed", 0)), fmt(w.get("pages_in_scope", 0)),
             " · %d min" % (tm["total_seconds"] // 60 + 1) if tm.get("total_seconds") else "")]
    if doc:
        s = doc.get("summary") or {}
        parts = ["%d %s" % (v["new"], k.lower()) for k, v in s.items() if v["new"]]
        L.append("- %s: %s · %d to check" % ("First recorded" if doc.get("first_scan") else "New", ", ".join(parts) or "nothing new", len(doc.get("to_check") or [])))
        L.append("- Report: [results/reports/%s/report.md](results/reports/%s/report.md)" % (doc["date"], doc["date"]))
    return "\n".join(L)


def replace_block(path, name, body):
    """Rewrite the text between <!-- BEGIN generated:name --> and <!-- END generated:name --> in a file."""
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        text = f.read()
    start, end = "<!-- BEGIN generated:%s -->" % name, "<!-- END generated:%s -->" % name
    if start not in text or end not in text:
        return False
    new = text[:text.index(start) + len(start)] + "\n" + body.rstrip() + "\n" + text[text.index(end):]
    if new != text:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    return True


def refresh():
    """Everything generated from the catalog: the latest report.md (its notes.md is kept, or carried
    forward), the README's Last Scan block, and index.html."""
    meta = load_meta()
    doc = latest_report()
    if doc:
        d = os.path.join(REPORTS, doc["date"])
        with open(os.path.join(d, "report.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(render_report(doc, meta))
        carry_notes(doc["date"])
    replace_block(os.path.join(ROOT, "README.md"), "last-scan", render_last_scan(meta, doc))
    build_explorer()
    return 0


# ---------- the catalog as documents: shared by the explorer and by libraries ----------

MAIN_TYPES = [("Policy", "/support/policies", "Policies"), ("Stencil", "/support/visio-stencils", "Visio stencils")]


def catalog_docs():
    """(rows, docs). A row is a docs-site entry or a main-site group, with its releases; a doc is one
    document in one row: title, release, latest, month, format, via, type, translation, url, and the
    entries it belongs to (a datasheet lives on /products/ but belongs to its docs entry)."""
    pairs = load_yaml(os.path.join(CONFIG, "main-site-entries.yaml"), {}) or {}
    pages = current(read_csv("docs-site/pages.csv"))
    listings = current(read_csv("docs-site/listings.csv"))
    rows, byname = [], {}
    by_entry = {}
    for r in listings:
        by_entry.setdefault(r["entry"], []).append(r)
    for p in sorted([p for p in pages if p["role"] == "entry"], key=lambda r: (r["heading"], r["entry"].lower())):
        mine = by_entry.get(p["entry"], [])
        rels = sorted({r["release"] or "" for r in mine}, key=lambda v: (vkey(v) or [-1], v), reverse=True)
        rels = [r for r in rels if r] + ([""] if "" in rels else [])
        best = {}
        for r in mine:
            c = best.get(r["url"])
            if c is None or (r["latest"] == "yes" and c["latest"] != "yes") or (vkey(r["release"]) or [-1]) > (vkey(c["release"]) or [-1]):
                best[r["url"]] = r
        row = {"n": p["entry"], "site": "docs", "path": "/support/documentation/", "h": p["heading"], "rel": rels, "d": []}
        for r in sorted(best.values(), key=lambda r: (r["month"], r["title"]), reverse=True):
            row["d"].append({"t": r["title"], "rel": r["release"] or "", "latest": r["latest"] == "yes", "m": r["month"], "format": r["format"],
                             "via": r["via"], "type": doc_type(r["title"], r["format"]), "tr": bool(TRANSLATION.search(r["title"])), "url": r["url"], "ents": None})
        rows.append(row)
        byname[p["entry"]] = row

    def mrow(name, path):
        for r in rows:
            if r["site"] == "main" and r["path"] == path and r["n"] == name:
                return r
        r = {"n": name, "site": "main", "path": path, "h": "", "rel": [""], "d": []}
        rows.append(r)
        return r

    def mdoc(title, typ, url, ents=None):
        return {"t": title, "rel": "", "latest": True, "m": "", "format": "file", "via": "content_hub", "type": typ, "tr": False, "url": url, "ents": ents}

    seen = set()
    for r in current(read_csv("main-site/datasheets.csv")):
        k = r["url"].split("?")[0]
        if k in seen:
            continue
        seen.add(k)
        ents = [r["product"]] if r["product"] in byname else [e for e in (pairs.get(r["product"]) or []) if e in byname]
        mrow(r["product"], "/products/")["d"].append(mdoc(r["product"] + " Data Sheet", "Datasheet", r["url"], ents))
    links = current(read_csv("main-site/listings.csv"))
    titles = {r["url"]: r["title"] for r in current(read_csv("main-site/pages.csv"))}
    seen = set()
    for r in links:
        if r["via"] != "content_hub" or r["url"] in seen:
            continue
        path = urllib.parse.urlsplit(r["page"]).path.rstrip("/")
        typ = next(((t, p, n) for t, p, n in MAIN_TYPES if path.endswith(p)), None)
        if typ:
            label = re.sub(r"^(Download( PDF| Matrix| Excel| Zip)?|Learn More)\s*", "", r["label"], flags=re.I) or r["heading"]
            if re.match(r"^(TAR|ZIP) Download$", r["label"]):
                label = "MIBs (%s)" % r["label"].split()[0]
            seen.add(r["url"])
            mrow(typ[2], typ[1])["d"].append(mdoc(label, typ[0], r["url"]))
        elif r["area"] == "support/training":
            seen.add(r["url"])
            track = path.split("/")[-1].replace("-", " ").title()
            mrow(track, "/support/training/")["d"].append(mdoc("%s (%s)" % (r["heading"], r["label"]) if r["heading"] else r["label"], "Course Outline", r["url"]))
        elif r["area"].startswith("resources/"):
            seen.add(r["url"])
            kind_ = r["area"].split("/")[1]
            mrow(kind_.replace("-", " ").capitalize() + "s", "/resources/" + kind_ + "/")["d"].append(mdoc(titles.get(r["page"], r["label"]).replace(" | Extreme Networks", ""), "Brief", r["url"]))
    return rows, byname


def main_rows():
    return [r for r in catalog_docs()[0] if r["site"] == "main"]


# ---------- compatibility: which hardware runs which software ----------

def compat_relations():
    """[{platform, software: [entries], kind, min, max, releases, source}] from config/compatibility.yaml
    and the snapshots it names. range: a stated minimum and last release; recommended: stated releases."""
    cfg = load_yaml(os.path.join(CONFIG, "compatibility.yaml"), {}) or {}
    out, unmapped = [], []
    for src in cfg.get("sources") or []:
        site, name = src["snapshot"].split("/", 1)
        folder = os.path.join(CATALOG, site, "snapshots", name)
        plats = {str(k).lower(): v for k, v in (src.get("platforms") or {}).items()}
        tables = sorted(f for f in os.listdir(folder) if f.endswith(".csv")) if os.path.isdir(folder) else []
        for fn in tables:
            with open(os.path.join(folder, fn), encoding="utf-8", newline="") as f:
                rows = list(csv.reader(f))
            if not rows:
                continue
            head = [h.lower() for h in rows[0]]
            if src.get("kind") == "range":
                key = re.sub(r"^\d+-", "", fn[:-4])
                key = re.sub(r"-(switches|port-extenders|series-routers)-software-support$", "", key)
                plat = plats.get(key)
                if not plat:
                    if key in [str(x).lower() for x in src.get("no_entry") or []]:
                        continue
                    unmapped.append("%s table %s" % (src["snapshot"], fn))
                    continue
                last_col = next((i for i, h in enumerate(head) if "last supported" in h), None)
                lo, hi, open_end = None, None, False
                for r in rows[1:]:
                    v = vkey(re.sub(r"^(switch engine|exos|extremexos)\s+", "", (r[1] if len(r) > 1 else "").strip(), flags=re.I))
                    if not v:
                        continue
                    lo = v if lo is None or v < lo else lo
                    if last_col is not None and len(r) > last_col:
                        last = r[last_col].strip().lower()
                        if last.startswith("latest"):
                            open_end = True
                        else:
                            lv = vkey(last.replace(".x", ".999"))
                            if lv:
                                hi = lv if hi is None or lv > hi else hi
                if lo is None or last_col is None or not (open_end or hi):
                    continue  # the table doesn't state both ends: left out rather than guessed
                out.append({"platform": plat, "software": list(src["software"]), "kind": "range", "min": ".".join(map(str, lo)),
                            "max": None if open_end else ".".join(map(str, hi)).replace(".999", ".x"), "releases": [], "source": src["snapshot"]})
            else:  # recommended: one row per platform, the software named in each cell
                prefixes = src.get("software_by_prefix") or {}
                cols = [i for i, h in enumerate(head) if "release" in h and "date" not in h]
                for r in rows[1:]:
                    plat = plats.get((r[0] if r else "").strip().lower())
                    if not plat:
                        if r and r[0].strip():
                            unmapped.append("%s platform %s" % (src["snapshot"], r[0].strip()))
                        continue
                    by_sw = {}
                    for i in cols:
                        cell = r[i].strip() if len(r) > i else ""
                        pre = next((p for p in sorted(prefixes, key=len, reverse=True) if cell.startswith(p + " ")), None)
                        if pre:
                            by_sw.setdefault(prefixes[pre], []).append(cell[len(pre) + 1:].strip())
                    for sw, rels in by_sw.items():
                        out.append({"platform": plat, "software": [sw], "kind": "recommended", "min": None, "max": None, "releases": rels, "source": src["snapshot"]})
    compat_relations.unmapped = unmapped
    return out


def compat_unmapped():
    compat_relations()
    names = compat_relations.unmapped
    return ["Compatibility: %d table rows or tables name platforms config/compatibility.yaml doesn't map (%s)." % (len(names), ", ".join(names[:4]))] if names else []


# ---------- the explorer: index.html from scanner/template.html ----------

def explorer_data():
    rows, byname = catalog_docs()
    types = TYPE_ORDER
    tix = {t: i for i, t in enumerate(types)}
    out_rows = []
    for r in rows:
        rix = {x: i for i, x in enumerate(r["rel"])}
        out_rows.append({"n": r["n"], "site": r["site"], "path": r["path"], "h": r["h"], "rel": r["rel"],
                         "d": [[d["t"], rix.get(d["rel"], 0), 1 if d["latest"] else 0, d["m"], d["format"], d["via"], tix.get(d["type"], tix.get("Other", 0)),
                                1 if d["tr"] else 0, d["url"]] + ([d["ents"]] if d["ents"] is not None else []) for d in r["d"]]})
    snaps = []
    for s in read_csv("snapshots.csv"):
        if not s["name"]:
            continue
        site, name = s["name"].split("/", 1)
        folder = os.path.join(CATALOG, site, "snapshots", name)
        csvs = sorted(f for f in os.listdir(folder) if f.endswith(".csv")) if os.path.isdir(folder) else []
        base = "results/catalog/%s/snapshots/%s" % (site, name)
        snaps.append({"n": s["name"], "site": "docs" if site == "docs-site" else "main", "title": s["title"], "url": s["url"],
                      "tables": int(s["tables"] or 0), "md": base + ".md", "csv": [base + "/" + f for f in csvs]})
    meta = load_meta()
    c = meta.get("counts") or {}
    d, w = c.get("docs-site") or {}, c.get("main-site") or {}
    profiles = {n: {"about": p.get("about", ""), "filters": p.get("filters") or [], "snapshots": p.get("snapshots") or []} for n, p in starter_profiles().items()}
    return {"scanned": min((meta.get("scanned") or {"x": "?"}).values()), "every": rescan_after_days(), "types": types,
            "typeinfo": {"types": TYPES_CFG.get("types") or [], "values": TYPES_CFG.get("values") or {}},
            "rows": out_rows, "snapshots": snaps, "profiles": profiles, "repo": repo_url(),
            "compat": {"relations": compat_relations(), "software": (load_yaml(os.path.join(CONFIG, "compatibility.yaml"), {}) or {}).get("software") or {}},
            "unmatched": [r["n"] for r in rows if r["site"] == "main" and r["path"] == "/products/" and any(x["ents"] == [] for x in r["d"])],
            "coverage": {"docs": {"available": d.get("available", d.get("pages", 0)), "read": d.get("pages", 0) - d.get("pages_failed", 0)},
                         "main": {"available": w.get("available", w.get("sitemap_pages", 0)), "scanned": w.get("pages_in_scope", 0)}},
            "not_read": {n: s.get("not_read") or [] for n, s in SITES_CFG["sites"].items() if n in SITES},
            "unread_sites": [s["url"] for n, s in SITES_CFG["sites"].items() if n not in SITES]}


def repo_url():
    """This repo's web address, from its git remote (the explorer links to its README and reports)."""
    u = (git("remote", "get-url", "origin") or "").strip()
    u = re.sub(r"^git@([^:]+):", r"https://\1/", u)
    return re.sub(r"\.git$", "", u)


def build_explorer(out=None):
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read()
    data = json.dumps(explorer_data(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = page.replace("<!-- The explorer. scanner/scan.py fills __DATA__ and writes index.html. -->",
                        "<!-- Generated by scanner/scan.py from scanner/template.html. Do not edit; edit the template. -->", 1)
    page = page.replace("window.CATALOG=__DATA__;", "window.CATALOG=" + data + ";", 1)
    out = out or EXPLORER
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    print("  wrote %s" % os.path.relpath(out, ROOT))
    return 0


# ---------- releases: presets that combine (the explorer's rules, the same here) ----------

def vkey(s):
    m = re.match(r"^v?(\d+(?:\.\d+)*)", s or "")
    return [int(x) for x in m.group(1).split(".")] if m else None


def vcmp(a, b):
    for i in range(max(len(a), len(b))):
        x, y = (a[i] if i < len(a) else 0), (b[i] if i < len(b) else 0)
        if x != y:
            return x - y
    return 0


PRESETS = ("latest", "latest-per-major", "latest-per-line", "first-per-line")


def to_rules(releases, until=None):
    R = {"all": False, "presets": [], "newest": 0, "from": "", "to": "", "specific": []}
    for x in ["latest"] if releases is None else (releases if isinstance(releases, list) else [releases]):
        if x == "all":
            R["all"] = True
        elif x in PRESETS:
            R["presets"].append(x)
        elif isinstance(x, dict):
            R["newest"] = int(x.get("newest") or R["newest"])
            R["from"] = str(x["from"]) if x.get("from") is not None else R["from"]
            R["to"] = str(x["to"]) if x.get("to") is not None else R["to"]
        elif isinstance(x, (str, int, float)):
            R["specific"].append(str(x))
    if until:
        R["all"], R["to"] = False, str(until)
    return R


def rel_set(rels, R):
    """The release labels in `rels` (one entry's) that the numbered rules include."""
    out = set(R["specific"])
    nums = [(l, vkey(l)) for l in rels if vkey(l)]

    def best(group, pick_max):
        m = {}
        for l, k in nums:
            g = group(k)
            if g not in m or (vcmp(k, m[g][1]) > 0 if pick_max else vcmp(k, m[g][1]) < 0):
                m[g] = (l, k)
        out.update(l for l, _ in m.values())
    if "latest-per-major" in R["presets"]:
        best(lambda k: k[0], True)
    if "latest-per-line" in R["presets"]:
        best(lambda k: (k[0], k[1] if len(k) > 1 else 0), True)
    if "first-per-line" in R["presets"]:
        best(lambda k: (k[0], k[1] if len(k) > 1 else 0), False)
    if R["newest"]:
        lines = sorted({(k[0], k[1] if len(k) > 1 else 0) for _, k in nums}, reverse=True)[:R["newest"]]
        out.update(l for l, k in nums if (k[0], k[1] if len(k) > 1 else 0) in lines)
    if R["from"] or R["to"]:
        f, t = vkey(R["from"]), vkey(R["to"])
        cut = lambda k, b: vcmp(k[:len(b)], b)
        out.update(l for l, k in nums if (not f or cut(k, f) >= 0) and (not t or cut(k, t) <= 0))
    return out


def rel_ok(d, rels, R):
    none = not (R["all"] or R["presets"] or R["newest"] or R["from"] or R["to"] or R["specific"])
    if none or (R["all"] and not R["from"] and not R["to"]):
        return True
    if "latest" in R["presets"] and d["latest"]:
        return True
    return d["rel"] in rel_set(rels, R)


# ---------- a library: its profile, and what that takes ----------

LIBRARY = None


def lib(*parts):
    return os.path.join(LIBRARY, *parts)


def ds(*parts):
    return lib(".docsync", *parts)


def load_profile(path=None):
    """A profile with its starter profiles (use:) folded in: filters, snapshots, skip, and the library settings."""
    p = load_yaml(path or ds("profile.yaml"), None)
    if p is None:
        raise SystemExit("%s: no profile. Save one from the explorer, then run update --profile <file>" % (path or ds("profile.yaml")))
    starters = starter_profiles()
    filters = []
    for n in p.get("use") or []:
        if n not in starters:
            raise SystemExit("profile uses %r, which config/profiles/ doesn't have" % n)
        filters += [dict(f, _profile=n) for f in starters[n].get("filters") or []]
        p["snapshots"] = list(dict.fromkeys((p.get("snapshots") or []) + (starters[n].get("snapshots") or [])))
    filters += [dict(f, _profile="own") for f in p.get("filters") or []]
    p["_filters"] = filters
    return p


def matches(f, d, row):
    if f.get("url"):
        return d["url"] in (f["url"] if isinstance(f["url"], list) else [f["url"]])
    if f.get("entries"):
        names = set(d["ents"] or []) | {row["n"]}
        if not names & set(f["entries"]):
            return False
    if f.get("types") and d["type"] not in f["types"]:
        return False
    if f.get("format") and d["format"] not in (f["format"] if isinstance(f["format"], list) else [f["format"]]):
        return False
    if not rel_ok(d, row["rel"], to_rules(f.get("releases"), f.get("until"))):
        return False
    if f.get("translations") is not True and d["tr"]:
        return False
    return True


def plan(profile):
    """The files a profile takes: [{path (no extension), url, title, entry, type, release, via, filter}]."""
    rows, _ = catalog_docs()
    skip = set(profile.get("skip") or [])
    pattern = profile.get("folders") or "{entry}"
    names = profile.get("names") or "title"
    out, seen = [], set()
    for row in rows:
        for d in row["d"]:
            if d["url"] in skip or d["url"] in seen:
                continue
            f = next((f for f in profile["_filters"] if matches(f, d, row)), None)
            if not f:
                continue
            seen.add(d["url"])
            entry = (d["ents"] or [row["n"]])[0]
            folder = f.get("into") or pattern.format(entry=entry, type=d["type"], release=d["rel"] or "no release", profile=f["_profile"])
            folder = "/".join(safe(x) for x in folder.split("/") if x)
            base = urllib.parse.unquote(urllib.parse.urlsplit(d["url"]).path.rstrip("/").split("/")[-1]) if names == "original" else d["t"]
            base = re.sub(r"\.(pdf|zip|xlsx?)$", "", safe(base), flags=re.I)
            out.append({"path": folder + "/" + base, "url": d["url"], "title": d["t"], "entry": entry, "type": d["type"], "release": d["rel"], "via": d["via"]})
    return out


def safe(name):
    """A file or folder name that works on Windows, macOS and Linux."""
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", name)).strip(" .")[:120] or "untitled"


# ---------- a library: downloading, the lock, history ----------

EXT = {"application/pdf": ".pdf", "application/zip": ".zip", "application/x-zip-compressed": ".zip",
       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx", "application/x-tar": ".tar",
       "application/vnd.ms-excel": ".xls", "text/csv": ".csv"}


def sniff(body):
    for magic, ext in ((b"%PDF", ".pdf"), (b"PK\x03\x04", ".zip"), (b"\x1f\x8b", ".tar.gz")):
        if body.startswith(magic):
            return ext
    return ""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_lock():
    return (load_yaml(ds("lock.yaml"), {}) or {}).get("files") or []


def save_lock(entries):
    import yaml
    with open(ds("lock.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Generated by scanner/scan.py update. Every downloaded file: where it came from, when, its checksum, earlier versions.\n"
                "# Verify: python scanner/scan.py check --library <this library>\n")
        yaml.safe_dump({"lock_version": 1, "files": sorted(entries, key=lambda e: e["path"])}, f, sort_keys=False, allow_unicode=True, width=200)


def resolve(url):
    """An HTML guide resolves to the PDF inside it (<guide>/downloads/*.pdf), following its redirect pages;
    other files resolve to themselves."""
    if kind(url) != "portal":
        return url
    target = url
    for _ in range(4):
        s, final, _, body = http(target)
        if s != 200 or not body:
            return url
        d = html(body)
        for href in d.xpath("//a[@href]/@href"):
            full = urllib.parse.urljoin(final, href.strip())
            if "/downloads/" in full and full.lower().endswith(".pdf"):
                return full
        refresh_ = d.xpath("//meta[translate(@http-equiv,'REFSH','refsh')='refresh']/@content")
        m = re.search(r"url=(.+)$", refresh_[0], re.I) if refresh_ else None
        if not m:
            return url
        target = urllib.parse.urljoin(final, m.group(1).strip().strip("'\""))
    return url


def unchanged_upstream(old):
    """True when the server's ETag, Last-Modified and size all match the locked copy (a HEAD, no download)."""
    if not old or not (old.get("etag") or old.get("last_modified")):
        return False
    if not os.path.exists(lib(*old["path"].split("/"))):
        return False
    s, _, h, _ = http(old.get("file_url") or old["url"], method="HEAD")
    if s != 200:
        return False
    for k, H in (("etag", "ETag"), ("last_modified", "Last-Modified")):
        if old.get(k) and h.get(H) and old[k] != h[H]:
            return False
    return not h.get("Content-Length") or int(h["Content-Length"]) == old["bytes"]


def set_aside(rel, how):
    """An earlier copy: moved to .docsync/archive/<date>/, kept beside with its date, or deleted."""
    src = lib(*rel.split("/"))
    if not os.path.exists(src):
        return None
    today = dt.date.today().isoformat()
    if how == "delete":
        os.remove(src)
        return "deleted"
    if how == "keep":
        stem, ext = os.path.splitext(src)
        shutil.move(src, "%s (%s)%s" % (stem, today, ext))
        return "kept beside"
    dst = ds("archive", today, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return "archived"


def fetch_one(item, by_path, old=None, replaced="archive"):
    """Download item {path (no extension), url, title}; returns (status, entry). With `old` (its lock entry),
    a HEAD comes first and an unchanged file is not downloaded again."""
    if unchanged_upstream(old):
        return "same", old
    url = (old or {}).get("file_url") or resolve(item["url"])
    s, final, headers, body = http(url)
    if s != 200 or not body:
        return "failed %s" % s, None
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = EXT.get(ctype) or sniff(body) or os.path.splitext(urllib.parse.urlsplit(final).path)[1].lower() or ".bin"
    rel = item["path"] + ext
    dst = lib(*rel.split("/"))
    new_sha = hashlib.sha256(body).hexdigest()
    old = by_path.get(rel)
    entry = {"path": rel, "sha256": new_sha, "bytes": len(body), "title": item["title"], "url": item["url"]}
    if url != item["url"]:
        entry["file_url"] = url
    for k, h in (("etag", "ETag"), ("last_modified", "Last-Modified")):
        if headers.get(h):
            entry[k] = headers[h]
    entry["fetched"] = dt.date.today().isoformat()
    earlier = list((old or {}).get("earlier") or [])
    status = "new"
    if old and os.path.exists(dst) and sha256(dst) == new_sha:
        entry["fetched"] = old.get("fetched", entry["fetched"])
        status = "same"
    elif old and os.path.exists(dst):
        set_aside(rel, replaced)
        earlier.append({k: old[k] for k in ("sha256", "bytes", "last_modified", "fetched") if k in old})
        status = "updated"
    if status != "same":
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(body)
    if earlier:
        entry["earlier"] = earlier
    return status, entry


def cmd_update(profile_file=None, dry_run=False):
    """Download what the library's profile takes: new files, files changed upstream; handle files that
    left the profile; copy its snapshots; write details if asked; record it all in history.md."""
    import yaml
    os.makedirs(ds(), exist_ok=True)
    if profile_file:
        shutil.copyfile(profile_file, ds("profile.yaml"))
        print("profile: %s -> .docsync/profile.yaml" % profile_file)
    p = load_profile()
    items = plan(p)
    entries = {e["path"]: e for e in load_lock()}
    by_url = {e["url"]: e for e in entries.values()}
    if dry_run:
        todo = [i for i in items if i["url"] not in by_url]
        print("profile takes %d files (%d already here, %d to download) and %d snapshots" % (len(items), len(items) - len(todo), len(todo), len(p.get("snapshots") or [])))
        for i in todo:
            print("  %-72s %s" % (i["path"][:72], i["url"]))
        return 0
    tally, notes = {}, {"new": [], "updated": [], "dropped": [], "failed": []}

    def one(item):
        prev = by_url.get(item["url"])
        if prev:  # a file keeps the path it got the first time, whatever its title does later
            item = dict(item, path=os.path.splitext(prev["path"])[0])
        return item, fetch_one(item, entries, prev, p.get("replaced") or "archive")
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for item, (status, entry) in ex.map(one, items):
            tally[status.split()[0]] = tally.get(status.split()[0], 0) + 1
            if entry:
                entries[entry["path"]] = entry
            if status == "new":
                notes["new"].append(entry["path"])
            elif status == "updated":
                notes["updated"].append(entry)
            elif status.startswith("failed"):
                notes["failed"].append("%s (%s)" % (item["path"], status))
    wanted = {i["url"] for i in items}
    for path, e in list(entries.items()):
        if e["url"] not in wanted:  # left the profile
            how = set_aside(path, p.get("dropped") or "archive")
            notes["dropped"].append("%s (%s)" % (path, how or "already gone"))
            del entries[path]
    save_lock(list(entries.values()))
    snaps = copy_snapshots(p.get("snapshots") or [])
    details = write_details(notes["updated"]) if p.get("details") in (True, "on") else {}
    write_history(notes, snaps, details)
    print("update: %s; %d snapshots" % (", ".join("%d %s" % (v, k) for k, v in sorted(tally.items())) or "nothing to do", len(snaps)))
    return 0 if not notes["failed"] else 1


def copy_snapshots(names):
    """A profile's snapshots, copied from this repo's catalog into <library>/snapshots/ (the page as text and each table)."""
    out = []
    for n in names:
        site, name = n.split("/", 1) if "/" in n else ("docs-site", n)
        src = os.path.join(CATALOG, site, "snapshots", name)
        if not os.path.exists(src + ".md"):
            continue
        dst = lib("snapshots", name)
        os.makedirs(dst, exist_ok=True)
        shutil.copyfile(src + ".md", os.path.join(dst, name + ".md"))
        for f in os.listdir(src) if os.path.isdir(src) else []:
            shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))
        out.append(n)
    return out


def write_details(updated):
    """details.yaml: facts read from each PDF (pages, revision, part number, outline). For a re-published
    file, what changed inside goes into history.md."""
    import yaml
    import docprofile
    det = load_yaml(ds("details.yaml"), {}) or {}
    changes = {}
    for e in load_lock():
        cur = det.get(e["path"])
        if cur and cur.get("sha256") == e["sha256"]:
            continue
        src = lib(*e["path"].split("/"))
        if not os.path.exists(src):
            continue
        card, text = docprofile.profile(src)
        card = dict(card, sha256=e["sha256"])
        if cur:
            changes[e["path"]] = "%s → %s pages%s" % (cur.get("pages"), card.get("pages"),
                                                    ", revision %s → %s" % (cur.get("revision"), card.get("revision")) if cur.get("revision") != card.get("revision") else "")
        det[e["path"]] = card
    with open(ds("details.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Generated by scanner/scan.py update (details: on). Facts read from each file; rebuilt from the files if lost.\n")
        yaml.safe_dump(det, f, sort_keys=True, allow_unicode=True, width=150)
    return changes


def write_history(notes, snaps, details):
    """history.md: what each update brought, newest first."""
    today = dt.date.today().isoformat()
    L = ["## %s" % today, ""]
    L += ["- New: %s" % p for p in notes["new"]]
    L += ["- Re-published: %s%s" % (e["path"], " (%s)" % details[e["path"]] if e["path"] in details else "") for e in notes["updated"]]
    L += ["- Left the profile: %s" % p for p in notes["dropped"]]
    L += ["- Failed: %s" % p for p in notes["failed"]]
    if snaps:
        L.append("- Snapshots copied: %s" % ", ".join(snaps))
    if len(L) == 2:
        L.append("- Nothing new.")
    old = ""
    if os.path.exists(ds("history.md")):
        with open(ds("history.md"), encoding="utf-8") as f:
            text = f.read()
        old = text[text.index("## "):] if "## " in text else ""
    with open(ds("history.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# History\n\nWhat each update brought, newest first. Written by scanner/scan.py update.\n\n" + "\n".join(L) + "\n\n" + old)


def cmd_check(remote):
    entries = load_lock()
    locked = {e["path"] for e in entries}
    r = {"ok": [], "missing": [], "changed": [], "new": [], "upstream": [], "unreachable": []}
    for e in entries:
        p = lib(*e["path"].split("/"))
        if not os.path.exists(p):
            r["missing"].append(e["path"])
        elif sha256(p) != e["sha256"]:
            r["changed"].append(e["path"])
        else:
            r["ok"].append(e["path"])
    for d, dirs, files in os.walk(LIBRARY):
        dirs[:] = [x for x in dirs if x not in (".docsync", "snapshots", ".git")]
        for n in files:
            rel = os.path.relpath(os.path.join(d, n), LIBRARY).replace(os.sep, "/")
            if rel not in locked and not rel.startswith("."):
                r["new"].append(rel)
    if remote:
        def head(e):
            s, _, h, _ = http(e.get("file_url") or e["url"], method="HEAD")
            if s != 200:
                return e, "unreachable", s
            same = all(not e.get(k) or not h.get(H) or e[k] == h[H] for k, H in (("etag", "ETag"), ("last_modified", "Last-Modified")))
            if h.get("Content-Length") and int(h["Content-Length"]) != e["bytes"]:
                same = False
            return e, "ok" if same else "upstream", s
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            for e, verdict, s in ex.map(head, entries):
                if verdict != "ok":
                    r[verdict].append("%s (%s)" % (e["path"], s if verdict == "unreachable" else "run update"))
    print("files: %d ok, %d missing, %d changed, %d new%s" % (len(r["ok"]), len(r["missing"]), len(r["changed"]), len(r["new"]),
          ", %d changed upstream, %d unreachable" % (len(r["upstream"]), len(r["unreachable"])) if remote else ""))
    for k in ("missing", "changed", "new", "upstream", "unreachable"):
        for p in r[k]:
            print("  %-11s %s" % (k, p))
    return 0 if not any(r[k] for k in ("missing", "changed", "new", "upstream")) else 1


# ---------- compare two scans ----------

def cmd_compare(a, b):
    import yaml
    cat = catalog_changes(snapshot(a), snapshot(b))
    snaps = snapshot_changes(snapshot_hashes(a), snapshot_hashes(b))
    summary = {CHANGE_LABELS[k]: {x: len(v[x]) for x in ("new", "no_longer_listed", "changed")} for k, v in cat.items()}
    summary["Snapshots"] = {"new": len(snaps["new"]), "no_longer_listed": len(snaps["gone"]), "changed": len(snaps["changed"])}
    print("from %s to %s" % (a, b))
    print(yaml.safe_dump(summary, sort_keys=False))
    for what in ("changed", "new", "gone"):
        for p in snaps[what]:
            print("  snapshot %-7s %s" % (what, p))
    return 0


# ---------- command line ----------

def main(argv):
    global ALLOW_SHRINK, LIBRARY
    args = argv[1:]
    opt = lambda k: args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else None
    if "--allow-shrink" in args:
        ALLOW_SHRINK = True
    if "--library" in args:
        LIBRARY = os.path.abspath(opt("--library") or ".")
    cmd = args[0] if args else ""
    if cmd == "scan":
        if "--if-due" in args:
            due, msg = scan_due()
            print(msg)
            if not due:
                return 0
        return cmd_scan(opt("--only"))
    if cmd == "due":
        due, msg = scan_due()
        print(msg)
        return 0 if due else 1
    if cmd == "refresh":
        return refresh()
    if cmd == "compare" and len(args) >= 3:
        return cmd_compare(args[1], args[2])
    if cmd in ("update", "check"):
        if not LIBRARY:
            print("%s needs --library DIR (the library's folder)" % cmd)
            return 2
        return cmd_update(opt("--profile"), "--dry-run" in args) if cmd == "update" else cmd_check("--remote" in args)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
