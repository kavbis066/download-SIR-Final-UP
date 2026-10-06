#!/usr/bin/env python3
"""
ocr_extract.py (v3) -- shared engine for ECI SIR Final Roll card extraction.

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

_TPL = "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P7MI0JMCC35vCZr3H2rm/4ffgdrTTkI4u7bCXDj15v/E48R7CABQSwMELQAACAgAAAAhAKSPij3//////////xgAFABHX+CkquClgeCksOClgeCkt19fMC5ucHkBABAAZwEAAAAAAADwAAAAAAAAAJvsF+obEMnIUMZQrZ6SWpxcpG6loF5Taqiuo6Cell9UUpSYF59flJIKEndLzClOBYoXZyQWpAL5GoaGOgpGhpo6CrUKZAKuP/fB4DYSef/XO2/5hARFj5SEWN6EhAwN1rgEroRn/48F/v8ffuD////Sj///z/Jt/C8JZB8HCz4qmifm7fk/a0PiLpDgTZv//10O3GB2vPdf4nfNnK+OfEDB/wHu5uIH/k+I/3vO8P8t7UBDbpDg/6efQGY2Rn688///r/t/JMGC/59HrgWSE5dAeMEgYuPbIz0Ou/6jgh3mRz4UTEUT/H8p0SH/G4oIAFBLAwQtAAAICAAAACEAagUKf///////////GAAUAEdf4KSq4KWB4KSw4KWB4KS3X18xLm5weQEAEABwAQAAAAAAAOsAAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkahkY6CkYGmjoKtQpkAq7/WMCJBDAIA5NpYDLvS2Xt/fvi927fX+t9//4ddb0be80f/e/q//9f6u///0cV6x7+11mQecP+//++bpBYh7h6/yTf/4avy2uBYjs87i5g/3uV99D/M67/o2dckwCK/Z8Y0CDy9/9mu4+zl/5/kRHaYA+xHGTeepNHYPYNiNgErSYgeformPNx+////3yuvf3ncwHV1TeMsyvV3qJ55c+xjahCAFBLAwQtAAAICAAAACEAGv9FYP//////////GAAUAEdf4KSu4KS54KS/4KSy4KS+X18wLm5weQEAEADfAQAAAAAAABYBAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkahsY6CkbmmjoKtQpkAq7/MPD82If/2EGxUYJdwH1sMt+kfv3/v0n7OrLYl/tgcEvu2p3793e63L4N4QOpZycSOBMStOzTFTxTExISJAMSQCBAMtZNuen/f4n//wtW/g89/P/BggVaV8BGXXX6vzv2P5KU/PQFC97/8FP3TNGFSMWxOTjIgKVMm8oW/9se9r8sA6qrVQSq66/O9P0RE28Z/znxDGbgd6jUS93//xsb/7dG/oLbBQRT9v1vuPTf9uiXgo///3et/f+g8P+ZVmQvPjXejyOo/v9//Q6FCwBQSwMELQAACAgAAAAhAFqYYHj//////////xgAFABHX+CkruCkueCkv+CksuCkvl9fMS5ucHkBABAA7AEAAAAAAAANAQAAAAAAAJvsF+obEMnIUMZQrZ6SWpxcpG6loF5Taqiuo6Cell9UUpSYF59flJIKEndLzClOBYoXZyQWpAL5GobGOgpGFpo6CrUKZAKu/zDw99l/XKBfxcGw4B1Wqe+Sn///m2iAVe8f5W9Acr/dL2TBfQ5sDg4yWoGihr4ODg6Cph5A0sFM0FlfPBMoK/H/f8HK/6GH/3/av9/oCljDVaf/u2P/I8tpljc0vPqTp53RYgiVq+RMSNACy2ktmnbk/x7PPznBF6Fy2bz37yeB5SzK5zsuumj3f8ttmJn/7kLNfKnz///0yv9ZZUj2AUHT5v9Zp/8Z3vgS8Oz/36wV/29H/T9ciOLHqwZncAbb/1sPUPkAUEsDBC0AAAAIAAAAIQADuJNb//////////8HABQATV9TLm5weQEAEADQAAAAAAAAAJMAAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkahgY6ChaaOgq1CuQCrv///168+B8ILpsUZJic+//f/9T//xds///33gMUXPf//z0L11awgv9359kU//8RCWT9tb7xX3PH///fdF/8P2noEaMzFyT/8v6///8BUEsDBC0AAAAIAAAAIQDuirZA//////////8HABQATV9SLm5weQEAEADIAAAAAAAAAH0AAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkaljoKFpo6CrUKZAOuVw1AsPrP/yvW+/fvj0n+f8X/////v+Ug9GXH/1dEHBxsxW+A+X9acyHi700h9H+N/6+Wg+gpnwFQSwMELQAAAAgAAAAhAKXkdTj//////////wcAFABNX0UubnB5AQAQAMYAAAAAAAAAbQAAAAAAAACb7BfqGxDJyFDGUK2eklqcXKRupaBeU2qorqOgnpZfVFKUmBefX5SSChJ3S8wpTgWKF2ckFqQC+RqGBjoK5po6CrUK5AKuT/dB4PcM0wQgeDmj9z8IwCiQYNX/GdVAFU//o8otNXIAglv/IQAAUEsDBC0AAAAIAAAAIQB20gEO//////////8HABQATV8jLm5weQEAEADYAAAAAAAAAJIAAAAAAAAAm+wX6hsQychQxlCtnpJanFykbqWgXlNqqK6joJ6WX1RSlJgXn1+UkgoSd0vMKU4FihdnJBakAvkahoY6ChaaOgq1CuQCrv8IUPb/fzGQ+pL7/38ikN6/6P/9pv+HE8wCEhydl///nw6U/vAfLJf6///hIKOEGO2E5f9Xbft/ag5QuOj9/wnXgHTn//99//7/BwBQSwMELQAAAAgAAAAhAKjY2qv//////////w0AFABTX21hc2tfXzAubnB5AQAQAEySAAAAAAAArgIAAAAAAADt2LGO00AQgOFsy1O4M0gpCFDxAHScaCioUMQFUSAOJXAN8BS8MGdI7NndWXsnkrNL/H/VnZOTdn+NN879vnn7+s07t7pf/Whvd4cP+/Zl0/78vmnXTfvxbv9tv/3y/m5/u+uuv9p+Puwerh8+bb/uHn5/vHn2Yt08f7p5sm5+NWd5tAIASO6f0sv4PxDLgFg2tDIglgGxDIhlQCwDYhkQy4BYBsQyIJYBsQzyYjm+RXayCrjB/CuqWMb+necSi6rV5PZd6DLrqk/G9qNWxJp6B7VMsfLefs3GNx8P07JrjdDuPGLpTl20WkUXVh8xUl4tRivi3X+M1qjgrFJGq+TqZnLmtsJjnVjjf9UnCc/4q201xMrfn4tSyVj+xF2p7A1qrVzwyEWs4X2x6IWZV1tY3g71VHGs2Zd7MeFmsreYSuXCV+dZdxFyN244nCf3KGsoc3SVrc6MFcRZxFgFhq1Pv1Gvo724cMkjarWQRyuLZKz8I28ZXES/XHiZNfBrjNQqu8wqBC2SsQousR5BjNSNWG6BNZE1wkkile5vktQZX3ht1eljxTdi4ZVVSEwU4zQpikWto1MU/V96xPIonWQeYkl6LPkysTpxFO2MolVHG6H+GvPkU2847xG01MoqpMcSPxKrM3aWy286BZdYj9FYtPL5IxRcppU0tAiq0CqWPshJFRn51KPVoL/5xIU+DWPlk7Gizz5S+dQxIpSuL8JUTVPaECswpFAGiVQeWSMeI1p5Js4nUp1o997QhqmSUkPFv/gUpxrEyiBraL2IJfijox/wONJi8fHnC249cZnJisgSsos/WmXWVp2cWGVWVrswln8JnvC56vhD0TVVy8lEIlvRRdXKyZOKWOOUB3diJfF0ZUArC1oZ0MqCVBakAgAAAAAAAAAAAAAAAIC0P1BLAQItAy0AAAgIAAAAIQAjMt5PyAAAAMABAAAVAAAAAAAAAAAAAACAAQAAAABSX+CkquCkv+CkpOCkvl9fMC5ucHlQSwECLQMtAAAICAAAACEAvYb+mMQAAADAAQAAFQAAAAAAAAAAAAAAgAEPAQAAUl/gpKrgpL/gpKTgpL5fXzEubnB5UEsBAi0DLQAACAgAAAAhANflCJ66AAAAoAEAABIAAAAAAAAAAAAAAIABGgIAAFJf4KSq4KSk4KS/X18wLm5weVBLAQItAy0AAAgIAAAAIQBBiJorvQAAAKABAAASAAAAAAAAAAAAAACAARgDAABSX+CkquCkpOCkv19fMS5ucHlQSwECLQMtAAAICAAAACEApsh2ZLsAAACwAQAAFQAAAAAAAAAAAAAAgAEZBAAAUl/gpK7gpL7gpKTgpL5fXzAubnB5UEsBAi0DLQAACAgAAAAhALjnacioAAAA0AEAABUAAAAAAAAAAAAAAIABGwUAAFJf4KSu4KS+4KSk4KS+X18xLm5weVBLAQItAy0AAAgIAAAAIQBiNB4A3QAAANABAAAVAAAAAAAAAAAAAACAAQoGAABSX+CkheCkqOCljeCkr19fMC5ucHlQSwECLQMtAAAICAAAACEApI+KPfAAAABnAQAAGAAAAAAAAAAAAAAAgAEuBwAAR1/gpKrgpYHgpLDgpYHgpLdfXzAubnB5UEsBAi0DLQAACAgAAAAhAGoFCn/rAAAAcAEAABgAAAAAAAAAAAAAAIABaAgAAEdf4KSq4KWB4KSw4KWB4KS3X18xLm5weVBLAQItAy0AAAgIAAAAIQAa/0VgFgEAAN8BAAAYAAAAAAAAAAAAAACAAZ0JAABHX+CkruCkueCkv+CksuCkvl9fMC5ucHlQSwECLQMtAAAICAAAACEAWphgeA0BAADsAQAAGAAAAAAAAAAAAAAAgAH9CgAAR1/gpK7gpLngpL/gpLLgpL5fXzEubnB5UEsBAi0DLQAAAAgAAAAhAAO4k1uTAAAA0AAAAAcAAAAAAAAAAAAAAIABVAwAAE1fUy5ucHlQSwECLQMtAAAACAAAACEA7oq2QH0AAADIAAAABwAAAAAAAAAAAAAAgAEgDQAATV9SLm5weVBLAQItAy0AAAAIAAAAIQCl5HU4bQAAAMYAAAAHAAAAAAAAAAAAAACAAdYNAABNX0UubnB5UEsBAi0DLQAAAAgAAAAhAHbSAQ6SAAAA2AAAAAcAAAAAAAAAAAAAAIABfA4AAE1fIy5ucHlQSwECLQMtAAAACAAAACEAqNjaq64CAABMkgAADQAAAAAAAAAAAAAAgAFHDwAAU19tYXNrX18wLm5weVBLBQYAAAAAEAAQAPYDAAA0EgAAAAA="
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

def S(v, s):
    """Scale a constant that was calibrated in AC-86 pixels (card = 300x124,
    page = 949 wide) to a page whose pixel grid is `s` times bigger/smaller.
    At s == 1.0 this returns round(v) == v for every integer constant used
    below, so AC 86 output is unchanged."""
    return int(round(v * s))


def to_ref(region, s):
    """Bring a region cut at scale `s` back to the 300x124 reference grid, which
    is where the embedded templates (TPL) live. Only used for template matching
    -- OCR crops are never shrunk. INTER_AREA when shrinking (anti-aliased),
    INTER_CUBIC when enlarging."""
    if s == 1.0:
        return region
    h, w = region.shape[:2]
    nw, nh = max(1, int(round(w / s))), max(1, int(round(h / s)))
    return cv2.resize(region, (nw, nh), interpolation=cv2.INTER_AREA if s > 1 else cv2.INTER_CUBIC)


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
CALIBRATED_PAGE_WIDTH = 949  # width of the AC-86 page image every hardcoded constant was tuned on
CARD_REF_W, CARD_REF_H = 300, 124


def page_images(pdf):
    """Yields (page_index, gray_page_at_NATIVE_resolution, n_pages). Pages are
    never resized here: the rest of the engine is scale-aware (see S())."""
    doc = fitz.open(pdf)
    for i, page in enumerate(doc):
        imgs = page.get_images(full=True)
        pix = fitz.Pixmap(doc, imgs[0][0]) if len(imgs) == 1 else page.get_pixmap(dpi=115)
        if pix.n - pix.alpha >= 4: pix = fitz.Pixmap(fitz.csRGB, pix)
        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
        yield i, cv2.cvtColor(a, cv2.COLOR_RGB2GRAY), len(doc)


def page_scale(g):
    """Pixel-grid scale of this page relative to the AC-86 calibration page.
    Snapped to exactly 1.0 within 2% so AC 86 takes the identical code path."""
    s = g.shape[1] / CALIBRATED_PAGE_WIDTH
    return 1.0 if abs(s - 1.0) < 0.02 else s


_KERNELS = {}

def _kernels(s):
    k = _KERNELS.get(s)
    if k is None:
        k = _KERNELS[s] = (cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, S(120, s)), 1)),
                           cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, S(60, s)))),
                           np.ones((3, 3), np.uint8))
    return k


def find_cards(g, s=None):
    """Card boxes (x, y, w, h) in reading order (row by row, left to right),
    at page scale `s` (default: derived from the page width)."""
    if s is None: s = page_scale(g)
    kh, kv, dil = _kernels(s)
    bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kh)
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kv)
    cnts, _ = cv2.findContours(cv2.dilate(h | v, dil), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cand = [b for b in map(cv2.boundingRect, cnts)
            if 270 * s < b[2] < 320 * s and 100 * s < b[3] < 135 * s]
    cand.sort(key=lambda b: (b[1], b[0]))           # outer frame (smaller x/y) wins over its inner twin
    tol = max(3, S(10, s))
    out = []
    for b in cand:
        if not any(abs(b[0] - o[0]) < tol and abs(b[1] - o[1]) < tol for o in out):
            out.append(b)
    # Reading order. The old key (y // 40, x) could split one visual row in two
    # when the row's y straddled a multiple of 40 px, putting cards out of
    # order (=> out-of-sequence serials). Cluster rows by real y distance instead.
    out.sort(key=lambda b: (b[1], b[0]))
    rows, cur, y0 = [], [], None
    for b in out:
        if y0 is None or b[1] - y0 > 0.5 * b[3]:
            if cur: rows.append(cur)
            cur, y0 = [b], b[1]
        else:
            cur.append(b)
    if cur: rows.append(cur)
    return [b for r in rows for b in sorted(r, key=lambda b: b[0])]


def detect_cards(g, s_hint=None):
    """Returns (boxes, s). Tries the page-width scale first (or `s_hint`); if that
    finds nothing sensible it sweeps nearby scales, so an unexpected DPI degrades
    to a slower search instead of silently producing zero cards."""
    s0 = s_hint if s_hint else page_scale(g)
    boxes = find_cards(g, s0)
    if len(boxes) >= 3:
        return boxes, s0
    best = (boxes, s0)
    for f in (0.94, 1.06, 0.88, 1.12, 0.8, 1.2, 0.7, 1.35, 0.6, 1.5):
        s1 = s0 * f
        b1 = find_cards(g, s1)
        if len(b1) > len(best[0]):
            best = (b1, s1)
        if len(b1) >= 20:
            break
    boxes, s1 = best
    if len(boxes) >= 3:
        # refine from the cards actually found (301 = AC-86 card width incl. 3x3 dilation)
        s1 = float(np.median([b[2] for b in boxes])) / 301.0
        s1 = 1.0 if abs(s1 - 1.0) < 0.02 else s1
        boxes = find_cards(g, s1) or boxes
    return boxes, s1


def card_at_scale(img, s):
    """Pages narrower than the calibration page (s < 0.95) have thinner,
    fainter strokes than the line/colon detectors were tuned for. Enlarging
    such a CARD to the reference grid (never shrinking one) restores them, so
    the rest of the engine just runs at s = 1. Returns (card, effective_scale)."""
    if s >= 0.95:
        return img, s
    h, w = img.shape[:2]
    up = cv2.resize(img, (int(round(w / s)), int(round(h / s))), interpolation=cv2.INTER_CUBIC)
    # Down-sampling blurred the strokes, so the darkest ink is no longer as dark
    # as the fixed ink thresholds (<80 / <110 / <128) assume: stretch the levels
    # so the darkest 0.3% of pixels map back to ~0.
    lo = float(np.percentile(up, 0.3))
    if 25 < lo < 200:
        up = np.clip((up.astype(np.float32) - lo) * (255.0 / (255.0 - lo)), 0, 255).astype(np.uint8)
    return up, 1.0


def runs(mask):
    idx = np.flatnonzero(mask)
    if not len(idx): return []
    out, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i > p + 1: out.append((s, p)); s = i
        p = i
    return out + [(s, p)]


# (ink threshold, row-gap, min band height, label) tried in order until a layout
# with 4 or 5 text lines comes out. The first row is the original behaviour.
_LINE_PASSES = ((2.0, 2.0, 4.0), (1.0, 2.0, 3.0), (2.0, 1.0, 3.0), (1.0, 1.0, 3.0), (1.0, 3.0, 3.0))


def _text_lines_pass(c, s, ink_thr, gap, min_h):
    prof = (c[:, S(3, s):S(220, s)] < 80).sum(1)
    lo = S(26, s)
    rows = np.flatnonzero(prof[lo:S(120, s)] > ink_thr * s) + lo
    if not len(rows): return []
    bands, st, p = [], rows[0], rows[0]
    for r in rows[1:]:
        if r > p + gap * s: bands.append((st, p)); st = r
        p = r
    bands.append((st, p))
    out = []
    stack = [b for b in bands if b[1] - b[0] >= min_h * s]
    while stack:
        a, b = stack.pop(0)
        if b - a > 18 * s:
            a8, b7 = a + S(8, s), b - S(7, s)
            if b7 > a8:
                mid = a8 + int(np.argmin(prof[a8:b7]))
                stack[0:0] = [(a, mid - 1), (mid + 1, b)]
                continue
        out.append((a + b) // 2)
    return out


def text_lines(c, s=1.0, retry=True):
    """Line centres in the body; threshold 80 ignores the grey DELETED stamp.
    Bands taller than one line (touching descenders) are split at their thinnest row.
    Every size constant is scaled by `s`. If the first pass doesn't give a
    plausible 4- or 5-line card, progressively looser passes are tried."""
    first = None
    for i, (thr, gap, mh) in enumerate(_LINE_PASSES):
        ys = _text_lines_pass(c, s, thr, gap, mh)
        if first is None: first = ys
        if len(ys) in (4, 5) or not retry:
            return ys
    return first


def colon_x(line, lo, hi, default, prefer=None, s=1.0):
    lo, hi = S(lo, s), S(hi, s)
    maxw = 2 * s + 0.5            # colon is <=2 px wide at s=1 (3 px at 1.25 ...)
    c = [b for a, b in runs((line < 110).any(0))
         if lo <= a <= hi and b - a <= maxw and len(runs((line[:, a:b + 1] < 110).any(1))) == 2]
    if not c: return None if default is None else S(default, s)
    return min(c, key=lambda v: abs(v - S(prefer, s))) if prefer else c[0]

def label_end(line, lo=15, hi=80, mingap=3, s=1.0):
    """First real gap after a label word (the colon often touches the word before it)."""
    lo, hi, mingap = S(lo, s), S(hi, s), max(1, S(mingap, s))
    ink = (line < 110).any(0)
    for a, b in runs(~ink[lo:hi]):
        if b - a + 1 >= mingap and a > 0: return lo + a
    return None

def wipe_borders(crop, edge=3, s=1.0):
    """Whiten box border lines, but only within `edge` px of the crop sides
    (a digit 1 is also a tall dark column and must survive)."""
    edge = max(1, S(edge, s))
    crop = crop.copy(); h, w = crop.shape
    col = (crop < 128).mean(0) > 0.7; row = (crop < 128).mean(1) > 0.7
    col[edge:w - edge] = False; row[edge:h - edge] = False
    crop[:, col] = 255; crop[row, :] = 255
    return crop

def destamp(c, core=50, reach=2, s=1.0):
    """Remove the grey DELETED stamp: keep grey pixels only next to black text cores.
    Also thins some real strokes, so it is only a second opinion for OCR."""
    reach = max(1, S(reach, s))
    dark = (c < core).astype(np.uint8)
    near = cv2.dilate(dark, np.ones((2 * reach + 1, 2 * reach + 1), np.uint8)) > 0
    out = c.copy(); out[(c >= core) & ~near] = 255
    return out

def card_mask(c):
    """Stamp footprint resized to this card's exact size."""
    return cv2.resize(STAMP_MASK, (c.shape[1], c.shape[0]), interpolation=cv2.INTER_NEAREST) * 255


def stamp_px(c, s=1.0):
    """Amount of grey (stamp) ink in the stamp window, normalised to AC-86 pixels."""
    w = c[S(100, s):S(122, s), S(80, s):S(225, s)]
    return int(round(((w > 60) & (w < 235)).sum() / (s * s)))


def serial_layout(c, s):
    """Locate the serial box (and, on addition-list cards, the section box) by
    their own border lines instead of fixed x offsets.

    Why: the serial box is NOT a fixed width. Its right border sits anywhere
    from x~76 to x~104 (AC-86 pixels) depending on the card (digit count, and a
    status marker such as # / Q printed inside the box). The old fixed crop
    30:95 therefore (a) cut the last digit(s) off when the border was further
    right, (b) kept the border line as 'ink' when it was further left, and
    (c) the old "addition card" test (a vertical line at 60<x<85) misfired on
    ordinary cards whose box border landed there -- giving a 46-px crop that
    chopped off the last digit ('54' -> '5', '24' -> '2').
    A card is an addition-list card when it has TWO boxes (>=4 vertical borders)."""
    top = c[S(3, s):S(25, s), :]
    bd = [(a, b) for a, b in runs((top < 128).sum(0) > 14 * s) if S(2, s) < a < S(160, s)]
    out = {"addition": len(bd) >= 4, "exact": False}
    m = max(1, S(1, s))
    if len(bd) >= 2:
        xr = bd[1][0] - m
        if S(55, s) <= xr <= S(125, s):
            out["ser"] = (S(30, s), xr)
            xin0, xin1 = bd[0][1] + 1, bd[1][0]
            # horizontal border rows; the first line (y~1) is the card frame, not the box
            hr = [r for r in runs(((c[:S(32, s), xin0:xin1] < 128).mean(1) > 0.7)) if r[0] >= S(3, s)]
            if len(hr) >= 2 and hr[1][0] - hr[0][1] > S(8, s):
                out["y"] = (hr[0][1] + 1 + m, hr[1][0] - m)
                out["exact"] = True
    if out["addition"] and bd[3][0] - bd[2][1] > S(20, s):
        out["sec"] = (bd[2][1] + 1 + m, bd[3][0] - m)
    return out


def serial_alt_crops(c, s, lay=None):
    """Extra crops to re-read a serial that disagrees with its neighbours."""
    lay = lay or serial_layout(c, s)
    x1 = lay["ser"][1] if "ser" in lay else S(95, s)
    y0, y1 = lay.get("y", (S(4, s), S(24, s)))
    out = [c[S(5, s):S(22, s), S(30, s):x1 + max(1, S(1, s))],           # looser: 1px more on the right
           c[y0:y1, S(40, s):x1],                                          # drop anything left of the digits
           wipe_borders(c[S(4, s):S(24, s), S(30, s):S(95, s)], s=s),      # the old fixed crop
           c[max(y0 - S(2, s), 0):y1 + S(2, s), S(24, s):x1]]              # taller + wider on the left
    return [o for o in out if o.size]


def cut_card(c, alt=None, s=1.0):
    """c = card image (cut at page scale `s`). Returns crops for OCR + pixel-derived facts.
    If alt is given, value crops are also cut from alt with identical geometry.
    All pixel constants are the AC-86 ones, scaled through S()."""
    lay = serial_layout(c, s)
    addition = lay["addition"]
    f, info = {}, {"list_type": "addition" if addition else "main"}
    wb = lambda x: wipe_borders(x, s=s)
    if "ser" in lay:
        y0, y1 = lay.get("y", (S(4, s), S(24, s)))
        ser = c[y0:y1, lay["ser"][0]:lay["ser"][1]]
        # inside the located borders there is nothing to wipe -- and wipe_borders
        # would delete a trailing '1' / the stem of a '4' sitting next to the edge
        f["serial"] = ser if lay["exact"] else wb(ser)
    else:
        f["serial"] = wb(c[S(4, s):S(24, s), S(30, s):S(76 if addition else 95, s)])
    if addition:
        if "sec" in lay:
            f["section"] = c[y0:y1, lay["sec"][0]:lay["sec"][1]] if lay["exact"] else wb(c[S(4, s):S(24, s), lay["sec"][0]:lay["sec"][1]])
        else:
            f["section"] = wb(c[S(4, s):S(24, s), S(94, s):S(155, s)])
        f["epic"] = wb(c[S(3, s):S(24, s), S(158, s):S(297, s)])
    else:
        f["epic"] = wb(c[S(3, s):S(24, s), S(150, s):S(297, s)])
    info["marker_ink"] = int(round((c[S(6, s):S(21, s), S(8, s):S(30, s)] < 128).sum() / (s * s)))
    info["stamp_px"] = stamp_px(c, s)
    if info["marker_ink"] > 8:
        info["marker"] = match(np.pad(to_ref(c[S(6, s):S(24, s), S(6, s):S(34, s)], s), 4, constant_values=255), "M")

    ys = text_lines(c, s)
    info["layout_ok"] = len(ys) in (4, 5)
    if not info["layout_ok"]:
        k = c.shape[0] / float(CARD_REF_H)       # proportional (not absolute-pixel) fallback
        ys = [int(round(v * k)) for v in (36, 50, 64, 78)]
    L = lambda cy, x0=0, x1=None, src=None, half=None: (c if src is None else src)[
        max(cy - (S(9, s) if half is None else half), 0):cy + (S(9, s) if half is None else half),
        x0:(S(222, s) if x1 is None else x1)]
    name_y, rel_y, house_ys, age_y = ys[0], ys[1], ys[2:-1], ys[-1]

    nx = colon_x(L(name_y), 24, 34, 29, s=s) + S(3, s)
    f["name"] = L(name_y, nx)
    info["relation"] = match(to_ref(c[max(rel_y - S(11, s), 0):rel_y + S(11, s), 0:S(45, s)], s), "R")
    rc = colon_x(L(rel_y), 20, 70, None, prefer=60, s=s)
    if rc is None:
        rc = S({"अन्य": 29, "पति": 59}.get(info["relation"][0], 62), s)
    f["relation_name"] = L(rel_y, rc + S(3, s))
    hc = colon_x(L(house_ys[0]), 58, 70, 64, s=s)
    padw = S(10, s)
    house = lambda src=None: np.hstack([np.pad(p, ((0, 0), (0, padw)), constant_values=255) for p in
                                        [L(house_ys[0], hc + S(3, s), src=src)] + [L(cy, S(3, s), src=src) for cy in house_ys[1:]]])
    f["house_no"] = house()
    c1 = colon_x(L(age_y), 26, 36, 31, s=s)
    f["age"] = L(age_y, c1 + S(2, s), c1 + S(19, s))
    if alt is not None:
        f["name~"] = L(name_y, nx, src=alt); f["relation_name~"] = L(rel_y, rc + S(3, s), src=alt)
        f["house_no~"] = house(alt); f["age~"] = L(age_y, c1 + S(2, s), c1 + S(19, s), src=alt)
        mk = card_mask(c)                # stamp footprint, cut exactly like the values
        f["mask:name"] = L(name_y, nx, src=mk); f["mask:relation_name"] = L(rel_y, rc + S(3, s), src=mk)
        f["mask:house_no"] = house(mk); f["mask:age"] = L(age_y, c1 + S(2, s), c1 + S(19, s), src=mk)
    info["gender"] = match(np.pad(to_ref(L(age_y, S(60, s), S(160, s)), s), 4, constant_values=255), "G")
    info["wrapped_house"] = len(house_ys) > 1
    # geometry kept so a suspicious name / serial can be re-cropped later
    info["geom"] = dict(s=s, name_y=name_y, nx=nx, rel_y=rel_y, rc=rc, addition=addition, lay=lay)
    return f, info


def line_variants(c, geom, field, src=None):
    """Alternative crops of the name / relation_name line, used only when the
    first read is suspiciously short or low-confidence: different window
    heights and a start point a little left of the detected colon (a colon
    mis-detection is what cuts a name down to its last letter)."""
    s = geom["s"]
    cy, x0 = (geom["name_y"], geom["nx"]) if field == "name" else (geom["rel_y"], geom["rc"] + S(3, s))
    src = c if src is None else src
    out = []
    for half, dx in ((S(9, s), -S(6, s)), (S(11, s), 0), (S(7, s), 0), (S(11, s), -S(6, s))):
        crop = src[max(cy - half, 0):cy + half, max(x0 + dx, 0):S(222, s)]
        if crop.size: out.append(crop)
    return out


def prep(crop, mask=None, s=1.0):
    """Trim to the text, pad, upscale. The digit 1 is drawn light grey, so grey counts as ink.
    On stamped cards (mask = stamp footprint) grey inside the footprint is stamp, not text,
    unless it is black (text on top of the stamp).
    The upscale factor is 3/s, so the recogniser always receives text at the same
    physical size (~48 px tall) whatever the source DPI."""
    ink = crop < 200
    if mask is not None:
        ink &= (mask == 0) | (crop < 50)
    ink &= (ink.sum(0) >= max(1, S(2, s)))[None, :]    # ignore isolated specks
    cols, rows = np.flatnonzero(ink.any(0)), np.flatnonzero(ink.any(1))
    if not len(cols): return None
    m2, m3 = S(2, s), S(3, s)
    crop = crop[max(rows[0] - m2, 0):rows[-1] + m3, max(cols[0] - m2, 0):cols[-1] + m3]
    crop = np.pad(crop, ((m3, m3), (S(6, s), S(6, s))), constant_values=255)
    f = 3.0 / s
    crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f >= 1 else cv2.INTER_AREA)
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

def load_rec_all(candidates, device):
    """Like load_rec(), but loads every candidate that's available instead of
    stopping at the first success. Used to run two Hindi recognition models
    (v5 + v3 mobile) as a second opinion on name/relation_name — verified
    against real cards that these models can confidently misread a correctly
    cropped, legible conjunct (e.g. रवींद्र -> र्वीद्र, चन्द्र -> चनद्र): the
    crop isn't the problem, the single model's reading sometimes is. A second
    model voting (highest score wins, picked the same way the existing
    stamped/destamp "~" alt crop is already picked in process_pdf's get())
    costs double the Hindi-field OCR time but doesn't touch the sensitive
    cropping/template code at all. Returns a list of (model, name) for every
    candidate that loaded; empty list if none did (caller decides how to
    treat that — see run_rec_voted below)."""
    from paddleocr import TextRecognition
    loaded = []
    for m in candidates:
        try:
            r = TextRecognition(model_name=m, device=device) if device else TextRecognition(model_name=m)
            print(f"[model] loaded {m}", file=sys.stderr)
            loaded.append((r, m))
        except Exception as e:
            print(f"[model] {m} unavailable ({type(e).__name__}: {str(e)[:80]})", file=sys.stderr)
    return loaded

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

# Devanagari letters + vowel signs + virama etc. (U+0900-0963, U+0971-097F);
# excludes danda/double danda (0964/0965), Devanagari digits (0966-096F) and
# the abbreviation sign (0970). ZWJ/ZWNJ are kept only between letters.
_DEV_OK = re.compile(r"[\u0900-\u0963\u0971-\u097F]")

def clean_hindi(t):
    """Names are Devanagari. Whitelist filter: keep Devanagari letters/signs and
    single spaces; everything else (Latin stamp remnants, . , - _ | : ; quotes,
    brackets, digits, dandas, stray symbols) is dropped wherever it occurs --
    not only at the ends, which is all the old version handled."""
    t = clean(t)
    if not t: return ""
    out = []
    for i, ch in enumerate(t):
        if _DEV_OK.match(ch):
            out.append(ch)
        elif ch in "\u200c\u200d":
            if out and out[-1] != " " and i + 1 < len(t) and _DEV_OK.match(t[i + 1]):
                out.append(ch)
        elif ch.isspace():
            if out and out[-1] != " ": out.append(" ")
        # else: drop
    return "".join(out).strip()


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