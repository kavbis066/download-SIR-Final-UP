#!/usr/bin/env python3
"""
ocr_extract_paddleocr.py (v9) -- fast extraction pipeline, rebuilt on the
architecture from your colleague's parse_roll.py.

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
  subfolders). Writes one CSV per PDF into ac86_out/ plus a combined
  ac86_out/_combined.csv. --limit caps how many PDFs are processed, for
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

VERSION = "v9"

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

# Extra safety net on top of ocr_extract.clean_hindi (which already
# strips a trailing run of Latin/junk characters): catches a DELETED-
# stamp fragment that landed in the MIDDLE of a name/relation_name
# instead of at the end.
WATERMARK_FRAGMENT_RE = re.compile(r"\b[A-Z]{0,2}ETED\b|\bDELET\w*\b|हटाय\w*", re.IGNORECASE)

FIELDNAMES = [
    "serial_no", "name", "relation_type", "relation_name", "house_no", "age", "gender",
    "epic_no", "status_code", "status_meaning", "deleted", "deleted_stamp",
    "list_type", "addition_section_no", "file_name", "pdf_page", "card_on_page",
    "confidence", "needs_review", "review_reason",
    # extra audit columns beyond your colleague's schema -- safe to delete in Excel
    "raw_ocr_body", "stamp_px",
]


def apply_name_corrections(value: str) -> str:
    return NAME_CORRECTIONS.get(value, value)


def clean_name_field(raw: str) -> str:
    t = base.clean_hindi(raw)
    t = WATERMARK_FRAGMENT_RE.sub("", t).strip()
    return apply_name_corrections(t)


# ---------------------------------------------------------------------------
# Per-PDF processing -- collects every field crop for the whole PDF, OCRs
# each language in 2 big batched calls (via ocr_extract.run_rec), then
# cleans + assembles rows. Mirrors parse_roll.process_pdf structurally;
# the cleaning/schema layer on top is ours.
# ---------------------------------------------------------------------------

def process_pdf(pdf_path: pathlib.Path, hi_models, en, log=print, dump_review=False, review_dir=None):
    """hi_models: list of (model, name) tuples -- one or more loaded Hindi
    TextRecognition models (see base.load_rec_all). When more than one is
    given, every Hindi field crop is OCR'd through ALL of them and the
    highest-scoring reading wins (see get() below), the same way a
    stamped/destamp "~" alt crop is already picked by score today.

    Why: visually confirmed against real cards (page 3 of AC 86 part 10 --
    see the names_crosscheck review) that a SINGLE Hindi model can
    confidently misread a correctly-cropped, legible conjunct -- e.g. the
    crop plainly shows "रवींद्र" but the model returns "र्वीद्र", or shows
    "रूप चन्द्र" but returns "रूप चनद्र" (dropped halant). The crop is not
    the problem in these cases -- cutting a taller/different window
    wouldn't fix it. A second model's independent reading, picked by score,
    catches many of these without touching the cropping/template code at
    all. Costs roughly 2x the Hindi OCR time (not the English/numeric
    fields, which aren't affected by this and stay on one model); falls
    back to single-model behavior automatically if only one Hindi model is
    available in this environment.
    """
    meta = base.parse_filename(pdf_path)
    cards = []
    t0 = time.time()
    for pno, g, npg in base.page_images_calibrated(str(pdf_path)):
        if pno < 2 or pno == npg - 1:
            continue
        log(f"    cutting cards: page {pno + 1}/{npg}")
        for ci, (x, y, w, h) in enumerate(base.find_cards(g)):
            img = g[y:y + h, x:x + w]
            stamped = int(((img[100:122, 80:225] > 60) & (img[100:122, 80:225] < 235)).sum()) > 60
            f, info = base.cut_card(img, base.destamp(img) if stamped else None)
            info["stamped"] = stamped
            cards.append(dict(pdf_page=pno + 1, card_on_page=ci + 1, f=f, info=info, hi={}, en={}))

    jobs = {"hi": [], "en": []}
    for k, cd in enumerate(cards):
        F = cd["f"]
        mask_of = lambda fld: F.get("mask:" + fld.rstrip("~"))
        for fld in ("name", "relation_name", "house_no", "name~", "relation_name~", "house_no~"):
            if fld in F:
                im = base.prep(F[fld], mask_of(fld))
                if im is not None:
                    jobs["hi"].append((k, fld, im))
        for fld in ("serial", "epic", "age", "house_no", "section", "age~", "house_no~"):
            if fld in F:
                im = base.prep(F[fld], mask_of(fld))
                if im is not None:
                    jobs["en"].append((k, fld, im))

    log(f"    {len(cards)} cards found, running OCR...")
    # English/numeric fields: one model, same as before.
    imgs = [j[2] for j in jobs["en"]]
    if imgs:
        for (k, fld, _), res in zip(jobs["en"], base.run_rec(en, imgs, "English")):
            cards[k]["en"][fld] = res

    # Hindi fields: run through every loaded Hindi model. Index 0's result
    # is stored under the plain field name (e.g. "name") for backward
    # compatibility; model i>=1's result is stored under "name#i". get()
    # below picks whichever scored highest across all of them.
    imgs = [j[2] for j in jobs["hi"]]
    if imgs:
        for i, (model, mname) in enumerate(hi_models):
            label = "Hindi" if i == 0 else f"Hindi(#{i} {mname})"
            for (k, fld, _), res in zip(jobs["hi"], base.run_rec(model, imgs, label)):
                key = fld if i == 0 else f"{fld}#{i}"
                cards[k]["hi"][key] = res

    rows, prev = [], None
    for cd in cards:
        H, E, info = cd["hi"], cd["en"], cd["info"]
        why, sc = [], []
        # Picks the highest-scoring reading for field `k` among every
        # variant present in `d`: the plain crop (k), the destamp-alt crop
        # (k~), and -- for Hindi fields now that >1 model may have run --
        # each extra model's reading of either crop (k#1, k~#1, k#2, ...).
        # Falls back to the old two-candidate behavior automatically when
        # only one Hindi model loaded (no "#N" keys exist at all then).
        _field_re_cache = {}
        def get(d, k):
            pat = _field_re_cache.get(k)
            if pat is None:
                pat = _field_re_cache[k] = re.compile(r"^" + re.escape(k) + r"(~)?(#\d+)?$")
            best = ("", 0.0)
            for key, val in d.items():
                if pat.match(key) and val[1] > best[1]:
                    best = val
            return best

        def model_disagreement(d, k):
            """True if two different Hindi models produced different
            non-empty cleaned readings for this field, both with some real
            confidence -- a useful review signal even when each model's own
            score looked fine on its own (a confidently-wrong single-model
            read, like रवींद्र -> र्वीद्र, doesn't trigger low_ocr_confidence,
            but a second model disagreeing with it does flag something)."""
            texts = set()
            for key, (txt, score) in d.items():
                if key == k or key.startswith(k + "#"):
                    t = clean_name_field(txt)
                    if t and score >= 0.5:
                        texts.add(t)
            return len(texts) > 1

        st, ss = get(E, "serial")
        dg = re.sub(r"\D", "", st.upper().translate(str.maketrans("OIL", "011")))
        serial = int(dg) if dg else None
        sc.append(ss)
        if serial is None:
            why.append("serial_unreadable")
        elif prev is not None and serial != prev + 1:
            why.append("serial_out_of_sequence")
        prev = serial if serial is not None else (prev + 1 if prev is not None else None)

        code = ""
        if info["marker_ink"] > 8:
            lab, s, m = info["marker"]
            if lab:
                # Use the best-matched letter even when its score/margin is
                # below the confidence cutoff — a low-confidence E/S/R/M/Q is
                # still far more informative than throwing it away and
                # writing "?" instead (which also silently discarded the
                # actual reason a card was deleted, exactly the field this
                # whole project cares most about getting right). Flag it for
                # review instead of discarding it.
                code = lab
                if not (s >= TPL_MIN - 0.15 and m >= TPL_MARGIN):
                    why.append("status_letter_unclear")
            else:
                # No template cleared even the loosest match at all (not a
                # confidence issue — nothing scored) — genuinely unknown.
                code = "?"
                why.append("status_letter_unclear")
        stamped = info["stamp_px"] > 60

        e_raw, es = get(E, "epic")
        epic = base.fix_epic(e_raw)
        sc.append(es)
        if not re.match(r"^[A-Z]{3}\d{7}$", epic):
            why.append("epic_format")

        name_raw, ns = get(H, "name")
        name = clean_name_field(name_raw)
        sc.append(ns)
        if not name:
            why.append("name_missing")
        elif model_disagreement(H, "name"):
            why.append("name_model_disagreement")
        rname_raw, rs = get(H, "relation_name")
        rname = clean_name_field(rname_raw)
        sc.append(rs)
        if rname and model_disagreement(H, "relation_name"):
            why.append("relation_name_model_disagreement")

        lab, s, m = info["relation"]
        if s >= TPL_MIN and m >= TPL_MARGIN:
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
        # Same fix as status code above: keep the best-matched gender even
        # at low confidence instead of blanking it out — this was exactly
        # the काजल bug (clearly female on the card, template match found
        # "महिला" correctly, but score fell just under the cutoff so the
        # field was wiped to empty instead of just being flagged).
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
            f"{k}={v[0]!r}({v[1]:.2f})" for d in (H, E) for k, v in d.items()
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
            "raw_ocr_body": raw_ocr_body, "stamp_px": info["stamp_px"],
        })

    log(f"    done in {time.time() - t0:.0f}s, {len(rows)} cards")

    df = pd.DataFrame(rows, columns=FIELDNAMES)
    if df.empty:
        return df
    # a serial that breaks the sequence while both neighbours agree is a misread: correct it
    s_ = df["serial_no"].tolist()
    for i in range(1, len(s_) - 1):
        a, b, c = s_[i - 1], s_[i], s_[i + 1]
        if a is not None and c is not None and c == a + 2 and b != a + 1:
            df.at[i, "serial_no"] = a + 1
            s_[i] = a + 1
            r = df.at[i, "review_reason"].replace("serial_out_of_sequence", "serial_corrected")
            r = r.replace("serial_unreadable", "serial_corrected")
            df.at[i, "review_reason"] = r
    for i in range(len(s_)):
        if i and s_[i] is not None and s_[i - 1] is not None and s_[i] == s_[i - 1] + 1:
            df.at[i, "review_reason"] = "|".join(
                x for x in df.at[i, "review_reason"].split("|") if x != "serial_out_of_sequence"
            )
    df["needs_review"] = np.where(df["review_reason"] != "", "Y", "N")
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


def _worker_init(device, cpu_threads):
    _limit_threads(cpu_threads)
    hi_models = base.load_rec_all(base.HI_MODELS, device)
    if not hi_models:
        raise RuntimeError(f"none of {base.HI_MODELS} could be loaded")
    en, _ = base.load_rec(base.EN_MODELS, device)
    _WORKER["hi"], _WORKER["en"] = hi_models, en
    _WORKER["hname"] = "+".join(m for _, m in hi_models)


def _worker_process_pdf(pdf_path_str, out_csv_str):
    pdf_path = pathlib.Path(pdf_path_str)
    out_csv = pathlib.Path(out_csv_str)
    tag = f"[{pdf_path.name}]"

    def log(msg):
        print(f"{tag} {msg}", flush=True)

    t0 = time.time()
    df = process_pdf(pdf_path, _WORKER["hi"], _WORKER["en"], log=log)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    elapsed = time.time() - t0
    print(f"{tag} done: {len(df)} cards in {elapsed:.1f}s "
          f"({elapsed/max(len(df),1):.2f}s/card) -> {out_csv}", flush=True)
    return str(out_csv), len(df)


def run_folder(folder: pathlib.Path, out_dir: pathlib.Path, workers: int,
               recursive: bool, device, cpu_threads: int, limit=None):
    pdfs = sorted(folder.rglob("*.pdf") if recursive else folder.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {folder} (recursive={recursive})")
        return
    total_found = len(pdfs)
    if limit:
        pdfs = pdfs[:limit]
    out_dir.mkdir(parents=True, exist_ok=True)
    limit_note = f" (limited from {total_found} found)" if limit else ""
    print(f"Found {total_found} PDFs, processing {len(pdfs)}{limit_note}. "
          f"Running with {workers} worker process(es), {cpu_threads} CPU threads each "
          f"(~{workers * cpu_threads} threads total -- watch Activity Monitor)...")

    results = []
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, initializer=_worker_init, initargs=(device, cpu_threads)) as pool:
        futures = {}
        for pdf in pdfs:
            out_csv = out_dir / (pdf.stem + ".csv")
            fut = pool.submit(_worker_process_pdf, str(pdf), str(out_csv))
            futures[fut] = pdf
        for fut in concurrent.futures.as_completed(futures):
            pdf = futures[fut]
            try:
                out_csv, n = fut.result()
                results.append(out_csv)
            except Exception as exc:
                print(f"  ! {pdf.name} failed: {exc}", file=sys.stderr)

    combined_path = out_dir / "_combined.csv"
    frames = [pd.read_csv(p) for p in results if pathlib.Path(p).exists()]
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=FIELDNAMES)
    combined.to_csv(combined_path, index=False, encoding="utf-8-sig")
    print(f"\nDone: {len(results)}/{len(pdfs)} PDFs processed, {len(combined)} total cards.")
    print(f"Combined CSV: {combined_path}")


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
                          "(one CSV per PDF + _combined.csv)")
    args = ap.parse_args()

    if not args.pdf and not args.folder:
        ap.error("provide a PDF file, or --folder for batch mode")

    if args.folder:
        run_folder(pathlib.Path(args.folder), pathlib.Path(args.out), args.workers,
                   args.recursive, args.device, args.cpu_threads, args.limit)
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
    df = process_pdf(pdf_path, hi_models, en)
    elapsed = time.time() - t0
    print(f"Done: {len(df)} cards in {elapsed:.1f}s ({elapsed/max(len(df),1):.2f}s/card)")

    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()