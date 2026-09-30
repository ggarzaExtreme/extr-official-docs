"""python -m unittest discover scanner/tests   (no network: every test uses fixtures or stubs)"""
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan as o  # noqa: E402

PAGE = b"""<html><head><title>Fabric Engine - 9.4.0 | Extreme Networks Support Documentation</title></head><body>
<select><option value="">Pick</option>
<option value="https://supportdocs.extremenetworks.com/support/documentation/fabric-engine-document-collections/">Version Document Collection</option>
<option value="https://supportdocs.extremenetworks.com/support/documentation/fabric-engine-9-4-1-0/">Version 9.4.1.0</option>
<option value="https://supportdocs.extremenetworks.com/support/documentation/fabric-engine-9-4-0/">Version 9.4.0</option></select>
<ul><li><a href="https://documentation.extremenetworks.com/Fabric%20Engine%20v9.4%20User%20Guide"><h3>Fabric Engine v9.4 User Guide</h3><span>Apr 2026</span></a></li>
<li><a href="/x.pdf"><h3>Old PDF</h3><span>Sept 2019</span></a></li>
<li><a href="/nav">Not a document</a></li></ul></body></html>"""

INDEX = b"""<html><body><section><h2>Routing &amp; Switching</h2><ul>
<li><a href="https://supportdocs.extremenetworks.com/support/documentation/fabric-engine-document-collections/">Fabric Engine</a></li>
<li><a href="https://supportdocs.extremenetworks.com/support/documentation/5520/">5520 Series</a></li></ul></section>
<section><h2>Wireless</h2><a href="https://supportdocs.extremenetworks.com/support/documentation/ap4000/">AP4000</a>
<a href="https://supportdocs.extremenetworks.com/support/documentation/5520/">5520 Series</a></section></body></html>"""


def write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(rows) + "\n")


class Parse(unittest.TestCase):
    def test_docs_page(self):
        url = "https://supportdocs.extremenetworks.com/support/documentation/fabric-engine-9-4-0/"
        p = o.parse_docs_page(PAGE, url)
        self.assertEqual(p["title"], "Fabric Engine - 9.4.0")
        self.assertEqual(p["selected"], "9.4.0")
        self.assertEqual([t for t, _ in p["options"]], ["Document Collection", "9.4.1.0", "9.4.0"])
        self.assertEqual([i["title"] for i in p["items"]], ["Fabric Engine v9.4 User Guide", "Old PDF"])
        self.assertEqual(p["items"][0]["date"], "Apr 2026")
        self.assertEqual(p["items"][1]["url"], "https://supportdocs.extremenetworks.com/x.pdf")

    def test_latest_line(self):
        opts = [("Document Collection", "a"), ("TCG/TSG", "b"), ("9.4.1.0", "c"), ("9.4.0", "d"), ("9.3.4.0", "e")]
        self.assertEqual(o.latest_line(opts), {"c", "d"})
        self.assertEqual(o.latest_line([("Latest Documentation", "x")]), set())
        self.assertEqual(o.clean("Guide​, 22.2 Engine™"), "Guide, 22.2 Engine")

    def test_index_headings_and_dedupes(self):
        rows = o.parse_docs_index(INDEX)
        self.assertEqual([(g, l) for g, l, _ in rows],
                         [("Routing & Switching", "Fabric Engine"), ("Routing & Switching", "5520 Series"), ("Wireless", "AP4000")])

    def test_month_format_via(self):
        self.assertEqual(o.month_iso("Sept 2019"), "2019-09")
        self.assertEqual(o.month_iso("Q3 2019"), "")
        self.assertEqual(o.format_via("https://documentation.extremenetworks.com/Switch%20Engine%20v33.7.1%20User%20Guide/"), ("pdf", "html_guide"))
        self.assertEqual(o.format_via("https://documentation.extremenetworks.com/Fabric%20Engine%20v9.4%20User%20Guide"), ("pdf", "html_guide"))
        self.assertEqual(o.format_via("https://documentation.extremenetworks.com/a/b.PDF"), ("pdf", "direct"))
        self.assertEqual(o.format_via("https://youtu.be/x"), ("video", "direct"))
        self.assertEqual(o.format_via("https://extr-p-001.sitecorecontenthub.cloud/api/public/content/abc?v=1"), ("file", "content_hub"))
        self.assertEqual(o.format_via("https://developer.extremecloudiq.com/"), ("api", "direct"))
        self.assertEqual(o.format_via("https://x/all.zip"), ("zip", "direct"))
        self.assertEqual(o.sniff(b"\x1f\x8b\x08"), ".tar.gz")

    def test_snapshot_page(self):
        page = (b"<html><head><title>Release Recommendations | Extreme Networks</title></head><body><main>"
                b"<h2>ExtremeCloud IQ - Site Engine</h2><p>We recommend Site Engine version 26.02.13.</p>"
                b"<button id='button-tab-1'>Fabric Engine</button><div aria-labelledby='button-tab-1'>"
                b"<table><thead><th>Platform</th><th>Maintenance Release</th><th>Date</th></thead><tbody>"
                b"<tr><td>5520</td><td>Fabric Engine <a href='/rn.pdf'>9.4.1.0</a></td>"
                b"<td><span style='display: none'>20260715</span>15-Jul-26</td></tr></tbody></table></div>"
                b"</main></body></html>")
        title, md, tables = o.snapshot_page(page, "https://x/")
        self.assertEqual(title, "Release Recommendations")
        self.assertIn("We recommend Site Engine version 26.02.13.", md)
        self.assertIn("### Fabric Engine", md)
        self.assertIn("| Platform | Maintenance Release | Date |", md)
        self.assertEqual(tables[0]["header"], ["Platform", "Maintenance Release", "Date"])
        self.assertEqual(tables[0]["rows"], [["5520", "Fabric Engine 9.4.1.0", "15-Jul-26"]])
        self.assertEqual(tables[0]["links"], ["/rn.pdf"])
        self.assertEqual(tables[0]["caption"], "Fabric Engine")

    def test_html_guide_redirect_and_revision(self):
        body = b'<html><head><meta http-equiv="Refresh" content="0;url=fe_9.4_ug_revacdev/content/purpose.shtml" /></head></html>'
        t = o.portal_target(body, "https://documentation.extremenetworks.com/Fabric%20Engine%20v9.4%20User%20Guide/")
        self.assertEqual(t, "https://documentation.extremenetworks.com/Fabric%20Engine%20v9.4%20User%20Guide/fe_9.4_ug_revacdev/content/purpose.shtml")
        self.assertEqual(o.portal_revision(t), "AC")
        self.assertEqual(o.portal_revision("https://x/se_26_reverse_proxy/a.shtml"), "")
        self.assertEqual(o.portal_target(b"<html>no redirect</html>", "https://x/"), "")

    def test_snapshot_pages_from_config(self):
        rules = ("/support/policies", "/support/compatibility-matrices", "/support/compatibility-matrices/*")
        www = "https://www.extremenetworks.com"
        self.assertTrue(o.is_snapshot_page(www + "/support/policies", rules))
        self.assertFalse(o.is_snapshot_page(www + "/support/policies/open-source-declaration", rules))
        self.assertTrue(o.is_snapshot_page(www + "/support/compatibility-matrices/sw-release-extremexos-eos", rules))


class Types(unittest.TestCase):
    def test_types_from_config(self):
        t = o.doc_type
        self.assertEqual(t("Switch Engine v33.7.1 Release Notes", "pdf"), "Release Notes")
        self.assertEqual(t("AP5060D and AP5060U Quick Install Guide", "pdf"), "Quick Reference")
        self.assertEqual(t("Installing Ethernet Routing Switch 3500 Series", "pdf"), "Installation Guide")
        self.assertEqual(t("Extreme 9920 Software Scale Matrix", "pdf"), "Feature Matrix")
        self.assertEqual(t("ExtremeXOS and Switch Engine v33.7.x EMS Messages Catalog", "pdf"), "Reference")
        self.assertEqual(t("Anything", "api"), "API")
        self.assertEqual(t("Fabric Engine Document Collection", "zip"), "Doc Collection")
        self.assertEqual(set(o.VALUE.values()), set(o.TYPES_CFG["values"]))
        self.assertIn("Other", o.TYPE_ORDER)


class Releases(unittest.TestCase):
    RELS = ["33.7.1", "33.7.0", "33.6.2", "33.6.1", "32.7.4", "32.7.2", "31.7.1", "Latest Documentation"]

    def pick(self, releases):
        return sorted(o.rel_set(self.RELS, o.to_rules(releases)), key=o.vkey, reverse=True)

    def test_positions_combine(self):
        self.assertEqual(self.pick(["latest-per-major"]), ["33.7.1", "32.7.4", "31.7.1"])
        self.assertEqual(self.pick(["latest-per-line"]), ["33.7.1", "33.6.2", "32.7.4", "31.7.1"])
        self.assertEqual(self.pick([{"newest": 1}]), ["33.7.1", "33.7.0"])
        self.assertEqual(self.pick(["first-per-line", {"newest": 1}]), ["33.7.1", "33.7.0", "33.6.1", "32.7.2", "31.7.1"])  # rules add up
        self.assertEqual(self.pick([{"from": "32.7", "to": "33.6"}]), ["33.6.2", "33.6.1", "32.7.4", "32.7.2"])
        self.assertEqual(self.pick(["31.7.1"]), ["31.7.1"])

    def test_latest_and_all(self):
        d = {"rel": "33.6.2", "latest": False}
        self.assertFalse(o.rel_ok(d, self.RELS, o.to_rules(None)))  # default: latest only
        self.assertTrue(o.rel_ok(dict(d, latest=True), self.RELS, o.to_rules(None)))
        self.assertTrue(o.rel_ok(d, self.RELS, o.to_rules("all")))
        self.assertFalse(o.rel_ok(d, self.RELS, o.to_rules("all", until="32.7")))


class Changes(unittest.TestCase):
    def test_new_gone_changed(self):
        k = lambda u: (u, "page")
        name = "docs-site/listings.csv"
        before = {name: {k("a"): {"url": "a", "title": "A", "last_seen": "2026-09-01"},
                         k("b"): {"url": "b", "title": "B", "last_seen": "2026-09-01"}}}
        after = {name: {k("a"): {"url": "a", "title": "A2", "last_seen": "2026-09-29"},
                        k("b"): {"url": "b", "title": "B", "last_seen": "2026-09-01"},
                        k("c"): {"url": "c", "title": "C", "last_seen": "2026-09-29"}}}
        c = o.catalog_changes(before, after)[name]
        self.assertEqual([r["url"] for r in c["new"]], ["c"])
        self.assertEqual([r["url"] for r in c["no_longer_listed"]], ["b"])
        self.assertEqual([(r["url"], r["changed"]["title"]) for r in c["changed"]], [("a", ["A", "A2"])])

    def test_page_hash_ignores_the_generated_header(self):
        a = "<!-- Generated by tools/catalog.py crawl. Do not edit. -->\n# Policies\n"
        b = "<!-- Generated by scanner/scan.py scan. Do not edit. -->\r\n# Policies\r\n"
        self.assertEqual(o.page_hash(a), o.page_hash(b))
        self.assertNotEqual(o.page_hash(a), o.page_hash(a + "new row\n"))


class Scan(unittest.TestCase):
    def test_pace_backs_off_and_recovers(self):
        p = o.Pace()
        p.bad()
        p.bad()
        self.assertEqual(p.delay, 1.0)
        for _ in range(20):
            p.good()
        self.assertAlmostEqual(p.delay, 0.7)
        for _ in range(400):
            p.good()
        self.assertEqual(p.delay, 0.0)

    def test_one_site_keeps_the_other(self):
        old = {"scanned": {"docs-site": "2026-09-01", "main-site": "2026-09-01"},
               "counts": {"docs-site": {"pages": 10}, "main-site": {"listings": 5}}}
        m = o.merge_meta(old, {"main-site": {"listings": 6}}, ("main-site",), "2026-10-01", {})
        self.assertEqual(m["scanned"], {"docs-site": "2026-09-01", "main-site": "2026-10-01"})
        self.assertEqual(m["counts"], {"docs-site": {"pages": 10}, "main-site": {"listings": 6}})
        self.assertEqual(m["scanner_version"], o.SCANNER_VERSION)

    def test_scan_due(self):
        meta = {"scanned": {"docs-site": "2026-09-01", "main-site": "2026-09-20"}}
        self.assertTrue(o.scan_due(dt.date(2026, 10, 1), meta, 30)[0])  # the older site decides
        self.assertFalse(o.scan_due(dt.date(2026, 9, 30), meta, 30)[0])
        self.assertTrue(o.scan_due(dt.date(2026, 9, 30), meta, 7)[0])
        self.assertTrue(o.scan_due(dt.date(2026, 9, 30), {"scanned": {"main-site": "2026-09-29"}}, 30)[0])

    def test_guard_stops_a_partial_scan(self):
        with self.assertRaises(SystemExit):
            o.guard("pages", 263, 1475)
        with self.assertRaises(SystemExit):
            o.guard("pages", 1475, 1475, failed=100)
        o.guard("pages", 1400, 1475, failed=3)  # within bounds
        o.guard("pages", 10, None)  # a first scan has nothing to compare with

    def test_replace_block(self):
        path = os.path.join(tempfile.mkdtemp(), "R.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# T\n<!-- BEGIN generated:last-scan -->\nold\n<!-- END generated:last-scan -->\nrest\n")
        self.assertTrue(o.replace_block(path, "last-scan", "new"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# T\n<!-- BEGIN generated:last-scan -->\nnew\n<!-- END generated:last-scan -->\nrest\n")
        self.assertFalse(o.replace_block(path, "numbers", "x"))

    def test_notes_carry_forward(self):
        saved = o.REPORTS
        o.REPORTS = tempfile.mkdtemp()
        try:
            write(os.path.join(o.REPORTS, "2026-09-01", "notes.md"),
                  ["# Notes, Scan 2026-09-01", "", "*Checked at this scan.* Facts checked by reading the snapshots.", "", "Switch Engine 32.7.4.15 recommended."])
            os.makedirs(os.path.join(o.REPORTS, "2026-10-01"))
            o.carry_notes("2026-10-01")
            with open(os.path.join(o.REPORTS, "2026-10-01", "notes.md"), encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(text.startswith("# Notes, Scan 2026-10-01\n\n*Carried forward from [2026-09-01]"))
            self.assertIn("Switch Engine 32.7.4.15 recommended.", text)
            self.assertNotIn("Checked at this scan.*", text.split("\n\n", 2)[2].split("\n", 1)[1])
            o.carry_notes("2026-10-01")  # a second run keeps what is there
            with open(os.path.join(o.REPORTS, "2026-10-01", "notes.md"), encoding="utf-8") as f:
                self.assertEqual(f.read(), text)
        finally:
            o.REPORTS = saved


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.saved = o.CATALOG, o.CONFIG
        o.CATALOG, o.CONFIG = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(o.CONFIG, "compatibility.yaml"), "w", encoding="utf-8") as f:
            f.write("software: {Switch Engine: Switch Engine, Fabric Engine: Fabric Engine}\n"
                    "sources:\n"
                    "- {snapshot: docs-site/se, kind: range, software: [Switch Engine], platform_by: file,\n"
                    "   platforms: {5520: 5520 Series, x870: X870}, no_entry: [x430]}\n"
                    "- {snapshot: main-site/fe, kind: recommended, software_by_prefix: {Fabric Engine: Fabric Engine},\n"
                    "   platform_by: row, platforms: {5520: 5520 Series}}\n")
        se = os.path.join(o.CATALOG, "docs-site", "snapshots", "se")
        write(os.path.join(se, "01-5520-switches-software-support.csv"),
              ["5520 Component,Minimum Software Version,Last Supported Software Version,links", "5520-24T,Switch Engine 31.1.1,latest available,"])
        write(os.path.join(se, "02-x870-switches-software-support.csv"),
              ["X870 Component,Minimum EXOS Software Version,Last Supported EXOS Software Version,links", "X870-32c,22.2.1.5,31.7.x,"])
        write(os.path.join(se, "03-x430-switches-software-support.csv"), ["X430 Component,Minimum,Last Supported,links", "X430-24t,15.3.2,16.x,"])
        write(os.path.join(se, "04-x999-switches-software-support.csv"), ["X999 Component,Minimum,Last Supported,links", "X999,1.0,latest,"])
        write(os.path.join(o.CATALOG, "main-site", "snapshots", "fe", "01-table.csv"),
              ["Platform,Maintenance Release,Latest Feature Release,Recommendation Effective Date,links",
               "5520,Fabric Engine 9.4.1.0,Fabric Engine 9.4.0.0,15-Jul-26,", "9999,Fabric Engine 1.0,,,"])

    def tearDown(self):
        o.CATALOG, o.CONFIG = self.saved

    def test_ranges_and_recommendations(self):
        r = sorted(o.compat_relations(), key=lambda x: (x["platform"], x["kind"]))
        self.assertEqual([(x["platform"], x["software"], x["kind"], x["min"], x["max"], x["releases"]) for x in r],
                         [("5520 Series", ["Switch Engine"], "range", "31.1.1", None, []),
                          ("5520 Series", ["Fabric Engine"], "recommended", None, None, ["9.4.1.0", "9.4.0.0"]),
                          ("X870", ["Switch Engine"], "range", "22.2.1.5", "31.7.x", [])])
        self.assertEqual(sorted(o.compat_relations.unmapped),  # x430 is listed as having no entry; x999 and 9999 are unknown
                         ["docs-site/se table 04-x999-switches-software-support.csv", "main-site/fe platform 9999"])


class Library(unittest.TestCase):
    """update and check end to end on a tiny catalog, with the network stubbed."""
    def setUp(self):
        self.saved = o.CATALOG, o.CONFIG, o.http, o.LIBRARY
        o.CATALOG, o.CONFIG, o.LIBRARY = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        c = o.CATALOG
        write(os.path.join(c, "docs-site", "pages.csv"), [
            "heading,entry,role,latest,release,page_title,status,items,releases,page,first_seen,last_seen",
            "R,Fabric Engine,entry,yes,9.4.0,FE,200,2,2,https://d/fe/,2026-09-29,2026-09-29",
            "R,5520 Series,entry,,,5520,200,0,0,https://d/5520/,2026-09-29,2026-09-29"])
        write(os.path.join(c, "docs-site", "listings.csv"), [
            "heading,entry,release,latest,title,date,month,format,via,host,url,page,first_seen,last_seen",
            "R,Fabric Engine,9.4.0,yes,Fabric Engine v9.4 User Guide,Apr 2026,2026-04,pdf,direct,d,https://d/fe-ug.pdf,https://d/fe/,2026-09-29,2026-09-29",
            "R,Fabric Engine,9.3.0,,Fabric Engine v9.3 User Guide,Sep 2025,2025-09,pdf,direct,d,https://d/fe-ug93.pdf,https://d/fe93/,2026-09-29,2026-09-29"])
        write(os.path.join(c, "main-site", "datasheets.csv"), [
            "product,label,page,url,first_seen,last_seen", "5520 Series,View Data Sheet,https://www/products/5520,https://hub/ds5520?v=1,2026-09-29,2026-09-29"])
        write(os.path.join(c, "main-site", "listings.csv"), ["area,page,heading,label,format,via,host,url,first_seen,last_seen"])
        write(os.path.join(c, "main-site", "pages.csv"), ["area,title,status,links,hub_files,url,first_seen,last_seen"])
        self.profile({"filters": [{"entries": ["Fabric Engine"], "types": ["User Guide"]}, {"entries": ["5520 Series"], "types": ["Datasheet"]}]})
        self.version = "1"

        def http(url, method="GET", tries=4):
            body = b"%PDF-1 " + url.encode() + b" v" + self.version.encode()
            h = {"Content-Type": "application/pdf", "ETag": url + self.version, "Content-Length": str(len(body))}
            return 200, url, h, body if method == "GET" else b""
        o.http = http

    def tearDown(self):
        o.CATALOG, o.CONFIG, o.http, o.LIBRARY = self.saved

    def profile(self, p):
        import yaml
        os.makedirs(o.ds(), exist_ok=True)
        with open(o.ds("profile.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(dict({"profile_version": 1}, **p), f)

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, n), o.LIBRARY).replace(os.sep, "/") for d, _, fs in os.walk(o.LIBRARY) for n in fs)

    def test_update_republish_and_drop(self):
        self.assertEqual(o.cmd_update(), 0)
        self.assertIn("5520 Series/5520 Series Data Sheet.pdf", self.files())
        self.assertIn("Fabric Engine/Fabric Engine v9.4 User Guide.pdf", self.files())
        self.assertNotIn("Fabric Engine/Fabric Engine v9.3 User Guide.pdf", self.files())  # not the latest release
        self.assertEqual(o.cmd_check(False), 0)
        self.version = "2"  # both files re-published upstream
        o.cmd_update()
        lock = {e["path"]: e for e in o.load_lock()}
        self.assertEqual(len(lock["Fabric Engine/Fabric Engine v9.4 User Guide.pdf"]["earlier"]), 1)
        self.assertEqual(len([f for f in self.files() if f.startswith(".docsync/archive/")]), 2)
        self.profile({"filters": [{"entries": ["Fabric Engine"], "types": ["User Guide"]}]})  # the datasheet leaves the profile
        o.cmd_update()
        self.assertNotIn("5520 Series/5520 Series Data Sheet.pdf", self.files())
        self.assertEqual(o.cmd_check(False), 0)
        with open(o.ds("history.md"), encoding="utf-8") as f:
            history = f.read()
        self.assertIn("Left the profile: 5520 Series/5520 Series Data Sheet.pdf (archived)", history)
        self.assertIn("Re-published: Fabric Engine/Fabric Engine v9.4 User Guide.pdf", history)

    def test_starters_and_folders(self):
        write(os.path.join(o.CONFIG, "profiles", "guides.yaml"), ["profile_version: 1", "filters:", "  - {types: [User Guide], releases: all}"])
        self.profile({"use": ["guides"], "folders": "{type}/{entry}", "filters": []})
        self.assertEqual([i["path"] for i in o.plan(o.load_profile())],
                         ["User Guide/Fabric Engine/Fabric Engine v9.4 User Guide", "User Guide/Fabric Engine/Fabric Engine v9.3 User Guide"])
        self.profile({"use": ["nope"]})
        with self.assertRaises(SystemExit):
            o.load_profile()


class Explorer(unittest.TestCase):
    def test_page_builds_from_the_catalog(self):
        out = os.path.join(tempfile.mkdtemp(), "index.html")
        o.build_explorer(out=out)
        with open(out, encoding="utf-8") as f:
            page = f.read()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertNotIn("__DATA__", page)
        data = json.loads(page.split("window.CATALOG=", 1)[1].split(";</script>", 1)[0])
        self.assertTrue(data["rows"] and data["profiles"] and data["compat"]["relations"])
        self.assertEqual(set(data["types"]), {t["name"] for t in data["typeinfo"]["types"]})
        self.assertTrue(set(data["compat"]["software"]) >= {"Switch Engine", "ExtremeXOS", "Fabric Engine", "VOSS"})


if __name__ == "__main__":
    unittest.main()
