"""QA for GOAI v2 deck: PDF basics, number cross-check, desensitization, page renders."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "deck_v2" / "pages"
PDF = ROOT / "deck_v2" / "deck_v2_print.pdf"
QA_IMAGES = ROOT / ".qa-images-v2"

EXPECTED = [
    "478.8", "480", "4,414", "458.9", "501.1", "3.92", "4.41", "5,000",
    "37.4", "72.9", "85.3", "NO_TRADE", "BLOCK", "DRAFT_ONLY",
    "READY_FOR_CONFIRMATION", "-904", "-424", "+971", "+1,451", "+2,846",
    "+3,326", "-711", "-340", "-820", "-403", "-873", "-422",
    "-2.4", "-7.8", "21.0", "-49.1", "-47.5", "-22.6", "36%", "16%",
]

SENSITIVE = ["20484565", "password", "token", "secret", "api_key", "C:\\Users", "F:\\GOAi"]


def main() -> int:
    failures = 0
    doc = fitz.open(PDF)
    print(f"PDF pages: {len(doc)}")
    if len(doc) != 16:
        print("FAIL: PDF page count != 16")
        failures += 1

    fonts = sorted({f[3] for page in doc for f in page.get_fonts()})
    print("Fonts:", fonts)
    for bad in ("TimesNewRoman", "Noto"):
        if any(bad in f for f in fonts):
            print(f"FAIL: unexpected font family containing {bad}")
            failures += 1

    page_text = "".join(p.read_text(encoding="utf-8") for p in sorted(PAGES.glob("*.page")))
    missing = [n for n in EXPECTED if n not in page_text]
    if missing:
        print("MISSING numbers in pages:", ", ".join(missing))
        failures += 1
    else:
        print("Number cross-check: all expected values present in pages")

    pdf_text = "".join(page.get_text() for page in doc)
    leaks = []
    for pattern in SENSITIVE:
        if pattern.lower() in page_text.lower() or pattern.lower() in pdf_text.lower():
            leaks.append(pattern)
    if leaks:
        print("SENSITIVE LEAK:", "; ".join(leaks))
        failures += 1
    else:
        print("Desensitization scan: clean")

    QA_IMAGES.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        pix.save(QA_IMAGES / f"slide_{index:02d}.png")
    print(f"Rendered {len(doc)} page images -> {QA_IMAGES}")

    doc.close()
    if failures:
        print(f"QA FAILED with {failures} issue(s)")
        return 1
    print("QA PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
