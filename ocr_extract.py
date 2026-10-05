#!/usr/bin/env python3
"""
ocr_extract.py (v2) -- shared engine for ECI SIR Final Roll card extraction.

ARCHITECTURE CHANGE FROM v1
----------------------------
v1 of this file held a fixed grid calibration (REF_W/REF_H/ROW_TOPS/
COL_LEFTS) and leaned on PaddleOCR's full detection+recognition pipeline
to read whole pages. That pipeline's text-DETECTION model is the slow
part -- it is what made ocr_extract_paddleocr.py take 15-30 minutes per
PDF. Your colleague's script (parse_roll.py) proved a much faster
architecture on the exact same card template, and this version adopts it
wholesale instead of patching around the old one:

  - Cards and text lines are found with classical OpenCV (contours,
    thresholding, run-length analysis) -- NO neural network involved, so
    it's effectively free compared to a detection model.
  - Fixed-vocabulary fields (relation type, gender, status letter, the
    DELETED stamp, list type) are read by pixel TEMPLATE MATCHING against
    a small embedded reference image set (_TPL below) -- also no OCR call
    at all for these fields.
  - Only free-text fields (name, relation name, house number, age, EPIC,
    serial) go through the OCR recognition model -- and only the
    lightweight TextRecognition model (recognition only, no detection),
    the same devanagari_PP-OCRv5_mobile_rec / en_PP-OCRv5_mobile_rec
    models PaddleOCR 3.1+ ships.
  - Pages are rasterized with PyMuPDF (fitz) in-process instead of
    shelling out to poppler's pdfimages -- one less subprocess + disk
    round-trip per page.

The CV/template-matching functions below (_load_tpl/_TPL/TPL/STAMP_MASK,
match, page_images, find_cards, runs, text_lines, colon_x, label_end,
wipe_borders, destamp, card_mask, cut_card, prep, load_rec, run_rec,
fix_epic, clean, clean_hindi), and the constants above them (HI_MODELS,
EN_MODELS, TPL_MIN, TPL_MARGIN, STATUS, REL, DEV), are copied byte-for-
byte from your colleague's parse_roll.py -- copied with a file-level
`cp`, never retyped, specifically so the embedded base64 template blob
(_TPL) and the numeric thresholds throughout (card size ranges, colon-gap
widths, line-band heights, etc.) couldn't be corrupted by a transcription
error. They are UNCHANGED from that proven, working script.

What ocr_extract_paddleocr.py (v7) builds on top of these primitives is
the part that's new: our own field-cleaning rules (NAME_CORRECTIONS,
extra watermark-fragment stripping, gender fallback patterns, the output
CSV schema and review-reason logic) that were developed against your
real output across v2-v6, now applied as a thin layer on top of this
faster engine instead of being baked into a slow page-level OCR pass.

parse_filename() at the bottom is the one function carried over unchanged
from the old v1 ocr_extract.py -- it has nothing to do with OCR speed and
still works the same way (extracts ac_number/part_number from the PDF's
own filename).

SETUP
-----
    pip install "paddleocr>=3.1" paddlepaddle pymupdf opencv-python numpy
"""
import base64
import io
import pathlib
import re
import sys
import time

import cv2
import fitz
import numpy as np

HI_MODELS = ["devanagari_PP-OCRv5_mobile_rec", "devanagari_PP-OCRv3_mobile_rec"]
EN_MODELS = ["en_PP-OCRv5_mobile_rec", "PP-OCRv5_mobile_rec", "en_PP-OCRv4_mobile_rec"]
REVIEW_BELOW = 0.90          # OCR score below this -> needs_review
TPL_MIN, TPL_MARGIN = 0.78, 0.07
STATUS = {"S": "shifted (स्थानांतरित)", "E": "dead (मृतक)", "R": "repeated (पुनरावृत्ति)",
          "M": "missing (लापता)", "Q": "ineligible (अयोग्य)", "#": "# (not in legend)"}
REL = {"पिता": "father", "पति": "husband", "माता": "mother", "अन्य": "other"}
DEV = str.maketrans("०१२३४५६७८९", "0123456789")

_TPL = "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P7MI0JMCC35vCZr3H2rm/4ffgdrTTkI4u7bCXDj15v/E48R7CABQSwMELQAACAgAAAAhAKSPij3//////////xgAFABHX+CkquClgeCksOClgeCkt19fMC5ucHkBABAAZwEAAAAAAADwAAAAAAAAAJvsF+obEMnIUMZQrZ6SWpxcpG6loF5Taqiuo6Cell9UUpSYF59flJIKEndLzClOBYoXZyQWpAL5GoaGOgpGhpo6CrUKZAKuP/fB4DYSef/XO2/5hARFj5SEWN6EhAwN1rgEroRn/48F/v8ffuD////Sj///z/Jt/C8JZB8HCz4qmifm7fk/a0PiLpDgTZv//10O3GB2vPdf4nfNnK+OfEDB/wHu5uIH/k+I/3vO8P8t7UBDbpDg/6efQGY2Rn688///r/t/JMGC/59HrgWSE5dAeMEgYuPbIz0Ou/6jgh3mRz4UTEUT/H8p0SH/G4oIAFBLAwQtAAAICAAAACEAagUKf///////////GAAUAEdf4KSq4KWB4KSw4KWB4KS3X18xLm5weQEAEABwAQAAAAAAAOsAAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkahkY6CkYGmjoKtQpkAq7/WMCJBDAIA5NpYDLvS2Xt/fvi927fX+t9//4ddb0be80f/e/q//9f6u///0cV6x7+11mQecP+//++bpBYh7h6/yTf/4avy2uBYjs87i5g/3uV99D/M67/o2dckwCK/Z8Y0CDy9/9mu4+zl/5/kRHaYA+xHGTeepNHYPYNiNgErSYgeformPNx+////3yuvf3ncwHV1TeMsyvV3qJ55c+xjahCAFBLAwQtAAAICAAAACEAGv9FYP//////////GAAUAEdf4KSu4KS54KS/4KSy4KS+X18wLm5weQEAEADfAQAAAAAAABYBAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkahsY6CkbmmjoKtQpkAq7/MPD82If/2EGxUYJdwH1sMt+kfv3/v0n7OrLYl/tgcEvu2p3793e63L4N4QOpZycSOBMStOzTFTxTExISJAMSQCBAMtZNuen/f4n//wtW/g89/P/BggVaV8BGXXX6vzv2P5KU/PQFC97/8FP3TNGFSMWxOTjIgKVMm8oW/9se9r8sA6qrVQSq66/O9P0RE28Z/znxDGbgd6jUS93//xsb/7dG/oLbBQRT9v1vuPTf9uiXgo///3et/f+g8P+ZVmQvPjXejyOo/v9//Q6FCwBQSwMELQAACAgAAAAhAFqYYHj//////////xgAFABHX+CkruCkueCkv+CksuCkvl9fMS5ucHkBABAA7AEAAAAAAAANAQAAAAAAAJvsF+obEMnIUMZQrZ6SWpxcpG6loF5Taqiuo6Cell9UUpSYF59flJIKEndLzClOBYoXZyQWpAL5GobGOgpGFpo6CrUKZAKu/zDw99l/XKBfxcGw4B1Wqe+Sn///m2iAVe8f5W9Acr/dL2TBfQ5sDg4yWoGihr4ODg6Cph5A0sFM0FlfPBMoK/H/f8HK/6GH/3/av9/oCljDVaf/u2P/I8tpljc0vPqTp53RYgiVq+RMSNACy2ktmnbk/x7PPznBF6Fy2bz37yeB5SzK5zsuumj3f8ttmJn/7kLNfKnz///0yv9ZZUj2AUHT5v9Zp/8Z3vgS8Oz/36wV/29H/T9ciOLHqwZncAbb/1sPUPkAUEsDBC0AAAAIAAAAIQADuJNb//////////8HABQATV9TLm5weQEAEADQAAAAAAAAAJMAAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkahgY6ChaaOgq1CuQCrv///168+B8ILpsUZJic+//f/9T//xds///33gMUXPf//z0L11awgv9359kU//8RCWT9tb7xX3PH///fdF/8P2noEaMzFyT/8v6///8BUEsDBC0AAAAIAAAAIQDuirZA//////////8HABQATV9SLm5weQEAEADIAAAAAAAAAH0AAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkaljoKFpo6CrUKZAOuVw1AsPrP/yvW+/fvj0n+f8X/////v+Ug9GXH/1dEHBxsxW+A+X9acyHi700h9H+N/6+Wg+gpnwFQSwMELQAAAAgAAAAhAKXkdTj//////////wcAFABNX0UubnB5AQAQAMYAAAAAAAAAbQAAAAAAAACb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"
def _load_tpl():
    z = np.load(io.BytesIO(base64.b64decode(_TPL)))
    out = {"R": {}, "G": {}, "M": {}, "S": {}}
    for k in z.files:
        grp, rest = k.split("_", 1)
        out[grp].setdefault(rest.split("__")[0], []).append(z[k])
    return out
TPL = _load_tpl()
STAMP_MASK = TPL["S"]["mask"][0]     # where the DELETED stamp sits on a 301x124 card

# ---------------------------------------------------------------------------
# Supplementary templates — added on top of TPL without ever touching the
# embedded _TPL blob above (never retyped/regenerated; that stays exactly
# as copied from parse_roll.py).
#
# WHY THIS EXISTS: the embedded _TPL set has status-letter templates for
# only S, R, E and # (TPL["M"] == {"S":.., "R":.., "E":.., "#":..}) — there
# is NO template for "Q" or "M" at all. Any card actually marked Q/M can
# therefore never match correctly; match() is forced to pick whichever of
# S/R/E/# scores (wrongly) highest. This was found directly: 8 rows
# flagged "status_letter_unclear" were checked against the real PDF pages
# and every one was visibly Q on the card, yet came out as E or S in the
# CSV — not a confidence problem, a missing-template problem.
#
# Fix: extract_q_templates.py (next to this file) pulled tight glyph crops
# of the "Q" marker from 3 of those confirmed cards (AC 86 part 101, pages
# 22/20, visually verified against the source PDF) and saved them to
# extra_templates.npz, in the same key format _load_tpl() above expects
# (e.g. "M_Q__0"). This loader merges that file's entries into TPL after
# the real one loads. No "M" (missing) examples have been found/confirmed
# yet, so that gap remains — a card genuinely marked "M" will still
# mismatch until a confirmed example is added the same way.
#
# extra_templates.npz is optional: if it isn't present, TPL just stays
# exactly as the embedded blob defines it (current behavior, unchanged).
# -----------------------------------------------------------------------
_EXTRA_TPL_PATH = pathlib.Path(__file__).resolve().parent / "extra_templates.npz"

def _load_extra_templates(tpl, path=_EXTRA_TPL_PATH):
    if not path.exists():
        return tpl
    try:
        z = np.load(path)
    except Exception as exc:
        print(f"[tpl] could not load {path.name}: {exc}", file=sys.stderr)
        return tpl
    added = []
    for k in z.files:
        grp, rest = k.split("_", 1)
        label = rest.split("__")[0]
        tpl.setdefault(grp, {}).setdefault(label, []).append(z[k])
        added.append(f"{grp}/{label}")
    if added:
        print(f"[tpl] loaded {len(added)} supplementary template(s) from {path.name}: {added}",
              file=sys.stderr)
    return tpl

TPL = _load_extra_templates(TPL)

def match(region, group):
    """Best template label, its score, and margin over the runner-up."""
    sc = {}
    for lab, ts in TPL[group].items():
        s = [cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED).max()
             for t in ts if t.shape[0] <= region.shape[0] and t.shape[1] <= region.shape[1]]
        if s: sc[lab] = float(max(s))
    if not sc: return None, 0.0, 0.0
    r = sorted(sc.items(), key=lambda t: -t[1])
    return r[0][0], r[0][1], r[0][1] - (r[1][1] if len(r) > 1 else 0.0)

def log(msg):
    print(msg, file=sys.stderr, flush=True)

# ------------------------------------------------------------------ pages & cards
def page_images(pdf):
    doc = fitz.open(pdf)
    for i, page in enumerate(doc):
        imgs = page.get_images(full=True)
        pix = fitz.Pixmap(doc, imgs[0][0]) if len(imgs) == 1 else page.get_pixmap(dpi=115)
        if pix.n - pix.alpha >= 4: pix = fitz.Pixmap(fitz.csRGB, pix)
        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
        yield i, cv2.cvtColor(a, cv2.COLOR_RGB2GRAY), len(doc)

def find_cards(g):
    bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (120, 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 60)))
    cnts, _ = cv2.findContours(cv2.dilate(h | v, np.ones((3, 3), np.uint8)), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for b in sorted([b for b in map(cv2.boundingRect, cnts) if 270 < b[2] < 320 and 100 < b[3] < 135],
                    key=lambda b: (b[1] // 40, b[0])):
        if not any(abs(b[0] - o[0]) < 10 and abs(b[1] - o[1]) < 10 for o in out): out.append(b)
    return out

def runs(mask):
    idx = np.flatnonzero(mask)
    if not len(idx): return []
    out, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i > p + 1: out.append((s, p)); s = i
        p = i
    return out + [(s, p)]

def text_lines(c):
    """Line centres in the body; threshold 80 ignores the grey DELETED stamp.
    Bands taller than one line (touching descenders) are split at their thinnest row."""
    prof = (c[:, 3:220] < 80).sum(1)
    rows = np.flatnonzero(prof[26:120] > 2) + 26
    if not len(rows): return []
    bands, s, p = [], rows[0], rows[0]
    for r in rows[1:]:
        if r > p + 2: bands.append((s, p)); s = r
        p = r
    bands.append((s, p))
    out = []
    stack = [b for b in bands if b[1] - b[0] >= 4]
    while stack:
        a, b = stack.pop(0)
        if b - a > 18:
            mid = a + 8 + int(np.argmin(prof[a + 8:b - 7]))
            stack[0:0] = [(a, mid - 1), (mid + 1, b)]
        else:
            out.append((a + b) // 2)
    return out

def colon_x(line, lo, hi, default, prefer=None):
    c = [b for a, b in runs((line < 110).any(0))
         if lo <= a <= hi and b - a <= 2 and len(runs((line[:, a:b + 1] < 110).any(1))) == 2]
    if not c: return default
    return min(c, key=lambda v: abs(v - prefer)) if prefer else c[0]

def label_end(line, lo=15, hi=80, mingap=3):
    """First real gap after a label word (the colon often touches the word before it)."""
    ink = (line < 110).any(0)
    for a, b in runs(~ink[lo:hi]):
        if b - a + 1 >= mingap and a > 0: return lo + a
    return None

def wipe_borders(crop, edge=3):
    """Whiten box border lines, but only within `edge` px of the crop sides
    (a digit 1 is also a tall dark column and must survive)."""
    crop = crop.copy(); h, w = crop.shape
    col = (crop < 128).mean(0) > 0.7; row = (crop < 128).mean(1) > 0.7
    col[edge:w - edge] = False; row[edge:h - edge] = False
    crop[:, col] = 255; crop[row, :] = 255
    return crop

def destamp(c, core=50, reach=2):
    """Remove the grey DELETED stamp: keep grey pixels only next to black text cores.
    Also thins some real strokes, so it is only a second opinion for OCR."""
    dark = (c < core).astype(np.uint8)
    near = cv2.dilate(dark, np.ones((2 * reach + 1, 2 * reach + 1), np.uint8)) > 0
    out = c.copy(); out[(c >= core) & ~near] = 255
    return out

def card_mask(c):
    """Stamp footprint resized to this card's exact size."""
    return cv2.resize(STAMP_MASK, (c.shape[1], c.shape[0]), interpolation=cv2.INTER_NEAREST) * 255

def cut_card(c, alt=None):
    """c = card image. Returns crops for OCR + pixel-derived facts.
    If alt is given, value crops are also cut from alt with identical geometry."""
    top_v = [a for a, b in runs((c[3:25, :] < 128).sum(0) > 14)]
    addition = any(60 < a < 85 for a in top_v)
    f, info = {}, {"list_type": "addition" if addition else "main"}
    if addition:
        f["serial"] = wipe_borders(c[4:24, 30:76]); f["section"] = wipe_borders(c[4:24, 94:155])
        f["epic"] = wipe_borders(c[3:24, 158:297])
    else:
        f["serial"] = wipe_borders(c[4:24, 30:95]); f["epic"] = wipe_borders(c[3:24, 150:297])
    info["marker_ink"] = int((c[6:21, 8:30] < 128).sum())
    info["stamp_px"] = int(((c[100:122, 80:225] > 60) & (c[100:122, 80:225] < 235)).sum())
    if info["marker_ink"] > 8:
        info["marker"] = match(np.pad(c[6:24, 6:34], 4, constant_values=255), "M")

    ys = text_lines(c)
    info["layout_ok"] = len(ys) in (4, 5)
    if not info["layout_ok"]: ys = [36, 50, 64, 78]
    L = lambda cy, x0=0, x1=222, src=None: (c if src is None else src)[max(cy - 9, 0):cy + 9, x0:x1]
    name_y, rel_y, house_ys, age_y = ys[0], ys[1], ys[2:-1], ys[-1]

    nx = colon_x(L(name_y), 24, 34, 29) + 3
    f["name"] = L(name_y, nx)
    info["relation"] = match(c[max(rel_y - 11, 0):rel_y + 11, 0:45], "R")
    rc = colon_x(L(rel_y), 20, 70, None, prefer=60)
    if rc is None:
        rc = {"अन्य": 29, "पति": 59}.get(info["relation"][0], 62)
    f["relation_name"] = L(rel_y, rc + 3)
    hc = colon_x(L(house_ys[0]), 58, 70, 64)
    house = lambda src=None: np.hstack([np.pad(p, ((0, 0), (0, 10)), constant_values=255) for p in
                                        [L(house_ys[0], hc + 3, src=src)] + [L(cy, 3, src=src) for cy in house_ys[1:]]])
    f["house_no"] = house()
    c1 = colon_x(L(age_y), 26, 36, 31)
    f["age"] = L(age_y, c1 + 2, c1 + 19)
    if alt is not None:
        f["name~"] = L(name_y, nx, src=alt); f["relation_name~"] = L(rel_y, rc + 3, src=alt)
        f["house_no~"] = house(alt); f["age~"] = L(age_y, c1 + 2, c1 + 19, src=alt)
        mk = card_mask(c)                # stamp footprint, cut exactly like the values
        f["mask:name"] = L(name_y, nx, src=mk); f["mask:relation_name"] = L(rel_y, rc + 3, src=mk)
        f["mask:house_no"] = house(mk); f["mask:age"] = L(age_y, c1 + 2, c1 + 19, src=mk)
    info["gender"] = match(np.pad(L(age_y, 60, 160), 4, constant_values=255), "G")
    info["wrapped_house"] = len(house_ys) > 1
    return f, info

def prep(crop, mask=None):
    """Trim to the text, pad, upscale. The digit 1 is drawn light grey, so grey counts as ink.
    On stamped cards (mask = stamp footprint) grey inside the footprint is stamp, not text,
    unless it is black (text on top of the stamp)."""
    ink = crop < 200
    if mask is not None:
        ink &= (mask == 0) | (crop < 50)
    ink &= (ink.sum(0) >= 2)[None, :]    # ignore isolated specks
    cols, rows = np.flatnonzero(ink.any(0)), np.flatnonzero(ink.any(1))
    if not len(cols): return None
    crop = crop[max(rows[0] - 2, 0):rows[-1] + 3, max(cols[0] - 2, 0):cols[-1] + 3]
    crop = np.pad(crop, ((3, 3), (6, 6)), constant_values=255)
    crop = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)

# ------------------------------------------------------------------ OCR models
def load_rec(candidates, device):
    from paddleocr import TextRecognition
    for m in candidates:
        try:
            r = TextRecognition(model_name=m, device=device) if device else TextRecognition(model_name=m)
            print(f"[model] loaded {m}", file=sys.stderr); return r, m
        except Exception as e:
            print(f"[model] {m} unavailable ({type(e).__name__}: {str(e)[:80]})", file=sys.stderr)
    raise RuntimeError(f"none of {candidates} could be loaded")

def run_rec(model, imgs, label="", bs=64, chunk=500):
    out, n, t0 = [], len(imgs), time.time()
    for i in range(0, n, chunk):
        for r in model.predict(input=imgs[i:i + chunk], batch_size=bs):
            d = r if "rec_text" in r else r.json.get("res", r.json)
            out.append((str(d["rec_text"]).strip(), float(d["rec_score"])))
        done = min(i + chunk, n)
        log(f"    {label} OCR: {done}/{n} crops ({done * 100 // n}%) - {time.time() - t0:.0f}s")
    return out

# ------------------------------------------------------------------ cleaning
def fix_epic(t):
    t = re.sub(r"[^A-Z0-9]", "", t.upper())
    if len(t) == 10:
        t = t[:3].translate(str.maketrans("0158264", "OISBZGA")) +             t[3:].translate(str.maketrans("OILSBZGDQ", "011582600"))
    return t

def clean(t):
    t = re.sub(r"^\s*(नाम|पिता|पति|माता|अन्य)?\s*(का\s*नाम)?\s*[:ः]\s*", "", t)  # leaked label
    return re.sub(r"^[\s:ः;.,|\-]+", "", t).strip()

def clean_hindi(t):
    """Names are Devanagari: drop Latin letters/junk stuck to the end (stamp remnants like EV, TEV, V)."""
    t = clean(t)
    if re.search(r"[\u0900-\u097F]", t):
        t = re.sub(r"[\s.\-_|]*[A-Za-z|][\sA-Za-z|.\-_]*$", "", t)   # only tails containing Latin/|
    return t.strip()


# ---------------------------------------------------------------------------
# Filename parsing -- unchanged from v1. Extracts the AC (assembly
# constituency) number and part number straight from the PDF's own
# filename, e.g.:
#   2026-EROLLGEN-S24-86-SIR-FinalRoll-Revision1-HIN-17-WI.pdf -> ac=86, part=17
# ---------------------------------------------------------------------------
FILENAME_RE = re.compile(
    r"(?P<year>\d{4})-EROLLGEN-(?P<state>[A-Z0-9]+)-(?P<ac>\d+)-.*?-(?P<part>\d+)-WI"
)


def parse_filename(path: pathlib.Path):
    m = FILENAME_RE.search(path.stem)
    if not m:
        return {"ac_number": "", "part_number": ""}
    return {"ac_number": m.group("ac"), "part_number": m.group("part")}