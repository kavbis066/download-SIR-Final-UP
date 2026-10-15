# ECI SIR Final Roll 2026 (Uttar Pradesh) — download + OCR pipeline

Everything in one place: downloading the roll PDFs, extracting the **summary pages**, and OCR of the **voter cards**.

| Step | Script | What it does | Output |
|---|---|---|---|
| A | `download_first_last_pages.py` | downloads only **page 1 + last page** of each part | `first_last/<AC>/*.pdf` (2 pages each) |
| B | `ocr_summary_pages.py` | reads those two pages: booth, totals, Mool / Additions / Deletions / Net | `out_summary/<AC>_summary.xlsx` |
| C | `download_pdfs_direct.py` | downloads the **full** PDFs (needed only for card OCR) | `downloads/<AC>/*.pdf` |
| D | `ocr_extract_paddleocr.py` (+ `ocr_extract.py`) | reads every voter card (names, relation, house no., status, deleted) | `<out>/<AC>/_combined_<AC>.csv` |

A + B give the constituency summary quickly and cheaply (about 1 MB per part instead of about 8 MB).
C + D are the heavy card-level extraction. Use the same `UP_parts_by_AC.csv` for both downloaders.

Keep `ocr_extract_paddleocr.py` and `ocr_extract.py` in the same folder. `ocr_summary_pages.py` is standalone.

---

## 0. Setup (once per Mac)

```bash
pip install -r requirements.txt          # downloaders: requests, pypdf
pip install paddleocr paddlepaddle pymupdf opencv-python numpy pandas openpyxl
```

* `UP_parts_by_AC.csv` (columns `acNumber`, `total_parts`) must be next to the downloader scripts.
* The first OCR run downloads the Hindi and English recognition models.
* Tesseract is optional and OFF by default (`--tess` for the card script, `--engine tess` for the summary script).

### The AC list file (used by every command below)

Create a text file with the AC numbers, one per line (commas/spaces also work, `#` starts a comment), e.g. `acs.txt`:

```
174
400
401
```

Do not run all 403 at once. Suggested batches: first `174` and `400` (compare with the summaries you already checked),
then about 10 ACs (check speed and blocking), then 30–50 ACs per file (`acs_batch1.txt`, `acs_batch2.txt` …). Every command resumes safely.

---

## A. Download only the first and last page — `download_first_last_pages.py`

Uses HTTP Range requests: the index at the end of the PDF is read first, then only the bytes of page 1 and the last page.
About 0.85 MB and about 33 small requests per part (measured on a local test server). If the server ignores Range, that file is
downloaded fully and trimmed to two pages (slower, same result).

```bash
# check Range support on one file first (downloads part 1 of the first AC in the list, reports, exits)
python download_first_last_pages.py --acs-file acs.txt --test

# the ACs listed in acs.txt
python download_first_last_pages.py --acs-file acs.txt --concurrency 8

# a few ACs on the command line
python download_first_last_pages.py --acs 174,400 --concurrency 8

# everything in the CSV
python download_first_last_pages.py --all --concurrency 8

# retry only what failed (reads first_last_failed.csv)
python download_first_last_pages.py --retry-failed

# if the server starts blocking: one request per file instead of ~33 (about 8 MB each)
python download_first_last_pages.py --acs-file acs.txt --concurrency 4 --full
```

* **`--concurrency`**: start at 8, go to 12 only if the log is clean. This tool makes many small requests per file, so a
  block is more likely to be triggered by request count than by bytes. Watch `first_last_log.jsonl` and `first_last_failed.csv`
  for 429 / 403 / "still failing".
* Other options: `--csv PATH` (parts CSV), `--out DIR` (default `first_last`).
* Files land in `first_last/<AC>/2026-EROLLGEN-S24-<AC>-SIR-FinalRoll-Revision1-HIN-<part>-WI.pdf` — same names as the full
  downloader, so the summary script reads the AC and part number from the name.
* Resumable: valid existing outputs are skipped.

---

## B. Summary pages — `ocr_summary_pages.py`

Reads page 1 (AC name, part, booth number and name, last serial number, male / female / third / total) and the last page
(I Mool Namavali, II Additions, III Deletions, IV Net) of every part. Output is **xlsx only**: `<AC>_summary.xlsx`, one row per
part, including **अंतिम क्रम संख्या (Last Serial No.)** before Total Electors. Rows are in ascending part order; a part that needs a
look is marked in `Needs review` / `Review reason`.

```bash
# every AC folder listed in acs.txt (folders inside first_last/ are named by AC number), on CPU
python ocr_summary_pages.py --root first_last --only-file acs.txt --out out_summary --device cpu --workers 6 --cpu-threads 2

# quick test: 10 PDFs per folder
python ocr_summary_pages.py --root first_last --only-file acs.txt --out out_summary --device cpu --workers 6 --cpu-threads 2 --limit 10

# one AC folder
python ocr_summary_pages.py --folder first_last/174 --out out_summary --device cpu --workers 6 --cpu-threads 2

# one PDF (prints every field)
python ocr_summary_pages.py first_last/174/2026-EROLLGEN-S24-174-SIR-FinalRoll-Revision1-HIN-1-WI.pdf --out one.xlsx
```

* `--out` is a folder (the file is named `<AC>_summary.xlsx`) or an `.xlsx` path.
* Resumable: PDFs already in the workbook are skipped; `--force` redoes them. The workbook is saved every 20 PDFs.
* **Heat / speed**: `workers × cpu-threads` should stay under the 16 cores. `--workers 6 --cpu-threads 2` is a safe start; go to 4 if the
  Mac throttles, 8 if it stays cool. Each worker loads its own models, so memory grows with workers. Test with `--limit 10` first.
* `--engine tess --tessdata DIR` uses Tesseract instead of PaddleOCR (only a stand-in; Hindi accuracy is lower).

### Booth-combined workbook (optional, later)

Parts that share one building (for example room 1, 2, 3 of the same school) are grouped by fuzzy booth-name match into one row with booth
number 1 and the counts added up: `<AC>_booth_combined.xlsx`. It is **off by default**. Build it from a finished summary without re-reading any PDF:

```bash
python ocr_summary_pages.py --combine out_summary/400_summary.xlsx
```

or add `--booths` to the summary command to write it automatically. Groups marked `verify` (name match weak, a count missing, or the
same name in parts more than 100 apart) should be checked by hand.

---

## C. Download the full PDFs — `download_pdfs_direct.py`

Needed only for the card-level OCR (step D). The PDFs are public static files; no captcha, no login.

```bash
python download_pdfs_direct.py --acs 86                    # one AC (good first test)
python download_pdfs_direct.py --acs 86,87,88              # several ACs
python download_pdfs_direct.py --all                       # everything in the CSV
python download_pdfs_direct.py --acs 86 --concurrency 32   # faster
python download_pdfs_direct.py --retry-failed              # only the parts listed in failed_or_missing.csv
python download_pdfs_direct.py --resolve-missing-counts    # fill the ACs with a blank part count in the CSV, then exit
```

* **Disk**: `--all` is about 174,088 files and roughly 900 GB – 1.2 TB. Check free space, or scope with `--acs`.
* **Blank part counts**: ACs 55, 61, 62 and 170 have no `total_parts` in the CSV. Run `--resolve-missing-counts` once (saves
  `part_count_overrides.json`), then the normal commands include them.
* **`--concurrency`** (default 24): this step waits on the network, not the CPU. Watch `download_log.jsonl` for 429 / 503; if none
  appear after a few minutes try 32 or 48, and drop back if they do.
* **Resume**: valid existing files (size + `%PDF-` header) are skipped; Ctrl+C and re-run any time.
* Failures and 404s are kept in `failed_or_missing.csv` (cleared automatically when they succeed later).
* Files land in `downloads/<AC>/2026-EROLLGEN-S24-<AC>-SIR-FinalRoll-Revision1-HIN-<part>-WI.pdf`.

---

## D. Card OCR — `ocr_extract_paddleocr.py`

Reads the full PDFs (30 cards per page) and writes one CSV set per folder (one folder = one constituency).

### D1. One PDF, to test

```bash
python ocr_extract_paddleocr.py downloads/86/2026-EROLLGEN-S24-86-SIR-FinalRoll-Revision1-HIN-17-WI.pdf --out test17.csv
```

### D2. Only the DELETED cards (fast)

A card is "deleted" when it has a status letter (E, S, R, M, Q) or a DELETED stamp. With `--deleted-only` the models read only the serial
number of every card and the full card only where it can be deleted, so a PDF takes a fraction of the normal time (measured: 6 PDFs in
229 s; 364 PDFs is about 3–4 hours).

```bash
# one folder
python ocr_extract_paddleocr.py --folder downloads/86 --out out_deleted/86 --deleted-only --workers 6 --cpu-threads 2

# the folders listed in acs.txt (looked up inside downloads/)
python ocr_extract_paddleocr.py --root downloads --only-file acs.txt --out out_deleted --deleted-only --workers 6 --cpu-threads 2

# same, in the background with a log
nohup python ocr_extract_paddleocr.py --root downloads --only-file acs.txt --out out_deleted --deleted-only --workers 6 --cpu-threads 2 > deleted_run.log 2>&1 &
tail -f deleted_run.log
```

### D3. All cards (names, relation names, house numbers, status …)

```bash
# one folder
python ocr_extract_paddleocr.py --folder downloads/86 --out ac86_out --workers 6 --cpu-threads 2

# the folders listed in acs.txt
python ocr_extract_paddleocr.py --root downloads --only-file acs.txt --out all_out --workers 6 --cpu-threads 2

# every folder inside downloads
python ocr_extract_paddleocr.py --root downloads --out all_out --workers 6 --cpu-threads 2
```

### D4. Useful options

| Option | Effect |
|---|---|
| `--workers N` | N PDFs at the same time, each in its own process with its own models (default 1) |
| `--cpu-threads N` | caps math-library threads per worker (default 4). **`workers × cpu-threads` ≈ real thread count**; keep it at or under the 16 cores. Uncapped it froze the MacBook once. |
| `--limit N` | only the first N PDFs per folder (test run) |
| `--recursive` | PDFs in sub-folders |
| `--xlsx` | write `.xlsx` with text cells (Excel turns CSV house numbers like `10/10` into dates) |
| `--gzip` | `.csv.gz` output |
| `--slim` | leave out the two audit columns |
| `--max-rows N` | rows per output file (default 100,000) |
| `--upload s3://BUCKET/PREFIX` | upload each finished folder |
| `--force` | start the folder again from scratch |
| `--tess` | switch Tesseract on (off by default) |

### D5. Recommended order of operations

```bash
# 1. one file – confirm it runs and the CSV looks right
python ocr_extract_paddleocr.py downloads/86/<one-file>.pdf --out test_one.csv

# 2. small batch – timing, accuracy, system load (watch Activity Monitor)
python ocr_extract_paddleocr.py --folder downloads/86 --out test_batch --workers 2 --cpu-threads 4 --limit 30

# 3. whole AC folder
python ocr_extract_paddleocr.py --folder downloads/86 --out ac86_out --workers 2 --cpu-threads 4

# 4. many folders – raise --workers only after (3) ran cleanly
ls downloads | sort -V | head -100 > first100.txt
python ocr_extract_paddleocr.py --root downloads --only-file first100.txt --out outputs --workers 3 --cpu-threads 2 --gzip

# 5. shrink the PDFs already done, then do the rest
python compress_pdfs.py downloads --out downloads_small --skip-file first100.txt --workers 4
ls downloads | sort -V | tail -n +101 > rest.txt
python ocr_extract_paddleocr.py --root downloads_small --only-file rest.txt --out outputs --workers 3 --cpu-threads 2 --gzip
```

### Extracting ASSD
```
python ocr_extract_assd.py --root assd --out out_assd --only 1 2 3
python ocr_extract_assd.py --root assd --out out_assd --only-file acs.txt --workers 6
python ocr_extract_assd.py --root assd --out out_assd              # every AC folder
```

---

## If a run is interrupted

All scripts continue where they stopped — run the SAME command again.

* **Downloaders**: valid files are skipped, failures are retried with `--retry-failed`.
* **Summary**: PDFs already in `<AC>_summary.xlsx` are skipped.
* **Card OCR**: every finished PDF is saved at once in `<out>/<folder>/_parts/`. Finished PDFs are reused (the log says
  `RESUMING: N PDF(s) were finished by an earlier run`); with `--root`, finished folders are skipped and a half-finished folder resumes.
  Saved progress is reused only when the settings are the same (`--deleted-only`, `--second-opinion`, `--slim`, `--tess`, script version);
  otherwise the folder starts again. A failed PDF is retried on the next run (listed in `_failed.txt`). `_parts/` is deleted when the folder is complete.

---

## Output files

### Summary (`out_summary/`)

* `<AC>_summary.xlsx` — one row per part: `ac_part`, AC name, booth number and name, last serial no., total electors,
  Mool / Additions / Deletions / Net (each male, female, third gender, total), part number, page-1 counts, PDF name, pages,
  needs review, review reason, confidence. Rows flagged for review are shaded.
* `<AC>_booth_combined.xlsx` — only when requested (see B).

### Card OCR (`<out>/<AC>/`)

* `_combined_<AC>.csv` (for example `_combined_400.csv`), or `_combined_<AC>_001.csv`, `_combined_<AC>_002.csv` … when there are more than
  100,000 rows (`--max-rows`, cut between PDFs). With `--xlsx` / `--gzip` the extension changes accordingly.
* `_run_info.json` — what was done: `cards` (all cards found), `deleted_cards`, `rows_written` (equals `deleted_cards` with `--deleted-only`),
  `mode` (`deleted_only` / `all_cards`), `pdfs_found`, `pdfs_processed`, `pdfs_resumed_from_earlier_run`, `pdfs_failed`, `complete`,
  `seconds`, and **`per_pdf`**: `ac_part`, `pdf`, `cards`, `deleted`, `seconds` for every PDF.
* `_all_folders_summary.csv` (in `--out`, with `--root`) — one line per folder.

### Reading the status columns

`status_code` is E (dead), S (shifted), R (repeated), M (missing), Q (ineligible) — all deleted — or `#` (printed on some cards; not a
deletion). `deleted` = Y when the code is E/S/R/M/Q or a DELETED stamp is on the card.
In `review_reason`: `status_letter_unclear` = a letter is there but could not be identified (code `?`); `deleted_without_status_code` =
DELETED stamp but no status letter found — check those cards by eye. For house numbers, `house_no_readings_disagree` means the two
readings differ; the number is printed as read and never converted to a date.