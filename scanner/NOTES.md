# Scanner Notes

How Extreme's sites are built, what the scanner relies on, and the rules it follows because of past scans.
This changes only when a site is redesigned; the health counts in `results/catalog/<site>/health.csv` are
the sign that it has. What is scanned, and what isn't read, is set in `config/sites.yaml`.

## The Hosts

| Host | Found through | Catalog |
|---|---|---|
| `supportdocs.extremenetworks.com` (the docs site) | the index page, the WordPress sitemaps (`s_product`, `s_product_version`, `s_comp_matrix`) and every release menu | `docs-site/` |
| `documentation.extremenetworks.com` | links on docs-site pages; an HTML guide's root is read once to record its redirect | `docs-site/listings.csv`, `documents.csv` |
| `www.extremenetworks.com` (the main site) | `sitemap.xml`, filtered by `config/sites.yaml` | `main-site/` |
| `extr-p-001.sitecorecontenthub.cloud` (the content hub) | links on main-site pages | `via = content_hub` |

Both sites allow crawling (`robots.txt`). The content hub answers cross-origin requests
(`Access-Control-Allow-Origin: *`), so the explorer can download its files in the browser. The docs hosts
don't, so those files are one click each, or `update`.

## The Docs Site

**The index** lists entries under headings (Routing & Switching, Wireless & Mobility, ...). An entry is a
product, an operating system, a series, or a cross-product collection.

**Pages.** Each page has a role:
- `entry`: the index links to it.
- `release`: another option in an entry's release menu.
- `orphan`: in the sitemap, but reachable from neither the index nor a release menu.

Orphans that still list documents are mostly legacy: Industrial Series 1.01, WLAN 8100, D/G/I Series 6.03.x.
Pages that list no documents are WiNG how-to articles and placeholders (`empty-page` in `health.csv`).

**Release menus.** Every entry page has the same `<select>`, whose options are sibling pages, newest first.
Some options are not releases: "Latest Documentation", "Previous Documentation", "Document Collection",
"TCG/TSG", "SD-WAN Appliances".

**Latest.** `latest = yes` marks every release page in the newest release line: the same major.minor as the
first numbered option. It also marks the entry page when that is a "Latest Documentation" page or the entry
has no numbered releases.
- One page is not enough. The Fabric Engine 9.4.1.0 page lists only its release notes; the 9.4 User Guide,
  Command Reference and Feature Matrix are on the 9.4.0 page.
- Latest means most recently published, not recommended. Recommendations are on the release-recommendation
  pages, which are kept as snapshots.
- An entry whose release labels aren't numbers is flagged in To Check, so its latest mark gets a look.

**Formats and via.**
- `pdf` via `direct`.
- `pdf` via `html_guide`: a small site for one guide.
  - Its root is a meta-refresh page, and its landing page links the full PDF at `<guide>/downloads/<name>.pdf`,
    which is what `update` fetches.
  - `documents.csv` records where each latest guide redirects. Newer guides name their revision there
    (`fe_9.4_ug_revacdev/` is Rev AC). Older ones don't, but their landing path still changes when they are
    re-published.
- `zip`: Document Collections.
- `xlsx`, `api` (REST, GraphQL, developer portals) and `video` (YouTube, Vimeo).

**Release notes.** The individual release-note pages in the sitemap (about 2,150) return HTTP 500, so the
scanner reads the archive's per-product pages instead. Release pages list their release notes too.

**Cross-product entries.**
- Extreme Solution Guides: the CLI Cross Reference, Fabric Connect network virtualization, the API with Python.
- ERS Migration: Fabric Edge design, SPB multi-area, VSP Edge deployment.
- Fabric Engine TCG/TSG.
- Pluggable Hardware.

## The Main Site

**Products.** `/products` is a search page. Almost every product page links a datasheet on the content hub.
The hub URL carries a `?v=` token that changes with the file, so a re-published datasheet shows as one link
gone and one new. `config/main-site-entries.yaml` pairs each product name with the docs entries it belongs to.

**Support pages.**
- Policies: warranty, lifecycle, End of Sale and End of Support, GTAC, service matrix, licensing, MIBs.
- Compatibility matrices: a landing page with the Site Engine recommendation as prose, and pages of tables.
- End of Sale and End of Service Life, Mean Time Between Failures, RoHS.
- Visio stencils, and training (a public course outline for every course).

Types on the main site come from where a file is listed:
- a datasheet link on a product page: Datasheet
- `/support/policies`: Policy
- `/support/visio-stencils`: Stencil
- `support/training`: Course Outline
- `resources/*`: Brief

**Resources.** Only the types that carry documents are in scope. Blogs, case studies, webinars and videos
(about 1,000 pages) are out.

**Navigation** is dropped by frequency: a link on 20% of all pages, or 60% of one area's pages, is site
chrome. Content-hub links are always kept.

## Snapshots

Table-bearing support pages are kept whole:
- `<name>.md`: the page's prose, headings, tab labels and tables.
- `<name>/NN-<caption>.csv`: one file per table, with its header row and a `links` column.

Which pages is set in `config/sites.yaml` (main site) and the `s_comp_matrix` sitemap (docs site). Hidden
sort keys in date cells are dropped.

## Types

Types come from `config/types.yaml`. The format decides first (api, video, zip). After that, the first
title rule that matches wins, and anything left over is Other. Other is about 3.6% of documents. Each report
counts it, so a new kind of document gets noticed; when that count grows, add a rule.

## Compatibility

`config/compatibility.yaml` names the support tables that say which hardware runs which software. So far
there are two:

- **The Summit, ExtremeSwitching and E4G software support tables** (docs site). There is one table per
  platform, so it is `platform_by: file`.
  - Each gives a minimum release and a last supported release, or "latest available", which makes it
    `kind: range`, for Switch Engine and ExtremeXOS (one numbering).
  - A table whose third column is a minimum BootROM, not a last release, doesn't state both ends. It is left
    out rather than guessed.
  - Older models without a docs entry (X430, X440, X450, X460) are listed under `no_entry`.
- **The VOSS, Fabric Engine and VSP 8600 recommendations** (main site). There is one row per platform, so
  it is `platform_by: row`.
  - It gives recommended releases, which makes it `kind: recommended`. The software is named in each cell
    ("Fabric Engine 9.4.1.0", "VOSS 8.10.9.0").
  - A recommendation says a release line runs there, not from which release.

A release matches by line: 31.7.1 matches "31.7.x", and a recommendation of 9.4.1.0 covers the 9.4 pages.
Each report lists table rows naming platforms the file doesn't map. To add another source, add it here and
map its platforms.

## Running a Scan

A full scan takes about an hour. Every rule below is already in the scanner and came from a real failure;
the point is not to undo them.

- **Parse on one thread, download on many.** lxml work inside worker threads deadlocked, so workers only
  download.
- **Every wait needs an exit.** A batch that finishes nothing for 120 s is retried once, then recorded as
  stalled, so a scan always ends. Failed pages get one slow retry at the end.
- **Never shrink silently.** A sitemap that failed under load once turned 1,475 pages into 263 without an
  error. The scan now stops before writing when:
  - a sitemap fails,
  - it finds under 90% of the last scan's pages or listings, or
  - more than 2% of pages fail.

  If the site really shrank, run it again with `--allow-shrink`.
- **Resume, don't restart.** Pages are saved in `.cache/pages/` until a scan finishes, so a stopped scan
  picks up where it left off, after any gap. A finished scan clears them, and the next one reads the live
  sites.
- **Be gentle, don't evade.** The docs site sits behind Cloudflare and throttles heavy parallel load.
  - The scanner uses 3 workers and backs off on 429/5xx, honouring Retry-After.
  - It shares one delay across workers, which speeds back up after runs of successes.
  - It never rotates addresses or identities. If speed is ever needed, ask for the user agent to be
    allowlisted.
- **Timing is recorded.** Each step's pages, time, rate, and live, saved, throttled, retried and failed
  requests go into `last-scan.yaml` and the report. A step at under half the last scan's rate is flagged.
- **One scan at a time, in the background.** Send the output to `.cache/scan.log` and check it now and
  then.
- **Commit after every scan.** The next scan compares itself with the last committed catalog. Two
  uncommitted scans fold into one report.
