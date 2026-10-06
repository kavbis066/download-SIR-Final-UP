#!/usr/bin/env python3
"""
ocr_extract_paddleocr.py (v13) -- fast extraction pipeline.

WHAT CHANGED IN v13 (on top of v12, below) -- joined names, garbled names, stray marks
----------------------------------------------------------------------
Measured on the AC 2 part-2 CSV (688 cards): where the two Hindi models disagreed
(~900 name / relation-name fields) the older v3 model won the score vote in 236 of
them, and was wrong in nearly all: dropped conjuncts (बुद्धराम -> बुदराम), moved matras
(ललित -> लिलत, साजिद अली -> सिजदअली), merged words (लालो मोची -> लालोमोची), a stray
"े" (हरपाल -> हरपालने, युसुफ -> युसुफे) -- and it reports 1.00 confidence on many of them.
 * get_name(): the primary v5 model is trusted; v3 can only win when v5 produced
   nothing usable or v3 beats it by V3_NEEDS_MARGIN (0.15). If two readings differ only
   by spaces, the one WITH spaces is kept (first name / last name no longer merge).
 * --second-opinion auto (default): v3 only re-reads crops where v5 scored < 0.90, was
   too short, or produced a word that cannot start that way. Confident v5 reads skip v3
   entirely, so Hindi OCR time drops by roughly 40%. 'all' = v11 behaviour, 'off' = never.
 * Words starting with nasal + halant + stop consonant (न्दू, म्ब...) cannot occur in
   Hindi -- they mean a dropped first letter. Flagged name_invalid_start /
   relation_name_invalid_start and sent through the re-crop retry.
 * Decomposed independent vowels (अा, अाे, अाै) are recomposed to आ, ओ, औ.

--- v12 notes ---

ocr_extract_paddleocr.py (v12) -- fast extraction pipeline.

WHAT CHANGED IN v12 (on top of v11, below)
----------------------------------------------------------------------
 * Wrong marks in names ("सपना" read as "सपनाे", "रीनाे"): verified on AC 2 that the
   crops sent to the model are perfect, so this is the Hindi model hallucinating.
   clean_hindi() now applies a Devanagari orthography guard (ocr_extract.
   normalize_devanagari): a consonant takes one vowel sign, a halant must follow
   a consonant, no leading vowel sign / trailing halant ... impossible marks are dropped.
 * Dropped letters ("नन्दू" -> "न्दू", "जयपाल" -> "यपाल"): a vocabulary built from
   YOUR OWN confident reads (names and relation names share it) repairs a RARE token
   that is exactly one inserted/removed character away from a COMMON one. Never
   substitutions (रीना/रीता are never merged), thresholds in VOCAB_*. Every change
   is flagged name_vocab_fixed / relation_name_vocab_fixed and logged in the new
   autocorrect_log column. The vocabulary persists in name_vocab.json (--vocab) and
   improves with every AC you run -- keep using the same file. --no-vocab disables it.
 * --folder now processes files in NATURAL order (1, 2, 3 ... 10 ... 100), so --limit 30
   takes parts 1-30, and _combined.csv rows are written in that order even when
   workers finish out of order.
 * --upload s3://bucket/prefix [--s3-endpoint URL] [--presign-hours N]: uploads
   <prefix>/<folder>/_combined.csv (+ _run_info.json, _failed.txt) with server-side
   encryption; works with S3 and S3-compatible stores (Cloudflare R2, MinIO, B2).
   Needs `pip install boto3` and credentials in the environment / ~/.aws (never in code).
 * Batch mode writes _combined.partial.csv while running (kept if you interrupt),
   then the final _combined.csv after the vocabulary pass.

--- v11 notes ---

WHAT CHANGED IN v11 -- ACs other than 86 (AC 1, AC 2, ...): missing/truncated
serial numbers (24, 25 -> "2"), one-letter / half names, stray . , - in names,
and "works only for AC 86's DPI"
----------------------------------------------------------------------
Measured on your PDFs: AC 86 pages are embedded at 949 px wide, AC 1 / AC 2 at
1187 px (exactly 1.25x), and every pixel constant in the engine was tuned on
949 px. v9/v10 shrank each page to 949 px, which made cards findable but
thinned the strokes below the hard-coded thresholds. v11 stops resizing pages:

 1. SCALE-AWARE ENGINE (ocr_extract.py). The page is kept at native size; the
    scale s = page_width / 949 is computed per page (snapped to 1.0 within 2%,
    so AC 86 takes the identical code path) and EVERY size constant (card
    size filter, morphology kernels, line-band heights, colon widths, crop
    offsets, border-wipe width, destamp reach, pixel-count thresholds) is
    scaled by it. Template matching shrinks the *region* to the 300x124
    reference grid (templates are never touched), and the OCR crops are
    enlarged by 3/s so the recogniser always sees text at the same physical
    size. Cards found at ANY width (tested 700-1800 px). If the first scale
    guess finds nothing, nearby scales are swept instead of returning an empty
    CSV; a PDF with 0 cards now raises an error instead of writing nothing.
 2. SELF-CHECK on the first body page of every PDF: prints scale, card count and
    layout_ok rate; if layout_ok < 90% it retries with alternative scales.
 3. SERIAL BOX FOUND BY ITS BORDERS. Root cause of "24 -> 2": the serial box is
    NOT a fixed width (its right border sits anywhere from x=76 to x=104 in
    AC-86 pixels), and the old fixed crop 30:95 cut digits off or kept the
    border; ordinary cards whose border landed at 60<x<85 were even
    misclassified as "addition" cards and cropped to 46 px. Also, border
    wiping next to the edge deleted a trailing "1" / the stem of a "4".
    Now the crop is cut strictly inside the located border lines, with no
    wipe; addition cards are recognised by having two boxes.
 4. SERIAL SEQUENCE REPAIR (repair_serials) replaces the single-neighbour
    rescue: any run of truncated / duplicated / unreadable serials is repaired
    from the 1,2,3... sequence around it; suspicious ones are first re-read from
    alternative crops and only accepted when the re-read equals the expectation.
 5. CARD ORDER: cards on a page were sorted with y//40 buckets, which could swap
    cards of one row when the row straddled a multiple of 40 px; rows are now
    clustered by real y-distance.
 6. NAMES: a name / relation name that cleans to < 2 characters or scores < 0.60
    is re-cropped (different window heights, start point left of the colon,
    destamped copy) and re-read by both Hindi models; clean_hindi is now a
    Devanagari WHITELIST (drops . , - _ | : ; quotes, digits, dandas, Latin,
    anywhere in the string, not just at the ends).
 7. text_lines() tries progressively looser passes before giving up, and the
    last-resort positions are proportional to the card height, not absolute.

AC 86 regression: every non-serial OCR input crop, template label and list type
is bit-identical to v10 on all cards of the AC 86 sample (2 cards that v10 had
flagged layout_unusual now get a real layout). Serial crops change only in the
way described in (3). Name TEXT can differ from v10 only through the whitelist
cleaning (item 6), which is the point.

--- previous notes (v10 and older) ---
ocr_extract_paddleocr.py (v10) -- fast extraction pipeline, rebuilt on the
architecture from your colleague's parse_roll.py.

WHAT CHANGED IN v10 -- half-letter/garbage names and misread serial
numbers on ACs other than 86 (confirmed: AC 1, AC 2), even after the
v9 empty-CSV fix
----------------------------------------------------------------------
The v9 fix (rescaling a page to the calibrated 949px width before card
detection) made AC 1/AC 2 produce cards again, but their review flags
told a second story: ~40% of rows were "layout_unusual" on both, and
one card's serial read "23" and the next also read "2" instead of "24"
(card_on_page 23 and 24 both landing on CSV serial_no 2). Checked AC 2
page 3, card 23's actual serial crop directly -- "23" is clearly legible
in the crop, so this wasn't a bad card either.

Root cause: page_images_calibrated() (added in v9) was rescaling with
cv2.INTER_AREA, a box-filter downsample. Confirmed on AC 1 page 3's
first card: INTER_AREA thinned the "00" house-number digits just enough
to drop that line's ink band below text_lines()'s >=4px-tall minimum,
silently deleting a whole line from the card -- which pushes layout_ok
to False and falls back to hardcoded y-positions that don't match this
card's real layout, chopping other fields (names, serial) mid-glyph.
That's exactly the half-letter-name symptom. Switching to cv2.INTER_CUBIC
keeps that band intact (confirmed directly: all 4 lines detected,
layout_ok=True) without changing AC 86 at all -- AC 86 never gets
resized in the first place (already within tolerance of 949px), so the
interpolation choice was never visible there, only on ACs that actually
need rescaling.

Measured effect (same AC 1 / AC 2 PDFs, text_lines()/process_pdf() run
directly): layout_ok went from ~60% to 100% on both, and review_reason's
"layout_unusual" rate dropped from ~40% to 0%. Re-run your test batches
for AC 1/2 (and spot-check a few other non-86 ACs) to confirm this holds
up on real OCR output, not just the pixel-level check done here (no
network access to the OCR model hosters from this sandbox).

ALSO IN v10 -- batch mode (--folder) now writes ONE combined CSV only
----------------------------------------------------------------------
Previously every PDF got its own CSV in the output folder (N PDFs -> N
CSV files + _combined.csv), which is a lot of small files you never use
individually across tens of thousands of PDFs. Workers now return their
DataFrame directly instead of writing a per-PDF CSV; the main process
appends each one straight into _combined.csv as it finishes. Only
_combined.csv is written now -- nothing else changes about --folder's
other behavior (progress logging, --limit, --recursive, --workers).

WHAT ELSE CHANGED IN v9 -- some ACs (confirmed: AC 1) produce 0 cards /
an empty output CSV, with no error
----------------------------------------------------------------------
Root cause: page_images() in ocr_extract.py pulls each page's single
embedded image out of the PDF at ITS NATIVE RESOLUTION (no fixed DPI) --
and find_cards()/cut_card()/match() all assume one FIXED pixel size for
everything (card ≈300x124px, page ≈949px wide, calibrated against AC 86's
PDFs). AC 1's PDFs happen to embed their page image at 1.25x that
resolution (1187px wide instead of 949px) -- confirmed directly on the
file you sent: find_cards() found 0 cards on every page at native
resolution, but found all 30/page once the page was rescaled to 949px
wide. Different ACs' source PDFs can apparently come out of ECI's
generator at different native resolutions, and this pipeline silently
produces an empty CSV for any AC where that resolution isn't ~949px --
no error, no warning, just nothing in the output.

Fixed with a new page_images_calibrated() in ocr_extract.py (added
alongside page_images(), not editing that byte-exact function) that
rescales any page to the calibrated 949px width before find_cards() ever
sees it. ocr_extract_paddleocr.py's process_pdf() now calls that instead
of page_images() directly. Verified AC 86 is byte-identical before/after
(no regression -- its pages are already ~949px so nothing gets rescaled),
and that AC 1's PDF now extracts cards correctly end-to-end instead of 0.

If you still see 0 cards for some other AC after this, it likely means
that AC's native resolution is far enough from 949px that scaling alone
isn't the full story (e.g. a genuinely different physical page layout)
-- send me that PDF and I'll check the same way.

WHAT CHANGED IN v9 -- wrong names/relation names on conjunct-heavy words
(न्द्र, र्, etc.), e.g. "रवींद्र" read as "र्वीद्र", "रूप चन्द्र" read as
"रूप चनद्र"
----------------------------------------------------------------------
Found via the names_crosscheck review: errors clustered almost entirely
around one family of Devanagari conjuncts (न्द्र / ेन्द्र / र्द्र -- the
"-endra" cluster common in names like Rajendra, Devendra, Ravindra,
Dharmendra...). Checked the actual crops ocr_extract.py hands to the OCR
model for several of these flagged cards (AC 86, part 10, page 3, cards
3/5/11/16) by saving them to PNG and looking directly -- the crops are
correctly positioned and perfectly legible (e.g. "रूप चन्द्र" and "रवींद्र"
are both clearly readable in the saved crop). So this was NOT a cropping
or line-detection bug -- the single Hindi recognition model itself was
confidently misreading an correctly-cropped, legible conjunct.

Fixed by running Hindi name/relation_name fields through BOTH available
Hindi models (devanagari_PP-OCRv5_mobile_rec and _v3_mobile_rec) instead
of just whichever loads first, and keeping whichever reading scores
higher per field -- the same way a stamped vs. destamped "~" alt crop was
already being picked by score. When the two models disagree even if the
winning one still has a high score, the row is flagged
"name_model_disagreement" / "relation_name_model_disagreement" so it
surfaces for manual review even in cases the single-model confidence
score alone wouldn't have caught (a model can be confidently wrong).
Automatically falls back to single-model behavior (no behavior change)
if only one Hindi model is available in your environment. Costs roughly
2x the Hindi OCR time; English/numeric fields (serial, epic, age) are
unaffected and stay on one model.

This does not guarantee perfect names -- it's a second opinion, not a
different crop -- but it should catch a meaningful share of these, and
flags the rest for the needs_review column instead of silently shipping
a wrong name at high confidence.

WHAT CHANGED IN v8 -- status_code showing "?" and gender going blank
on real, correctly-matched cards
----------------------------------------------------------------------
Found by reviewing real output from v7 run on multiple PDFs: 8 rows
whose watermark stamp clearly showed status letter "Q" came out with
status_code "?" instead, and a card for "काजल" with an unmistakable
female photo/card came out with gender left blank entirely.

Root cause: the status-letter and gender template matches (pixel
matching against the embedded reference images, not OCR) DID find the
right answer in both cases -- but the match score/margin fell just under
the confidence cutoff, and the old code threw the match away entirely
or replaced it with "?" instead of just flagging it as unreliable. That
silently destroyed a known-correct value AND, for status letters, the
whole reason a card is shown as deleted -- exactly the piece of
information this project cares about getting right.

Fixed to match how relation_type already handled this correctly: always
keep the best-matched value (the letter/gender the template matcher
actually found), and only use review_reason ("status_letter_unclear" /
"gender_unclear") to flag it when the match was below the confidence
threshold, instead of discarding it. "?" is now reserved for the
genuinely-no-match case (nothing scored at all), not "scored, but not
quite enough."

WHAT CHANGED FROM v6 -- WHY v6 WAS SLOW
----------------------------------------------------------------------
v6 (and everything before it) called PaddleOCR's FULL pipeline
(detection model + recognition model) once per PAGE. The detection model
is the expensive part -- it's a full neural network scanning the whole
page image to find where text might be, before any recognition even
starts. That's what made each PDF take 15-30 minutes.

v7 drops the detection model entirely, using the same approach your
colleague's script (parse_roll.py) proved runs in 2-5 min/PDF on the
exact same card template:
  - Classical OpenCV (contours, thresholding) finds the cards and text
    lines on each page directly from pixel geometry -- no detection model.
  - Fixed-vocabulary fields (relation type, gender, status letter, the
    DELETED stamp, list type) are read by pixel template matching, not
    OCR at all.
  - Only the free-text fields (name, relation name, house no, age, EPIC,
    serial) go through OCR -- and only the lightweight recognition-only
    model, applied to small pre-cropped field images.
  - Every crop for an ENTIRE PDF is collected first, then sent to the
    recognition model in just 2 big batched calls (one Hindi, one
    English) instead of one OCR call per page or per card.
All of that lives in ocr_extract.py (v2) now -- see its docstring. This
file is the orchestration layer: turning those raw per-field OCR reads
into the clean CSV columns you've been reviewing, using the same
accumulated fixes from v2-v6 (NAME_CORRECTIONS, Devanagari-digit
normalization, watermark-fragment stripping), batch/parallel processing
across many PDFs, and your colleague's exact output schema.

WATERMARKED / DELETED CARDS -- now handled structurally, not by a
separate rescan pass. v6 needed a slow "rescan this one card at high
zoom" fallback because a stamped card sometimes gave the page-level OCR
pass nothing at all to parse. v7 doesn't have that problem: every stamped
card is OCR'd TWICE per field (once on the original crop, once on a
"destamped" copy with the watermark pixels removed, via ocr_extract.destamp)
and whichever read scores higher is kept -- cheaply, because these are
small field-level crops, not whole extra page passes. The old
--no-rescan/--rescan-scale flags are gone; there's nothing to toggle.

STATUS CODE MEANINGS -- now confirmed for all five codes (M and Q were
UNCONFIRMED as of v6; your colleague's script confirms M = "missing
(लापता)" and Q = "ineligible (अयोग्य)").

SPEED -- target is under 5 minutes/PDF on your machine, matching your
colleague's reported range. UNVERIFIED like every version before it --
I cannot run PaddleOCR in this sandbox. Please run it on a handful of
PDFs you've already hand-checked and report back actual timing +
accuracy before pointing this at the full folder.

SETUP
-----
    pip install "paddleocr>=3.1" paddlepaddle pymupdf opencv-python pandas numpy

USAGE -- single file
---------------------
    python ocr_extract_paddleocr.py sample.pdf --out out.csv

USAGE -- a whole folder of PDFs, in parallel
----------------------------------------------
    python ocr_extract_paddleocr.py --folder downloads/86 --out ac86_out --workers 2 --limit 30

  Processes PDFs directly inside downloads/86 (add --recursive for
  subfolders). Writes ONE combined CSV, ac86_out/_combined.csv (v10+; no per-PDF CSVs).
  --limit caps how many PDFs are processed, for
  testing on a subset before committing to the whole folder.

--workers N runs N PDFs in parallel, each in its own process with its
own copy of the OCR models, same as v6's --folder mode. --cpu-threads
caps the math-library thread pool PER WORKER PROCESS -- the fix for the
system freeze you hit earlier with uncapped threads. Keep workers *
cpu-threads at or below your machine's core count; start low (2 workers
x 4 threads on your M3 Max) and watch Activity Monitor before raising it.

By default this skips pages 1-2 (metadata/photos) and the last page of
each PDF, same rule as before, carried over from parse_roll.py's own
`if pno < 2 or pno == npg - 1: continue`.
"""
import argparse
import collections
import concurrent.futures
import csv
import os
import pathlib
import re
import sys
import time

import cv2
import numpy as np
import pandas as pd

import ocr_extract as base

VERSION = "v13"

TPL_MIN, TPL_MARGIN = base.TPL_MIN, base.TPL_MARGIN
REVIEW_BELOW = 0.90  # confirmed against your colleague's CSV in earlier versions

# base.STATUS already has all five codes confirmed (E/S/R from your CSV,
# M/Q confirmed via your colleague's script -- see v7 docstring above).
STATUS_MEANING = base.STATUS
KNOWN_STATUS_CODES = {"E", "S", "R", "M", "Q"}

# Hard-coded corrections for specific OCR misreads YOU have personally
# verified against the real card (not guesses). Add more as you confirm
# them -- key is the misread OCR text, value is the correct text. Applied
# as an exact match on the whole name/relation_name field only, never a
# partial/substring replace, so it can't accidentally corrupt an
# unrelated name that happens to contain the same substring.
NAME_CORRECTIONS = {
    "मुत्ञा": "मुन्ना",
}

# Extra safety net on top of ocr_extract.clean_hindi: catches a DELETED-
# stamp fragment that landed in the MIDDLE of a name/relation_name
# instead of at the end.
WATERMARK_FRAGMENT_RE = re.compile(r"\b[A-Z]{0,2}ETED\b|\bDELET\w*\b|हटाय\w*", re.IGNORECASE)

FIELDNAMES = [
    "serial_no", "name", "relation_type", "relation_name", "house_no", "age", "gender",
    "epic_no", "status_code", "status_meaning", "deleted", "deleted_stamp",
    "list_type", "addition_section_no", "file_name", "pdf_page", "card_on_page",
    "confidence", "needs_review", "review_reason",
    # extra audit columns beyond your colleague's schema -- safe to delete in Excel
    "raw_ocr_body", "stamp_px", "autocorrect_log",
]

SHORT_NAME_BELOW = 2       # a cleaned name shorter than this is treated as a mis-crop
RETRY_SCORE_BELOW = 0.60   # ... and so is a name/relation_name read below this score
SECOND_OPINION_BELOW = 0.90  # v5 read scoring below this (or short / invalid start) also gets the v3 model
V3_NEEDS_MARGIN = 0.15       # a v3 reading must beat the v5 reading by this much to be used (v3 is overconfident)
_OIL = str.maketrans("OIL|", "0111")


def apply_name_corrections(value: str) -> str:
    return NAME_CORRECTIONS.get(value, value)


def clean_name_field(raw: str) -> str:
    """Whitelist clean: Devanagari only (see base.clean_hindi). The watermark
    fragment regex still runs first because it matches Latin text, which the
    whitelist would otherwise strip down to a leftover Devanagari fragment."""
    t = base.clean(raw)
    t = WATERMARK_FRAGMENT_RE.sub("", t)
    t = base.clean_hindi(t)
    return apply_name_corrections(t)


def serial_digits(txt):
    dg = re.sub(r"\D", "", txt.upper().translate(_OIL))
    return int(dg) if dg else None


# ---------------------------------------------------------------------------
# Serial-number sequence repair
# ---------------------------------------------------------------------------

def repair_serials(reads, groups, win=6, near=4, min_votes=3):
    """Serials in a roll run 1, 2, 3 ... in card order, so a misread (a digit
    cut off: 24 -> '2'; a duplicate: 23 -> '2', 24 -> '2'; an unreadable
    crop) can be repaired from the cards around it.

    For every card, offset = read - position-in-its-list. Cards that are
    read correctly all share the same offset. A card is overwritten only when
      * its own offset differs from the local majority offset M (taken over a
        +-`win` window, needing at least `min_votes` agreeing cards), AND
      * the majority also holds on BOTH sides of it (>=2 cards each side, fewer
        only at the very start/end of a list).
    That handles runs of several consecutive bad reads, while a real change
    in numbering (a section that restarts or skips) is left alone, because
    there the cards on the two sides disagree with each other.
    `groups` keeps lists apart (main vs addition).
    Returns (fixed, flags) -- flags[i] == "serial_corrected" where overwritten."""
    n = len(reads)
    fixed, flags = list(reads), [None] * n
    by = {}
    for i, g in enumerate(groups):
        by.setdefault(g, []).append(i)
    for idxs in by.values():
        m = len(idxs)
        offs = [(reads[i] - k) if reads[i] is not None else None for k, i in enumerate(idxs)]
        for k, i in enumerate(idxs):
            around = [offs[j] for j in range(max(0, k - win), min(m, k + win + 1)) if j != k and offs[j] is not None]
            if not around:
                continue
            M, cnt = collections.Counter(around).most_common(1)[0]
            if cnt < min_votes or offs[k] == M:
                continue
            left = sum(1 for j in range(max(0, k - near), k) if offs[j] == M)
            right = sum(1 for j in range(k + 1, min(m, k + near + 1)) if offs[j] == M)
            if left >= min(2, k) and right >= min(2, m - 1 - k) and left + right >= 3 and M + k >= 1:
                fixed[i], flags[i] = M + k, "serial_corrected"
    return fixed, flags


# ---------------------------------------------------------------------------
# Per-PDF processing -- collects every field crop for the whole PDF, OCRs
# each language in big batched calls (via ocr_extract.run_rec), then
# cleans + assembles rows.
# ---------------------------------------------------------------------------

def _cut_page(g, boxes, s):
    """Cut every card of a page (native resolution) and run the pixel-level
    analysis. Returns a list of (card_image, scale_used, crops, info)."""
    out = []
    for (x, y, w, h) in boxes:
        img, se = base.card_at_scale(g[y:y + h, x:x + w], s)
        stamped = base.stamp_px(img, se) > 60
        f, info = base.cut_card(img, base.destamp(img, s=se) if stamped else None, se)
        info["stamped"] = stamped
        out.append((img, se, f, info))
    return out


def _layout_rate(cut):
    return sum(1 for c in cut if c[3]["layout_ok"]) / len(cut) if cut else 0.0


def extract_cards(pdf_path, log=print):
    """Pixel stage: detect + cut all cards of all body pages, at the PDF's own
    native resolution (no page resizing -- everything downstream is scale-aware).
    Includes the automatic self-check on the first body page."""
    cards, hint_ratio, checked, pages_seen = [], None, False, 0
    t0 = time.time()
    for pno, g, npg in base.page_images(str(pdf_path)):
        if pno < 2 or pno == npg - 1:
            continue
        pages_seen += 1
        ps = base.page_scale(g)
        boxes, s = base.detect_cards(g, ps * hint_ratio if hint_ratio else None)
        cut = _cut_page(g, boxes, s)
        if not checked and cut:
            checked = True
            rate = _layout_rate(cut)
            log(f"    self-check: page {g.shape[1]}x{g.shape[0]}px, scale s={s:.3f}, "
                f"{len(boxes)} cards, layout_ok {rate:.0%}")
            if rate < 0.90:
                best = (rate, len(cut), s, boxes, cut)
                for f_ in (0.97, 1.03, 0.94, 1.06, 0.90, 1.10):
                    b2 = base.find_cards(g, s * f_)
                    if not b2:
                        continue
                    c2 = _cut_page(g, b2, s * f_)
                    cand = (_layout_rate(c2), len(c2), s * f_, b2, c2)
                    if cand[:2] > best[:2]:
                        best = cand
                    if cand[0] >= 0.98:
                        break
                rate, _, s, boxes, cut = best
                log(f"    self-check retry: using s={s:.3f}, layout_ok {rate:.0%}"
                    + ("" if rate >= 0.90 else "  [WARN: layout still unreliable -- check this PDF]"))
            hint_ratio = s / ps
        log(f"    cutting cards: page {pno + 1}/{npg} ({len(cut)} cards)")
        if not cut:
            log(f"    [warn] page {pno + 1}: no cards found")
        for ci, (img, se, f, info) in enumerate(cut):
            cards.append(dict(pdf_page=pno + 1, card_on_page=ci + 1, f=f, info=info, img=img, s=se, hi={}, en={}))
    if not cards:
        raise RuntimeError(
            f"{pathlib.Path(pdf_path).name}: 0 cards found on {pages_seen} body page(s) -- the page image "
            f"geometry is not recognised (not silently writing an empty CSV). Send this PDF for inspection.")
    return cards, time.time() - t0


def process_pdf(pdf_path: pathlib.Path, hi_models, en, log=print, dump_review=False, review_dir=None,
                second_opinion="auto"):
    """hi_models: list of (model, name) tuples -- one or more loaded Hindi
    TextRecognition models (see base.load_rec_all). Every Hindi field crop is
    OCR'd through ALL of them and the highest-scoring reading wins (see get()
    below); a disagreement between models is flagged for review.

    v11 additions on top of that:
      * resolution-independent cutting (extract_cards / base.* take a scale)
      * serial boxes located by their borders; serial sequence repair; a
        truncated/odd serial is re-read from alternative crops
      * names / relation names that came out too short or low-scoring are
        re-cropped (colon mis-detection, window height) and re-read
      * whitelist cleaning of names (no stray . , - etc.)
    """
    meta = base.parse_filename(pdf_path)
    t0 = time.time()
    cards, _ = extract_cards(pdf_path, log)

    jobs = {"hi": [], "en": []}
    for k, cd in enumerate(cards):
        F, s = cd["f"], cd["s"]
        mask_of = lambda fld: F.get("mask:" + fld.rstrip("~"))
        for fld in ("name", "relation_name", "house_no", "name~", "relation_name~", "house_no~"):
            if fld in F:
                im = base.prep(F[fld], mask_of(fld), s)
                if im is not None:
                    jobs["hi"].append((k, fld, im))
        for fld in ("serial", "epic", "age", "house_no", "section", "age~", "house_no~"):
            if fld in F:
                im = base.prep(F[fld], mask_of(fld), s)
                if im is not None:
                    jobs["en"].append((k, fld, im))

    log(f"    {len(cards)} cards found, running OCR...")
    # English/numeric fields: one model.
    imgs = [j[2] for j in jobs["en"]]
    if imgs:
        for (k, fld, _), res in zip(jobs["en"], base.run_rec(en, imgs, "English")):
            cards[k]["en"][fld] = res

    # Hindi fields: the primary model (v5) reads every crop. The second model (v3)
    # is only consulted where v5 looks unsure -- see "second opinion" below.
    # Model 0's result is stored under the plain field name ("name"); model i>=1
    # under "name#i".
    imgs = [j[2] for j in jobs["hi"]]
    if imgs and hi_models:
        for (k, fld, _), res in zip(jobs["hi"], base.run_rec(hi_models[0][0], imgs, "Hindi")):
            cards[k]["hi"][fld] = res

    # ------------------------------------------------------------------
    # Candidate picking (shared by the retry stage and the row builder)
    # ------------------------------------------------------------------
    _re_cache = {}

    def key_re(k):
        pat = _re_cache.get(k)
        if pat is None:
            # k, k~ (destamped crop), k@j (re-crop variant j), each optionally #i (Hindi model i)
            pat = _re_cache[k] = re.compile(r"^" + re.escape(k) + r"(~|@\d+)?(#\d+)?$")
        return pat

    def get(d, k):
        pat = key_re(k)
        best = ("", 0.0)
        for key, val in d.items():
            if pat.match(key) and val[1] > best[1]:
                best = val
        return best

    def _nlen(txt):
        return len(clean_name_field(txt).replace(" ", ""))

    def _best(items):
        """Highest score wins (first one on ties); a reading that cleans to < SHORT_NAME_BELOW
        characters only wins if nothing longer exists."""
        best, best_ok = ("", 0.0), ("", 0.0)
        for _, val in items:
            if val[1] > best[1]:
                best = val
            if val[1] > best_ok[1] and _nlen(val[0]) >= SHORT_NAME_BELOW:
                best_ok = val
        if _nlen(best[0]) < SHORT_NAME_BELOW and best_ok[1] > 0:
            return best_ok
        return best

    def get_name(d, k):
        """Pick the reading for a name / relation_name field.

        The primary (v5) model's readings -- plain crop, destamped crop, re-crops -- are
        compared among themselves. The secondary (v3) model is NOT allowed to win by
        score alone: on real AC 2 data it won ~25% of the disagreements and was wrong in
        almost all of them (dropped conjuncts, moved matras, merged words, a stray 'े',
        and confident 1.00 scores on wrong text). It is used only when v5 produced nothing
        usable, or when it beats v5 by V3_NEEDS_MARGIN.
        Finally, if two readings are identical apart from spaces, the one with spaces wins
        (first name / last name stay separate: 'लालो मोची', not 'लालोमोची')."""
        pat = key_re(k)
        items = [(key, val) for key, val in d.items() if pat.match(key)]
        prim = [it for it in items if "#" not in it[0]]
        sec = [it for it in items if "#" in it[0]]
        p, s2 = _best(prim), _best(sec)
        p_ok = p[1] > 0 and _nlen(p[0]) >= SHORT_NAME_BELOW and p[1] >= 0.5
        if p_ok:
            chosen = s2 if (s2[1] >= p[1] + V3_NEEDS_MARGIN and _nlen(s2[0]) >= SHORT_NAME_BELOW) else p
        elif p[1] > 0 and s2[1] < p[1] + V3_NEEDS_MARGIN:
            chosen = p
        else:
            chosen = _best(items)
        ct = clean_name_field(chosen[0])
        if ct and " " not in ct:
            for _, val in items:
                c2 = clean_name_field(val[0])
                if " " in c2 and c2.replace(" ", "") == ct and val[1] >= 0.8:
                    return (val[0], chosen[1])
        return chosen

    # ------------------------------------------------------------------
    # Second opinion: the v3 model reads name / relation_name crops only where v5 is
    # unsure (score < SECOND_OPINION_BELOW, too short, or a word that cannot start that way).
    # second_opinion="all" restores v11's behaviour (v3 on everything), "off" disables it.
    # Skipping v3 on confident reads also removes roughly half of the Hindi OCR time.
    # ------------------------------------------------------------------
    n_second = 0
    if len(hi_models) > 1 and second_opinion != "off":
        weak = {}

        def is_weak(k, f):
            key = (k, f)
            if key not in weak:
                txt, sc_ = get_name(cards[k]["hi"], f)
                ct = clean_name_field(txt)
                weak[key] = (second_opinion == "all" or sc_ < SECOND_OPINION_BELOW
                             or len(ct.replace(" ", "")) < SHORT_NAME_BELOW or base.invalid_word_start(ct))
            return weak[key]

        sel = [(k, fld, im) for (k, fld, im) in jobs["hi"]
               if fld.rstrip("~") in ("name", "relation_name") and is_weak(k, fld.rstrip("~"))]
        n_second = len({j[0] for j in sel})
        if sel:
            for i, (model, mname) in enumerate(hi_models[1:], 1):
                for (k, fld, _), res in zip(sel, base.run_rec(model, [j[2] for j in sel],
                                                              f"Hindi(#{i} {mname}) second opinion on {n_second} cards")):
                    cards[k]["hi"][f"{fld}#{i}"] = res

    # ------------------------------------------------------------------
    # Retry 1: serial numbers that break the 1,2,3... sequence are re-read
    # from alternative crops; a re-read is only accepted if it equals what
    # the neighbours say the serial must be.
    # ------------------------------------------------------------------
    reads = [serial_digits(get(cd["en"], "serial")[0]) for cd in cards]
    groups = [cd["info"]["list_type"] for cd in cards]
    expected, _ = repair_serials(reads, groups)
    redo = [k for k in range(len(cards)) if expected[k] is not None and reads[k] != expected[k]]
    n_reread = 0
    if redo:
        sj = []
        for k in redo:
            cd = cards[k]
            for crop in base.serial_alt_crops(cd["img"], cd["s"], cd["info"]["geom"].get("lay")):
                im = base.prep(crop, None, cd["s"])
                if im is not None:
                    sj.append((k, im))
        if sj:
            res = base.run_rec(en, [j[1] for j in sj], f"Serial re-read ({len(redo)} cards)")
            done = set()
            for (k, _), (txt, sc_) in zip(sj, res):
                if k not in done and serial_digits(txt) == expected[k] and sc_ >= 0.5:
                    cards[k]["en"]["serial"] = (txt, sc_)
                    cards[k]["serial_reread"] = True
                    done.add(k)
            n_reread = len(done)
        reads = [serial_digits(get(cd["en"], "serial")[0]) for cd in cards]
    fixed_serial, serial_flag = repair_serials(reads, groups)

    # ------------------------------------------------------------------
    # Retry 2: names / relation names that look mis-cropped (too short or low
    # score) are re-cut with different window heights / start points and
    # re-read by every Hindi model; get_name() then picks the best.
    # ------------------------------------------------------------------
    rj = []
    for k, cd in enumerate(cards):
        H = cd["hi"]
        for fld in ("name", "relation_name"):
            pat = key_re(fld)
            if not any(pat.match(x) for x in H):
                continue                     # nothing was inked on this line -> nothing to retry
            txt, sc_ = get_name(H, fld)
            ct0 = clean_name_field(txt)
            if len(ct0.replace(" ", "")) < SHORT_NAME_BELOW or sc_ < RETRY_SCORE_BELOW or base.invalid_word_start(ct0):
                srcs = [cd["img"]] + ([base.destamp(cd["img"], s=cd["s"])] if cd["info"]["stamped"] else [])
                j = 0
                for src in srcs:
                    for crop in base.line_variants(cd["img"], cd["info"]["geom"], fld, src):
                        im = base.prep(crop, None, cd["s"])
                        if im is not None:
                            rj.append((k, f"{fld}@{j}", im)); j += 1
    n_retry = len({(j[0]) for j in rj})
    if rj:
        for i, (model, mname) in enumerate(hi_models):
            if i >= 1 and second_opinion == "off":
                break
            for (k, fld, _), res in zip(rj, base.run_rec(model, [j[2] for j in rj],
                                                           f"Hindi re-crop ({n_retry} cards)" if i == 0 else f"Hindi(#{i}) re-crop")):
                cards[k]["hi"][fld if i == 0 else f"{fld}#{i}"] = res

    # ------------------------------------------------------------------
    # Row assembly
    # ------------------------------------------------------------------
    rows, prev, prev_type = [], None, None
    for idx, cd in enumerate(cards):
        H, E, info = cd["hi"], cd["en"], cd["info"]
        why, sc = [], []

        def model_disagreement(d, k):
            """True if two different Hindi models produced different
            non-empty cleaned readings for this field, both with some real
            confidence (a confidently-wrong single-model read doesn't trigger
            low_ocr_confidence, but a second model disagreeing does)."""
            texts = set()
            for key, (txt, score) in d.items():
                if key == k or key.startswith(k + "#"):
                    t = clean_name_field(txt)
                    if t and score >= 0.5:
                        texts.add(t)
            return len(texts) > 1

        st, ss = get(E, "serial")
        serial = fixed_serial[idx]
        sc.append(ss)
        if serial is None:
            why.append("serial_unreadable")
        else:
            if serial_flag[idx]:
                why.append(serial_flag[idx])
            elif cd.get("serial_reread"):
                why.append("serial_reread")
            if prev is not None and prev_type == info["list_type"] and serial != prev + 1:
                why.append("serial_out_of_sequence")
        prev, prev_type = serial, info["list_type"]

        code = ""
        if info["marker_ink"] > 8:
            lab, s_, m = info["marker"]
            if lab:
                # Use the best-matched letter even when its score/margin is
                # below the confidence cutoff -- flag it instead of discarding it.
                code = lab
                if not (s_ >= TPL_MIN - 0.15 and m >= TPL_MARGIN):
                    why.append("status_letter_unclear")
            else:
                # No template cleared even the loosest match at all -- genuinely unknown.
                code = "?"
                why.append("status_letter_unclear")
        stamped = info["stamp_px"] > 60

        e_raw, es = get(E, "epic")
        epic = base.fix_epic(e_raw)
        sc.append(es)
        if not re.match(r"^[A-Z]{3}\d{7}$", epic):
            why.append("epic_format")

        name_raw, ns = get_name(H, "name")
        name = clean_name_field(name_raw)
        sc.append(ns)
        if not name:
            why.append("name_missing")
        elif len(name.replace(" ", "")) < SHORT_NAME_BELOW:
            why.append("name_too_short")
        elif model_disagreement(H, "name"):
            why.append("name_model_disagreement")
        if base.invalid_word_start(name):
            why.append("name_invalid_start")
        rname_raw, rs = get_name(H, "relation_name")
        rname = clean_name_field(rname_raw)
        sc.append(rs)
        if rname and len(rname.replace(" ", "")) < SHORT_NAME_BELOW:
            why.append("relation_name_too_short")
        elif rname and model_disagreement(H, "relation_name"):
            why.append("relation_name_model_disagreement")
        if base.invalid_word_start(rname):
            why.append("relation_name_invalid_start")

        lab, s_, m = info["relation"]
        if s_ >= TPL_MIN and m >= TPL_MARGIN:
            rel = base.REL[lab]
        else:
            rel = base.REL.get(lab, "")
            why.append("relation_type_uncertain")

        (hh, hs), (he, hes) = get(H, "house_no"), get(E, "house_no")
        house, hsc = (he, hes) if hes > hs + 0.05 else (hh, hs)
        house = base.clean(house.translate(base.DEV))
        sc.append(hsc)

        at, ascr = get(E, "age")
        d = re.sub(r"\D", "", at.translate(base.DEV).upper().replace("O", "0"))
        age = int(d) if d else None
        sc.append(ascr)
        if age is None or not 18 <= age <= 120:
            why.append("age")

        glab, gs, gm = info["gender"]
        # Keep the best-matched gender even at low confidence, just flag it.
        gender = glab or ""
        if not (gs >= TPL_MIN - 0.1 and gm >= TPL_MARGIN):
            why.append("gender_unclear (possibly तृतीय लिंग)")

        sec = None
        if "section" in E:
            dsec = re.sub(r"\D", "", E["section"][0])
            sec = int(dsec) if dsec else None
        if not info["layout_ok"]:
            why.append("layout_unusual")
        conf = round(min(sc), 3) if sc else 0.0
        if conf < REVIEW_BELOW:
            why.append("low_ocr_confidence")

        raw_ocr_body = " | ".join(
            f"{k}={v[0]!r}({v[1]:.2f})" for d in (H, E) for k, v in d.items() if "@" not in k
        )

        rows.append({
            "serial_no": serial, "name": name, "relation_type": rel, "relation_name": rname,
            "house_no": house, "age": age, "gender": gender, "epic_no": epic,
            "status_code": code, "status_meaning": STATUS_MEANING.get(code, "?" if code else ""),
            "deleted": "Y" if (stamped or code in KNOWN_STATUS_CODES) else "N",
            "deleted_stamp": "Y" if stamped else "N",
            "list_type": info["list_type"], "addition_section_no": sec,
            "file_name": pdf_path.name, "pdf_page": cd["pdf_page"], "card_on_page": cd["card_on_page"],
            "confidence": conf, "needs_review": "Y" if why else "N", "review_reason": "|".join(why),
            "raw_ocr_body": raw_ocr_body, "stamp_px": info["stamp_px"], "autocorrect_log": "",
        })

    n_fix = sum(1 for f_ in serial_flag if f_)
    log(f"    done in {time.time() - t0:.0f}s, {len(rows)} cards "
        f"(serials repaired: {n_fix}, re-read: {n_reread}; names re-cropped: {n_retry}; "
        f"v3 second opinion on {n_second} cards)")

    df = pd.DataFrame(rows, columns=FIELDNAMES)
    df["serial_no"] = df["serial_no"].astype("Int64")
    return df


# ---------------------------------------------------------------------------
# Multi-PDF, multi-process batch mode
# ---------------------------------------------------------------------------

def _limit_threads(n: int):
    """Caps OMP/MKL/OpenBLAS thread pools in THIS process. Must run before
    `from paddleocr import TextRecognition` -- these libraries read the env
    vars once at init time. With --workers N, each worker process calls
    this separately; N processes x this many threads each is the real
    total thread count to keep under your core count. Same fix that solved
    the trackpad-freeze problem in v6."""
    n = str(max(1, n))
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = n


_WORKER = {}


def _worker_init(device, cpu_threads, second_opinion="auto"):
    _limit_threads(cpu_threads)
    _WORKER["so"] = second_opinion
    hi_models = base.load_rec_all(base.HI_MODELS, device)
    if not hi_models:
        raise RuntimeError(f"none of {base.HI_MODELS} could be loaded")
    en, _ = base.load_rec(base.EN_MODELS, device)
    _WORKER["hi"], _WORKER["en"] = hi_models, en
    _WORKER["hname"] = "+".join(m for _, m in hi_models)


def _worker_process_pdf(pdf_path_str):
    """Returns the extracted DataFrame itself (no per-PDF CSV file) --
    run_folder() appends it straight into the one combined CSV as each
    worker finishes, instead of every worker writing its own CSV that then
    has to be read back and concatenated. With ~500 files/AC and up to
    178,725 files statewide, a CSV-per-PDF was real, unnecessary disk
    churn for files nobody needs individually."""
    pdf_path = pathlib.Path(pdf_path_str)
    tag = f"[{pdf_path.name}]"

    def log(msg):
        print(f"{tag} {msg}", flush=True)

    t0 = time.time()
    df = process_pdf(pdf_path, _WORKER["hi"], _WORKER["en"], log=log, second_opinion=_WORKER.get("so", "auto"))
    elapsed = time.time() - t0
    print(f"{tag} done: {len(df)} cards in {elapsed:.1f}s "
          f"({elapsed/max(len(df),1):.2f}s/card)", flush=True)
    return pdf_path.name, df


# ---------------------------------------------------------------------------
# Natural file order  (1, 2, 3 ... 10 ... 100, not 1, 10, 100, 101 ...)
# ---------------------------------------------------------------------------

def natural_key(path):
    s = str(path)
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------------------
# Name vocabulary correction
#
# Why: the Hindi recogniser sometimes drops a letter from a conjunct or the
# first letter of a word ("नन्दू" -> "न्दू", "जयपाल" -> "यपाल") even though the
# crop is perfect (verified on AC 2). Voter-roll names repeat heavily, so the
# data itself is the best dictionary: a token that is RARE in the roll but is
# exactly one inserted/deleted code point away from a COMMON token is almost
# certainly a misread of it. Every change is flagged (<field>_vocab_fixed) and
# logged in the autocorrect_log column so it can be audited / reverted.
#
# Deliberately conservative: only the insertion of one dropped (non-final)
# character is tried -- never deletions or substitutions, so real look-alike
# names (रीना / रीता, राम / रामू, देव / देवी) are never merged. It stays off until
# the vocabulary holds VOCAB_MIN_TOKENS tokens.
# ---------------------------------------------------------------------------
VOCAB_RARE_MAX = 4        # a token seen this many times (or fewer) may be corrected (siblings repeat a misread father's name)
VOCAB_STRONG_MIN = 8      # ... but only into a token seen at least this often
VOCAB_RATIO = 6           # ... and at least this many times more often than itself
VOCAB_MIN_LEN = 3         # never touch very short tokens
VOCAB_MIN_TOKENS = 4000   # corrector stays OFF until the vocabulary has this many tokens (a single PDF is
                          # too small to tell a misread from a real rare name: it once "fixed" रामू -> राम)


class VocabCorrector:
    """Restores ONE dropped character. Deliberately never deletes or substitutes
    (रामू/राम, रीना/रीता are all real names) and never adds a missing LAST letter
    (देव/देवी, प्रीत/प्रीतम are real names; the model's failure is losing a letter
    inside a word or at its start, e.g. नन्दू -> न्दू, जयपाल -> यपाल, कुमार -> कमार)."""

    def __init__(self, counts):
        self.c = counts
        self.enabled = sum(counts.values()) >= VOCAB_MIN_TOKENS
        self.strong = {t: n for t, n in counts.items() if n >= VOCAB_STRONG_MIN} if self.enabled else {}
        self.ins = collections.defaultdict(list)       # token-with-one-char-removed -> strong tokens
        for tok in self.strong:
            for i in range(len(tok) - 1):               # i < last index: the final character is never "restored"
                self.ins[tok[:i] + tok[i + 1:]].append(tok)

    def fix(self, tok):
        n = self.c.get(tok, 0)
        if not self.enabled or len(tok) < VOCAB_MIN_LEN or n > VOCAB_RARE_MAX:
            return tok
        cands = [v for v in set(self.ins.get(tok, ())) if v != tok and self.strong[v] >= VOCAB_RATIO * max(n, 1)]
        if not cands:
            return tok
        cands.sort(key=lambda v: -self.strong[v])
        if len(cands) > 1 and self.strong[cands[0]] < 2 * self.strong[cands[1]]:
            return tok                                                             # ambiguous -> leave it
        return cands[0]


def build_vocab(df, base_counts=None):
    """Token counts from confident rows (names and relation names share a vocabulary)."""
    c = collections.Counter(base_counts or {})
    conf = pd.to_numeric(df["confidence"], errors="coerce").fillna(0)
    rr = df["review_reason"].fillna("")
    ok = (conf >= REVIEW_BELOW) & ~rr.str.contains("too_short|name_missing|vocab_fixed", regex=True)
    for col in ("name", "relation_name"):
        for v in df.loc[ok, col].fillna(""):
            for tok in str(v).split():
                if len(tok) >= 2:
                    c[tok] += 1
    return c


def apply_vocab(df, counts):
    """Returns (df, n_fixed). Operates on a copy of the name columns."""
    vc = VocabCorrector(counts)
    n_fixed = 0
    df = df.copy()
    for col in ("name", "relation_name"):
        for i, v in zip(df.index, df[col].fillna("").tolist()):
            toks = str(v).split()
            fixed = [vc.fix(x) for x in toks]
            if fixed != toks:
                n_fixed += 1
                df.at[i, col] = " ".join(fixed)
                log_ = "; ".join(f"{col}: {a}>{b}" for a, b in zip(toks, fixed) if a != b)
                prev = str(df.at[i, "autocorrect_log"]) if str(df.at[i, "autocorrect_log"]) not in ("", "nan") else ""
                df.at[i, "autocorrect_log"] = (prev + "; " if prev else "") + log_
                rr = str(df.at[i, "review_reason"]) if str(df.at[i, "review_reason"]) not in ("", "nan") else ""
                df.at[i, "review_reason"] = (rr + "|" if rr else "") + f"{col}_vocab_fixed"
                df.at[i, "needs_review"] = "Y"
    return df, n_fixed


def load_vocab(path):
    try:
        import json
        with open(path, encoding="utf-8") as fh:
            return collections.Counter({k: int(v) for k, v in json.load(fh).items()})
    except FileNotFoundError:
        return collections.Counter()
    except Exception as exc:
        print(f"[vocab] could not read {path}: {exc} -- starting empty", file=sys.stderr)
        return collections.Counter()


def save_vocab(path, counts):
    import json
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(counts.most_common()), fh, ensure_ascii=False)
    os.replace(tmp, path)


def finalize_names(df, vocab_path, log=print):
    """Vocabulary pass over a finished DataFrame; updates the persistent vocab file."""
    if vocab_path is None or df.empty:
        return df
    loaded = load_vocab(vocab_path)
    counts = build_vocab(df, loaded)
    df, n = apply_vocab(df, counts)
    save_vocab(vocab_path, build_vocab(df, loaded))
    log(f"  name vocabulary: {n} rows auto-corrected "
        f"(vocab file {vocab_path}: {len(counts)} tokens before this run's additions: {len(loaded)})")
    return df


# ---------------------------------------------------------------------------
# Cloud upload (S3 or any S3-compatible store: Cloudflare R2, MinIO, Backblaze B2...)
# ---------------------------------------------------------------------------

def upload_files(files, dest, endpoint=None, presign_hours=0):
    """files: {local_path: key_suffix}. dest: s3://bucket/optional/prefix.
    Credentials come from the normal AWS chain (env vars AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY, ~/.aws/credentials, an IAM role) -- never put keys in
    this file. Never raises: the CSVs are already saved locally."""
    try:
        import boto3
    except ImportError:
        print("[upload] boto3 not installed: pip install boto3   (files are saved locally)", file=sys.stderr)
        return
    m = re.match(r"^s3://([^/]+)/?(.*)$", dest)
    if not m:
        print(f"[upload] --upload must look like s3://bucket/prefix, got {dest!r}", file=sys.stderr)
        return
    bucket, prefix = m.group(1), m.group(2).strip("/")
    try:
        client = boto3.client("s3", endpoint_url=endpoint) if endpoint else boto3.client("s3")
        extra = {} if endpoint else {"ServerSideEncryption": "AES256"}
        for local, suffix in files.items():
            key = f"{prefix}/{suffix}" if prefix else suffix
            client.upload_file(str(local), bucket, key, ExtraArgs=extra or None)
            print(f"[upload] s3://{bucket}/{key}")
            if presign_hours and str(local).endswith(".csv"):
                url = client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                                    ExpiresIn=int(min(presign_hours, 168) * 3600))
                print(f"[upload]   download link (valid {min(presign_hours, 168)}h): {url}")
    except Exception as exc:
        print(f"[upload] FAILED ({type(exc).__name__}: {exc}) -- files are still saved locally", file=sys.stderr)


# ---------------------------------------------------------------------------
# Multi-PDF, multi-process batch mode
# ---------------------------------------------------------------------------

def run_folder(folder: pathlib.Path, out_dir: pathlib.Path, workers: int,
               recursive: bool, device, cpu_threads: int, limit=None,
               vocab_path=None, upload=None, s3_endpoint=None, presign_hours=0, second_opinion="auto"):
    # natural order: ...-1-WI, ...-2-WI, ...-10-WI, ...-100-WI  (plain sorted() gave 1, 10, 100, 101 ...)
    pdfs = sorted(folder.rglob("*.pdf") if recursive else folder.glob("*.pdf"),
                  key=lambda p: natural_key(p.relative_to(folder)))
    if not pdfs:
        print(f"No PDFs found in {folder} (recursive={recursive})")
        return
    total_found = len(pdfs)
    if limit:
        pdfs = pdfs[:limit]
    out_dir.mkdir(parents=True, exist_ok=True)
    combined_path = out_dir / "_combined.csv"
    partial_path = out_dir / "_combined.partial.csv"
    if partial_path.exists():
        partial_path.unlink()
    limit_note = f" (limited from {total_found} found)" if limit else ""
    print(f"Found {total_found} PDFs, processing {len(pdfs)}{limit_note}, in natural order "
          f"({pdfs[0].name} ... {pdfs[-1].name}). "
          f"Running with {workers} worker process(es), {cpu_threads} CPU threads each "
          f"(~{workers * cpu_threads} threads total -- watch Activity Monitor)...")
    print(f"Writing one combined CSV only (no per-PDF CSVs): {combined_path}")

    header_written = False
    total_rows = processed = 0
    failed = []
    done, next_i = {}, 0
    t_start = time.time()
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, initializer=_worker_init, initargs=(device, cpu_threads, second_opinion)) as pool:
        futures = {pool.submit(_worker_process_pdf, str(pdf)): i for i, pdf in enumerate(pdfs)}
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            try:
                done[i] = fut.result()
            except Exception as exc:
                done[i] = None
                failed.append(pdfs[i].name)
                print(f"  ! {pdfs[i].name} failed: {exc}", file=sys.stderr)
            # rows are appended strictly in file order, even when workers finish out of order
            while next_i in done:
                res = done.pop(next_i)
                next_i += 1
                if res is None:
                    continue
                name, df = res
                df.to_csv(partial_path, mode="a" if header_written else "w",
                          header=not header_written, index=False, encoding="utf-8-sig")
                header_written = True
                total_rows += len(df)
                processed += 1
                print(f"  [{processed}/{len(pdfs)}] {name}: {len(df)} cards "
                      f"(combined total: {total_rows})", flush=True)

    if header_written:
        if vocab_path:
            allrows = pd.read_csv(partial_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            allrows = finalize_names(allrows, vocab_path)
            allrows.to_csv(combined_path, index=False, encoding="utf-8-sig")
            partial_path.unlink()
        else:
            os.replace(partial_path, combined_path)
    else:
        # nothing succeeded -- still write an (empty, header-only) combined
        # CSV so downstream tooling that expects the file to exist doesn't break
        pd.DataFrame(columns=FIELDNAMES).to_csv(combined_path, index=False, encoding="utf-8-sig")
    print(f"\nDone: {processed}/{len(pdfs)} PDFs processed, {total_rows} total cards "
          f"in {time.time() - t_start:.0f}s.")
    if failed:
        (out_dir / "_failed.txt").write_text("\n".join(failed), encoding="utf-8")
        print(f"FAILED PDFs ({len(failed)}) listed in {out_dir / '_failed.txt'}", file=sys.stderr)
    print(f"Combined CSV: {combined_path}")

    if upload:
        import json
        info = {"version": VERSION, "folder": str(folder), "pdfs_processed": processed, "pdfs_failed": failed,
                "cards": total_rows, "first_pdf": pdfs[0].name, "last_pdf": pdfs[-1].name,
                "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        info_path = out_dir / "_run_info.json"
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        tag = folder.resolve().name
        files = {combined_path: f"{tag}/_combined.csv", info_path: f"{tag}/_run_info.json"}
        if failed:
            files[out_dir / "_failed.txt"] = f"{tag}/_failed.txt"
        upload_files(files, upload, s3_endpoint, presign_hours)


def main():
    print(f"ocr_extract_paddleocr.py {VERSION}")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", help="Single PDF file (omit if using --folder)")
    ap.add_argument("--folder", help="Process every PDF in this folder instead of a single file")
    ap.add_argument("--recursive", action="store_true", help="With --folder, also descend into subfolders")
    ap.add_argument("--device", default=None, help="e.g. 'cpu' -- passed straight to PaddleOCR's TextRecognition")
    ap.add_argument("--workers", type=int, default=1,
                     help="Parallel worker processes for --folder mode (default 1 = sequential, like a "
                          "single call to parse_roll.py). Each worker loads its own copy of the OCR models.")
    ap.add_argument("--cpu-threads", type=int, default=4,
                     help="CPU threads PER WORKER PROCESS for the math libraries underneath PaddleOCR "
                          "(default 4). workers * cpu-threads is your real total thread count -- keep it "
                          "at or below your machine's core count. This is the fix for the system-wide "
                          "freeze an uncapped thread count caused in v6 testing.")
    ap.add_argument("--limit", type=int,
                     help="--folder mode only: process at most this many PDFs (e.g. --limit 30) -- "
                          "use this to test on a subset before running the whole folder.")
    ap.add_argument("--out", required=True,
                     help="Single-file mode: output CSV path. --folder mode: output DIRECTORY "
                          "(one combined CSV: _combined.csv)")
    ap.add_argument("--vocab", default="name_vocab.json",
                     help="Persistent name vocabulary (JSON) used to repair dropped letters in names; it grows "
                          "with every run, so keep using the same file across ACs (default: name_vocab.json "
                          "in the current folder).")
    ap.add_argument("--second-opinion", choices=("auto", "all", "off"), default="auto",
                     help="When the second Hindi model (v3) reads name crops: 'auto' (default) = only where the "
                          "primary v5 reading is unsure -- v3 is much less accurate and roughly doubles Hindi OCR "
                          "time; 'all' = every crop (v11 behaviour); 'off' = never.")
    ap.add_argument("--no-vocab", action="store_true", help="Disable the vocabulary correction pass.")
    ap.add_argument("--upload", metavar="s3://BUCKET/PREFIX",
                     help="After the run, upload the CSV to S3 (or any S3-compatible store) under "
                          "PREFIX/<folder-name>/_combined.csv. Needs `pip install boto3` and AWS credentials "
                          "in the environment / ~/.aws.")
    ap.add_argument("--s3-endpoint", help="Custom endpoint for S3-compatible stores (Cloudflare R2, MinIO, B2).")
    ap.add_argument("--presign-hours", type=int, default=0,
                     help="With --upload, also print a temporary download link valid this many hours (max 168).")
    args = ap.parse_args()
    vocab_path = None if args.no_vocab else args.vocab

    if not args.pdf and not args.folder:
        ap.error("provide a PDF file, or --folder for batch mode")

    if args.folder:
        run_folder(pathlib.Path(args.folder), pathlib.Path(args.out), args.workers,
                   args.recursive, args.device, args.cpu_threads, args.limit,
                   vocab_path, args.upload, args.s3_endpoint, args.presign_hours, args.second_opinion)
        return

    _limit_threads(args.cpu_threads)
    print("Loading OCR models (Hindi + English TextRecognition, recognition-only -- no detection model)...")
    t0 = time.time()
    hi_models = base.load_rec_all(base.HI_MODELS, args.device)
    if not hi_models:
        raise RuntimeError(f"none of {base.HI_MODELS} could be loaded")
    en, _ = base.load_rec(base.EN_MODELS, args.device)
    if len(hi_models) == 1 and "v3" in hi_models[0][1].lower():
        print("[warn] Hindi model is the older PP-OCRv3 only. For better names: pip install -U paddleocr",
              file=sys.stderr)
    elif len(hi_models) > 1:
        print(f"  Hindi: running {len(hi_models)} models as a vote ({', '.join(m for _, m in hi_models)}) "
              f"-- highest-confidence reading wins per field.")
    print(f"  loaded in {time.time()-t0:.1f}s")

    pdf_path = pathlib.Path(args.pdf)
    t0 = time.time()
    df = process_pdf(pdf_path, hi_models, en, second_opinion=args.second_opinion)
    elapsed = time.time() - t0
    print(f"Done: {len(df)} cards in {elapsed:.1f}s ({elapsed/max(len(df),1):.2f}s/card)")

    df = finalize_names(df, vocab_path)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Wrote {args.out}")
    if args.upload:
        upload_files({pathlib.Path(args.out): f"{pdf_path.stem}.csv"}, args.upload, args.s3_endpoint, args.presign_hours)


if __name__ == "__main__":
    main()