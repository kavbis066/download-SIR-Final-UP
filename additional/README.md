# Electoral-roll card extraction (PaddleOCR) — v16

Reads the ECI "SIR Final Roll" PDFs (one PDF per part, 30 cards per page) and writes one CSV per folder
(one folder = one constituency, e.g. `downloads/86`).

Files: `ocr_extract_paddleocr.py` (run this) and `ocr_extract.py` (the card-reading engine). Keep them in the same folder.

## Setup (once per Mac)

```
pip install paddleocr paddlepaddle pymupdf opencv-python numpy pandas
```

The first run downloads the Hindi and English recognition models. Tesseract is optional and OFF by default
(`--tess` turns it on; see the script's docstring).

## 1. Only the DELETED cards (fast)

A card is "deleted" when it has a status letter (E, S, R, M, Q) or a DELETED stamp. With `--deleted-only` the
models read only the serial number of every card and the full card only where it can be deleted, so a PDF takes
a fraction of the normal time (measured: 6 PDFs in 229 s; 364 PDFs is about 3-4 hours).

### 1a. One folder

```
python ocr_extract_paddleocr.py --folder downloads/86 --out out_deleted/86 --deleted-only --workers 6 --cpu-threads 2
```

### 1b. Specific folders listed in a text file (e.g. first.txt)

`first.txt` holds only the folder numbers, one per line. They are looked up inside the downloads folder:

```
65
66
86
174
```

```
python ocr_extract_paddleocr.py --root downloads --only-file first.txt --out out_deleted --deleted-only --workers 6 --cpu-threads 2
```

Output goes to `out_deleted/<folder number>/` (for example `out_deleted/86/_combined_001.csv`).
Folders that are already finished are skipped, so the same command can simply be run again after a stop.

To run the same list in the background and keep a log:

```
nohup python ocr_extract_paddleocr.py --root downloads --only-file first.txt --out out_deleted --deleted-only --workers 6 --cpu-threads 2 > deleted_run.log 2>&1 &
tail -f deleted_run.log
```

## 2. All cards (names, relation names, house numbers, status ...)

```
# one folder
python ocr_extract_paddleocr.py --folder downloads/86 --out ac86_out --workers 6 --cpu-threads 2

# the folders listed in first.txt
python ocr_extract_paddleocr.py --root downloads --only-file first.txt --out all_out --workers 6 --cpu-threads 2

# every folder inside downloads
python ocr_extract_paddleocr.py --root downloads --out all_out --workers 6 --cpu-threads 2
```

Add `--gzip` for `.csv.gz` output, `--slim` to leave out the two audit columns, `--upload s3://BUCKET/PREFIX` to
upload each finished folder. `workers x cpu-threads` should not exceed the number of cores (16 on an M3 Max).

## If a run is interrupted

It continues where it stopped. Every finished PDF is saved at once in `<out>/<folder>/_parts/`. Run the SAME command again:

* PDFs that were finished are reused (the log says `RESUMING: N PDF(s) were finished by an earlier run`), only the rest are processed.
* With `--root`, folders that finished are skipped and a half-finished folder resumes.
* The saved progress is only reused when the settings are the same (`--deleted-only`, `--second-opinion`, `--slim`, `--tess`, script version);
  otherwise it is discarded and the folder starts again. `--force` also starts a folder again from scratch.
* A PDF that failed is retried on the next run (listed in `_failed.txt`).
* `_parts/` is deleted automatically when the folder is complete.

## Output files (per folder)

* `_combined.csv`, or `_combined_001.csv`, `_combined_002.csv` ... — one file per 100,000 rows (`--max-rows`), cut between PDFs.
* `_run_info.json` — what was done:
  `cards` (all cards found), `deleted_cards`, `rows_written` (rows in the CSV: equals `deleted_cards` with `--deleted-only`),
  `mode` (`deleted_only` / `all_cards`), `pdfs_found`, `pdfs_processed`, `pdfs_resumed_from_earlier_run`, `pdfs_failed`,
  `complete`, `seconds`, and **`per_pdf`**: a list with `pdf`, `cards`, `deleted` and `seconds` for every PDF.
* `_all_folders_summary.csv` (in `--out`, with `--root`) — one line per folder.

### Reading the status columns

`status_code` is E (dead), S (shifted), R (repeated), M (missing), Q (ineligible) — all deleted — or `#`
(printed on some cards; not a deletion). `deleted` = Y when the code is E/S/R/M/Q or a DELETED stamp is on the card.
In `review_reason`: `status_letter_unclear` = a letter is there but could not be identified (code `?`);
`deleted_without_status_code` = DELETED stamp but no status letter found — check those cards by eye.