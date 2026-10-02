# ECI SIR Final Roll 2026 (Uttar Pradesh) — download + OCR pipeline

Two stages:

1. **`download_pdfs_direct.py`** — downloads the voter-roll PDFs from
   voters.eci.gov.in, captcha-free, using `UP_parts_by_AC.csv` to know how
   many parts (files) each Assembly Constituency (AC) has.
2. **`ocr_extract_paddleocr.py`** (+ `ocr_extract.py`, its engine module) —
   OCRs the downloaded PDFs into a CSV of voter records.

Run stage 1 first for whichever ACs you want, then point stage 2 at the
folder it created.

---

## 1. Downloading PDFs — `download_pdfs_direct.py`

### Setup

```bash
pip install -r requirements.txt
```

Needs `UP_parts_by_AC.csv` in the same folder (already there) — it's
what tells the script how many part-files exist per AC, so it can build
the right number of download URLs without guessing.

### Download one AC (good first test)

```bash
python download_pdfs_direct.py --acs 86
```

### Download a specific set of ACs

```bash
python download_pdfs_direct.py --acs 86,87,88
```

### Download everything in the CSV (all of UP)

```bash
python download_pdfs_direct.py --all
```

**Before running `--all`:** this is ~174,088 files and roughly
900GB–1.2TB total — check you actually have that much disk free first.
If you only need certain districts/ACs, use `--acs` instead; there's no
`--all`-but-partial middle ground, `--acs` is the way to scope it down.

### ACs with no part count in the CSV

Four ACs (55, 61, 62, 170) have a blank `total_parts` in
`UP_parts_by_AC.csv`. Resolve them once via the live API (no captcha
needed) before downloading those specific ACs:

```bash
python download_pdfs_direct.py --resolve-missing-counts
```

This saves the resolved counts to `part_count_overrides.json` and exits
without downloading anything. Run it once, then your normal `--acs` /
`--all` commands will pick those ACs up automatically.

### Controlling speed — `--concurrency`

```bash
python download_pdfs_direct.py --acs 86 --concurrency 32
```

Default is 24 parallel downloads. This step is limited by network
waiting time, not your CPU, so raising `--concurrency` is the main way
to go faster — there's no equivalent of the OCR step's `--workers`/
`--cpu-threads` thread-oversubscription risk here, since each download
is just one thread waiting on a socket, not loading its own OCR model.

Guidance:
- Start at the default (24). Watch `download_log.jsonl` for
  `"bad_response"` entries with HTTP status 429 or 503 — those mean
  you're being rate-limited.
- If you see none after a few minutes, try 32 or 48.
- If you do see them, drop back down — the built-in retry/backoff
  absorbs the occasional one, but a sustained flood means you've found
  the server's real limit.
- Safe to stop (Ctrl+C) and restart with a different number any time:
  already-downloaded valid files are skipped automatically, so nothing
  is repeated.

### Resuming / re-running

Every run skips any file already downloaded and valid (checked by size +
`%PDF-` header), so if a run is interrupted — or you change
`--concurrency` — just run the same command again and it'll pick up
where it left off.

### Where files land

```
downloads/<AC>/2026-EROLLGEN-S24-<AC>-SIR-FinalRoll-Revision1-HIN-<part>-WI.pdf
```

e.g. `downloads/86/2026-EROLLGEN-S24-86-SIR-FinalRoll-Revision1-HIN-17-WI.pdf`

---

## 2. OCR extraction — `ocr_extract_paddleocr.py`

### Setup

```bash
pip install -r requirements_ocr.txt
```

No system packages needed (v7 dropped the poppler/`pdfimages`
dependency — PDF rasterization is pure Python now via PyMuPDF).

### One PDF, to test

```bash
python ocr_extract_paddleocr.py downloads/86/2026-EROLLGEN-S24-86-SIR-FinalRoll-Revision1-HIN-17-WI.pdf --out test17.csv
```

### A whole folder of PDFs — in parallel, with `--workers`

```bash
python ocr_extract_paddleocr.py --folder downloads/86 --out ac86_out --workers 2 --cpu-threads 4
```

- `--workers N` — process N PDFs **at the same time**, each in its own
  process with its own copy of the OCR models. Default is 1
  (sequential — same behavior as processing one file at a time).
- `--cpu-threads N` — caps how many CPU threads each worker process's
  underlying math libraries (OpenMP/MKL/OpenBLAS) can use. Default 4.
  **`workers × cpu-threads` is your real total thread count** — this is
  what froze your MacBook once before when left uncapped, so don't skip
  past this. On your M3 Max (multi-core, 48GB RAM), start with
  `--workers 2 --cpu-threads 4` (8 threads total), confirm it's stable
  via Activity Monitor, then raise `--workers` gradually from there.
- Add `--recursive` if the folder has PDFs nested in subfolders (not
  needed for `downloads/86`, which is flat).

Output: one CSV per PDF inside `ac86_out/`, plus `ac86_out/_combined.csv`
with every row from every file.

### Testing on a subset before committing to a whole folder — `--limit`

```bash
python ocr_extract_paddleocr.py --folder downloads/86 --out test_batch --workers 2 --cpu-threads 4 --limit 30
```

`--limit 30` processes only the first 30 PDFs found (alphabetically) in
the folder, so you can sanity-check output before pointing the same
command at all ~500 files in an AC (drop `--limit`) or at every
downloaded AC.

### Recommended order of operations

```bash
# 1. One file, confirm it runs and the CSV looks right
python ocr_extract_paddleocr.py downloads/86/<one-file>.pdf --out test_one.csv

# 2. Small batch, confirm timing + accuracy at scale, watch system load
python ocr_extract_paddleocr.py --folder downloads/86 --out test_batch --workers 2 --cpu-threads 4 --limit 30

# 3. Whole AC folder, once (2) looks good
python ocr_extract_paddleocr.py --folder downloads/86 --out ac86_out --workers 2 --cpu-threads 4

# 4. Scale --workers up only after (3) has run cleanly and you've
#    confirmed CPU/memory headroom in Activity Monitor
```