#!/usr/bin/env python3
"""
extract_q_templates.py — builds extra_templates.npz, the supplementary
status-letter template file ocr_extract.py loads on top of the embedded
_TPL blob (see the comment above _load_extra_templates() in that file
for the full story).

WHY THIS SCRIPT EXISTS
------------------------
The embedded template set in ocr_extract.py (copied byte-for-byte from
parse_roll.py — never hand-edited, to protect it from transcription
errors) only has status-letter templates for S, R, E and # — there's no
"Q" or "M" template at all. That means a genuinely-Q or genuinely-M card
can never match correctly, no matter the confidence threshold: match()
is choosing the least-bad option among S/R/E/#, not a real option.

Found by checking 8 rows flagged "status_letter_unclear" directly
against the source PDF (AC 86, part 101) — every one was visibly Q, none
were S/R/E/#.

THE CONFIRMED EXAMPLES BELOW
------------------------------
CONFIRMED_EXAMPLES lists specific (pdf, page, card_on_page, label) card
positions that have been visually checked against the actual PDF page —
not guessed. The three Q entries below are from AC 86 part 101, pages 20
and 22, cross-checked against the rendered page image before being
added. If you find a confirmed "M" example (or want more Q examples for
robustness), add it the same way: open the PDF, find the card's page
number and position (card_on_page = row-major position in the 3x10
grid, 1-indexed), confirm the letter in its marker box with your own
eyes, and add an entry here — then re-run this script.

Running this script OVERWRITES extra_templates.npz from the full list
below (it's not incremental) — regenerate it from CONFIRMED_EXAMPLES
rather than hand-editing the .npz.

USAGE
-----
    python extract_q_templates.py
"""
import sys
from pathlib import Path

import cv2
import numpy as np

import ocr_extract as e

OUT_PATH = Path(__file__).resolve().parent / "extra_templates.npz"

# (pdf_path, page_number_1indexed, card_on_page_1indexed, label)
# Visually confirmed against the rendered PDF page before being added here.
CONFIRMED_EXAMPLES = [
    ("downloads/86/2026-EROLLGEN-S24-86-SIR-FinalRoll-Revision1-HIN-101-WI.pdf", 22, 11, "Q"),
    ("downloads/86/2026-EROLLGEN-S24-86-SIR-FinalRoll-Revision1-HIN-101-WI.pdf", 20, 11, "Q"),
    ("downloads/86/2026-EROLLGEN-S24-86-SIR-FinalRoll-Revision1-HIN-101-WI.pdf", 25, 25, "Q"),
]


def extract_tight_glyph(card_img):
    """Same marker-box region cut_card() uses (c[6:24, 6:34]), tightened
    to just the ink bounding box + 1px margin — matching the size/format
    of the existing S/R/E/# templates in the embedded blob (roughly
    9-11 x 7-8 px, tight single-glyph crops, not the whole marker box)."""
    sub = card_img[6:24, 6:34]
    ink = sub < 128
    rows = np.flatnonzero(ink.any(1))
    cols = np.flatnonzero(ink.any(0))
    if not len(rows) or not len(cols):
        return None
    r0, r1 = int(rows.min()), int(rows.max())
    c0, c1 = int(cols.min()), int(cols.max())
    R0, R1 = max(6 + r0 - 1, 0), 6 + r1 + 2
    C0, C1 = max(6 + c0 - 1, 0), 6 + c1 + 2
    return card_img[R0:R1, C0:C1].astype(np.uint8)


def find_card(pdf_path: str, page_1idx: int, card_on_page: int):
    for pno, g, npg in e.page_images(pdf_path):
        if pno + 1 != page_1idx:
            continue
        cards = e.find_cards(g)
        if card_on_page - 1 >= len(cards):
            print(f"  ! page {page_1idx}: only {len(cards)} card(s) found by find_cards(), "
                  f"can't get card_on_page={card_on_page}", file=sys.stderr)
            return None
        x, y, w, h = cards[card_on_page - 1]
        return g[y:y + h, x:x + w]
    print(f"  ! page {page_1idx} not found in {pdf_path}", file=sys.stderr)
    return None


def main():
    templates = {}  # label -> list of arrays
    for pdf_path, page, card_on_page, label in CONFIRMED_EXAMPLES:
        if not Path(pdf_path).exists():
            print(f"  ! {pdf_path} not found — skipping this example "
                  f"(point CONFIRMED_EXAMPLES at wherever your sample PDFs actually live)")
            continue
        card_img = find_card(pdf_path, page, card_on_page)
        if card_img is None:
            continue
        tpl = extract_tight_glyph(card_img)
        if tpl is None:
            print(f"  ! {pdf_path} page {page} card {card_on_page}: no ink found in marker box")
            continue
        templates.setdefault(label, []).append(tpl)
        print(f"  {pdf_path} page {page} card {card_on_page}: extracted {label} template, shape {tpl.shape}")

    if not templates:
        print("No templates extracted — nothing written.")
        return

    out = {}
    for label, imgs in templates.items():
        for i, img in enumerate(imgs):
            out[f"M_{label}__{i}"] = img

    np.savez(OUT_PATH, **out)
    print(f"\nWrote {OUT_PATH.name}: " +
          ", ".join(f"{label}={len(imgs)}" for label, imgs in templates.items()))
    print("Re-run your extraction (ocr_extract_paddleocr.py) to pick this up — "
          "it's loaded automatically if extra_templates.npz sits next to ocr_extract.py.")


if __name__ == "__main__":
    main()