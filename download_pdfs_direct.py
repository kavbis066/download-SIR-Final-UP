#!/usr/bin/env python3
"""
download_pdfs_direct.py — Bulk, captcha-free downloader for ECI SIR Final
Roll 2026 PDFs (Uttar Pradesh), built on a confirmed finding:

    The PDFs at https://voters.eci.gov.in/eroll/2026/s24/sir-finalroll/<AC>/
    2026-EROLLGEN-S24-<AC>-SIR-FinalRoll-Revision1-HIN-<part>-WI.pdf
    are pre-published static files. The captcha in generate-published-pdfs
    only gates the *website's own UI flow* for triggering generation — once
    a file exists, it's a plain public GET, no auth, no captcha, no session.
    (Verified: HIN-400-WI.pdf for AC 86 downloaded directly, with no captcha
    ever solved for that part.)

So this script does NOT touch the encrypted API or captcha flow at all for
downloading. It only:
  1. Reads part-counts-per-AC from UP_parts_by_AC.csv (your export)
  2. For any AC missing a count, optionally looks it up live via the
     get-publish-part-list API (this call has never required a captcha —
     see eci_crypto.py / README from the previous script)
  3. Constructs every part URL and downloads them in parallel, with
     retries, resume, and a small overflow probe in case the CSV
     undercounts an AC's parts

SCALE — READ BEFORE RUNNING --all
-----------------------------------
174,088 files across UP. Your one sample file (AC 86, part 17) was 6.9MB
for ~905 voters / 35 pages. At that rate, total download size for the
whole state is in the neighborhood of 900GB-1.2TB. The bottleneck now is
bandwidth and disk, not captcha-solving or compute. Scope with --acs if
you don't need the whole state, and check disk space first either way.

SPEED
-----
This is network I/O-bound, not CPU-bound — one thread spends almost all
its time just waiting for ECI's server to respond, so --concurrency
(default 24, was 12) is the main speed lever, not your CPU core count.
Also switched to streaming the response straight to disk instead of
buffering the whole PDF in memory first, which is what makes raising
concurrency safe without ballooning RAM use. If download_log.jsonl stays
free of 429/503 "bad_response" entries at --concurrency 24, try 32 or 48
next; back off if they start appearing. Already-valid files are skipped
on re-run, so there's no harm in stopping and restarting with a
different --concurrency value.

SETUP
-----
    pip install -r requirements.txt

USAGE
-----
    # One AC, to test
    python download_pdfs_direct.py --acs 86

    # A handful of ACs
    python download_pdfs_direct.py --acs 86,87,88

    # Everything in the CSV
    python download_pdfs_direct.py --all

    # Resolve the 4 ACs with blank part counts in the CSV (55,61,62,170)
    # via the live API (no captcha needed) instead of downloading yet
    python download_pdfs_direct.py --resolve-missing-counts

Files land in ./downloads/<AC>/<filename>.pdf. Progress/failures are
logged to ./download_log.jsonl and safely resumable — already-downloaded
files (valid, non-empty, correct %PDF header) are skipped on re-run.
"""
import argparse
import concurrent.futures
import csv
import json
import pathlib
import sys
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STATE_CD = "S24"
YEAR = 2026
ROLL_TYPE_REF_ID = "SIR-FinalRoll"
PDF_GEN_TYPE = "EROLLGEN"
REVISION_NO = 1  # confirmed live via get-publish-eroll-type for S24/2026

BASE_URL = "https://voters.eci.gov.in/eroll/2026/s24/sir-finalroll"
GATEWAY = "https://gateway-voters.eci.gov.in"

HERE = pathlib.Path(__file__).parent
CSV_PATH = HERE / "UP_parts_by_AC.csv"
OUT_DIR = HERE / "downloads"
LOG_PATH = HERE / "download_log.jsonl"
OVERRIDES_PATH = HERE / "part_count_overrides.json"

OVERFLOW_PROBE = 3  # after the CSV's count, try this many extra part numbers
                     # in case the CSV undercounts; stop after this many
                     # consecutive misses

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
_stats = {"done": 0, "skipped": 0, "failed": 0, "bytes": 0, "missing_404": 0}


def log_event(event: dict):
    event["ts"] = time.time()
    with _log_lock:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")


def make_session(pool_size: int = 64):
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=4, backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    # pool_size should be >= --concurrency, or requests queues connections
    # behind urllib3's pool instead of actually running them in parallel.
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=pool_size, pool_connections=pool_size))
    return s


def pdf_url(ac: str, part: int) -> str:
    return f"{BASE_URL}/{ac}/{YEAR}-{PDF_GEN_TYPE}-{STATE_CD}-{ac}-{ROLL_TYPE_REF_ID}-Revision{REVISION_NO}-HIN-{part}-WI.pdf"


def local_path(ac: str, part: int) -> pathlib.Path:
    return OUT_DIR / ac / f"{YEAR}-{PDF_GEN_TYPE}-{STATE_CD}-{ac}-{ROLL_TYPE_REF_ID}-Revision{REVISION_NO}-HIN-{part}-WI.pdf"


def is_valid_pdf(path: pathlib.Path) -> bool:
    if not path.exists() or path.stat().st_size < 1024:
        return False
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"


def load_part_counts():
    counts = {}
    missing = []
    with open(CSV_PATH, newline="") as f:
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
        overrides = json.loads(OVERRIDES_PATH.read_text())
        for ac, n in overrides.items():
            counts[ac] = int(n)
            if ac in missing:
                missing.remove(ac)

    return counts, missing


def resolve_missing_counts_live(missing_acs):
    """Looks up exact part counts for ACs the CSV left blank, via the
    get-publish-part-list API. This call has never required a captcha
    (only the actual PDF-generation call did) — see eci_crypto.py."""
    try:
        import eci_crypto as crypto
    except ImportError:
        print("eci_crypto.py not found — can't resolve missing counts live. "
              "Fill part_count_overrides.json manually instead, e.g.:")
        print('  {"55": 400, "61": 380, "62": 390, "170": 420}')
        return {}

    session = make_session()
    pubkey = crypto.load_public_key()
    resolved = {}
    for ac in missing_acs:
        payload = {
            "stateCd": STATE_CD,
            "acNumber": str(ac),
            "rollTypeRefId": ROLL_TYPE_REF_ID,
            "pdfGenType": PDF_GEN_TYPE,
            "revisionNo": REVISION_NO,
            "year": YEAR,
            "misKey": crypto.MIS_KEY,
        }
        try:
            enc = crypto.hybrid_encrypt(pubkey, payload)
            r = session.post(
                f"{GATEWAY}/api/v1/printing-publish/get-publish-part-list",
                json=enc, headers={"content-type": "application/json"}, timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("statusCode") == 200 and data.get("payload"):
                n = len(data["payload"])
                resolved[ac] = n
                print(f"  AC {ac}: resolved {n} parts live")
            else:
                print(f"  AC {ac}: API returned no parts ({data.get('message')}) — check manually")
        except Exception as exc:
            print(f"  AC {ac}: live lookup failed ({exc}) — check manually or add to "
                  f"{OVERRIDES_PATH.name}")
    if resolved:
        existing = json.loads(OVERRIDES_PATH.read_text()) if OVERRIDES_PATH.exists() else {}
        existing.update(resolved)
        OVERRIDES_PATH.write_text(json.dumps(existing, indent=2))
        print(f"Saved to {OVERRIDES_PATH.name}")
    return resolved


def download_one(session, ac: str, part: int) -> str:
    """Returns 'ok' | 'skipped' | 'missing' | 'failed'.

    Streams the response straight to disk (stream=True + iter_content)
    instead of buffering the whole PDF in memory via r.content. With
    --concurrency raised well above the old default of 12, buffering
    every in-flight file fully in RAM at once stops being free — on a
    ~7MB average file, 64 concurrent downloads held in memory is ~450MB
    just for response bodies, on top of everything else running. This
    also means we stop writing a corrupt file: we check the %PDF- header
    on the first chunk and bail out (deleting the partial .part file)
    before most of a bad response has even been written, rather than
    discovering it only after writing the whole thing."""
    dest = local_path(ac, part)
    if is_valid_pdf(dest):
        with _stats_lock:
            _stats["skipped"] += 1
        return "skipped"

    dest.parent.mkdir(parents=True, exist_ok=True)
    url = pdf_url(ac, part)
    try:
        r = session.get(url, timeout=60, stream=True)
    except requests.RequestException as exc:
        log_event({"ac": ac, "part": part, "status": "error", "detail": str(exc)})
        with _stats_lock:
            _stats["failed"] += 1
        return "failed"

    try:
        if r.status_code == 404:
            with _stats_lock:
                _stats["missing_404"] += 1
            return "missing"
        if r.status_code != 200:
            log_event({"ac": ac, "part": part, "status": "bad_response",
                        "http_status": r.status_code})
            with _stats_lock:
                _stats["failed"] += 1
            return "failed"

        tmp_dest = dest.with_suffix(dest.suffix + ".part")
        size = 0
        first_chunk = True
        try:
            with open(tmp_dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 18):  # 256KB
                    if not chunk:
                        continue
                    if first_chunk:
                        if not chunk.startswith(b"%PDF-"):
                            log_event({"ac": ac, "part": part, "status": "bad_response",
                                        "http_status": r.status_code, "detail": "no %PDF- header"})
                            with _stats_lock:
                                _stats["failed"] += 1
                            return "failed"
                        first_chunk = False
                    f.write(chunk)
                    size += len(chunk)
        finally:
            if first_chunk:  # loop never ran, or we bailed before writing — nothing valid written
                tmp_dest.unlink(missing_ok=True)

        tmp_dest.rename(dest)
        with _stats_lock:
            _stats["done"] += 1
            _stats["bytes"] += size
        return "ok"
    finally:
        r.close()


def build_job_list(target_acs: dict):
    """target_acs: {ac: part_count}. Yields (ac, part) including overflow probes."""
    jobs = []
    for ac, count in target_acs.items():
        for part in range(1, count + 1):
            jobs.append((ac, part))
        for extra in range(count + 1, count + 1 + OVERFLOW_PROBE):
            jobs.append((ac, extra))  # probes; consecutive-miss cutoff handled at runtime
    return jobs


def print_progress(total):
    with _stats_lock:
        done, skipped, failed, missing, mb = (
            _stats["done"], _stats["skipped"], _stats["failed"],
            _stats["missing_404"], _stats["bytes"] / 1e6,
        )
    processed = done + skipped + failed + missing
    print(f"\r  {processed}/{total} processed | downloaded {done} ({mb:.0f} MB) | "
          f"skipped(cached) {skipped} | 404 {missing} | failed {failed}", end="", flush=True)


def run_downloads(target_acs: dict, concurrency: int):
    jobs = build_job_list(target_acs)
    total = len(jobs)
    print(f"Queued {total} URL(s) across {len(target_acs)} AC(s) (includes {OVERFLOW_PROBE} "
          f"overflow probe(s)/AC beyond the CSV count).")

    session = make_session(pool_size=max(64, concurrency))
    # group jobs by AC so we can stop probing overflow after consecutive misses
    by_ac = {}
    for ac, part in jobs:
        by_ac.setdefault(ac, []).append(part)

    def process_ac(ac, parts, base_count):
        results = []
        consecutive_miss = 0
        for part in parts:
            res = download_one(session, ac, part)
            results.append((part, res))
            if part > base_count:  # in overflow-probe territory
                if res == "missing":
                    consecutive_miss += 1
                    if consecutive_miss >= OVERFLOW_PROBE:
                        break
                else:
                    consecutive_miss = 0
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(process_ac, ac, parts, target_acs[ac]): ac
            for ac, parts in by_ac.items()
        }
        last_print = 0
        for fut in concurrent.futures.as_completed(futures):
            ac = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                print(f"\n  ! AC {ac} raised: {exc}")
            now = time.time()
            if now - last_print > 0.5:
                print_progress(total)
                last_print = now
    print_progress(total)
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--acs", help="comma-separated AC numbers, e.g. 86,87")
    ap.add_argument("--all", action="store_true", help="every AC found in the CSV / overrides")
    ap.add_argument("--concurrency", type=int, default=24,
                     help="parallel download workers (default 24). This is I/O-bound (waiting on "
                          "the network), not CPU-bound, so raising this is the main lever for "
                          "faster bulk downloads — try 32-48 if 24 runs cleanly with no 429/503s "
                          "in download_log.jsonl. Back off if you start seeing those (the built-in "
                          "retry/backoff will mostly absorb occasional ones on its own).")
    ap.add_argument("--resolve-missing-counts", action="store_true",
                     help="look up part counts for ACs the CSV left blank, via the live API (no captcha needed), and exit")
    args = ap.parse_args()

    counts, missing = load_part_counts()

    if args.resolve_missing_counts:
        if not missing:
            print("No missing ACs — nothing to resolve.")
            return
        print(f"Resolving {len(missing)} AC(s) with blank part counts: {missing}")
        resolve_missing_counts_live(missing)
        return

    if missing:
        print(f"Note: {len(missing)} AC(s) have no part count and will be skipped: {missing}")
        print(f"Run with --resolve-missing-counts to fetch them live, or add them to "
              f"{OVERRIDES_PATH.name} manually.\n")

    if args.acs:
        wanted = {s.strip() for s in args.acs.split(",")}
        target = {ac: n for ac, n in counts.items() if ac in wanted}
        not_found = wanted - set(target)
        if not_found:
            print(f"AC(s) not found in CSV/overrides: {sorted(not_found)}")
    elif args.all:
        target = counts
    else:
        print("Nothing to do — pass --acs 86,87 or --all (or --resolve-missing-counts).")
        sys.exit(1)

    if not target:
        print("No ACs to process.")
        return

    total_files_est = sum(target.values())
    print(f"About to fetch ~{total_files_est} files across {len(target)} AC(s) "
          f"with {args.concurrency} parallel workers.\n")

    run_downloads(target, args.concurrency)


if __name__ == "__main__":
    main()