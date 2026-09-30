"""Read one downloaded file into the same small card, whatever its product: pages, metadata, the bookmark
outline, part number, revision, the releases named on its first pages, and a hash of its text (which
changes only when the words change). scanner/scan.py update keeps the cards in <library>/.docsync/details.yaml
when the profile says details: on, and notes in history.md what changed inside a re-published file.
"""
import hashlib
import os
import re
import zipfile

PROFILER_VERSION = "1"
PART = re.compile(r"\b(9\d{6}-\d{2}|NN\d{5}-\d{3})\b")
REV = re.compile(r"\bRev(?:ision)?\.?\s*:?\s*([A-Z]{2})\b")
VERSION = re.compile(r"\b(?:v|version\s+|release\s+)?(\d{1,2}\.\d{1,2}(?:\.\d{1,4}){0,2})\b", re.I)
PUBLISHED = re.compile(r"\bPublished:?\s*([A-Z][a-z]+ \d{4}|\d{1,2} [A-Z][a-z]+ \d{4})")


def profile_pdf(path):
    import pymupdf
    doc = pymupdf.open(path)
    pages = [p.get_text() for p in doc]
    text = "".join("=== page %d ===\n%s\n" % (i + 1, t) for i, t in enumerate(pages))
    head = "\n".join(pages[:4])
    meta = {k: v for k, v in (doc.metadata or {}).items() if v and k in ("title", "subject", "creationDate", "modDate")}  # no author: a person's name
    outline = [{"level": lvl, "title": re.sub(r"\s+", " ", t).strip(), "page": pg}
               for lvl, t, pg in doc.get_toc(simple=True) if lvl <= 2]
    versions = []
    for m in VERSION.finditer(head):
        # "9.4.1.0" or "v9.4" count; a bare "2.1" is too often a section number
        named = m.group(0)[:1].lower() in ("v", "r") or m.group(1).count(".") >= 2
        if named and m.group(1) not in versions:
            versions.append(m.group(1))
    card = {
        "type": "pdf",
        "pages": len(pages),
        "words": sum(len(t.split()) for t in pages),
        "metadata": meta,
        "part_number": first(PART, head),
        "revision": first(REV, head),
        "published": first(PUBLISHED, head),
        "versions_on_first_pages": versions[:8],
        "outline": outline,
    }
    return card, text


def profile_zip(path):
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.startswith("__MACOSX/") and not n.endswith("/")]
    text = "\n".join(names) + "\n"
    exts = {}
    for n in names:
        e = os.path.splitext(n)[1].lower() or "(none)"
        exts[e] = exts.get(e, 0) + 1
    return {"type": "zip", "entries": len(names), "by_extension": dict(sorted(exts.items())), "sample": names[:25]}, text


def profile_xlsx(path):
    with zipfile.ZipFile(path) as z:
        sheets = re.findall(r'<sheet [^>]*name="([^"]+)"', z.read("xl/workbook.xml").decode("utf-8", "replace"))
    return {"type": "xlsx", "sheets": sheets}, "\n".join(sheets) + "\n"


def first(rx, text):
    m = rx.search(text or "")
    return m.group(1) if m else None


def profile(path):
    """(card, text) for a file, by its type; unknown types get a card with only size and type."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            card, text = profile_pdf(path)
        elif ext == ".zip":
            card, text = profile_zip(path)
        elif ext == ".xlsx":
            card, text = profile_xlsx(path)
        else:
            card, text = {"type": ext.lstrip(".") or "unknown"}, ""
    except Exception as e:  # a damaged or unusual file still gets a card that says so
        card, text = {"type": ext.lstrip("."), "error": "%s: %s" % (type(e).__name__, e)}, ""
    card["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
    return card, text
