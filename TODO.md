# To Do

Open work only, roughly in order of value. Findings go in each scan's report; how a scan runs is in the README.

## Coverage

- Read `optics.extremenetworks.com`, as claims only: it doesn't always match the optics datasheet, which is
  also sometimes wrong. It's listed in `config/sites.yaml` as known, not read.
- A reader for the docs site's article-style pages (WiNG how-tos, `empty-page` in `docs-site/health.csv`).
- HTML guides' tables of contents, so a guide's Details can show what it covers.
- HEAD metadata (size, Last-Modified, ETag, status) for every file, not just a library's. It gives the
  explorer sizes and dead links, and catches plain PDFs re-published at the same URL.
- HTML guide redirects for older release lines, if a library ever pins them (only latest guides are checked
  today, to keep scans short).

## Types and Compatibility

- The 285 documents still typed Other (3.6%): add title rules as patterns show up.
- More compatibility sources in `config/compatibility.yaml`: EOS, ERS/BOSS, SLX-OS, wireless (IQ Engine and
  its APs), ExtremeCloud IQ hardware support.
- A To Check item when an entry's index link isn't its newest release page.

## Explorer

- A Title Case and styling pass over every label.
- Narrow screens: the rails should overlay rather than squeeze below about 1,000 px.
- Matrix: no sideways scroll with both panels open at the full column set (27 types).
