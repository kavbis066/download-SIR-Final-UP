#!/usr/bin/env python3
"""
download_first_last_pages.py — download ONLY page 1 and the last page of each
ECI SIR Final Roll PDF (Uttar Pradesh), for the summary extraction
(ocr_summary_pages.py).

HOW IT WORKS
------------
A PDF keeps its index (xref table / page tree) at the END of the file, and every
page is a separate block of bytes. So instead of downloading the whole 6-10 MB
file we use HTTP Range requests ("give me bytes X..Y"):

  1. fetch the first block, which tells us the total size (Content-Range)
  2. pypdf reads the PDF through a small file-like object that fetches only the
     blocks it asks for: the tail (index), the page tree, and the byte ranges
     of page 1 and the last page (each page is one embedded image, ~150-400 KB)
  3. those two pages are written as a small 2-page PDF

Typical transfer: roughly 0.5-1 MB per part instead of 6-10 MB, and one PDF
needs only a handful of requests.

If the server ever ignores Range (answers 200 instead of 206), the script
falls back to downloading that file fully, keeping just the two pages and
discarding the rest — so it always works, it is just slower in that case.
The run summary tells you how many parts used each method.

OUTPUT
------
    first_last/<AC>/<same file name as the full PDF>.pdf

Same file names as the full downloader, so ocr_summary_pages.py picks the AC and
part number from the name exactly as before:

    python ocr_summary_pages.py --folder first_last/174 --out out_summary/ ...

Each output PDF has 2 pages (page 1 = the part/booth/totals page, page 2 = the
last page with the I/II/III/IV summary table). A 1-page PDF is saved as-is.

USAGE
-----
    python download_first_last_pages.py --acs 174
    python download_first_last_pages.py --acs 86,87,88 --concurrency 32
    python download_first_last_pages.py --acs-file acs.txt      # ACs listed in a text file
    python download_first_last_pages.py --all
    python download_first_last_pages.py --retry-failed
    python download_first_last_pages.py --acs 174 --test      # check Range support
                                                              # on 1 file, then exit

Resumable: an existing, valid output file is skipped. Failures are kept in
first_last_failed.csv (same idea as failed_or_missing.csv) and --retry-failed
re-attempts only those.

SETUP
-----
    pip install requests pypdf
"""
import argparse
import concurrent.futures
import csv
import io
import json
import pathlib
import sys
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover
    sys.exit("pypdf is required:  pip install pypdf")

STATE_CD = "S24"
YEAR = 2026
ROLL_TYPE_REF_ID = "SIR-FinalRoll"
PDF_GEN_TYPE = "EROLLGEN"
REVISION_NO = 1

BASE_URL = "https://voters.eci.gov.in/eroll/2026/s24/sir-finalroll"

HERE = pathlib.Path(__file__).parent
CSV_PATH = HERE / "UP_parts_by_AC.csv"
OUT_DIR = HERE / "first_last"
LOG_PATH = HERE / "first_last_log.jsonl"
OVERRIDES_PATH = HERE / "part_count_overrides.json"
FAILED_CSV_PATH = HERE / "first_last_failed.csv"
FAILED_CSV_FIELDS = ["ac", "part", "url", "status", "detail", "last_checked"]

OVERFLOW_PROBE = 3
BLOCK = 16 * 1024           # cache unit; page objects are tiny and scattered, so keep it small
READAHEAD = 16              # when reads are sequential (page image data), fetch this many units at once
MAX_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 2.0
MAX_RANGE_REQUESTS = 150    # a normal file needs ~40-60; far more means something is odd,
                            # so give up on ranges and fall back to a full download

HEADERS = {
    "accept": "*/*",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "referer": "https://voters.eci.gov.in/download-eroll",
}

_log_lock = threading.Lock()
_stats_lock = threading.Lock()
_stats = {"done": 0, "skipped": 0, "failed": 0, "missing_404": 0,
          "bytes": 0, "via_range": 0, "via_full": 0}
_run_lock = threading.Lock()
_run_record = {}


class NoRangeSupport(Exception):
    """Server answered 200 to a Range request (ignores ranges)."""


class NotFound(Exception):
    pass


class Transient(Exception):
    pass


# ----------------------------------------------------------------- logging
def log_event(event: dict):
    event["ts"] = time.time()
    with _log_lock:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")


def record_result(ac, part, status, detail="", is_probe=False):
    if is_probe:
        return
    with _run_lock:
        _run_record[(str(ac), str(part))] = {"status": status, "detail": detail}


def update_failure_csv(run_record: dict):
    existing = {}
    if FAILED_CSV_PATH.exists():
        with open(FAILED_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[(row["ac"], row["part"])] = row
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    added = updated = resolved = 0
    for (ac, part), info in run_record.items():
        key = (ac, part)
        if info["status"] in ("failed", "missing"):
            added, updated = (added, updated + 1) if key in existing else (added + 1, updated)
            existing[key] = {
                "ac": ac, "part": part, "url": pdf_url(ac, int(part)),
                "status": "404_missing" if info["status"] == "missing" else "failed",
                "detail": info.get("detail", ""), "last_checked": now,
            }
        elif key in existing:
            del existing[key]
            resolved += 1
    with open(FAILED_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FAILED_CSV_FIELDS)
        w.writeheader()
        for key in sorted(existing, key=lambda k: (int(k[0]), int(k[1]))):
            w.writerow(existing[key])
    print(f"{FAILED_CSV_PATH.name}: {added} new, {updated} updated, {resolved} resolved "
          f"— {len(existing)} currently listed as failed/missing.")


# ----------------------------------------------------------------- urls / csv
def make_session(pool_size=64):
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=4, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=pool_size,
                                    pool_connections=pool_size))
    s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=pool_size,
                                   pool_connections=pool_size))
    return s


def file_name(ac, part):
    return (f"{YEAR}-{PDF_GEN_TYPE}-{STATE_CD}-{ac}-{ROLL_TYPE_REF_ID}-"
            f"Revision{REVISION_NO}-HIN-{part}-WI.pdf")


def pdf_url(ac, part):
    return f"{BASE_URL}/{ac}/{file_name(ac, part)}"


def local_path(ac, part):
    return OUT_DIR / ac / file_name(ac, part)


def load_part_counts():
    counts, missing = {}, []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ac = (row.get("acNumber") or "").strip()
            if not ac:
                continue
            val = (row.get("total_parts") or "").strip()
            if val:
                counts[ac] = int(float(val))
            else:
                missing.append(ac)
    if OVERRIDES_PATH.exists():
        for ac, n in json.loads(OVERRIDES_PATH.read_text()).items():
            if ac not in counts:
                counts[ac] = int(n)
                if ac in missing:
                    missing.remove(ac)
    return counts, missing


def is_valid_output(path: pathlib.Path) -> bool:
    """A finished output: starts with %PDF- and opens with 1-2 pages."""
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        with open(path, "rb") as f:
            if f.read(5) != b"%PDF-":
                return False
        return 1 <= len(PdfReader(str(path)).pages) <= 2
    except Exception:
        return False


# ----------------------------------------------------------------- range file
class RangeFile(io.RawIOBase):
    """Read-only, seekable file object over an HTTP URL. Fetches 128 KB blocks
    on demand with Range requests and caches them, so pypdf only pulls the
    parts of the PDF it really needs."""

    def __init__(self, session, url):
        self.session, self.url = session, url
        self.pos = 0
        self.cache = {}
        self.requests = 0
        self.last_idx = -5
        self.bytes = 0
        self.size = None
        self._fetch(0)  # first block also gives the total size

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, offset, whence=0):
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.size + offset
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def _fetch(self, idx):
        if idx in self.cache:
            return self.cache[idx]
        if self.requests >= MAX_RANGE_REQUESTS:
            raise NoRangeSupport(f"more than {MAX_RANGE_REQUESTS} range requests")
        # sequential read (e.g. an image stream) -> grab several units at once
        units = READAHEAD if idx == self.last_idx + 1 else 1
        self.last_idx = idx
        start = idx * BLOCK
        end = start + units * BLOCK - 1
        if self.size is not None:
            end = min(end, self.size - 1)
        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = self.session.get(self.url, headers={"Range": f"bytes={start}-{end}"},
                                     timeout=(15, 60))
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(RETRY_BACKOFF_BASE * attempt)
                continue
            self.requests += 1
            if r.status_code == 404:
                raise NotFound("HTTP 404")
            if r.status_code == 200:
                raise NoRangeSupport("server ignored Range (HTTP 200)")
            if r.status_code == 416:
                self.cache[idx] = b""
                return b""
            if r.status_code != 206:
                raise Transient(f"HTTP {r.status_code}")
            cr = r.headers.get("Content-Range", "")  # bytes a-b/total
            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                self.size = int(cr.rsplit("/", 1)[1])
            elif self.size is None:
                raise NoRangeSupport("no Content-Range total")
            data = r.content
            if idx == 0 and not data.startswith(b"%PDF-"):
                raise Transient("no %PDF- header in response")
            self.bytes += len(data)
            for u in range(units):
                self.cache[idx + u] = data[u * BLOCK:(u + 1) * BLOCK]
            return self.cache[idx]
        raise Transient(f"range fetch failed: {last_exc}")

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        out = bytearray()
        while n > 0:
            idx, off = divmod(self.pos, BLOCK)
            blk = self._fetch(idx)
            piece = blk[off:off + n]
            if not piece:
                break
            out += piece
            self.pos += len(piece)
            n -= len(piece)
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def write_first_last(reader: PdfReader, dest: pathlib.Path) -> int:
    """Writes page 1 + the last page of `reader` to `dest` atomically.
    Returns the number of pages written."""
    n = len(reader.pages)
    if n < 1:
        raise Transient("PDF has no pages")
    w = PdfWriter()
    w.add_page(reader.pages[0])
    if n > 1:
        w.add_page(reader.pages[n - 1])
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        w.write(f)
    tmp.replace(dest)
    return 2 if n > 1 else 1


def extract_via_range(session, url, dest):
    rf = RangeFile(session, url)
    try:
        reader = PdfReader(rf)
        pages = write_first_last(reader, dest)
    except (NoRangeSupport, NotFound):
        raise
    except Transient:
        raise
    except requests.RequestException as exc:
        raise Transient(str(exc))
    except Exception as exc:
        # pypdf could not make sense of what it read through ranges — let the
        # caller fall back to a full download for this file.
        raise NoRangeSupport(f"pypdf over ranges failed: {exc}")
    return pages, rf.bytes


def extract_via_full(session, url, dest):
    """Fallback: download the whole PDF into memory, keep 2 pages."""
    try:
        r = session.get(url, timeout=(15, 120))
    except requests.RequestException as exc:
        raise Transient(str(exc))
    if r.status_code == 404:
        raise NotFound("HTTP 404")
    if r.status_code != 200:
        raise Transient(f"HTTP {r.status_code}")
    if not r.content.startswith(b"%PDF-"):
        raise Transient("no %PDF- header in response")
    pages = write_first_last(PdfReader(io.BytesIO(r.content)), dest)
    return pages, len(r.content)


# ----------------------------------------------------------------- one part
def download_one(session, ac, part, is_probe=False, force_full=False):
    dest = local_path(ac, part)
    if is_valid_output(dest):
        with _stats_lock:
            _stats["skipped"] += 1
        record_result(ac, part, "skipped", is_probe=is_probe)
        return "skipped"

    dest.parent.mkdir(parents=True, exist_ok=True)
    url = pdf_url(ac, part)
    status, detail, nbytes, method = "retry", "", 0, ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if force_full:
                raise NoRangeSupport("--full requested")
            try:
                pages, nbytes = extract_via_range(session, url, dest)
                method = "range"
            except NoRangeSupport as why:
                pages, nbytes = extract_via_full(session, url, dest)
                method = "full"
                detail = str(why)
            status = "ok"
            break
        except NotFound:
            status, detail = "missing", "HTTP 404"
            break
        except Transient as exc:
            status, detail = "retry", str(exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE * attempt)
        except Exception as exc:
            status, detail = "retry", f"unexpected: {exc}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE * attempt)

    if status == "retry":
        status = "failed"
        detail = f"{detail} (still failing after {MAX_ATTEMPTS} attempts)"

    if status == "missing":
        with _stats_lock:
            _stats["missing_404"] += 1
        record_result(ac, part, "missing", detail, is_probe=is_probe)
        return "missing"
    if status == "failed":
        dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
        log_event({"ac": ac, "part": part, "status": "error", "detail": detail})
        with _stats_lock:
            _stats["failed"] += 1
        record_result(ac, part, "failed", detail, is_probe=is_probe)
        return "failed"

    with _stats_lock:
        _stats["done"] += 1
        _stats["bytes"] += nbytes
        _stats["via_range" if method == "range" else "via_full"] += 1
    if method == "full":
        log_event({"ac": ac, "part": part, "status": "full_fallback", "detail": detail})
    record_result(ac, part, "ok", is_probe=is_probe)
    return "ok"


# ----------------------------------------------------------------- batch
def build_job_list(target_acs):
    jobs = []
    for ac, count in target_acs.items():
        for part in range(1, count + 1 + OVERFLOW_PROBE):
            jobs.append((ac, part))
    return jobs


def print_progress(total):
    with _stats_lock:
        s = dict(_stats)
    processed = s["done"] + s["skipped"] + s["failed"] + s["missing_404"]
    print(f"\r  {processed}/{total} | downloaded {s['done']} ({s['bytes']/1e6:.0f} MB; "
          f"range {s['via_range']}, full-fallback {s['via_full']}) | "
          f"skipped {s['skipped']} | 404 {s['missing_404']} | failed {s['failed']}",
          end="", flush=True)


def run_downloads(target_acs, concurrency, force_full=False):
    jobs = build_job_list(target_acs)
    total = len(jobs)
    print(f"Queued {total} part(s) across {len(target_acs)} AC(s) "
          f"(includes {OVERFLOW_PROBE} overflow probe(s)/AC beyond the CSV count).")
    session = make_session(max(64, concurrency))
    by_ac = {}
    for ac, part in jobs:
        by_ac.setdefault(ac, []).append(part)

    def process_ac(ac, parts, base):
        miss = 0
        for part in parts:
            probe = part > base
            try:
                res = download_one(session, ac, part, probe, force_full)
            except Exception as exc:
                log_event({"ac": ac, "part": part, "status": "error",
                           "detail": f"unexpected: {exc}"})
                with _stats_lock:
                    _stats["failed"] += 1
                record_result(ac, part, "failed", f"unexpected: {exc}", probe)
                res = "failed"
            if probe:
                if res == "missing":
                    miss += 1
                    if miss >= OVERFLOW_PROBE:
                        break
                else:
                    miss = 0

    # Parts of one AC run in ascending order inside one worker; ACs run in parallel.
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(process_ac, ac, parts, target_acs[ac]): ac
                for ac, parts in by_ac.items()}
        last = 0
        for fut in concurrent.futures.as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                print(f"\n  ! AC {futs[fut]} raised: {exc}")
            if time.time() - last > 0.5:
                print_progress(total)
                last = time.time()
    print_progress(total)
    print()
    update_failure_csv(_run_record)


def run_retry(jobs, concurrency, force_full=False):
    total = len(jobs)
    print(f"Retrying {total} previously failed/missing part(s)...")
    session = make_session(max(64, concurrency))
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(download_one, session, ac, part, False, force_full): (ac, part)
                for ac, part in jobs}
        last = 0
        for fut in concurrent.futures.as_completed(futs):
            ac, part = futs[fut]
            try:
                fut.result()
            except Exception as exc:
                with _stats_lock:
                    _stats["failed"] += 1
                record_result(ac, part, "failed", f"unexpected: {exc}")
            if time.time() - last > 0.5:
                print_progress(total)
                last = time.time()
    print_progress(total)
    print()
    update_failure_csv(_run_record)


def load_failed_jobs():
    if not FAILED_CSV_PATH.exists():
        return []
    with open(FAILED_CSV_PATH, newline="", encoding="utf-8") as f:
        return [(r["ac"], int(r["part"])) for r in csv.DictReader(f) if r.get("ac") and r.get("part")]


def run_test(ac, part):
    """Fetches one file and reports whether Range works and how much it saved."""
    session = make_session()
    url = pdf_url(ac, part)
    print(f"Testing {url}")
    h = session.get(url, headers={"Range": "bytes=0-1023"}, timeout=30, stream=True)
    print(f"  Range request -> HTTP {h.status_code}, Accept-Ranges={h.headers.get('Accept-Ranges')!r}, "
          f"Content-Range={h.headers.get('Content-Range')!r}")
    h.close()
    dest = local_path(ac, part)
    dest.unlink(missing_ok=True)
    t = time.time()
    res = download_one(session, ac, part)
    with _stats_lock:
        s = dict(_stats)
    print(f"  result={res} method={'range' if s['via_range'] else 'full-fallback' if s['via_full'] else '-'} "
          f"transferred={s['bytes']/1e3:.0f} KB in {time.time()-t:.1f}s -> {dest}")
    if dest.exists():
        print(f"  output pages: {len(PdfReader(str(dest)).pages)}, size {dest.stat().st_size/1e3:.0f} KB")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--acs", help="comma-separated AC numbers, e.g. 174,86")
    ap.add_argument("--acs-file", help="text file with AC numbers (one per line, or separated by spaces/commas; # starts a comment)")
    ap.add_argument("--all", action="store_true", help="every AC in the CSV / overrides")
    ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-attempt only the parts listed in first_last_failed.csv")
    ap.add_argument("--full", action="store_true",
                    help="skip Range requests; download each PDF fully and keep 2 pages")
    ap.add_argument("--test", action="store_true",
                    help="download part 1 of the first --acs AC, report Range support, exit")
    ap.add_argument("--csv", help="path to UP_parts_by_AC.csv (default: next to this script)")
    ap.add_argument("--out", help="output folder (default: ./first_last)")
    args = ap.parse_args()

    global CSV_PATH, OUT_DIR
    if args.csv:
        CSV_PATH = pathlib.Path(args.csv)
    if args.out:
        OUT_DIR = pathlib.Path(args.out)

    if args.retry_failed:
        jobs = load_failed_jobs()
        if not jobs:
            print(f"{FAILED_CSV_PATH.name} has nothing to retry.")
            return
        run_retry(jobs, args.concurrency, args.full)
        return

    counts, missing = load_part_counts()
    if missing:
        print(f"Note: {len(missing)} AC(s) have no part count and are skipped: {missing}\n"
              f"Fill them in the CSV or in {OVERRIDES_PATH.name}.\n")

    if args.acs or args.acs_file:
        wanted = set()
        if args.acs:
            wanted |= {t.strip() for t in args.acs.split(",") if t.strip()}
        if args.acs_file:
            for line in pathlib.Path(args.acs_file).read_text(encoding="utf-8-sig").splitlines():
                wanted |= {t for t in line.split("#")[0].replace(",", " ").split()}
        wanted = {str(int(t)) if t.isdigit() else t for t in wanted}      # 001 -> 1
        target = {ac: n for ac, n in counts.items() if ac in wanted}
        nf = wanted - set(target)
        if nf:
            print(f"AC(s) not found in CSV/overrides: {sorted(nf)}")
    elif args.all:
        target = counts
    else:
        print("Nothing to do — pass --acs 174 or --all.")
        sys.exit(1)
    if not target:
        print("No ACs to process.")
        return

    if args.test:
        run_test(next(iter(target)), 1)
        return

    print(f"About to fetch first+last page of ~{sum(target.values())} PDFs across "
          f"{len(target)} AC(s) with {args.concurrency} workers.\n")
    run_downloads(target, args.concurrency, args.full)


if __name__ == "__main__":
    main()