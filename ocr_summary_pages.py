#!/usr/bin/env python3
"""
ocr_summary_pages.py  --  reads ONLY page 1 and the LAST page of every roll PDF in a folder and writes ONE Excel
file (.xlsx, one row per PDF). Meant for checking the card-level extraction (ocr_extract_paddleocr.py) against the
totals the roll itself prints. There is no CSV: the workbook is the only output.

  page 1 :  AC name (first row of the table: the text after "174 - ", e.g. "लखनऊ मध्य (सामान्य)"), part number,
            booth number + booth name ("मतदान स्थल की संख्या और नाम"), and the voter counts of section 4
  last   :  table "A) निर्वाचकों की संख्या":  मूल नामावली, परिवर्धन की सूची, विलोपन की सूची and the net electors,
            each with male / female / third gender / total

Columns : AC_Part first, then AC Name, Booth No., Booth Name, Total Electors, the four coloured count blocks
(Mool Namavali / Additions / Deletions / Net electors, each Male | Female | Third gender | Total) and, for checking,
AC / Part number as printed in the PDF, the page-1 counts, PDF file, pages, Needs review, Review reason, Confidence.
Names are Hindi exactly as PaddleOCR read them (word by word; no transliteration, no dictionary). The AC name is the
same in every PDF of an AC, so a misread one is replaced by the reading most PDFs agree on (needs >= 3 agreeing;
flagged ac_name_by_majority).

Checks on every PDF (a failed check sets Needs review and says why in Review reason):
  male + female + third = total (page 1 and every row of the last page), original + additions - deletions
  (+ gender-correction row) = net, net (last page) = page-1 counts, AC / part number in the PDF = file name.
A single misread digit that breaks a row total is repaired from the models' other readings when exactly one
combination satisfies the sums (flagged repaired_by_sum).

Usage
  python ocr_summary_pages.py --folder downloads/174 --out summary_174.xlsx
  python ocr_summary_pages.py --root downloads --only 1 2 174 --out summaries/        # one .xlsx per folder
  python ocr_summary_pages.py --root downloads --only-file first.txt --out summaries/ --workers 3 --cpu-threads 2
  --limit N tests on the first N PDFs of each folder. Re-running the same command resumes: PDFs already in the
  workbook are skipped (--force to redo them). Close the .xlsx in Excel before a re-run (Excel locks the file).
"""
import argparse
import concurrent.futures
import itertools
import os
import pathlib
import re
import sys
import time
import unicodedata

import cv2
import numpy as np

VERSION = "s2"
REF_W = 1187.0          # page width the layout constants below were measured on (AC 1/2/174 PDFs); everything scales with W/REF_W

FIELDS = ["ac_part", "ac_name", "booth_number", "booth_name", "last_serial", "total_electors"]
GROUPS = [("mool", "mool"), ("parivardhan", "add"), ("vilopan", "del"), ("net", "net")]
for _g, _ in GROUPS:
    FIELDS += [f"{_g}_male", f"{_g}_female", f"{_g}_third", f"{_g}_total"]
FIELDS += ["ac_number", "part_number", "page1_male", "page1_female", "page1_third",
           "pdf", "pages", "needs_review", "review_reason", "confidence"]


_DIG = str.maketrans("०१२३४५६७८९", "0123456789")


# ----------------------------------------------------------------------------------------------- page / line helpers
def get_page(doc, i):
    """Gray image of page i (the pages are single embedded scans). Only this page is decoded."""
    import fitz
    page = doc[i]
    imgs = page.get_images(full=True)
    pix = fitz.Pixmap(doc, imgs[0][0]) if len(imgs) == 1 else page.get_pixmap(dpi=115)
    if pix.n - pix.alpha >= 4:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)
    if pix.n - pix.alpha == 1:
        return np.ascontiguousarray(a[:, :, 0])
    return cv2.cvtColor(np.ascontiguousarray(a[:, :, :3]), cv2.COLOR_RGB2GRAY)


class Page:
    def __init__(self, g):
        self.g = g
        self.H, self.W = g.shape
        s = self.W / REF_W
        self.s = 1.0 if abs(s - 1.0) < 0.02 else s
        ink = (g < 150).astype(np.uint8)
        self.hm = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, int(60 * self.s)), 1)))
        self.vm = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, int(30 * self.s)))))

    def S(self, v):
        return int(round(v * self.s))

    @staticmethod
    def _runs(flags):
        idx = np.flatnonzero(flags)
        if not len(idx):
            return []
        cuts = np.flatnonzero(np.diff(idx) > 3) + 1
        return [float(np.mean(r)) for r in np.split(idx, cuts)]

    def hlines(self, x0, x1, frac=0.85, y0=0, y1=None):
        y1 = self.H if y1 is None else y1
        x0, x1 = max(int(x0), 0), min(int(x1), self.W)
        prof = self.hm[y0:y1, x0:x1].sum(1)
        return [y + y0 for y in self._runs(prof >= frac * (x1 - x0))]

    def vlines(self, y0, y1, frac=0.8, x0=0, x1=None):
        x1 = self.W if x1 is None else x1
        y0, y1 = max(int(y0), 0), min(int(y1), self.H)
        prof = self.vm[y0:y1, int(x0):int(x1)].sum(0)
        return [x + int(x0) for x in self._runs(prof >= frac * (y1 - y0))]

    def crop(self, x0, y0, x1, y1):
        return self.g[max(int(y0), 0):min(int(y1), self.H), max(int(x0), 0):min(int(x1), self.W)]


def prep(crop, s):
    """Trim to the ink, pad, scale to ~48 px text height (same recipe as the card engine's prep())."""
    ink = crop < 200
    cols, rows = np.flatnonzero(ink.sum(0) >= 1), np.flatnonzero(ink.any(1))
    if not len(cols) or not len(rows):
        return None
    m2, m3 = max(2, int(2 * s)), max(3, int(3 * s))
    crop = crop[max(rows[0] - m2, 0):rows[-1] + m3, max(cols[0] - m2, 0):cols[-1] + m3]
    crop = np.pad(crop, ((m3, m3), (int(6 * s), int(6 * s))), constant_values=255)
    f = 3.0 / s
    crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f >= 1 else cv2.INTER_AREA)
    return cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)


def first_segment(gray, s, gap=7):
    """Leftmost group of ink columns (a leading number such as '174' or '12') -> prepped crop, or None."""
    cols = np.flatnonzero((gray < 200).sum(0) >= 1)
    if not len(cols):
        return None
    cut = np.flatnonzero(np.diff(cols) > max(4, int(gap * s)))
    end = cols[cut[0]] if len(cut) else cols[-1]
    return prep(gray[:, :end + 2], s)


def word_crops(gray, s, number_first=False):
    """One text line (gray, trimmed to its ink rows) -> one crop per WORD. The recognition models were trained on
    short crops: a whole 480 px line is squeezed into the model's input width, which drops spaces and conjunct
    letters (लखनऊ मध्य -> लखनऊमधय, कक्ष -> क्ष). Words are cut at the gaps between them (3+ empty pixel columns; the
    headline bar joins the letters inside a word, so gaps only occur between words).
    number_first: the line starts with "<number> - ": the digits of the number are glued into ONE crop (the thin digit 1
    sits a few pixels away from the next digit)."""
    cols = np.flatnonzero((gray < 200).any(0))
    if not len(cols):
        return []
    cut = np.flatnonzero(np.diff(cols) > max(2, int(round(3.0 * s)))) + 1
    spans = [(seg[0], seg[-1] + 1) for seg in np.split(cols, cut)]

    def hgt(x0, x1):
        rows = np.flatnonzero((gray[:, x0:x1] < 200).any(1))
        return (rows[-1] - rows[0] + 1) if len(rows) else 0

    if number_first:
        for d in range(1, min(len(spans), 5)):
            x0, x1 = spans[d]
            if x1 - x0 < 11 * s and hgt(x0, x1) < 7 * s:          # the "-" after the number
                spans = [(spans[0][0], spans[d - 1][1])] + spans[d:]
                break
    return [{"img": prep(gray[:, x0:x1], s), "w": x1 - x0, "h": hgt(x0, x1)} for x0, x1 in spans]


def read_words(reader, lines):
    """lines: list of lists of word dicts. Reads every word with all Hindi models (best score wins) and
    returns, per line, a list of (text, score, w, h)."""
    flat = [wd for ln in lines for wd in ln]
    res = reader.read_hi([wd["img"] for wd in flat])
    texts = [best_text(r) for r in res]
    out, k = [], 0
    for ln in lines:
        row = []
        for wd in ln:
            t, sc = texts[k]; k += 1
            row.append((t.strip(), sc, wd["w"], wd["h"]))
        out.append(row)
    return out


def is_dash(tok, s):
    t, sc, w, h = tok
    return t in ("-", "–", "—", "_", "", ".", "।") or (w < 11 * s and h < 7 * s)


def vote_with_address(name_toks, addr_toks):
    """A word of the booth name that is read differently in the address line (same wording, read again) is taken
    from whichever reading scored higher. Only near-identical words (same length +-1, similarity >= 0.75)."""
    import difflib
    out = []
    for t, sc, *_ in name_toks:
        best = (t, sc)
        for a, asc, *_ in addr_toks:
            if a != t and abs(len(a) - len(t)) <= 1 and asc > best[1] and difflib.SequenceMatcher(None, a, t).ratio() >= 0.75:
                best = (a, asc)
        out.append(best)
    return out


# ----------------------------------------------------------------------------------------------- recognisers
class Reader:
    """read_hi(list of images) -> per image: list of (text, score), one per Hindi model;  read_en likewise."""
    def read_hi(self, imgs): raise NotImplementedError
    def read_en(self, imgs): raise NotImplementedError


class PaddleReader(Reader):
    def __init__(self, device=None):
        import ocr_extract as base
        self.base = base
        base.log = lambda *a, **k: None                       # silence the per-batch progress lines
        self.hi = base.load_rec_all(base.HI_MODELS, device)
        if not self.hi:
            raise RuntimeError(f"none of {base.HI_MODELS} could be loaded")
        self.en, _ = base.load_rec(base.EN_MODELS, device)

    def _run(self, models, imgs):
        res = [[] for _ in imgs]
        ok = [i for i, im in enumerate(imgs) if im is not None]
        if not ok:
            return res
        for m, _name in models:
            out = self.base.run_rec(m, [imgs[i] for i in ok], "")
            for i, r in zip(ok, out):
                res[i].append(r)
        return res

    def read_hi(self, imgs): return self._run(self.hi, imgs)
    def read_en(self, imgs): return self._run([(self.en, "en")], imgs)


class TessReader(Reader):
    """Development stand-in (no PaddleOCR in some sandboxes): Tesseract. Select with --engine tess."""
    def __init__(self, tessdata=None):
        import subprocess, shutil
        self.sp, self.bin = subprocess, shutil.which("tesseract") or "tesseract"
        self.dir = tessdata or os.environ.get("OCR_TESSDATA")

    def _one(self, im, lang, extra):
        if im is None:
            return None
        ok, png = cv2.imencode(".png", im)
        cmd = [self.bin, "stdin", "stdout", "-l", lang, "--psm", "7"] + (["--tessdata-dir", self.dir] if self.dir and lang != "eng" else []) + extra
        env = dict(os.environ, OMP_THREAD_LIMIT="1")
        try:
            t = self.sp.run(cmd, input=png.tobytes(), capture_output=True, timeout=60, env=env).stdout.decode("utf-8", "replace")
        except Exception:
            return None
        return (" ".join(t.split()), 0.9)

    def read_hi(self, imgs): return [[r] if (r := self._one(im, "hin", [])) else [] for im in imgs]
    def read_en(self, imgs): return [[r] if (r := self._one(im, "eng", ["-c", "tessedit_char_whitelist=0123456789"])) else [] for im in imgs]


# ----------------------------------------------------------------------------------------------- parsing helpers
def digits_of(t):
    t = (t or "").translate(_DIG).translate(str.maketrans("OoQDlI|iSsBZ", "000011115582"))
    d = re.sub(r"[^0-9]", "", t)
    return int(d) if d and len(d) <= 7 else None


def best_text(cands):
    return max(cands, key=lambda c: c[1]) if cands else ("", 0.0)


def cell_values(reader, items):
    """items: list of images (numeric cells). -> list of (list of candidate ints sorted best-first, best score)."""
    en = reader.read_en(items)
    hi = None
    out = []
    need = [i for i, c in enumerate(en) if not c or digits_of(c[0][0]) is None or c[0][1] < 0.9]
    if need:
        hi_res = reader.read_hi([items[i] for i in need])
        hi = dict(zip(need, hi_res))
    for i, c in enumerate(en):
        cands = list(c) + (hi.get(i, []) if hi else [])
        seen, vals = {}, []
        for k, (t, sc) in enumerate(cands):
            v = digits_of(t)
            if v is None:
                continue
            seen.setdefault(v, 0.0)
            seen[v] = max(seen[v], sc + (0.001 if k == 0 else 0))
        vals = sorted(seen, key=lambda v: -seen[v])
        out.append((vals, seen[vals[0]] if vals else 0.0))
    return out


def fix_booth_text(t):
    """Repairs of conjuncts the Hindi models are known to drop in the fixed wording of booth names. Rules only (no name
    dictionary): the word before "संख्या-<n>" is always कक्ष; शिक्षा; and "प्रा0विद्यालय" read with 0 as ण."""
    t = unicodedata.normalize("NFC", t)
    m = re.search(r"(\S*)\s*(संख्या\s*[-–]?\s*\d+)\s*$", t)
    if m:
        w, tail = m.group(1), m.group(2).replace(" ", "")
        if w in ("क्ष", "कष", "क्स", "ककष", "कक्स", "कक्", "क्", "ष"):
            w = "कक्ष"                                        # the conjunct was dropped or split: the word is "कक्ष"
        elif w != "कक्ष" and not w.endswith("कक्ष") and re.search(r"(क्ष|कष)$", w) and len(w) > 3:
            w = re.sub(r"(क्ष|कष)$", "", w) + " कक्ष"                              # glued to the previous word ("ऐशबागक्ष")
        t = t[:m.start()] + (w + " " if w else "") + tail
    t = re.sub(r"श(?:ि)?[कक्][षष]ा|शक्षा|शिकषा|शिक्शा", "शिक्षा", t)
    t = re.sub(r"प्राण(?=विद्यालय)", "प्रा0", t)
    return re.sub(r"\s+", " ", t).strip()


_WORD_FIXES = {"मधय": "मध्य", "रकूल": "स्कूल", "वधालय": "विद्यालय", "विधालय": "विद्यालय", "पाथिमक": "प्राथमिक",
               "पसाद": "प्रसाद", "पाइमरी": "प्राइमरी"}


def fix_words(t):
    """Booth / AC names only: conjunct letters the Hindi models keep dropping in recurring institutional words
    (संख्या -> संखया, विद्यालय -> वधालय, स्कूल -> रकूल, मध्य -> मधय ...). A short fixed list, no person-name dictionary.
    The room label 'संखया', 'संखयान 1', 'संखया2', 'संखयाड' is rebuilt as 'संख्या' (+ '-<n>' when the digit was read)."""
    toks = unicodedata.normalize("NFC", t or "").split()
    out, i = [], 0
    while i < len(toks):
        tk = toks[i]
        m = re.match(r"^संख\S*?(?:[-–]?(\d+))?$", tk)
        if m and len(tk) <= 9:
            d = m.group(1)
            if not d and i + 1 < len(toks) and re.fullmatch(r"\d{1,2}", toks[i + 1]):
                d = toks[i + 1]; i += 1
            out.append("संख्या" + (f"-{d}" if d else ""))
        else:
            out.append(_WORD_FIXES.get(tk, tk))
        i += 1
    return " ".join(out)


def classify(t):
    t = unicodedata.normalize("NFC", t or "").replace(" ", "")
    if "नेट" in t or "इलेक्टर" in t or "इलैक्टर" in t:
        return "net"
    if "लिंग" in t or "अंतर" in t or "कारण" in t:
        return "diff"
    if "परिवर्धन" in t or "वर्धन" in t or "परिवधन" in t or "परिवर्ध" in t:
        return "add"
    if "विलोपन" in t or "लोपन" in t or "विलोप" in t:
        return "del"
    if "मूल" in t or "नामावली" in t and "प्रकार" not in t and "पहचान" not in t:
        return "mool"
    return None


def solve_row(cands4):
    """cands4: 4 candidate lists (male, female, third, total). Returns (values, repaired, ok)."""
    first = [c[0] if c else None for c in cands4]
    if None not in first and first[0] + first[1] + first[2] == first[3]:
        return first, False, True
    if any(not c for c in cands4):
        return first, False, False
    sols = [combo for combo in itertools.product(*[c[:3] for c in cands4]) if combo[0] + combo[1] + combo[2] == combo[3]]
    if len(sols) == 1:
        return list(sols[0]), True, True
    return first, False, False


# ----------------------------------------------------------------------------------------------- page 1
def parse_page1(pg, reader, rv):
    """Fills the dict rv. Returns nothing; problems are appended to rv['_flags']."""
    flags, s, W = rv["_flags"], pg.s, pg.W
    Hs = pg.hlines(0.02 * W, 0.98 * W, 0.85)
    if len(Hs) < 6:
        flags.append("page1_table_not_found"); return
    crops = {}                                                # name -> image

    # --- first row: AC cell (+ part number cell)
    y0, y1 = Hs[0], Hs[1]
    vl = pg.vlines(y0 + 4, y1 - 4, 0.8)
    left = vl[0] if vl else int(0.02 * W)
    right = vl[-1] if vl else int(0.98 * W)
    ac_r = vl[1] if len(vl) >= 3 else right
    ins = pg.S(4)
    ac_g = pg.crop(left + pg.S(430), y0 + ins, ac_r - ins, y1 - ins)
    rr = np.flatnonzero((ac_g < 200).any(1))
    ac_line = ac_g[rr[0]:rr[-1] + 1] if len(rr) else ac_g
    crops["part"] = None
    if len(vl) >= 3:
        # "भाग संख्या : : <n>" is centred in its cell, so a 3-digit number starts further left than a 1-digit one:
        # take the LAST ink group (digits sit 1-3 px apart, the gap before them is the width of a space).
        pg_ = pg.crop(ac_r + ins, y0 + ins, right - ins, y1 - ins)
        cols_ = np.flatnonzero((pg_ < 200).any(0))
        if len(cols_):
            cuts_ = np.flatnonzero(np.diff(cols_) > max(4, int(round(4.5 * s)))) + 1
            last_ = np.split(cols_, cuts_)[-1]
            crops["part"] = prep(pg_[:, max(last_[0] - 2, 0):last_[-1] + 3], s)

    # --- section 3: booth cell = lowest tall cell with a divider near 42% of the page width
    booth = None
    for i in range(len(Hs) - 2, -1, -1):
        a, b = Hs[i], Hs[i + 1]
        if b - a < pg.S(100):
            continue
        divs = [x for x in pg.vlines(a + 4, b - 4, 0.8, 0.36 * W, 0.47 * W)]
        if divs:
            booth = (a, b, divs[0]); break
    bands = []                                                # gray text lines of the booth cell
    if booth:
        a, b, dx = booth
        cell = pg.crop(left + ins, a + ins, dx - ins, b - ins)
        ink = (cell < 200).sum(1) > 0
        k = pg.S(5)                                           # merge gaps shorter than this (matras / dots)
        idx = np.flatnonzero(ink)
        if len(idx):
            groups = np.split(idx, np.flatnonzero(np.diff(idx) > k) + 1)
            for gp in groups:
                if gp[-1] - gp[0] >= pg.S(8):
                    bands.append(cell[max(gp[0] - 2, 0):gp[-1] + 3])
    else:
        flags.append("booth_cell_not_found")

    # --- last row: voter counts of section 4
    y0, y1 = Hs[-2], Hs[-1]
    vlr = pg.vlines(y0 + 4, y1 - 4, 0.8)
    cells = []
    if len(vlr) >= 7:
        xs = vlr[-7:]
        for j in range(6):
            cells.append(prep(pg.crop(xs[j] + ins, y0 + ins, xs[j + 1] - ins, y1 - ins), s))
    else:
        flags.append("page1_count_row_not_found")

    # --- recognise word by word
    lines = [word_crops(ac_line, s, True)] + [word_crops(bd, s, i == 1) for i, bd in enumerate(bands)]
    toks = read_words(reader, lines)
    ac_t, band_t = toks[0], toks[1:]
    # AC: "<no> - <name> (<reservation>)"
    num_img = lines[0][0]["img"] if lines[0] else None
    rest = ac_t[1:] if ac_t else []
    if rest and is_dash(rest[0], s):
        rest = rest[1:]
    name_t, resv = [], []
    for tk in rest:
        if resv or tk[0].startswith(("(", "（")):
            resv.append(tk)
        else:
            name_t.append(tk)
    if not resv and len(name_t) >= 3 and re.search(r"सामा|अ\.|अ०|अजा|अजजा", name_t[-1][0]):   # "(" not read
        resv = [name_t.pop()]
    nm = " ".join(t[0] for t in name_t).strip(" :-()（）")
    rs = " ".join(t[0] for t in resv).strip(" ()（）")
    nm = fix_words(nm)
    rv["ac_name"] = f"{nm} ({rs})" if nm and rs else nm              # the text after the hyphen: "लखनऊ मध्य (सामान्य)"
    rv["_conf"].extend(t[1] for t in name_t)
    if not rv["ac_name"]:
        flags.append("ac_name_unparsed")
    if num_img is not None:
        num = cell_values(reader, [num_img])[0]
        if num[0]:
            rv["ac_number"] = str(num[0][0])

    # booth: band 0 = label, then the name line(s) up to the label "मतदान स्थल का पता :", then the address line(s)
    if band_t:
        end = len(band_t)
        for i in range(1, len(band_t)):
            first = band_t[i][0][0] if band_t[i] else ""
            if first.startswith("मत") and len(band_t[i]) <= 6:
                end = i; break
        else:
            flags.append("booth_address_label_not_found")
        name_lines, addr_lines = band_t[1:end], band_t[end + 1:]
        words = [t for ln in name_lines for t in ln]
        no_tok = words[0] if words else None
        rest = words[1:] if words else []
        if rest and is_dash(rest[0], s):
            rest = rest[1:]
        addr = [t for ln in addr_lines for t in ln]
        voted = vote_with_address(rest, addr)
        rv["booth_name"] = fix_words(fix_booth_text(" ".join(t for t, _ in voted).strip(" -").translate(_DIG)))
        rv["_conf"].extend(sc for _, sc in voted)
        if no_tok is not None:
            nb = cell_values(reader, [lines[2][0]["img"]])[0] if len(lines) > 2 and lines[2] else ([], 0)
            if nb[0]:
                rv["booth_number"] = str(nb[0][0])
            else:
                d = digits_of(no_tok[0])
                if d is not None: rv["booth_number"] = str(d)
                else: flags.append("booth_number_unparsed")
        if not rv["booth_name"]:
            flags.append("booth_name_empty")

    if crops["part"] is not None:
        pc = cell_values(reader, [crops["part"]])[0]
        rv["part_number"] = str(pc[0][0]) if pc[0] else ""
    if cells:
        cv = cell_values(reader, cells)
        rv["_conf"].extend(sc for _, sc in cv[2:])
        if cv[1][0]:
            rv["last_serial"] = cv[1][0][0]                  # अंतिम क्रम संख्या: the last serial number printed in this part
        vals, rep, ok = solve_row([c for c, _ in cv[2:]])
        if rep: flags.append("page1_repaired_by_sum")
        if not ok: flags.append("page1_counts_sum_mismatch")
        if None not in vals:
            rv["page1_male"], rv["page1_female"], rv["page1_third"], rv["total_electors"] = vals


# ----------------------------------------------------------------------------------------------- last page
def parse_last(pg, reader, rv):
    flags, s, W = rv["_flags"], pg.s, pg.W
    xl, xr = pg.S(768), pg.S(1163)
    ys = pg.hlines(0.70 * W, 0.96 * W, 0.85)
    if len(ys) < 6:
        flags.append("summary_table_not_found"); return
    ins = pg.S(5)
    ivs = [(ys[i], ys[i + 1]) for i in range(min(len(ys) - 1, 14)) if ys[i + 1] - ys[i] >= pg.S(25)]
    lab_imgs, merged = [], []
    c2a, c2b, c3 = pg.S(130), pg.S(312), pg.S(319)
    for a, b in ivs:
        has_div = any(abs(x - c3) <= pg.S(10) for x in pg.vlines(a + 4, b - 4, 0.8, 0.2 * W, 0.4 * W))
        merged.append(not has_div)
        x_from = pg.S(130) if has_div else pg.S(8)
        x_to = c2b if has_div else xl - pg.S(8)
        if not has_div:                                       # merged label: from the left border to the numeric block
            lv = pg.vlines(a + 4, b - 4, 0.8, 0, 0.1 * W)
            x_from = (lv[0] + ins) if lv else pg.S(8)
            if b - a < pg.S(90) and "IV" in "":              # (kept simple: roman numeral glyphs are harmless to the match)
                pass
        lab_imgs.append(prep(pg.crop(x_from, a + ins, x_to, b - ins), s))
    labels = [best_text(c)[0] for c in reader.read_hi(lab_imgs)]
    cls = [classify(t) for t in labels]

    start = next((i for i, c in enumerate(cls) if c == "mool"), None)
    if start is None:                                         # label unreadable: layout position (5th interval onward)
        start = min(4, len(ivs) - 1)
    order = ["mool", "add", "del", "diff", "net"]
    rows, prev, k = [], None, 0
    for i in range(start, len(ivs)):
        c = cls[i]
        if c is None:                                         # fall back to the next class in the fixed order
            c = order[min(order.index(prev) + 1, 4)] if prev else "mool"
        rows.append((c, ivs[i]))
        prev = c
        if c == "net":
            break
    kinds = [c for c, _ in rows]
    if "net" not in kinds or "mool" not in kinds:
        flags.append("summary_rows_incomplete")
    if kinds.count("add") > 1 or kinds.count("del") > 1:
        flags.append("multiple_supplements_summed")

    # numeric columns: snap the 4 cells to the real vertical lines of the first data row
    a0, b0 = rows[0][1] if rows else ivs[0]
    xs = [xl, pg.S(867), pg.S(965), pg.S(1064), xr]
    found = pg.vlines(a0 + 4, b0 - 4, 0.8, 0.55 * W, W)
    snapped = []
    for x in xs:
        near = [f for f in found if abs(f - x) <= pg.S(15)]
        snapped.append(min(near, key=lambda f: abs(f - x)) if near else x)
    xs = snapped

    imgs = []
    for _c, (a, b) in rows:
        for j in range(4):
            imgs.append(prep(pg.crop(xs[j] + ins, a + ins, xs[j + 1] - ins, b - ins), s))
    cv = cell_values(reader, imgs)
    totals = {}
    for r, (c, _iv) in enumerate(rows):
        part = cv[r * 4:(r + 1) * 4]
        vals, rep, ok = solve_row([p[0] for p in part])
        rv["_conf"].extend(p[1] for p in part)
        if rep: flags.append(f"{c}_repaired_by_sum")
        if not ok: flags.append(f"{c}_sum_mismatch")
        if None in vals:
            flags.append(f"{c}_unreadable"); continue
        acc = totals.setdefault(c, [0, 0, 0, 0])
        for j in range(4):
            acc[j] += vals[j]
    for g, key in GROUPS:
        if key in totals:
            for name, v in zip(("male", "female", "third", "total"), totals[key]):
                rv[f"{g}_{name}"] = v
    # I + II - III + IV = net
    if all(k in totals for k in ("mool", "add", "del", "net")):
        diff = totals.get("diff", [0, 0, 0, 0])
        for j, n in enumerate(("male", "female", "third", "total")):
            if totals["mool"][j] + totals["add"][j] - totals["del"][j] + diff[j] != totals["net"][j]:
                flags.append("net_formula_mismatch_" + n)


# ----------------------------------------------------------------------------------------------- one PDF
def ac_part_of(path):
    m = re.search(r"-S\d+-(\d+)-SIR.*?-(\d+)-WI", pathlib.Path(path).stem) or re.search(r"-(\d+)-[A-Z]+-(\d+)-WI", pathlib.Path(path).stem)
    return f"{m.group(1)}_{m.group(2)}" if m else pathlib.Path(path).stem


def process_pdf(path, reader):
    import fitz
    path = pathlib.Path(path)
    rv = {k: "" for k in FIELDS}
    rv.update(ac_part=ac_part_of(path), pdf=path.name, _flags=[], _conf=[])
    try:
        doc = fitz.open(path)
        n = len(doc)
        rv["pages"] = n
        fm = re.search(r"-S\d+-(\d+)-SIR.*?-(\d+)-WI", path.stem)
        parse_page1(Page(get_page(doc, 0)), reader, rv)
        if n > 1:
            parse_last(Page(get_page(doc, n - 1)), reader, rv)
        else:
            rv["_flags"].append("single_page_pdf")
        fl = rv["_flags"]
        if fm:
            if rv["ac_number"] and rv["ac_number"] != str(int(fm.group(1))): fl.append("ac_number_differs_from_filename")
            if rv["part_number"] and rv["part_number"] != str(int(fm.group(2))): fl.append("part_number_differs_from_filename")
        if rv["total_electors"] != "" and rv["net_total"] != "":
            if rv["total_electors"] != rv["net_total"]: fl.append("page1_total_differs_from_net_total")
            for a, b in (("page1_male", "net_male"), ("page1_female", "net_female"), ("page1_third", "net_third")):
                if rv[a] != "" and rv[b] != "" and rv[a] != rv[b]: fl.append(f"{a}_differs_from_{b}")
        if rv["last_serial"] != "" and rv["total_electors"] != "" and int(rv["total_electors"]) > int(rv["last_serial"]):
            fl.append("total_exceeds_last_serial")
        for k in ("ac_name", "booth_number", "booth_name", "last_serial", "total_electors", "net_total", "mool_total"):
            if rv[k] == "": fl.append(f"missing_{k}")
    except Exception as exc:                                  # one bad PDF never stops the run
        rv["_flags"].append(f"error_{type(exc).__name__}: {str(exc)[:80]}")
    conf = min(rv["_conf"]) if rv["_conf"] else 0.0
    rv["confidence"] = round(conf, 3)
    hard = [f for f in rv["_flags"] if f not in ("page1_repaired_by_sum",) and not f.endswith("_repaired_by_sum")]
    rv["review_reason"] = ";".join(dict.fromkeys(rv["_flags"]))
    if conf < 0.6 and rv["_conf"]:
        rv["review_reason"] = (rv["review_reason"] + ";low_confidence").strip(";")
        hard.append("low_confidence")
    rv["needs_review"] = "Y" if hard or any(f.endswith("_repaired_by_sum") for f in rv["_flags"]) else ""
    return {k: rv[k] for k in FIELDS}


# ----------------------------------------------------------------------------------------------- batch
_W = {}


def _winit(engine, device, cpu_threads, tessdata):
    n = str(max(1, cpu_threads))
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[v] = n
    _W["r"] = TessReader(tessdata) if engine == "tess" else PaddleReader(device)


def _wrun(p):
    t0 = time.time()
    row = process_pdf(p, _W["r"])
    return row, time.time() - t0


def natural_key(p):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(p))]


GROUP_TITLES = {"mool": "मूल नामावली  (Mool Namavali)", "parivardhan": "परिवर्धन की सूची  (Additions)",
                "vilopan": "विलोपन की सूची  (Deletions)", "net": "नेट इलेक्टर  (Net electors)"}
GROUP_COLORS = {"mool": "DDEBF7", "parivardhan": "E2EFDA", "vilopan": "FCE4D6", "net": "FFF2CC"}
LABELS = {"ac_part": "AC_Part", "ac_name": "AC Name", "booth_number": "Booth No. (as printed)", "booth_name": "Booth Name",
          "last_serial": "अंतिम क्रम संख्या (Last Serial No.)",
          "total_electors": "Total Electors", "ac_number": "AC No. (PDF)", "part_number": "Part No. (PDF)",
          "page1_male": "Page 1 Male", "page1_female": "Page 1 Female", "page1_third": "Page 1 Third",
          "pdf": "PDF file", "pages": "Pages", "needs_review": "Needs review", "review_reason": "Review reason",
          "confidence": "Confidence"}
NUMERIC_EXTRA = ("booth_number", "last_serial", "total_electors", "ac_number", "part_number", "pages", "confidence", "n_parts")


def write_xlsx(rows, path, fields=None, labels=None, widths=None):
    """The only output file. The four count blocks get a merged coloured heading with Male / Female / Third gender / Total
    underneath, header + first columns frozen, filter on, rows that need a look highlighted. Written to a temp file
    and renamed, so an interrupted run never leaves a broken workbook."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    fields = fields or FIELDS
    labels = dict(LABELS, **(labels or {}))
    wd = {"ac_part": 11, "ac_name": 24, "booth_number": 12, "booth_name": 55, "last_serial": 15, "total_electors": 11,
          "pdf": 34, "review_reason": 36, "needs_review": 10, "parts": 30, "parts_to_check": 16, "match_check": 14}
    wd.update(widths or {})
    wb = Workbook(); ws = wb.active; ws.title = "summary"
    thin = Side(style="thin", color="BFBFBF"); box = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(bold=True)
    fill = lambda c: PatternFill("solid", fgColor=c)
    sub = {"male": "Male", "female": "Female", "third": "Third gender", "total": "Total"}
    plan = [(f, next((g for g in GROUP_TITLES if f.startswith(g + "_")), None)) for f in fields]
    i = 0
    while i < len(plan):
        fld, g = plan[i]
        c = i + 1
        if g:
            ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + 3)
            h = ws.cell(1, c, GROUP_TITLES[g]); h.alignment = Alignment(horizontal="center", vertical="center"); h.font = bold
            for j in range(4):
                ws.cell(1, c + j).fill = fill(GROUP_COLORS[g]); ws.cell(1, c + j).border = box
                sc = ws.cell(2, c + j, sub[plan[i + j][0].split("_", 1)[1]])
                sc.font = bold; sc.fill = fill(GROUP_COLORS[g]); sc.alignment = Alignment(horizontal="center"); sc.border = box
                ws.column_dimensions[get_column_letter(c + j)].width = 12 if j == 2 else 9
            i += 4
        else:
            ws.merge_cells(start_row=1, start_column=c, end_row=2, end_column=c)
            h = ws.cell(1, c, labels.get(fld, fld)); h.font = bold; h.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            for r_ in (1, 2):
                ws.cell(r_, c).fill = fill("D9D9D9"); ws.cell(r_, c).border = box
            ws.column_dimensions[get_column_letter(c)].width = wd.get(fld, 12)
            i += 1
    numeric = {f for f, _ in plan if f.split("_")[-1] in ("male", "female", "third", "total") or f in NUMERIC_EXTRA}
    for r, row in enumerate(rows, 3):
        flag = row.get("needs_review") == "Y" or bool(row.get("parts_to_check")) or row.get("match_check") == "verify"
        for c, (fld, g) in enumerate(plan, 1):
            v = row.get(fld, "")
            if fld in numeric and v not in ("", None):
                try: v = float(v) if fld == "confidence" else int(float(v))
                except (ValueError, TypeError): pass
            cell = ws.cell(r, c, v if v is not None else ""); cell.border = box
            if fld in ("booth_name", "parts"): cell.alignment = Alignment(wrap_text=True, vertical="top")
            elif fld in numeric: cell.alignment = Alignment(horizontal="center")
            if g == "net": cell.font = bold
            if fld == "confidence": cell.number_format = "0.00"
            if flag and fld in ("needs_review", "review_reason", "ac_part", "parts_to_check", "match_check"): cell.fill = fill("F8CBAD")
    ws.freeze_panes = ws.cell(3, 3)
    ws.row_dimensions[1].height = 30
    ws.auto_filter.ref = f"A2:{get_column_letter(len(plan))}{max(len(rows) + 2, 3)}"
    path = pathlib.Path(path)
    tmp = path.with_name(path.stem + ".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------------------------- booths: one row per building
_ROOM_TAIL = re.compile(r"\s*(?:कक्ष|क्ष|कमरा|रूम|रुम)?\s*(?:संख\S*|सं\.?|नं\.?|न|नम्बर|नंबर)\s*[-–:.]?\s*\S{0,3}\s*$")
BOOTH_FIELDS = (["ac_part", "ac_name", "booth_number", "booth_name", "total_electors"]
                + [f"{g}_{k}" for g, _ in GROUPS for k in ("male", "female", "third", "total")]
                + ["parts", "n_parts", "parts_to_check", "match_check"])
BOOTH_LABELS = {"booth_number": "Booth No.", "parts": "Part No. (PDF)", "n_parts": "No. of parts",
                "parts_to_check": "Parts needing review", "match_check": "Name match"}


def booth_base(name):
    """Booth name without the room label ('कक्ष संख्या-2', 'रूम न-3' ...): the building."""
    n = unicodedata.normalize("NFC", name or "").translate(_DIG)
    b = _ROOM_TAIL.sub("", n).strip(" ,-")
    return b if len(b.replace(" ", "")) >= 8 else n.strip()


def booth_key(base):
    """Comparison key only (never written out): drops the things the models get wrong or that spell the same word
    several ways -- halant, anusvara, nukta, short/long vowel signs, punctuation, spaces."""
    b = unicodedata.normalize("NFD", base).translate(_DIG)
    for a, z in (("\u093c", ""), ("\u094d", ""), ("\u0902", ""), ("\u0901", ""), ("ॉ", "ा"), ("ि", "ी"), ("ु", "ू"), ("ऑ", "आ")):
        b = b.replace(a, z)
    return re.sub(r"[^\u0900-\u097F0-9]|[\u0966-\u096F]|0", "", b)


def _same_numbers(a, b):
    f = lambda t: re.findall(r"[1-9]\d*", t.translate(_DIG))
    return f(a) == f(b)


BOOTH_SIM = 0.86            # spelling-insensitive similarity that merges two parts anywhere in the AC
BOOTH_SIM_NEXT = 0.76       # ... lower bar for neighbouring parts (n, n+1): the same building is usually printed in consecutive parts
BOOTH_VERIFY = 0.85         # a group whose least similar pair is below this is marked "verify"


def combine_booths(rows, ac):
    """rows: summary rows (dicts of strings). Parts whose booth names match (fuzzy, room label ignored) become ONE row
    with booth number 1 and the counts added up. Returns the list of combined rows."""
    import collections, difflib
    items = []
    for r in rows:
        try:
            part = int(str(r["ac_part"]).split("_")[-1])        # from the PDF file name (the printed part number is only a cross-check)
        except ValueError:
            part = 10 ** 6
        b = booth_base(r.get("booth_name", ""))
        items.append(dict(r=r, part=part, base=b, key=booth_key(b)))
    items.sort(key=lambda x: x["part"])
    n = len(items)
    par = list(range(n))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]; x = par[x]
        return x

    sim = {}
    for i in range(n):
        if not items[i]["key"]:
            continue
        for j in range(i + 1, n):
            a, b = items[i]["key"], items[j]["key"]
            if not b or abs(len(a) - len(b)) > 14:
                continue
            if abs(items[i]["part"] - items[j]["part"]) > 1 and difflib.SequenceMatcher(None, a, b).quick_ratio() < BOOTH_SIM:
                continue
            ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
            sim[(i, j)] = ratio
            if not _same_numbers(items[i]["base"], items[j]["base"]):
                continue
            if ratio >= BOOTH_SIM or (abs(items[i]["part"] - items[j]["part"]) <= 1 and ratio >= BOOTH_SIM_NEXT):
                par[find(i)] = find(j)
    groups = collections.defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    ac_names = collections.Counter(x["r"].get("ac_name", "") for x in items if x["r"].get("ac_name"))
    ac_name = ac_names.most_common(1)[0][0] if ac_names else ""
    out = []
    for k, ids in enumerate(sorted(groups.values(), key=lambda v: min(items[i]["part"] for i in v)), 1):
        members = [items[i] for i in sorted(ids, key=lambda i: items[i]["part"])]
        names = collections.Counter(m["base"] for m in members if m["base"])
        top = max(names.items(), key=lambda kv: (kv[1], len(kv[0])))[0] if names else ""
        row = {"ac_part": f"{ac}_B{k}", "ac_name": ac_name, "booth_number": 1, "booth_name": top}
        for fld in ["total_electors"] + [f"{g}_{x}" for g, _ in GROUPS for x in ("male", "female", "third", "total")]:
            vals = [m["r"].get(fld, "") for m in members]
            nums = [int(float(v)) for v in vals if v not in ("", None)]
            row[fld] = sum(nums) if nums else ""
            if len(nums) != len(members):
                row["match_check"] = "verify"                  # a part has no reading for this number: the sum is incomplete
        row["parts"] = ", ".join(str(m["part"]) for m in members)
        row["n_parts"] = len(members)
        row["parts_to_check"] = ", ".join(str(m["part"]) for m in members if m["r"].get("needs_review") == "Y")
        low = min([sim.get((a, b), 1.0) for a in ids for b in ids if a < b] or [1.0])
        if low < BOOTH_VERIFY:
            row["match_check"] = "verify"
        row.setdefault("match_check", "")
        out.append(row)
    return out


def booth_path_for(summary_path):
    p = pathlib.Path(summary_path)
    return p.with_name(p.name.replace("_summary", "_booth_combined") if "_summary" in p.name else p.stem + "_booth_combined.xlsx")


def write_booth_xlsx(rows, summary_path, ac):
    out = booth_path_for(summary_path)
    comb = combine_booths(rows, ac)
    write_xlsx(comb, out, fields=BOOTH_FIELDS, labels=BOOTH_LABELS)
    return out, len(comb)


def read_xlsx(path):
    """Rows written by write_xlsx() earlier (for resume), as dicts of strings."""
    from openpyxl import load_workbook
    ws = load_workbook(path, read_only=True).active
    out = []
    if ws.max_column != len(FIELDS):
        raise ValueError(f"made by an older version ({ws.max_column} columns instead of {len(FIELDS)})")
    for r in ws.iter_rows(min_row=3, values_only=True):
        if r and r[0] not in (None, ""):
            out.append({f: ("" if v is None else str(v)) for f, v in zip(FIELDS, r)})
    return out


def majority_ac_name(rows):
    """Replace a misread AC name by the name most PDFs of the same ac_number agree on (needs >= 3 agreeing)."""
    import collections
    by = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["ac_number"] and r["ac_name"]:
            by[r["ac_number"]][r["ac_name"]] += 1
    changed = 0
    for r in rows:
        c = by.get(r["ac_number"])
        if not c:
            continue
        top, n = c.most_common(1)[0]
        if n >= 3 and r["ac_name"] != top:
            r["ac_name"] = top
            if "ac_name_by_majority" not in r["review_reason"]:
                r["review_reason"] = (r["review_reason"] + ";ac_name_by_majority").strip(";")
            changed += 1
    return changed


def _run_sub(d, out_xlsx, pool, kw):
    """One folder -> one .xlsx. Resumes from the workbook if it exists. The workbook is rewritten every 20 PDFs and at the end."""
    d, out_xlsx = pathlib.Path(d), pathlib.Path(out_xlsx)
    pdfs = sorted(d.rglob("*.pdf") if kw["recursive"] else d.glob("*.pdf"), key=natural_key)
    if kw["limit"]:
        pdfs = pdfs[:kw["limit"]]
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    ac = ac_part_of(pdfs[0]).split("_")[0] if pdfs else d.name
    if out_xlsx.name == "_summary_pending.xlsx":                              # file name from the AC number: 174_summary.xlsx
        out_xlsx = out_xlsx.with_name(f"{ac}_summary.xlsx")
    rows = []
    if out_xlsx.exists() and not kw["force"]:
        try:
            rows = read_xlsx(out_xlsx)
        except Exception as exc:
            print(f"  ! could not read the existing {out_xlsx.name} ({exc}); starting this folder again")
    done = {r["pdf"] for r in rows}
    todo = [p for p in pdfs if p.name not in done]
    print(f"\n{d.name}: {len(pdfs)} PDFs, {len(done)} already in {out_xlsx.name}, {len(todo)} to do", flush=True)
    t0, n_rev = time.time(), 0

    def save(final=False):
        majority_ac_name(rows)
        order = {p.name: k for k, p in enumerate(sorted(pdfs, key=natural_key))}
        rows.sort(key=lambda r: order.get(r["pdf"], 10 ** 9))
        write_xlsx(rows, out_xlsx)
        if final:
            bx, nb = write_booth_xlsx(rows, out_xlsx, ac)
            print(f"{d.name}: {len(rows)} parts -> {nb} booths -> {bx.name}")

    try:
        it = pool.map(_wrun, map(str, todo)) if pool else (_wrun(str(p)) for p in todo)
        for i, (row, sec) in enumerate(it, 1):
            rows.append(row)
            n_rev += row["needs_review"] == "Y"
            print(f"[{d.name} {i}/{len(todo)}] {row['ac_part']}: booth {row['booth_number']} total {row['total_electors']} "
                  f"net {row['net_total']} {'REVIEW: ' + row['review_reason'] if row['needs_review'] else 'ok'} ({sec:.1f}s)", flush=True)
            if i % 20 == 0:
                save()
    finally:
        if rows:
            save(final=True)
    print(f"{d.name}: {len(todo)} PDFs in {time.time() - t0:.0f}s, {n_rev} need review -> {out_xlsx}")


def run_folder(folder, out_xlsx, workers=1, engine="paddle", device=None, cpu_threads=4, tessdata=None, limit=None,
               force=False, recursive=False):
    """One folder -> one .xlsx (resumes if the workbook already exists)."""
    pool = None
    if workers > 1:
        pool = concurrent.futures.ProcessPoolExecutor(max_workers=workers, initializer=_winit,
                                                      initargs=(engine, device, cpu_threads, tessdata))
    else:
        _winit(engine, device, cpu_threads, tessdata)
    try:
        _run_sub(folder, out_xlsx, pool, dict(limit=limit, force=force, recursive=recursive))
    finally:
        if pool:
            pool.shutdown()


def main():
    print(f"ocr_summary_pages.py {VERSION}")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", help="a single PDF")
    ap.add_argument("--folder", help="every PDF in this folder -> one .xlsx (--out is the .xlsx file)")
    ap.add_argument("--root", help="every sub-folder of this directory -> one .xlsx per sub-folder in --out (a directory)")
    ap.add_argument("--only", nargs="+", help="with --root: only these sub-folder names, e.g. --only 1 2 174")
    ap.add_argument("--only-file", help="with --root: text file with sub-folder names (whitespace separated)")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--out", help="--folder: a folder (file is named <AC number>_summary.xlsx) or an .xlsx path.  --root: output directory")
    ap.add_argument("--combine", metavar="SUMMARY.xlsx",
                    help="No PDF reading: build the booth-combined workbook (<AC>_booth_combined.xlsx, same folder) from an existing summary workbook")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--cpu-threads", type=int, default=4)
    ap.add_argument("--device", default=None)
    ap.add_argument("--limit", type=int, help="process at most this many PDFs per folder (test run)")
    ap.add_argument("--force", action="store_true", help="redo PDFs already present in the workbook")
    ap.add_argument("--engine", choices=("paddle", "tess"), default="paddle",
                    help="paddle (default) = PaddleOCR recognition models. tess = Tesseract, only a stand-in for machines without PaddleOCR")
    ap.add_argument("--tessdata", help="with --engine tess: folder containing hin.traineddata")
    a = ap.parse_args()
    if a.combine:
        p = pathlib.Path(a.combine)
        rows = read_xlsx(p)
        ac = (rows[0]["ac_part"].split("_")[0] if rows else p.stem.split("_")[0])
        bx, nb = write_booth_xlsx(rows, p, ac)
        print(f"{len(rows)} parts -> {nb} booths -> {bx}")
        return
    if not (a.pdf or a.folder or a.root):
        ap.error("give a PDF, --folder DIR or --root DIR")
    if not a.out:
        ap.error("--out is required")
    if not a.root and pathlib.Path(a.out).suffix.lower() not in (".xlsx", ".csv"):
        a.out = str(pathlib.Path(a.out) / "_summary_pending.xlsx")            # --out is a folder: file name = <AC number>_summary.xlsx
    elif not a.root and pathlib.Path(a.out).suffix.lower() == ".csv":
        a.out = str(pathlib.Path(a.out).with_suffix(".xlsx"))
        print(f"(output is an Excel file only: writing {a.out})")
    kw = dict(workers=a.workers, engine=a.engine, device=a.device, cpu_threads=a.cpu_threads, tessdata=a.tessdata,
              limit=a.limit, force=a.force, recursive=a.recursive)
    if a.root:
        root, out = pathlib.Path(a.root), pathlib.Path(a.out)
        want = (a.only or []) + (pathlib.Path(a.only_file).read_text(encoding="utf-8").split() if a.only_file else [])
        subs = sorted((d for d in root.iterdir() if d.is_dir() and any(d.glob("*.pdf"))), key=natural_key)
        if want:
            subs = [d for d in subs if d.name in set(want)]
        pool = None
        if a.workers > 1:
            pool = concurrent.futures.ProcessPoolExecutor(max_workers=a.workers, initializer=_winit,
                                                          initargs=(a.engine, a.device, a.cpu_threads, a.tessdata))
        else:
            _winit(a.engine, a.device, a.cpu_threads, a.tessdata)
        for d in subs:
            _run_sub(d, out / "_summary_pending.xlsx", pool, kw)
        if pool:
            pool.shutdown()
    elif a.folder:
        run_folder(a.folder, a.out, **kw)
    else:
        _winit(a.engine, a.device, a.cpu_threads, a.tessdata)
        row = process_pdf(a.pdf, _W["r"])
        write_xlsx([row], a.out)
        for k, v in row.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()