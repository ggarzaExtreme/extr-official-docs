# Notes, Scan 2026-09-29

*Checked at this scan.* Facts a person (or Claude) checked by reading the snapshots and pages, which the report cannot say. The next scan carries them forward to be checked again.

### Release recommendations, as the main site lists them

Extreme defines two columns (VOSS/Fabric Engine page): the **Maintenance Release** is the actively
maintained release (no open critical issues introduced in it, in the field at least three months, SQA
regression passed); the **Latest Feature Release** is typically the newest release, fully supported, with
less field history.

| Family | Main site (www) | Source |
|---|---|---|
| Fabric Engine on 4220, 5320, 5420, 5520, 5720, 7520, 7720, 7830 | Maintenance 9.4.1.0, Latest Feature 9.4.0.0; as of 15 July 2026 | `results/catalog/main-site/snapshots/software-release-recommendations-for-voss-vsp-8600.md` |
| Switch Engine on 4120, 4220, 5120, 5320, 5420, 5520 | Maintenance 32.7.4.15 (33.6.1.14 on some models), Feature 33.7.1.6 | `results/catalog/main-site/snapshots/sw-release-extremexos-eos.md` |
| ERS 3600 / 4900 / 5900 | Maintenance 6.5.7 / 7.9.6; as of 6 November 2024 | `results/catalog/main-site/snapshots/software-release-recommendations-for-ers-stackable-switches.md` |
| XCO, SLX-OS | XCO 3.8.5, SLX-OS 20.7.3b; dated 17-Mar-26 | `results/catalog/main-site/snapshots/software-release-recommendations-for-efa-slx-os-nos-and-netiron.md` |
| ExtremeCloud IQ Site Engine | 26.02.13, as of July 2026 (prose, not a table) | `results/catalog/main-site/snapshots/compatibility-matrices.md` |

Latest published and recommended differ, as expected: Site Engine's latest published release is 26.08.11
and the recommendation is 26.02.13; Fabric Engine's newest release, 9.4.1.0, is the recommended
Maintenance Release.

### Where the published material disagrees with itself

**The docs site's release-recommendation pages are stale copies of the main site's**, except Fabric Engine
and Site Engine (dates in the table under Numbers). The versions:

| Page | Docs site | Main site |
|---|---|---|
| ExtremeXOS/Switch Engine | rows dated 16-May-24, Switch Engine 32.7.1.9 | Maintenance 32.7.4.15, Feature 33.7.1.6 |
| ERS (Stackable) | ERS 3600 6.5.4 | ERS 3600 6.5.7 |
| XCO, SLX-OS, NOS, NetIron | XCO 3.5.0, SLX-OS 20.5.3b | XCO 3.8.5, SLX-OS 20.7.3b |

The column names differ too: "Latest Feature Release" on the docs-site Switch Engine page, "Feature Release"
on the main site.

- The ExtremeCloud SD-WAN product page links the 23.6.0 User Guide, Release Notes and Installation Guide
  (PDFs last modified Oct 2023); the docs site's latest SD-WAN documentation is v26.2.0 (Aug 2026).
- The Customer Success Portal page links SD-WAN 24.1.0 documents, and its "ExtremeCloud SD-WAN Audit Guide"
  link points to the 24.1.0 Release Notes.
- The documentation index's "ExtremeControl" entry opens legacy Extreme Management Center 8.5.1 (2020); the
  current ExtremeControl guides are on the Site Engine pages.
- `optics.extremenetworks.com` covers the same subject as the optics datasheet but does not always match it,
  and the datasheet itself is sometimes wrong. Neither is a reference on its own.

### Broken or odd links

- Individual release-note pages on supportdocs return HTTP 500 (about 2,150 in the sitemap).
- The "Meet Network Security Basics" course outline on the Universal ZTNA training page returns 404.
- The 5420 product page's "5420 Hardware Installation Guide" link points to the docs index.
- The ExtremeCloud IQ product page's "View Data Sheet" file is a zip containing a PDF (Nov-24).
- The 4000 Series datasheet URL ends in `%27` (a stray apostrophe); it still resolves.

### Counted by hand (main site)

`/products` lists 83 entries (73 products, 10 solution pages). Policies: 19 files, MIBs as zip and tar.gz
of the same 493 files. Open Source Declaration: about 315 PDFs, about 800 MB. End of Sale: 291 announcement
rows, 295 notice PDFs. MTBF: 2,162 rows. Visio stencils: 63 zips.
