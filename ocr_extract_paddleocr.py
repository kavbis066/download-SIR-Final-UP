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
import functools
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

VERSION = "v15"

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

TESS_COLS = ["name_tess", "relation_name_tess"]     # internal: "text<TAB>confidence"; consumed and dropped by finalize_names()
SHORT_NAME_BELOW = 2       # a cleaned name shorter than this is treated as a mis-crop
RETRY_SCORE_BELOW = 0.60   # ... and so is a name/relation_name read below this score
SECOND_OPINION_BELOW = 0.90  # v5 read scoring below this (or short / invalid start) also gets the v3 model
V3_NEEDS_MARGIN = 0.15       # a v3 reading must beat the v5 reading by this much to be used (v3 is overconfident)
_OIL = str.maketrans("OIL|", "0111")



# ---------------------------------------------------------------------------
# Second reader for names: Tesseract's Hindi model (v15)
# ---------------------------------------------------------------------------
# Audit of 1,344 AC 86 cards (2,688 name / relation_name fields): the PaddleOCR Devanagari model
# returns a confidently wrong name on ~8% of fields -- it drops conjuncts and repeated letters
# (रश्मी -> रशमी, लक्ष्मी -> लक्षम, ज्ञान -> जान, गुडडी -> गुडी, पप्पू -> पपू, सन्नो -> सत्रो) with scores of
# 0.9-1.00, so no confidence threshold can catch them, and the crop itself is perfect. Tesseract's
# Hindi LSTM reads these conjuncts correctly (but has its own, different, mistakes: व <-> द, a spurious
# nukta / chandrabindu, short junk words). The two engines disagree on ~14% of the fields; the final
# reading is chosen by arbitrate_name() below. It needs `tesseract` and hin.traineddata on the machine
# (brew install tesseract tesseract-lang); without them the pipeline runs exactly as v14.
import math
import shutil
import subprocess

TESS = {"ok": None, "dir": None, "bin": None}
TESS_MIN_WORD_CONF = 0.60      # a trailing 1-2 character word below this is junk (a stray dot / mark read as a letter)


def tess_setup(tessdata=None, quiet=False):
    """Locate tesseract + the Hindi data; sets TESS["ok"]. Safe to call repeatedly (per worker)."""
    if TESS["ok"] is not None:
        return TESS["ok"]
    TESS["ok"] = False
    exe = shutil.which("tesseract")
    if not exe:
        if not quiet:
            print("[tesseract] not installed -- names are read by PaddleOCR only (brew install tesseract tesseract-lang)", file=sys.stderr)
        return False
    cands = [tessdata, os.environ.get("OCR_TESSDATA"), os.environ.get("TESSDATA_PREFIX"), None,
             "/opt/homebrew/share/tessdata", "/usr/local/share/tessdata", "/usr/share/tesseract-ocr/5/tessdata",
             "/usr/share/tesseract-ocr/4.00/tessdata", str(pathlib.Path.home() / "tessdata")]
    for cand in cands:
        cmd = [exe, "--list-langs"] + (["--tessdata-dir", cand] if cand else [])
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.split()
        except Exception:
            continue
        if "hin" in out:
            TESS.update(ok=True, dir=cand, bin=exe)
            if not quiet:
                print(f"[tesseract] Hindi reader ready ({exe}, data: {cand or 'default'})", file=sys.stderr)
            return True
    if not quiet:
        print("[tesseract] installed but the Hindi data (hin.traineddata) was not found -- names are read by "
              "PaddleOCR only. Put hin.traineddata in a folder and pass --tessdata FOLDER", file=sys.stderr)
    return False


def tess_read(im):
    """One name-line crop -> (text, min word confidence 0-1) or None. Single-threaded on purpose
    (OMP_THREAD_LIMIT=1): the workers already use every core."""
    if not TESS["ok"] or im is None:
        return None
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) if im.ndim == 3 else im
    g = cv2.copyMakeBorder(g, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    ok, png = cv2.imencode(".png", g)
    if not ok:
        return None
    cmd = [TESS["bin"], "stdin", "stdout", "-l", "hin", "--psm", "7"] + (["--tessdata-dir", TESS["dir"]] if TESS["dir"] else []) + ["-c", "tessedit_create_tsv=1"]
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    try:
        out = subprocess.run(cmd, input=png.tobytes(), capture_output=True, timeout=60, env=env).stdout.decode("utf-8", "replace")
    except Exception:
        return None
    words = []
    for line in out.splitlines()[1:]:
        f = line.split("\t")
        if len(f) >= 12 and f[0] == "5" and f[11].strip():
            try:
                words.append((f[11].strip(), float(f[10]) / 100.0))
            except ValueError:
                pass
    return tess_clean_words(words)


def tess_clean_words(words):
    """words [(text, conf)] -> (text, confidence). Drops stray 1-2 character words at the edges that Tesseract read
    with low confidence (a dot above the line becomes 'हि', 'नि', ':' ...)."""
    words = [(w, c) for w, c in words if re.sub(r"[\s:;.,_'\"|\-]", "", w)]
    while words and len(words[-1][0]) <= 2 and words[-1][1] < TESS_MIN_WORD_CONF and len(words) > 1:
        words.pop()
    while words and len(words[0][0]) <= 2 and words[0][1] < TESS_MIN_WORD_CONF and len(words) > 1:
        words.pop(0)
    if not words:
        return ("", 0.0)
    return (" ".join(w for w, _ in words), min(c for _, c in words))


def apply_name_corrections(value: str) -> str:
    return NAME_CORRECTIONS.get(value, value)


def clean_name_field(raw: str, rules: bool = True) -> str:
    """Whitelist clean: Devanagari only (see base.clean_hindi). The watermark
    fragment regex still runs first because it matches Latin text, which the
    whitelist would otherwise strip down to a leftover Devanagari fragment.
    rules=True also applies the deterministic model-error rules (fix_name_rules)."""
    t = base.clean(raw)
    t = WATERMARK_FRAGMENT_RE.sub("", t)
    t = t.replace("\u0909\u094d", "\u0909\u0930\u094d")      # 'उ्मिला' (र् lost) -> उर्मिला; a halant cannot follow a vowel
    t = t.replace("\u200c", "").replace("\u200d", "")      # zero-width joiners (Tesseract emits them)
    t = base.clean_hindi(t)
    t = strip_label_fragment(t)
    t = apply_name_corrections(t)
    return fix_name_rules(t)[0] if rules else t


# The relation_name crop sometimes starts inside the printed label ("पिता का नाम:" / "पति का नाम:"), so its tail
# ("नाम", "म:", "ाम") ends up in front of the name. A real name never starts with one of these words.
LABEL_FRAGMENTS = {"\u092e", "\u092e\u0903", "\u0928\u093e\u092e", "\u093e\u092e", "\u0917\u092e", "\u092e\u0930", "\u092e\u093f",
                   "\u092e\u0902", "\u0938\u092e\u093f\u0903", "\u0939\u092e", "\u0903"}


def strip_label_fragment(t):
    toks = t.split(" ")
    while len(toks) > 1 and toks[0] in LABEL_FRAGMENTS:
        toks.pop(0)
    return " ".join(toks)


# ---------------------------------------------------------------------------
# Deterministic fixes for PaddleOCR's systematic Devanagari mistakes (v14)
# ---------------------------------------------------------------------------
# Each of these was verified against the printed cards of AC 86 parts 1-8 (6096 cards):
# the crop is perfect and the model returns the SAME wrong text every time, so re-reading
# cannot help -- the text itself has to be repaired. Every rule only fires on a pattern
# that is not a valid Hindi spelling, so it cannot damage a correct name.
_CONS = "\u0915-\u0939\u0958-\u095F"
_HAL = "\u094d"
_MATRA = "\u093e-\u094c\u0901-\u0903\u093c"
_SINGH_END = re.compile("(?:\u0938\u0938\u0902\u0939|\u0938\u093f\u0902\u0939\u0902|\u0938\u093f\u0947\u0939|\u0938\u093f\u0939|\u0938\u0902\u0939|\u0938\u093f\u0902)$")
_ENDRA_BARE = re.compile(f"(?<=[{_CONS}{_MATRA}])(?<!{_HAL})([{_CONS}])(?=\u0928{_HAL}\u0926{_HAL}\u0930)")


@functools.lru_cache(maxsize=500000)
def _fix_token(t):
    """-> (fixed_token, (rule names)). Order matters. Cached: the same words come up thousands of times."""
    hits = []

    def sub(rule, pat, rep, tok, flags=0):
        new = re.sub(pat, rep, tok, flags=flags)
        if new != tok:
            hits.append(rule)
        return new

    # 1. word cannot start with a doubled consonant: CTC merged the two glyphs (प्पू -> पप्पू)
    m = re.match(f"^([{_CONS}]){_HAL}\\1", t)
    if m and len(t) >= 2:
        t = m.group(1) + t
        hits.append("initial_geminate")
    # 2. initial न्द (न्दू -> नन्दू)
    t = sub("initial_nd", f"^(?=\u0928{_HAL}\u0926(?!{_HAL}))", "\u0928", t)
    # 3. धमेन्द्र -> धर्मेन्द्र (र् lost)
    t = sub("dharm", "^\u0927\u092e\u0947(?=\u0928{h}[\u0930\u0926])".replace("{h}", _HAL), "\u0927\u0930\u094d\u092e\u0947", t)
    # 4. न्र -> न्द्र (the द lost); never when a vowel sign follows (मुन्री is something else)
    t = sub("endra_d", f"\u0928{_HAL}\u0930(?![{_MATRA}\u0926])", f"\u0928{_HAL}\u0926{_HAL}\u0930", t)
    t = sub("endra_ee", f"(?<=\u0947)\u0928{_HAL}\u0930\u0940$", f"\u0928{_HAL}\u0926{_HAL}\u0930", t)
    # 4b'. महन्री -> महेन्द्री, सुरेनत्र -> सुरेन्द्र (े and द lost)
    t = sub("endra_ri", f"(?<=[{_CONS}])(?<!{_HAL})([{_CONS}])\u0928{_HAL}\u0930\u0940$", "\\1\u0947\u0928"+_HAL+"\u0926"+_HAL+"\u0930\u0940", t) if len(t) >= 5 else t
    t = sub("endra_n", "(?<=\u0947)\u0928\u0924\u094d\u0930", f"\u0928{_HAL}\u0926{_HAL}\u0930", t)
    # 4b. न्न read as न्र when a vowel sign follows (मुन्नी -> मुन्री, चुन्नीलाल, अन्नू, किन्ना); the -endra case
    #     (...ेन्री) was handled above, so it never gets here
    t = sub("nn", f"(?<!\u0947)\u0928{_HAL}\u0930(?=[\u093e\u093f\u0940\u0942\u094b])", f"\u0928{_HAL}\u0928", t)
    # 4c. the ं / न् before द्र is lost: हरेद्र, गजेनद्र (printed हरेन्द्र / गजेन्द्र); वी + न्द्र lost रे
    t = sub("endra_n", "(?<=\u0947)\u0928\u0926\u094d\u0930", f"\u0928{_HAL}\u0926{_HAL}\u0930", t)
    t = sub("endra_n", "(?<=\u0947)\u0926\u094d\u0930(?!\u093f)", f"\u0928{_HAL}\u0926{_HAL}\u0930", t)
    t = sub("endra_vi", f"^\u0935\u0940(?=\u0928{_HAL}\u0926{_HAL}\u0930)", "\u0935\u0940\u0930\u0947", t)
    # 4d. न्न misread as ज्न (मुज्नी -> मुन्नी); लक्मी -> लक्ष्मी; पुष्पा -> पुष्षा / पुष्या ("लक्म" and "पुष्षा" are not spellings)
    # 4c'. त्र / न्त्र where न्द्र / न्न was printed (विरन्त्र -> विरेन्द्र, मुत्रा -> मुन्ना). "रन्त्र" after a bare
    #      र/ह/ज/ग/ल/क/ध/ब/भ/श/स/प/व is never a spelling (मन्त्र, यन्त्र, तन्त्र are not in this list)
    t = sub("ntra", "(?<=[\u0930\u0939\u091c\u0917\u0932\u0915\u0927\u092c\u092d\u0936\u0938\u092a\u0935])\u0928" + _HAL + "\u0924" + _HAL + "\u0930(?=$)", "\u0928" + _HAL + "\u0926" + _HAL + "\u0930", t) if len(t) >= 5 else t
    t = sub("nn", "^([\u092e\u091a\u091f\u091b])\u0941\u0924" + _HAL + "\u0930(?=[\u093e\u0940\u0942])", "\\1\u0941\u0928" + _HAL + "\u0928", t)
    t = sub("nn", "(?<=\u0941)\u091c" + _HAL + "\u0928(?=[\u093e\u0940\u0942])", "\u0928" + _HAL + "\u0928", t)
    t = sub("laxmi", "^\u0932\u0915" + _HAL + "\u092e", "\u0932\u0915" + _HAL + "\u0937" + _HAL + "\u092e", t)
    t = sub("pushpa", "^\u092a\u0941\u0937" + _HAL + "[\u0937\u092f]\u093e$", "\u092a\u0941\u0937" + _HAL + "\u092a\u093e", t)
    # 5. ...रन्द्र -> ...रेन्द्र (े lost); not after च (चन्द्र is right) and never inside a conjunct
    if len(t) >= 5:
        new = _ENDRA_BARE.sub(lambda m_: m_.group(1) if m_.group(1) == "\u091a" else m_.group(1) + "\u0947", t)
        if new != t:
            t = new
            hits.append("endra_e")
    # 6. -endra Singh/Kumar are always separate words
    t = sub("endra_split", f"(\u0928{_HAL}\u0926{_HAL}\u0930)(?=\u0938\u093f|\u0938\u0902|\u0915\u0941\u092e|\u0915\u092e)", "\\1 ", t)
    return t, tuple(hits)


@functools.lru_cache(maxsize=500000)
def _fix_tail(tok):
    hits = []
    new = _SINGH_END.sub("\u0938\u093f\u0902\u0939", tok)
    if new != tok:
        hits.append("singh")
        tok = new
    new = re.sub("(?:^\u0915\u0915\u092e\u093e\u0930|(?<![\u0941\u0915])(?:\u0915\u094d\u0930\u092e\u093e\u0930|\u0915\u092e\u093e\u0930))$", "\u0915\u0941\u092e\u093e\u0930", tok)
    if new != tok:
        hits.append("kumar")
        tok = new
    return tok, tuple(hits)


# CTC merged the two identical first syllables: ममता -> मता, बबीता -> बीता. As a first word before देवी/रानी/कुमारी
# these are never names.
DOUBLED_FIRST = {"\u092e\u0924\u093e": "\u092e\u092e\u0924\u093e", "\u092c\u0940\u0924\u093e": "\u092c\u092c\u0940\u0924\u093e"}


def fix_name_rules(text, want_log=False):
    """-> (fixed_text, [(before, after, rule), ...]). Applied per word."""
    if not text:
        return text, []
    out, log = [], []
    words = text.split(" ")
    for tok in words:
        t, h1 = _fix_token(tok)
        h1 = list(h1)
        for _ in range(2):                      # rules can enable each other (धमेद्रे -> धमेन्द्रे -> धर्मेन्द्रे)
            t2, h = _fix_token(t)
            if t2 == t:
                break
            t, h1 = t2, h1 + list(h)
        parts = []
        for piece in t.split(" "):          # rule 6 may have split the token
            p2, h2 = _fix_tail(piece)
            parts.append(p2)
            h1 += list(h2)
        t = " ".join(parts)
        if t != tok:
            log.append((tok, t, "+".join(dict.fromkeys(h1))))
        out.append(t)
    # ममता lost its first म (CTC collapse of the two identical syllables): 'मता देवी' is never a name
    FEM = ("\u0926\u0947\u0935\u0940", "\u0930\u093e\u0928\u0940", "\u0915\u0941\u092e\u093e\u0930\u0940")      # देवी रानी कुमारी
    if len(out) >= 2 and out[1] in FEM and out[0] in DOUBLED_FIRST:
        log.append((out[0], DOUBLED_FIRST[out[0]], "doubled_first"))
        out[0] = DOUBLED_FIRST[out[0]]
    return " ".join(out), log


def suspicious_name(text):
    """True if the (rule-free) cleaned text contains a pattern fix_name_rules would repair --
    used to prefer another model's reading when there is one."""
    return bool(text) and fix_name_rules(text)[0] != text


# ---------------------------------------------------------------------------
# house_no: pick the best of ALL candidate readings and make them valid (v14)
# ---------------------------------------------------------------------------
_OM = "\u0950"                       # the symbol ॐ -- the Hindi model's usual misread of a digit
_HOUSE_JUNK = re.compile(r"[^0-9A-Za-z\u0900-\u097F /\-" + _OM + "]")
_HOUSE_DIGITS = re.compile(r"^\d{1,5}(?:\s?[/\-]\s?\d{1,4})*$")
_HOUSE_DIGIT_LETTER = re.compile(r"^\d{1,5}\s?([\u0900-\u097F]{1,3}|[A-Za-z])$")
_DEV_LETTERS = re.compile(r"[\u0904-\u0939\u0958-\u095F]")


def house_norm(txt):
    """One raw reading -> (clean_text, has_om). Devanagari digits become ASCII, junk goes,
    o/O next to a digit becomes 0, Hindi words (village names) stay Hindi."""
    t = base.clean((txt or "").translate(base.DEV))
    t = _HOUSE_JUNK.sub(" ", t)
    t = re.sub(r"(?<=\d)[oO](?=\d|$|\s)|(?<=^)[oO](?=\d)|(?<=\s)[oO](?=\d)", "0", t)
    t = re.sub(r"^[oO]$", "0", t)
    t = re.sub(r"(?<![0-9])[A-Za-z]+(?![0-9])", lambda m: m.group(0) if re.fullmatch(r"[A-D]", m.group(0)) else " ", t)
    t = re.sub(r"(?<=\d)([A-Za-z])", lambda m: m.group(1).upper(), t)
    t = re.sub(r"\s+", " ", t).strip(" -/\u094d")
    return t, _OM in t


def _house_class(t):
    """-> (weight, kind). weight 0 = unusable."""
    if not t:
        return 0.0, "empty"
    if _HOUSE_DIGITS.match(t):
        return 1.0, "digits"
    if _HOUSE_DIGIT_LETTER.match(t):
        return (0.6 if re.search(r"[A-Za-z]$", t) else 0.95), "digit+letter"
    if len(_DEV_LETTERS.findall(t)) >= 3 and not re.search(r"[A-Za-z]", t):
        return 0.9, "place"
    return 0.0, "junk"


def _om_repair(t):
    """ॐ is how the model renders a mangled digit: alone it is the 0, beside another digit it is
    the Devanagari ७ (both verified on the cards: 'ॐ' = 0, 'ॐ७' = ७७). Always flagged."""
    digits = re.sub(r"[^0-9]", "", t)
    return t.replace(_OM, "7" if digits else "0")


_BENIGN = re.compile(r"[\s\"'`.,:;|_]")


def resolve_house(items):
    """items: [(raw_text, score, source), ...], source 'hi' (Hindi models) or 'en' (English model),
    one entry per model/variant. -> (value, score, [flags]).

    What the AC 86 audit showed (4 cards checked by eye: the English model was right and the
    old pick wrong -- Hindi read 81 as 8, 182 as 12, 02 as 2, 38 as 8):
      * plain ASCII digits: the English reading is the reliable one;
      * Devanagari digits (१५३) and Devanagari letters (13अ, 10ब) exist only in the Hindi
        reading -- English turns them into junk ('$43') or a wrong digit ('133'), so an English
        reading that is junk-contaminated is down-weighted, and one that merely extends or equals
        the digits of a Hindi 'number+letter' reading is dropped;
      * ॐ (and similar symbols) never survive: they are repaired or outvoted."""
    parsed = []
    for raw, sc, src in items:
        if not raw:
            continue
        t, om = house_norm(raw)
        removed = _BENIGN.sub("", raw.translate(base.DEV))
        junk = len(re.sub(r"[0-9A-Za-z\u0900-\u097F/\-]", "", removed))
        parsed.append((t, om, sc, src, junk, bool(re.search("[\u0966-\u096F]", raw))))
    # a Hindi reading that is a clean number written in Devanagari digits: the English model
    # cannot read those, and what it returns looks plausible ('383' for ' १४३') -- ignore it.
    hi_dev = any(src == "hi" and dv and not om and sc >= 0.6 and _house_class(t)[1] == "digits"
                 for t, om, sc, src, junk, dv in parsed)
    parsed = [x for x in parsed if not (hi_dev and x[3] == "en")]
    hi_dl = [t for t, om, sc, src, j, dv in parsed if src == "hi" and not om and sc >= 0.5
             and _HOUSE_DIGIT_LETTER.match(t) and re.match(r"^\d+", t) and not re.search(r"[A-Za-z]$", t)]
    cands = []
    for t, om, sc, src, junk, dv in parsed:
        note = ""
        if src == "en" and any(re.fullmatch(r"\d+", t) and (t == re.match(r"\d+", h).group(0)
                                                             or (t.startswith(re.match(r"\d+", h).group(0)) and len(t) <= len(re.match(r"\d+", h).group(0)) + 2))
                               for h in hi_dl):
            continue                                   # English read the Devanagari letter as a digit / dropped it
        if om:
            t, note = _om_repair(t), "om_repaired"
            t = re.sub(r"\s+", " ", t).strip()
        w, kind = _house_class(t)
        if note:
            w *= 0.8
        if junk:
            w *= 0.6
        if w > 0:
            cands.append((w * sc, t, note))
    if not cands:
        best = max(((sc, t) for t, om, sc, src, j, dv in parsed if t), default=(0.0, ""))
        return best[1], best[0] * 0.5, ["house_no_unclear"] if best[1] else []
    agree = collections.Counter(c[1] for c in cands)
    cands = [(c[0] + (0.1 if agree[c[1]] > 1 else 0.0), c[1], c[2]) for c in cands]
    sc, t, note = max(cands, key=lambda c: c[0])
    return t, min(sc, 1.0), (["house_no_symbol_fixed"] if note else [])


_SERIAL_LETTER = re.compile(r"^([ESRMQ])\s*0*(\d{1,6})$")



def serial_letter(ser, s):
    """Status letter printed INSIDE the serial box of an addition-list card ("R 801", "S 802"), found from the pixels:
    a narrow glyph (4-10 px at AC-86 scale) separated from the digits by a gap of >= 3.5 px (gaps between digits are <= 3).
    -> (found, label, score): label is the best status template ('' if none matched well). Checked on 3,192 cards of three
    PDFs: found on exactly the 'R 752' / 'R 772' cards, on none of the other ~3,190 serial boxes."""
    if ser is None or ser.size == 0:
        return False, "", 0.0
    ink = ser < 128
    cols = ink.sum(0) > 0
    runs, st = [], None
    for i, v in enumerate(list(cols) + [False]):
        if v and st is None:
            st = i
        elif not v and st is not None:
            runs.append((st, i - 1)); st = None
    # a border line left in the crop is a thin, full-height run -- not a glyph
    runs = [q for q in runs if not ((q[1] - q[0] + 1) <= max(2, base.S(2, s)) and ink[:, q[0]:q[1] + 1].any(1).sum() > 0.8 * ser.shape[0])]
    if len(runs) < 2:
        return False, "", 0.0
    a, b_ = runs[0]
    width, gap = (b_ - a + 1) / s, (runs[1][0] - b_ - 1) / s
    if not (4 <= width <= 10 and gap >= 3.5):
        return False, "", 0.0
    reg = np.pad(ser[:, max(a - base.S(6, s), 0):b_ + base.S(7, s)], 4, constant_values=255)
    lab, sc, _ = base.match(base.to_ref(reg, s), "M")
    return True, (lab or ""), float(sc)


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
    if TESS["ok"] is None and os.environ.get("OCR_USE_TESS"):
        tess_setup(quiet=True)
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
    deleted_only = bool(os.environ.get("OCR_DELETED_ONLY"))
    if deleted_only:
        # Serial number of EVERY card first (cheap; the sequence check needs the neighbours), then the full read only on
        # cards that can be deleted: a status marker at the left, a DELETED stamp, or a letter printed inside the serial box.
        sj = [j for j in jobs["en"] if j[1] == "serial"]
        if sj:
            for (k, fld, _), res in zip(sj, base.run_rec(en, [j[2] for j in sj], "English (serials)")):
                cards[k]["en"][fld] = res
        cand = set()
        for k, cd in enumerate(cards):
            inf = cd["info"]
            if inf["marker_ink"] > 8 or inf["stamp_px"] > 60:
                cand.add(k)
            else:
                st_ = cd["en"].get("serial")
                if st_ and _SERIAL_LETTER.match(re.sub(r"[^A-Z0-9 ]", "", st_[0].upper()).strip()):
                    cand.add(k)
        jobs["en"] = [j for j in jobs["en"] if j[1] != "serial" and j[0] in cand]
        jobs["hi"] = [j for j in jobs["hi"] if j[0] in cand]
        log(f"    deleted-only: {len(cand)} of {len(cards)} cards can be deleted -> full OCR on those only")
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

    # Tesseract's Hindi reading of every name / relation_name crop (cards[k]["tess"][field] = (text, conf)); the
    # PaddleOCR-vs-Tesseract decision is made later, per folder, in arbitrate_names() because it uses a vocabulary
    # built from the whole folder.
    n_tess = 0
    if TESS["ok"]:
        for k, fld, im in jobs["hi"]:
            if fld.rstrip("~") in ("name", "relation_name"):
                res = tess_read(im)
                if res is not None:
                    cards[k].setdefault("tess", {})[fld] = res
                    n_tess += 1

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
        # Addition-list cards print the status letter INSIDE the serial box, left of the number ("R 801"), where the
        # left-hand marker detector does not look. Evidence, in order: (1) the OCR reading of the serial box is a known
        # letter + the digits of the final serial; (2) a detached glyph is found in the serial box (serial_letter()) and
        # matches a status template; (3) the glyph exists but is unclear -> '?' (flagged), never a blank.
        if not code and serial is not None:
            ocr_letter = ""
            for kk, vv in E.items():
                if kk.startswith("serial") and vv[1] >= 0.6:
                    mm = _SERIAL_LETTER.match(re.sub(r"[^A-Z0-9 ]", "", vv[0].upper()).strip())
                    if mm and int(mm.group(2)) == serial:
                        ocr_letter = mm.group(1)
                        break
            found, glab, gsc = serial_letter(cd["f"].get("serial"), cd["s"])
            if ocr_letter:
                code = ocr_letter
            elif found:
                if glab and gsc >= 0.5 and glab in KNOWN_STATUS_CODES:
                    code = glab
                else:
                    code = "?"
                if gsc < TPL_MIN - 0.15:
                    why.append("status_letter_unclear")
        stamped = info["stamp_px"] > 60
        if stamped and not code:
            why.append("deleted_without_status_code")      # a DELETED stamp but no status letter found: look at the card

        e_raw, es = get(E, "epic")
        epic = base.fix_epic(e_raw)
        sc.append(es)
        if not re.match(r"^[A-Z]{3}\d{7}$", epic):
            why.append("epic_format")

        name_raw, ns = get_name(H, "name")
        name, nlog = fix_name_rules(clean_name_field(name_raw, rules=False))
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
        rname, rlog = fix_name_rules(clean_name_field(rname_raw, rules=False))
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

        hpat = key_re("house_no")
        house, hsc, hnotes = resolve_house([(v[0], v[1], src) for src, d in (("hi", H), ("en", E)) for kk, v in d.items() if hpat.match(kk)])
        why.extend(hnotes)
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
        tess_cols = {}
        for fld in ("name", "relation_name"):
            tv = [v for kk, v in cd.get("tess", {}).items() if kk.rstrip("~") == fld and v[0]]
            tbest = max(tv, key=lambda v: v[1]) if tv else None
            tess_cols[fld + "_tess"] = f"{tbest[0]}\t{tbest[1]:.3f}\t{(ns if fld == 'name' else rs):.3f}" if tbest else ""
            if tbest:
                raw_ocr_body += f" | {fld}#T={tbest[0]!r}({tbest[1]:.2f})"

        rows.append({
            "serial_no": serial, "name": name, "relation_type": rel, "relation_name": rname,
            "house_no": house, "age": age, "gender": gender, "epic_no": epic,
            "status_code": code, "status_meaning": STATUS_MEANING.get(code, "?" if code else ""),
            "deleted": "Y" if (stamped or code in KNOWN_STATUS_CODES) else "N",
            "deleted_stamp": "Y" if stamped else "N",
            "list_type": info["list_type"], "addition_section_no": sec,
            "file_name": pdf_path.name, "pdf_page": cd["pdf_page"], "card_on_page": cd["card_on_page"],
            "confidence": conf, "needs_review": "Y" if why else "N", "review_reason": "|".join(why),
            "raw_ocr_body": raw_ocr_body, "stamp_px": info["stamp_px"], **(tess_cols if TESS["ok"] else {}),
            "autocorrect_log": "; ".join([f"name: {a}>{b} [{r}]" for a, b, r in nlog]
                                         + [f"relation_name: {a}>{b} [{r}]" for a, b, r in rlog]
                                         + ([f"house_no: {house}" + " [ॐ repaired]"] if hnotes and "house_no_symbol_fixed" in hnotes else [])),
        })

    n_fix = sum(1 for f_ in serial_flag if f_)
    log(f"    done in {time.time() - t0:.0f}s, {len(rows)} cards "
        f"(serials repaired: {n_fix}, re-read: {n_reread}; names re-cropped: {n_retry}; "
        f"v3 second opinion on {n_second} cards; tesseract name reads: {n_tess})")

    df = pd.DataFrame(rows, columns=FIELDNAMES + (TESS_COLS if TESS["ok"] else []))
    df["serial_no"] = df["serial_no"].astype("Int64")
    if deleted_only:
        df = df[df["deleted"] == "Y"].reset_index(drop=True)
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
    if os.environ.get("OCR_USE_TESS"):
        tess_setup(quiet=True)
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
            fixed = [fix_name_rules(vc.fix(x))[0] for x in toks]   # the vocabulary can re-create a pattern the rules repair (वीन्द्र -> वीरन्द्र)
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



# ---------------------------------------------------------------------------
# PaddleOCR vs Tesseract: which reading of a name is right? (v15)
# ---------------------------------------------------------------------------
# Hand-labelled on 376 fields of AC 86 where the two engines disagreed (against the printed cards):
#   Tesseract right 222, PaddleOCR right 106, neither 29 (the rest were label noise / equivalent spellings).
# Neither engine's own confidence separates them (PaddleOCR is "1.00" on its wrong reads), so the decision
# uses (a) how well each candidate's words are known -- the words that BOTH engines read identically are
# near-certainly right and form the lexicon -- (b) Tesseract's confidence, (c) PaddleOCR's confidence,
# (d) a candidate that merely lost a word ("भूरे खॉँ" -> "भूरे") is not preferred. With these weights 277/316
# of the disagreements were decided correctly (87%; always-Tesseract: 70%, always-PaddleOCR: 30%).
ARB_W, ARB_K, ARB_C, ARB_KP, ARB_CP, ARB_KD, ARB_CAP = 2.0, 6.0, 0.5, 3.0, 0.95, 0.3, 5
ARB_UNSURE = 1.5            # |score| below this: the pick is a coin-flip-ish -> flagged <field>_engine_pick in review_reason
LEX_MIN_TOKENS = 3000       # below this the lexicon is too small to mean much (its weight is scaled down)


def _tess_norm(t):
    """Tesseract adds a spurious chandrabindu to word-final ो (शहरवानो -> शहरवानों); real words ending in ों are rare in names."""
    return re.sub("ों(?=$|\\s)", "ो", t)


def _lex_score(text, lex):
    ts = text.split()
    return sum(min(math.log2(1 + lex.get(t, 0)), ARB_CAP) for t in ts) / len(ts) if ts else 0.0


def arbitrate_pair(p, pc, t, tc, lex, lex_weight=1.0):
    """-> (use_tesseract: bool, score). p / t are cleaned texts; score > 0 favours Tesseract."""
    if not t:
        return False, -99.0
    if not p:
        return True, 99.0
    lp, lt = p.split(), t.split()
    if len(lt) < len(lp) and lp[:len(lt)] == lt:
        return False, -99.0                                  # Tesseract lost the last word(s)
    dl = max(-3, min(3, len(t.replace(" ", "")) - len(p.replace(" ", ""))))
    v = (ARB_W * lex_weight * (_lex_score(t, lex) - _lex_score(p, lex)) + ARB_K * (tc - ARB_C)
         - ARB_KP * (pc - ARB_CP) + ARB_KD * dl)
    return v > 0, v


def arbitrate_names(df, lex_base=None, log=print):
    """Replace name / relation_name by the Tesseract reading where arbitrate_pair() says so.
    Returns (df, agreed_counter): the tokens both engines read identically (to be added to the persistent lexicon)."""
    cols = [c for c in ("name", "relation_name") if c + "_tess" in df.columns]
    agreed = collections.Counter()
    if not cols or df.empty:
        return df, agreed
    df = df.copy()
    parsed = {}
    for col in cols:
        lst = []
        for p_, tv in zip(df[col].fillna("").astype(str), df[col + "_tess"].fillna("").astype(str)):
            f = tv.split("\t")
            if len(f) >= 3 and f[0].strip():
                try:
                    t = _tess_norm(clean_name_field(f[0])); tc = float(f[1]); pc = float(f[2])
                except ValueError:
                    t, tc, pc = "", 0.0, 0.0
            else:
                t, tc, pc = "", 0.0, 0.0
            lst.append((t, tc, pc))
            if t and p_.replace(" ", "") == t.replace(" ", ""):
                agreed.update(p_.split())
        parsed[col] = lst
    lex = collections.Counter(lex_base or {})
    lex.update(agreed)
    lw = min(1.0, sum(lex.values()) / LEX_MIN_TOKENS)
    n_t = n_unsure = n_diff = 0
    for col in cols:
        for idx, p_, (t, tc, pc) in zip(df.index, df[col].fillna("").astype(str), parsed[col]):
            if not t:
                continue
            if p_.replace(" ", "") == t.replace(" ", ""):
                if t.count(" ") > p_.count(" "):
                    df.at[idx, col] = t                       # same letters, Tesseract kept the word spaces
                continue
            n_diff += 1
            use_t, v = arbitrate_pair(p_, pc, t, tc, lex, lw)
            if use_t:
                df.at[idx, col] = t
                n_t += 1
                prev = str(df.at[idx, "autocorrect_log"]) if str(df.at[idx, "autocorrect_log"]) not in ("", "nan") else ""
                df.at[idx, "autocorrect_log"] = (prev + "; " if prev else "") + f"{col}: {p_}>{t} [tesseract]"
            if abs(v) < ARB_UNSURE:
                n_unsure += 1
                rr = str(df.at[idx, "review_reason"]) if str(df.at[idx, "review_reason"]) not in ("", "nan") else ""
                df.at[idx, "review_reason"] = (rr + "|" if rr else "") + f"{col}_engine_pick"
                df.at[idx, "needs_review"] = "Y"
    log(f"  names: PaddleOCR and Tesseract disagreed on {n_diff} fields -> Tesseract's reading used for {n_t}, "
        f"{n_unsure} close calls flagged (*_engine_pick)")
    return df, agreed


HOUSE_CANON_MIN = 4       # a village / locality spelling seen at least this often is the reference spelling
HOUSE_CANON_RATIO = 1.5     # ... and at least this many times more often than the variant being normalised
HOUSE_CANON_SIM = 0.74    # spelling similarity (spaces ignored)


def canon_house(df, log=print):
    """House-number fields that are village names (गिजौली, धिग्रोली, डेरा बंजारा ...) are misspelled in
    a dozen ways by the model (गिजीली, गिजोली, वीलेज गिज़ली). Map each rare spelling onto the
    dominant, near-identical spelling in the same run. Numbers are never touched."""
    import difflib
    if df.empty or "house_no" not in df:
        return df
    vals = df["house_no"].fillna("").astype(str)
    is_place = vals.str.fullmatch(r"[\u0900-\u097F ]{4,}")
    cnt = collections.Counter(vals[is_place])
    # same letters, different spacing (डेराबंजारा / डेरा बंजारा): the model drops spaces, so the spaced form wins
    groups = collections.defaultdict(list)
    for v, n in cnt.items():
        groups[v.replace(" ", "")].append((" " in v, n, v))
    merge = {}
    for g in groups.values():
        tgt = max(g)[2]
        for _, _, v in g:
            merge[v] = tgt
    if any(v != t for v, t in merge.items()):
        vals = vals.map(lambda v: merge.get(v, v))
        cnt = collections.Counter(vals[is_place])
    canon = {v: n for v, n in cnt.items() if n >= HOUSE_CANON_MIN}
    if not canon:
        return df
    df = df.copy()
    nfix = 0
    for i in vals.index[is_place]:
        v = vals[i]
        if v in canon:
            if df.at[i, "house_no"] != v:
                df.at[i, "house_no"] = v
                nfix += 1
            continue
        key = v.replace(" ", "")
        best, best_r = None, 0.0
        for c, n in canon.items():
            r = difflib.SequenceMatcher(None, key, c.replace(" ", "")).ratio()
            if r > best_r and n >= HOUSE_CANON_RATIO * cnt[v]:
                best, best_r = c, r
        if best and best_r >= HOUSE_CANON_SIM:
            df.at[i, "house_no"] = best
            prev = str(df.at[i, "autocorrect_log"]) if str(df.at[i, "autocorrect_log"]) not in ("", "nan") else ""
            df.at[i, "autocorrect_log"] = (prev + "; " if prev else "") + f"house_no: {v}>{best}"
            nfix += 1
    if nfix:
        log(f"  house_no: {nfix} village-name spellings normalised ({len(canon)} reference names)")
    return df


def finalize_names(df, vocab_path, log=print):
    """Engine arbitration + vocabulary pass over a finished DataFrame; updates the persistent vocab / lexicon files."""
    lex_path = (str(vocab_path) + ".agreed.json") if vocab_path else None
    lex_loaded = load_vocab(lex_path) if lex_path else collections.Counter()
    df, agreed = arbitrate_names(df, lex_loaded, log)
    if lex_path and agreed:
        save_vocab(lex_path, lex_loaded + agreed)
    df = df.drop(columns=[c for c in TESS_COLS if c in df.columns])
    df = canon_house(df, log)
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
               vocab_path=None, upload=None, s3_endpoint=None, presign_hours=0, second_opinion="auto",
               pool=None, gzip_csv=False, max_rows=0, slim=False):
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
    for old in list(out_dir.glob("_combined*.csv")) + list(out_dir.glob("_combined*.csv.gz")) + list(out_dir.glob("_combined*.partial*")):
        old.unlink()                       # leftovers of an earlier run of this folder (a re-run may produce a different number of files)
    chunk_paths = []                       # with max_rows: _combined.partial_001.csv, _002 ... each closed at a PDF boundary
    chunk_rows = 0
    limit_note = f" (limited from {total_found} found)" if limit else ""
    print(f"Found {total_found} PDFs, processing {len(pdfs)}{limit_note}, in natural order "
          f"({pdfs[0].name} ... {pdfs[-1].name}). "
          f"Running with {workers} worker process(es), {cpu_threads} CPU threads each "
          f"(~{workers * cpu_threads} threads total -- watch Activity Monitor)...")
    print(f"Writing {'CSV files of ~' + format(max_rows, ',') + ' rows (split at PDF boundaries)' if max_rows else 'one combined CSV only (no per-PDF CSVs)'}: {out_dir}")

    header_written = False
    total_rows = processed = 0
    failed = []
    done, next_i = {}, 0
    t_start = time.time()
    own_pool = pool is None          # --root mode passes ONE pool in, so the OCR models are loaded once per worker, not once per folder
    if own_pool:
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, initializer=_worker_init, initargs=(device, cpu_threads, second_opinion))
    try:
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
                if slim:
                    df = df.drop(columns=[c for c in ("raw_ocr_body", "stamp_px") if c in df.columns])
                if max_rows:
                    if not chunk_paths or chunk_rows >= max_rows:      # next PDF starts a new file
                        chunk_paths.append(out_dir / f"_combined.partial_{len(chunk_paths) + 1:03d}.csv")
                        chunk_rows = 0
                    target, first = chunk_paths[-1], chunk_rows == 0
                else:
                    target, first = partial_path, not header_written
                df.to_csv(target, mode="w" if first else "a", header=first, index=False, encoding="utf-8-sig")
                chunk_rows += len(df)
                header_written = True
                total_rows += len(df)
                processed += 1
                el = max(time.time() - t_start, 1e-9)
                print(f"  [{processed}/{len(pdfs)}] {name}: {len(df)} cards "
                      f"(total {total_rows}, {total_rows / el:.1f} cards/s)", flush=True)

    finally:
        if own_pool:
            pool.shutdown()

    out_files = []
    if header_written:
        parts = chunk_paths if max_rows else [partial_path]
        for k, part in enumerate(parts, 1):
            final = out_dir / (f"_combined_{k:03d}.csv" if max_rows else "_combined.csv")
            rows_ = pd.read_csv(part, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            finalize_names(rows_, vocab_path).to_csv(final, index=False, encoding="utf-8-sig")
            part.unlink()
            out_files.append(final)
    else:
        # nothing succeeded -- still write an (empty, header-only) combined
        # CSV so downstream tooling that expects the file to exist doesn't break
        pd.DataFrame(columns=FIELDNAMES).to_csv(combined_path, index=False, encoding="utf-8-sig")
        out_files.append(combined_path)
    print(f"\nDone: {processed}/{len(pdfs)} PDFs processed, {total_rows} total cards "
          f"in {time.time() - t_start:.0f}s.")
    if failed:
        (out_dir / "_failed.txt").write_text("\n".join(failed), encoding="utf-8")
        print(f"FAILED PDFs ({len(failed)}) listed in {out_dir / '_failed.txt'}", file=sys.stderr)
    if gzip_csv:
        import gzip, shutil
        gz = []
        for f_ in out_files:
            g_ = f_.with_name(f_.name + ".gz")
            with open(f_, "rb") as fi, gzip.open(g_, "wb", compresslevel=6) as fo:
                shutil.copyfileobj(fi, fo)
            f_.unlink()                              # CSV text of this kind compresses ~7-10x
            gz.append(g_)
        out_files = gz
    print(f"Output: {', '.join(f.name for f in out_files)}")

    import json
    info = {"version": VERSION, "folder": str(folder), "pdfs_found": total_found, "pdfs_processed": processed,
            "pdfs_failed": failed, "cards": total_rows, "files": [f.name for f in out_files], "first_pdf": pdfs[0].name, "last_pdf": pdfs[-1].name,
            "seconds": round(time.time() - t_start, 1), "complete": (not failed and len(pdfs) == total_found),
            "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    info_path = out_dir / "_run_info.json"                # also the "this folder is done" marker for --root resume
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if upload:
        tag = folder.resolve().name
        files = {f_: f"{tag}/{f_.name}" for f_ in out_files}
        files[info_path] = f"{tag}/_run_info.json"
        if failed:
            files[out_dir / "_failed.txt"] = f"{tag}/_failed.txt"
        upload_files(files, upload, s3_endpoint, presign_hours)
    return info


def run_root(root: pathlib.Path, out_root: pathlib.Path, workers, device, cpu_threads, limit=None, vocab_path=None,
             upload=None, s3_endpoint=None, presign_hours=0, second_opinion="auto", gzip_csv=False,
             force=False, only=None, recursive=False, max_rows=0, slim=False):
    """Every sub-folder of `root` that contains PDFs is one job (downloads/1, downloads/2 ... downloads/100).
    One worker pool is shared by all of them. A folder is skipped if its _run_info.json says complete, so the
    same command can simply be re-run after an interruption (or when new folders appear). One bad folder
    never stops the others."""
    import json
    subs = sorted((d for d in root.iterdir() if d.is_dir() and (any(d.rglob("*.pdf")) if recursive else any(d.glob("*.pdf")))),
                  key=lambda p: natural_key(p.name))
    if only:
        wanted = set(only)
        subs = [d for d in subs if d.name in wanted]
    if not subs:
        print(f"No sub-folders with PDFs under {root}")
        return
    print(f"{len(subs)} folders under {root}: {subs[0].name} ... {subs[-1].name}; one shared pool of "
          f"{workers} worker(s) x {cpu_threads} thread(s)")
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=workers, initializer=_worker_init, initargs=(device, cpu_threads, second_opinion))
    summary, t_all = [], time.time()
    try:
        for n, sub in enumerate(subs, 1):
            out_dir = out_root / sub.name
            marker = out_dir / "_run_info.json"
            if marker.exists() and not force and not limit:
                try:
                    if json.loads(marker.read_text(encoding="utf-8")).get("complete"):
                        print(f"[{n}/{len(subs)}] {sub.name}: already done -- skipped (use --force to redo)")
                        continue
                except Exception:
                    pass
            print(f"\n[{n}/{len(subs)}] ===== {sub.name} =====", flush=True)
            try:
                info = run_folder(sub, out_dir, workers, recursive, device, cpu_threads, limit, vocab_path,
                                  upload, s3_endpoint, presign_hours, second_opinion, pool=pool, gzip_csv=gzip_csv,
                                  max_rows=max_rows, slim=slim)
                summary.append({"folder": sub.name, **({k: info[k] for k in ("pdfs_found", "pdfs_processed", "cards", "seconds", "complete")} if info else {})})
            except Exception as exc:                         # keep going with the next folder
                print(f"  ! folder {sub.name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                summary.append({"folder": sub.name, "error": f"{type(exc).__name__}: {exc}"})
            if getattr(pool, "_broken", False):               # a worker was killed (out of memory?) -> fresh pool
                pool.shutdown(wait=False)
                pool = concurrent.futures.ProcessPoolExecutor(
                    max_workers=workers, initializer=_worker_init, initargs=(device, cpu_threads, second_opinion))
            if summary:
                out_root.mkdir(parents=True, exist_ok=True)
                sp = out_root / "_all_folders_summary.csv"
                prev = pd.read_csv(sp, dtype=str) if sp.exists() else pd.DataFrame()
                cur = pd.DataFrame(summary).astype(str)
                if not prev.empty and "folder" in prev:
                    cur = pd.concat([prev[~prev["folder"].isin(cur["folder"])], cur], ignore_index=True)
                cur.to_csv(sp, index=False)
    finally:
        pool.shutdown()
    print(f"\nAll done: {len(summary)} folders run in {(time.time() - t_all) / 60:.1f} min. "
          f"Summary: {out_root / '_all_folders_summary.csv'}")


def main():
    print(f"ocr_extract_paddleocr.py {VERSION}")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", help="Single PDF file (omit if using --folder)")
    ap.add_argument("--folder", help="Process every PDF in this folder instead of a single file")
    ap.add_argument("--recursive", action="store_true", help="With --folder, also descend into subfolders")
    ap.add_argument("--root", help="Process EVERY sub-folder of this directory (e.g. downloads -> downloads/1 ... downloads/100); "
                                   "outputs go to --out/<sub-folder>/. Finished folders are skipped, so re-running resumes.")
    ap.add_argument("--only", nargs="+", help="With --root: only these sub-folder names (e.g. --only 86 2 3)")
    ap.add_argument("--only-file", help="With --root: text file with one sub-folder name per line (use for a list of 100 of the 403 folders)")
    ap.add_argument("--force", action="store_true", help="With --root: redo folders that are already marked complete")
    ap.add_argument("--max-rows", type=int, default=100000,
                     help="Start a new CSV (_combined_001.csv, _002 ...) once the current one holds this many rows; "
                          "files are cut between PDFs (parts), never inside one, so a file can exceed the limit by < 1 PDF. "
                          "0 = one file per folder. Excel's limit is 1,048,576 rows per sheet (default 100000).")
    ap.add_argument("--slim", action="store_true", help="Leave out the audit columns raw_ocr_body and stamp_px "
                                                       "(about 60%% smaller CSVs; you lose the data needed to debug a wrong name)")
    ap.add_argument("--gzip", action="store_true", help="Store _combined.csv.gz instead of _combined.csv (about 8-10x smaller)")
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
    ap.add_argument("--rec-width", type=int, default=0,
                     help="EXPERIMENT (default 0 = off). The recognition models pad every crop to 320 px wide, but these "
                          "crops are only ~65-105 px wide at model height, so 70-80%% of every model input is empty. "
                          "--rec-width 160 lets crops use a narrower input (batches are sorted by width so they stay uniform). "
                          "Compare speed AND the CSV against a normal run on the same PDFs before trusting it.")
    ap.add_argument("--tessdata", metavar="DIR",
                    help="Folder that contains hin.traineddata (Tesseract's Hindi model, ideally the 'tessdata_best' one). "
                         "Names are read by Tesseract as well as PaddleOCR and the better reading is kept. Found automatically "
                         "when installed with: brew install tesseract tesseract-lang")
    ap.add_argument("--tess", action="store_true", help="Also read names with Tesseract and keep the better reading (off by default: names come from the "
                                                         "PaddleOCR v5 + v3 models only).")
    ap.add_argument("--deleted-only", action="store_true",
                    help="Output only the deleted cards (status letter E/S/R/M/Q or a DELETED stamp). The Hindi/English models run only on those "
                         "cards (plus the serial number of every card, for the sequence check), so the run is many times faster.")
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
    if args.rec_width:
        os.environ["OCR_REC_WIDTH"] = str(args.rec_width)      # inherited by the worker processes
    if args.deleted_only:
        os.environ["OCR_DELETED_ONLY"] = "1"
    if args.tess:
        os.environ["OCR_USE_TESS"] = "1"
        if args.tessdata:
            os.environ["OCR_TESSDATA"] = args.tessdata
        tess_setup(args.tessdata)                               # prints once whether the Hindi Tesseract reader is available

    if not args.pdf and not args.folder and not args.root:
        ap.error("provide a PDF file, --folder DIR, or --root DIR for batch mode")

    if args.root:
        run_root(pathlib.Path(args.root), pathlib.Path(args.out), args.workers, args.device, args.cpu_threads,
                 args.limit, vocab_path, args.upload, args.s3_endpoint, args.presign_hours, args.second_opinion,
                 args.gzip, args.force,
                 (args.only or []) + (pathlib.Path(args.only_file).read_text(encoding="utf-8").split() if args.only_file else []) or None,
                 args.recursive, args.max_rows, args.slim)
        return

    if args.folder:
        run_folder(pathlib.Path(args.folder), pathlib.Path(args.out), args.workers,
                   args.recursive, args.device, args.cpu_threads, args.limit,
                   vocab_path, args.upload, args.s3_endpoint, args.presign_hours, args.second_opinion,
                   gzip_csv=args.gzip, max_rows=args.max_rows, slim=args.slim)
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