"""The license server's Persian documents as PDF, for the evaluation file (AFTA) and the release.

Each Markdown document under DOCS becomes a right-to-left A4 PDF in the Vazirmatn font: a cover
(product, title, document id, product version, date), a linked table of contents, the text, and
page numbers. Links between the documents point to the other PDFs. The Markdown stays the source;
nothing here is edited by hand.

    pip install markdown==3.5.2 playwright==1.63.0 pypdf==5.4.0 && python -m playwright install chromium
    python scripts/docs_pdf.py                 # -> dist/docs/*.pdf
    python scripts/docs_pdf.py --out /tmp/pdf
    python scripts/docs_pdf.py --chromium /usr/bin/chromium   # an installed browser instead

The font is the npm package @fontsource/vazirmatn (pinned below), fetched with `npm pack`; the
admin UI does not use it, so it is not one of its dependencies.
"""
from __future__ import annotations

import argparse
import base64
import html
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = "NGCorion License Server"
FONT_PACKAGE = "@fontsource/vazirmatn@5.3.0"  # the version NGCorion's own UI uses

# source, PDF name, document id. The title is the document's first heading.
DOCS = [
    ("docs/fa/ADMIN_GUIDE.md", "NGCorion-License-Server-Admin-Guide-fa.pdf", "NLS-AGD-PRE-OPE"),
    ("docs/fa/ARCHITECTURE.md", "NGCorion-License-Server-Security-Architecture-fa.pdf", "NLS-ADV-ARC"),
    ("docs/SECURITY_TARGET.md", "NGCorion-License-Server-Security-Target-fa.pdf", "NLS-ASE-ST"),
]


def product_version() -> str:
    text = (ROOT / "license_server/app/main.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "?"


def font_dir() -> Path:
    work = Path(tempfile.mkdtemp(prefix="vazirmatn-"))
    subprocess.run(["npm", "pack", FONT_PACKAGE, "--silent", "--pack-destination", str(work)],
                   check=True, capture_output=True)
    tgz = next(work.glob("*.tgz"))
    with tarfile.open(tgz) as tar:
        members = [m for m in tar.getmembers()
                   if m.name.startswith("package/files/") and m.name.endswith(".woff2") and m.isfile()]
        tar.extractall(work, members=members, filter="data")
    return work / "package/files"


def font_css(files: Path) -> str:
    """@font-face rules with the woff2 files inlined, so the page needs nothing from outside."""
    rules = []
    for weight in (400, 700):
        for subset in ("arabic", "latin"):
            f = files / f"vazirmatn-{subset}-{weight}-normal.woff2"
            if f.exists():
                data = base64.b64encode(f.read_bytes()).decode()
                rules.append(f"@font-face{{font-family:Vazirmatn;font-weight:{weight};font-style:normal;"
                             f"src:url(data:font/woff2;base64,{data}) format('woff2');}}")
    if not rules:
        sys.exit(f"no Vazirmatn woff2 files in {files}")
    return "\n".join(rules)


CSS = """
@page { size: A4; margin: 22mm 18mm 20mm 18mm; }
html { direction: rtl; }
body { font-family: Vazirmatn, Tahoma, sans-serif; font-size: 10.5pt; line-height: 1.85; color: #1f2933;
       text-align: justify; text-justify: inter-word; }
h1, h2, h3, h4 { color: #12355b; line-height: 1.5; text-align: right; break-after: avoid; }
h2 { font-size: 15pt; margin: 22pt 0 8pt; padding-bottom: 4pt; border-bottom: 1.5pt solid #12355b; break-before: page; }
h3 { font-size: 12.5pt; margin: 16pt 0 6pt; }
h4 { font-size: 11pt; margin: 12pt 0 4pt; }
p, li { orphans: 3; widows: 3; }
ul, ol { padding-inline-start: 18pt; }
a { color: #12355b; text-decoration: none; }
code { font-family: "DejaVu Sans Mono", Consolas, monospace; font-size: 8.8pt; direction: ltr; unicode-bidi: isolate;
       background: #f1f4f8; padding: 0 3pt; border-radius: 2pt; }
pre { direction: ltr; text-align: left; background: #f1f4f8; border: 0.6pt solid #d5dde8; border-radius: 3pt;
      padding: 7pt 9pt; white-space: pre-wrap; word-break: break-word; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.5pt; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0; font-size: 9.5pt; break-inside: auto; }
tr { break-inside: avoid; }
th, td { border: 0.6pt solid #c9d3df; padding: 4pt 6pt; text-align: right; vertical-align: top; }
th { background: #e8eef5; color: #12355b; }
blockquote { margin: 8pt 0; padding: 6pt 10pt; border-inline-start: 3pt solid #12355b; background: #f5f8fb; }
hr { border: none; border-top: 0.6pt solid #d5dde8; margin: 14pt 0; }

.cover { height: 247mm; display: flex; flex-direction: column; justify-content: space-between; break-after: page; }
.cover .brand { font-size: 13pt; font-weight: 700; color: #12355b; letter-spacing: 0.04em; direction: ltr; text-align: right; }
.cover .band { border-top: 4pt solid #12355b; padding-top: 18pt; }
.cover h1 { font-size: 24pt; margin: 0 0 10pt; border: none; }
.cover .kind { font-size: 12pt; color: #52606d; }
.cover table { width: auto; min-width: 60%; font-size: 10pt; }
.cover th { width: 34%; }
.cover .note { font-size: 8.5pt; color: #7b8794; }
nav.toc { break-after: page; }
nav.toc h2 { break-before: avoid; }
nav.toc ul { list-style: none; padding-inline-start: 0; }
nav.toc ul ul { padding-inline-start: 14pt; }
nav.toc li { margin: 2pt 0; }
nav.toc > ul > li { font-weight: 700; margin-top: 6pt; }
nav.toc ul ul li { font-weight: 400; }
"""

FOOTER = """
<div style="width:100%;font-family:Vazirmatn,Tahoma,sans-serif;font-size:7.5pt;color:#7b8794;
            padding:0 18mm;display:flex;justify-content:space-between;direction:rtl;">
  <span>{title}</span><span style="direction:ltr">{doc_id} · {version} · <span class="pageNumber"></span> / <span class="totalPages"></span></span>
</div>
"""


def render(source: Path, pdf_names: dict[str, str], doc_id: str, version: str, fonts: str) -> tuple[str, str]:
    text = source.read_text(encoding="utf-8")
    m = re.search(r"^# (.+)$", text, re.M)
    title = m.group(1).strip() if m else source.stem
    if m:
        text = text[:m.start()] + text[m.end():]  # the title is on the cover

    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
                           extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
    body = md.convert(text)
    # links to the other documents go to their PDFs; to anything else, the repository path as text
    def link(m):
        href = m.group(1)
        target = href.split("#")[0]
        name = Path(target).name
        if target.endswith(".md") and name in pdf_names:
            return f'href="{pdf_names[name]}"'
        return m.group(0)
    body = re.sub(r'href="([^"#][^"]*)"', link, body)

    cover = f"""
<section class="cover">
  <div class="brand">{PRODUCT}</div>
  <div class="band">
    <h1>{html.escape(title)}</h1>
    <div class="kind">مستندات ارزیابی امنیتی (معیار مشترک / افتا)</div>
  </div>
  <table>
    <tr><th>محصول</th><td>{PRODUCT}</td></tr>
    <tr><th>نسخهٔ محصول</th><td dir="ltr" style="text-align:right">{html.escape(version)}</td></tr>
    <tr><th>شناسهٔ سند</th><td dir="ltr" style="text-align:right">{doc_id}</td></tr>
    <tr><th>تاریخ تولید</th><td id="built" data-iso="{date.today().isoformat()}">{date.today().isoformat()}</td></tr>
    <tr><th>منبع</th><td dir="ltr" style="text-align:right"><code>{source.relative_to(ROOT).as_posix()}</code></td></tr>
  </table>
  <div class="note">این PDF به‌طور خودکار از متن Markdown مخزن ساخته شده است؛ مرجع، همان متن است.</div>
</section>
<nav class="toc"><h2>فهرست مطالب</h2>{md.toc_tokens and md.toc.replace('<div class="toc">', '').rsplit('</div>', 1)[0]}</nav>
"""
    page = f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>{html.escape(title)}</title><style>{fonts}{CSS}</style></head>
<body>{cover}<main>{body}</main>
<script>
  const el = document.getElementById("built");
  const [y, mo, d] = el.dataset.iso.split("-").map(Number);
  el.textContent = new Date(Date.UTC(y, mo - 1, d, 12)).toLocaleDateString("fa-IR-u-ca-persian",
      {{ year: "numeric", month: "long", day: "numeric", timeZone: "UTC" }}) + " (" + el.dataset.iso + ")";
</script></body></html>"""
    return title, page


def fix_outline(path: Path, headings: list[str]) -> None:
    """Chromium writes the bookmarks of right-to-left headings in visual order (the words of a
    Persian heading come out reversed). Its bookmarks follow the headings in document order, so put
    the headings' own text back, one for one. If the counts differ, the bookmarks are left as they
    are rather than mislabelled."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, TextStringObject

    reader = PdfReader(str(path))
    items = []

    def walk(node):
        while node is not None:
            node = node.get_object()
            items.append(node)
            if "/First" in node:
                walk(node["/First"])
            node = node.get("/Next")

    outlines = reader.trailer["/Root"].get("/Outlines")
    if outlines is None or "/First" not in outlines.get_object():
        return
    walk(outlines.get_object()["/First"])
    if len(items) != len(headings):
        print(f"  {path.name}: {len(items)} bookmarks for {len(headings)} headings, left unchanged")
        return
    for item, text in zip(items, headings, strict=True):
        item[NameObject("/Title")] = TextStringObject(text)
    writer = PdfWriter(clone_from=reader)
    with open(path, "wb") as f:
        writer.write(f)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "dist/docs"), help="output directory (default dist/docs)")
    parser.add_argument("--chromium", help="a Chromium executable to use instead of Playwright's own")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    version = product_version()
    fonts = font_css(font_dir())
    pdf_names = {Path(src).name: pdf for src, pdf, _ in DOCS}
    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch(executable_path=args.chromium) if args.chromium else p.chromium.launch()
        page = browser.new_page()
        for src, pdf, doc_id in DOCS:
            title, doc = render(ROOT / src, pdf_names, doc_id, version, fonts)
            page_file = Path(tmp) / (Path(pdf).stem + ".html")
            page_file.write_text(doc, encoding="utf-8")
            page.goto(page_file.as_uri())
            page.evaluate("document.fonts.ready")
            # outline: the headings become PDF bookmarks; tagged: a structured (accessible) PDF
            page.pdf(path=str(out / pdf), format="A4", print_background=True, display_header_footer=True,
                     outline=True, tagged=True,
                     header_template="<span></span>",
                     footer_template=FOOTER.format(title=html.escape(title), doc_id=doc_id, version=html.escape(version)),
                     margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
            headings = page.evaluate("""() => [...document.querySelectorAll(
                'h1, h2, h3, h4, h5, h6')].map((h) => h.textContent.trim().replace(/\\s+/g, ' '))""")
            fix_outline(out / pdf, headings)
            print(f"{out / pdf}  ({(out / pdf).stat().st_size // 1024} KB)  {title}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
