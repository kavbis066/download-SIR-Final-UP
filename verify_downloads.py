#!/usr/bin/env python3
"""
verify_downloads.py — checks every AC's downloads/<AC>/ folder against
UP_parts_by_AC.csv, part by part, so you don't have to eyeball file
sequences by hand across hundreds of folders.

WHY A COUNT ISN'T ENOUGH
--------------------------
"downloads/86 has 520 files and the CSV says 520 parts" can still be
WRONG — e.g. part 217 could be missing while an overflow part (521)
is sitting there instead, and a bare file count would show "520/520,
all good" while actually missing a real part. This script checks every
individual part number 1..count for each AC, not just the total count,
and also validates each file is actually a real, uncorrupted PDF (same
%PDF- header + minimum size check download_pdfs_direct.py itself uses)
rather than just checking the filename exists — a 0-byte or truncated
file left over from an interrupted run would otherwise look "present".

It reuses download_pdfs_direct.py's own CSV-loading, path-building and
validity-check logic directly (same folder, imported as a module), so
this check always agrees with what the downloader itself considers
complete/valid — there's no separate, possibly-drifting definition of
"done" to keep in sync.

USAGE
-----
    # Check every AC in the CSV
    python verify_downloads.py

    # Check a specific set of ACs only
    python verify_downloads.py --acs 86,87,88

    # Write the per-missing-part detail to a different file
    python verify_downloads.py --out missing_parts.csv

Always writes missing_parts.csv (or --out) listing exactly which parts
are missing/invalid per AC, and prints a one-line-per-AC summary plus a
final totals line. Exit code is 0 if everything checked out complete,
1 if anything is missing/invalid — so it's safe to use in a script,
e.g.: `python verify_downloads.py --acs 86 && echo "86 is complete"`.
"""
import argparse
import csv
import pathlib
import sys

import download_pdfs_direct as dl

OUT_PATH_DEFAULT = dl.HERE / "missing_parts.csv"


def check_ac(ac: str, count: int):
    """Returns (found_valid, missing_parts) where missing_parts is a
    list of (part, reason) for every part 1..count that's absent or
    fails the %PDF- validity check."""
    missing = []
    found_valid = 0
    for part in range(1, count + 1):
        path = dl.local_path(ac, part)
        if not path.exists():
            missing.append((part, "missing"))
        elif not dl.is_valid_pdf(path):
            missing.append((part, "invalid_or_corrupt"))
        else:
            found_valid += 1
    return found_valid, missing


def extra_files(ac: str, count: int):
    """Files physically present in downloads/<AC>/ with a part number
    beyond the CSV's known count — not a problem by itself (could be a
    legitimate overflow the CSV undercounts), but worth surfacing since
    it's a sign the CSV's count for this AC might be stale."""
    folder = dl.OUT_DIR / ac
    if not folder.exists():
        return []
    extras = []
    prefix = f"{dl.YEAR}-{dl.PDF_GEN_TYPE}-{dl.STATE_CD}-{ac}-{dl.ROLL_TYPE_REF_ID}-Revision{dl.REVISION_NO}-HIN-"
    for p in folder.glob("*.pdf"):
        name = p.stem
        if not name.startswith(prefix) or not name.endswith("-WI"):
            continue
        num_str = name[len(prefix):-len("-WI")]
        if num_str.isdigit() and int(num_str) > count:
            extras.append(int(num_str))
    return sorted(extras)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--acs", help="comma-separated AC numbers to check, e.g. 86,87 (default: every AC in the CSV)")
    ap.add_argument("--out", default=str(OUT_PATH_DEFAULT), help=f"detail CSV path (default: {OUT_PATH_DEFAULT.name})")
    ap.add_argument("--quiet", action="store_true", help="only print the final summary, not one line per AC")
    args = ap.parse_args()

    counts, csv_missing = dl.load_part_counts()
    if csv_missing:
        print(f"Note: {len(csv_missing)} AC(s) have no part count in the CSV and can't be "
              f"checked: {csv_missing}\n")

    if args.acs:
        wanted = {s.strip() for s in args.acs.split(",")}
        target = {ac: n for ac, n in counts.items() if ac in wanted}
        not_found = wanted - set(target)
        if not_found:
            print(f"AC(s) not found in CSV/overrides, skipped: {sorted(not_found)}")
    else:
        target = counts

    if not target:
        print("No ACs to check.")
        sys.exit(1)

    detail_rows = []
    complete_acs = incomplete_acs = 0
    total_expected = total_found = total_missing = 0

    for ac in sorted(target, key=lambda a: int(a)):
        count = target[ac]
        found_valid, missing = check_ac(ac, count)
        extras = extra_files(ac, count)
        total_expected += count
        total_found += found_valid
        total_missing += len(missing)

        if missing:
            incomplete_acs += 1
            status = f"INCOMPLETE — {len(missing)} missing/invalid"
        else:
            complete_acs += 1
            status = "OK"
        if extras:
            status += f" ({len(extras)} extra file(s) beyond known count — CSV may undercount this AC)"

        if not args.quiet:
            print(f"AC {ac:>4}: {found_valid:>4}/{count:<4} valid — {status}")

        for part, reason in missing:
            detail_rows.append({
                "ac": ac, "part": part,
                "expected_path": str(dl.local_path(ac, part)),
                "reason": reason,
            })

    out_path = pathlib.Path(args.out)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ac", "part", "expected_path", "reason"])
        writer.writeheader()
        for row in sorted(detail_rows, key=lambda r: (int(r["ac"]), int(r["part"]))):
            writer.writerow(row)

    print(f"\n{'='*60}")
    print(f"ACs checked:     {len(target)}")
    print(f"  complete:      {complete_acs}")
    print(f"  incomplete:    {incomplete_acs}")
    print(f"Parts expected:  {total_expected}")
    print(f"Parts valid:     {total_found}")
    print(f"Parts missing:   {total_missing}")
    print(f"Detail written to: {out_path}")

    sys.exit(1 if total_missing else 0)


if __name__ == "__main__":
    main()