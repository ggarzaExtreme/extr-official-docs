# The Explorer

What the explorer (`index.html`, built from `scanner/template.html`) must do. A rebuild must meet
everything here; the look may change within the colour tokens at the top of the template. The page is one
file: styles, script and js-yaml inline, and the catalog as `window.CATALOG`. `scanner/scan.py` fills it in.
The page reads everything else from that data: types, values, starter profiles, compatibility, coverage and
what isn't read. Nothing about products is hardcoded.

## Model

- **Four layers:**
  - the **catalog**: what exists
  - **filters**: what you look at
  - the **view**: how you look at it
  - the **profile**: what you keep
- The profile never changes the view. Every view marks what the profile takes.
- **One way to add.** + (and "+ Add All Filtered") adds what the current filters show, releases included,
  as a rule: later releases that match join on their own. ✓/○ on a single file adds just that file, which
  stays as it is. Taking out mirrors it. Adding the same thing twice is refused.
- **Words.** Filters are what you see; the view is how (the page, its Group By and columns); the profile is
  what you keep; Checkout is getting the files. "Selection" means only Search's Select Several.
- Counts in the profile are **files** (unique URLs), not listings.
- A document has a **place** and **entries**.
  - Its place is where it lives: site and path.
  - Its entries are what it belongs to. A datasheet lives on the main site's /products/, and belongs to its
    docs entry through `config/main-site-entries.yaml`.
  - Where It Lives uses the place (in the Matrix and in Browse). Everything else uses the entries.
- A view is a link (`#/<page>?v=`). A profile is a file, or a link (`#/<page>?p=`).

## Pages

The page opens on the Dashboard. The Filters and Profile panels, and Details, are one setting for every page:
open on one page means open on all of them.

| Page | Purpose | Requirements |
|---|---|---|
| Dashboard | the catalog at a glance | facts; documents by type, by heading and per year; most documents; recently updated; starter profiles; snapshots and compatibility; Freshness always last, since it spans the full width (a card after it reads as a separate group) |
| Matrix | entries by document type | rows grouped by where it lives (host › heading or path › entry › file), product (kind › family › entry), docs-site heading, starter profile, or none (A–Z); columns are types, coloured by value, with short labels and the full name on hover; first column resizable; + on every group, row and column; rows open down to single files (default) |
| Search | every document | grouped, collapsible, sortable (Title, Release, Published), resizable columns; a ✓/○ first column adds or takes out without opening Details; Select Several (shift-click for a run) with a bulk bar; row actions: open, add, copy link, details |
| Browse | curated ways in | five lists side by side: Starter Profiles, Docs-Site Headings, Where It Lives, Ways In, Snapshots. The chosen item is highlighted and what it holds shows underneath. Choosing never moves the page |
| Checkout | getting the files | Get These Files first: what to get, then three ways (1. the scanner, recommended; 2. download here; 3. the link list). Then totals by how each file can be fetched, the table (keep or leave out; group by entry, type, folder, how to get it), snapshots, left-out files, and the profile as a file |

**Freshness** lists entries with numbered releases (at least three) and one published in the last 12 months.
For each it shows the newest release, when it was published, how old it is, and how many releases came out
in the last 12 months. Newest is by number, not recommendation.

## Shared Parts

- **Top bar:** name; scan date, age and next due; the tabs, with the Checkout count; Legend · Settings · About.
- **Toolbar:** the Filters toggle (with a count); while filters are active, Clear All and then the chips
  (Clear All first, so it never moves); at the right, "+ Add All Filtered" and the Profile toggle (with the
  name, file count and "modified").
- **Filters (left, resizable).** Search, then Link (a link to this page, its filters and Group By) and
  Expand All / Collapse All, then cards for:
  - Sites, Releases, Type, Value (a setting), Docs-Site Heading, Entry, Compatibility, Format, Published
  - Choices stack vertically, lists grow, and "only" never shifts the layout: it shows in the count's place
    on hover. A type's checkbox carries its value colour, so every type name fits on one line at 280 px.
  - What the profile takes is shown by the marks on every page, not by a filter.
- **Profile (right, resizable): the cart.** It holds:
  - the profile select (edited profiles show "name · modified", so any choice resets)
  - the file count, Save, Link (a link that opens the profile) and the value bars
  - "Checkout: Get the Files →"
  - "Adding hardware also adds its software" (a setting): adding a platform the support tables name also
    adds the software that runs there, of the same types (all types for a whole row or group), in the
    releases the filters show that run on it. On the default (latest), software sharing one numbering
    (ExtremeXOS, Switch Engine) adds only the newest. Taking out never cascades. Hardware with no table
    says so in the toast.
  - a find box (title, entry or type, e.g. "Other")
  - one line per entry: its name (opens its Details), ↻ when a rule takes it (hover for the rules), a pin
    when files were added one by one, its file count, its type chips, and × to take it all out. A line opens
    to its files (✓, title, release, −), 300 at a time, then Show All.
  - one line per snapshot, with ×
  - the left-out files
- **Details.** A panel at the right or bottom (a setting), resizable, with Pop Out to a window. It shows one
  of three things:
  - an entry: software support, the release × type grid, latest documents, related entries
  - a document: its facts, where it runs, other editions
  - a snapshot: its files, and the page as kept
- **About:** About This Catalog (coverage, Not Read), Starter Profiles (a table with Use), README, Latest
  Report.

## Releases

There are two kinds of rule:
- **Position rules** work for every product, each by its own numbering, and combine: latest, latest of each
  major, latest of each release line, first of each line, newest N lines, all.
- **Release numbers** (a range, or exact releases) belong to one product. They appear only when one entry is
  chosen, list that entry's own releases, and are cleared when the entry changes.

Nothing ticked means every release; Latest is only the default. The only release marker is **latest**.
Recommendations live in their snapshots and the report's notes.

## Compatibility

Compatibility comes from `config/compatibility.yaml`: relations of platform, software, and a kind. A range
has a first and a last release; a recommendation names releases. The software today is Switch Engine,
ExtremeXOS, Fabric Engine and VOSS. Nothing in the page names them; adding a source adds software.

- **Filter.** Two directions:
  - Hardware Running a Release: pick the software, type a release.
  - Software for Hardware: tick platforms.

  A release matches by line: 31.7.1 matches 31.7.x, and a recommendation covers its major.minor. Choosing
  hardware widens Releases to All when Releases is on the default.
- **Details.** An entry lists each relation, e.g. "Switch Engine / ExtremeXOS 31.1.1 to latest available
  (table)" and "Fabric Engine recommended 9.4.1.0 / 9.4.0.0 (table)". A software document lists the
  platforms that run its release. The release grid has a Runs On column.

## Getting Files

- **Documents.**
  - Main-site files download in bulk from the page, because the content hub allows it.
  - Docs-site files are one click each.
  - HTML guides need the scanner, which fetches the PDF inside.
- **Snapshots** download from the page as CSV, Markdown or both. Profiles can name them, and `update` copies
  them into `<library>/snapshots/`.
- **Everything:** the `update` command, shown with Copy, and the link list as a text file.

## Settings

- **Theme:** System, Light or Dark. The dot in the scan status starts the Strange Attractor; it stops for
  reduced motion. Its panel has:
  - shape (de Jong, Clifford, Svensson), mirror (none, ×2, ×4), how far it drifts, zoom
  - drift speed, points, trail; palette, glow, colour shift; panel glass
  - ∿ on any knob: it wobbles on its own (an LFO, with a rate and a depth)
  - Glow When the Profile Changes: adding blooms, taking out dims
  - Hide Interface, keeping any of top bar, toolbar, filters, profile and page; Esc brings it back
  - Pause, New Shape, Randomize, Back to Normal
- **Matrix:** rows down to files, colour by value, group shading, a count click opens details, totals, hide
  empty columns.
- **Clicks and layout:**
  - the mark style: a square frame (default) or a ring
  - what a single file's click does: any combination of details, open, add or take out, and copy link
  - details at the right or bottom, one line per row in Search, value as a filter
- Panel and column widths are remembered.

Stop there.

## Look

- Title Case for titles, headings, buttons, menus and short instructions.
- In every table, the first group level is strong and the second lighter.
- Columns carry their type's value colour.
- The mark for what the profile takes is a frame around the count: stronger as it fills, solid when full,
  never a fill. Single files use ✓ and ○.
- The details button appears on hover, in reserved space.
- One chevron style for every dropdown.
- Counts that are 0 disappear.

## Rebuild Checklist

- [ ] No console errors on any page
- [ ] 1920×1080 with both panels open: panels sit flush, and Matrix fits without sideways scroll at the default column set
- [ ] Every starter profile resolves (no unknown entries; the report says so) and round-trips through Save and Open
- [ ] A profile link opens the same profile, and a view link the same filters
- [ ] Release rules give the expected releases for an entry with deep numbering (Switch Engine, VOSS)
- [ ] Compatibility both ways for each software: Switch Engine 32.7 → its platforms; Fabric Engine 9.4.1 → the universal switches; the 5520 shows both relations
- [ ] Browse › Where It Lives › /products/ counts and adds the datasheets
- [ ] Matrix › Group By › Product places every docs-site entry (the report says so); with the setting on, adding the 5520 row adds Switch Engine and Fabric Engine
- [ ] Main-site and snapshot downloads fetch
