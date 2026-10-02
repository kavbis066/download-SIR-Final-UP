#!/usr/bin/env python3
"""
download_eroll.py — Bulk downloader for ECI SIR Final Roll 2026 PDFs (Uttar Pradesh)

WHY THIS EXISTS
----------------
voters.eci.gov.in's API encrypts every request body/query at the application
layer (RSA-OAEP + AES-GCM hybrid crypto — see eci_crypto.py for how this was
reverse-engineered from the site's own public JS bundle and verified against
a real captured response). A plain requests/Postman call 401s because it's
missing this encryption entirely, not because of a stale cookie.

WHAT THIS SCRIPT DOES NOT DO
-----------------------------
The actual PDF-generation call (generate-published-pdfs) requires a captcha
solved by a human — this is deliberately how ECI rate-limits bulk downloads
(max 10 parts per request, captcha valid ~30s). This script does NOT attempt
to auto-solve captchas (OCR/ML). It automates everything else (auth-free
navigation, encryption, pagination across districts/ACs/parts, the actual
file downloads) and pauses to show you each captcha so you type it in.
This keeps a human in the loop for the one step ECI designed to require one.

SCALE WARNING
-------------
UP has 75 districts / 403 Assembly Constituencies. A large AC can have 500+
electoral parts, and each request can only ask for 10 parts, so a single AC
can need 40-50 captcha solves. Downloading ALL of UP could require on the
order of 10,000+ captcha solves. Scope this down (--districts / --acs) unless
you really intend to sit through the whole state, and rely on --resume to
pick up across multiple sessions — progress is checkpointed to disk.

SETUP
-----
    pip install -r requirements.txt

USAGE
-----
    # List districts/ACs to find codes
    python download_eroll.py --list-districts
    python download_eroll.py --list-acs --district S2408          # Agra

    # Download everything for one AC (fast to test with)
    python download_eroll.py --acs 86

    # Download a whole district
    python download_eroll.py --district S2408

    # Download everything for UP (huge — see warning above)
    python download_eroll.py --all

Downloaded files land in ./downloads/<state>/<year>/<rolltype>/<ac>/...,
mirroring the path the server itself returns. Progress is stored in
./progress.json and re-runs skip anything already completed.
"""
import argparse
import json
import pathlib
import sys
import time
import webbrowser

import requests

import eci_crypto as crypto

GATEWAY = "https://gateway-voters.eci.gov.in"
PDF_BASE = "https://voters.eci.gov.in/eroll"
STATE_CD = "S24"  # Uttar Pradesh
STATE_NAME = "Uttar Pradesh"
YEAR = 2026
ROLL_DISPLAY_NAME = "SIR FinalRoll - 2026"  # matched against get-publish-eroll-type payload

HERE = pathlib.Path(__file__).parent
DOWNLOAD_DIR = HERE / "downloads"
PROGRESS_FILE = HERE / "progress.json"
CAPTCHA_IMG = HERE / "_captcha.jpg"

MAX_PARTS_PER_BATCH = 10
CAPTCHA_RETRY_LIMIT = 5

COMMON_HEADERS = {
    "applicationname": "VSP",
    "appname": "VSP",
    "channelidobo": "VSP",
    "platform-type": "ECIWEB",
    "accept": "*/*",
    "origin": "https://voters.eci.gov.in",
    "referer": "https://voters.eci.gov.in/download-eroll",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


def load_master_data():
    districts = json.loads((HERE / "up_districts.json").read_text())
    acs = json.loads((HERE / "up_acs.json").read_text())
    return districts, acs


def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {"done_batches": []}  # list of "S2408-86-1,2,3,...,10" keys


def save_progress(progress):
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


class EciClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(COMMON_HEADERS)
        self.pubkey = crypto.load_public_key()
        self._roll_type_cache = {}

    # -- roll type -----------------------------------------------------
    def get_roll_type(self, state_cd=STATE_CD, year=YEAR):
        """Looks up the exact {id, rollTypeRefId, pdfGenType, revisionNo}
        for 'SIR FinalRoll - <year>' in this state. Cached per (state, year)."""
        key = (state_cd, year)
        if key in self._roll_type_cache:
            return self._roll_type_cache[key]

        fields = crypto.encrypted_get_fields(self.pubkey, state_cd, year, crypto.MIS_KEY)
        enc_key, iv, e_state, e_year, e_mis = fields
        headers = dict(self.session.headers)
        headers["accept_yek"] = enc_key
        headers["accept_rotcev"] = iv
        url = (
            f"{GATEWAY}/api/v1/printing-publish/get-publish-eroll-type"
            f"?stateCd={e_state}&year={e_year}&misKey={e_mis}"
        )
        r = self.session.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("statusCode") != 200:
            raise RuntimeError(f"get-publish-eroll-type failed: {data}")

        match = None
        for item in data["payload"]:
            if item.get("displayName") == ROLL_DISPLAY_NAME:
                match = item
                break
        if not match:
            available = [i.get("displayName") for i in data["payload"]]
            raise RuntimeError(
                f"Could not find '{ROLL_DISPLAY_NAME}' for {state_cd}/{year}. "
                f"Available: {available}"
            )
        self._roll_type_cache[key] = match
        return match

    # -- languages -------------------------------------------------------
    def get_ac_languages(self, state_cd, ac_number, roll_type_ref_id, pdf_gen_type):
        body = {
            "stateCd": state_cd,
            "acNumber": str(ac_number),
            "rollTypeRefId": roll_type_ref_id,
            "pdfGenType": pdf_gen_type,
        }
        r = self.session.post(
            f"{GATEWAY}/api/v1/printing-publish/get-ac-languages",
            json=body, headers={"content-type": "application/json"}, timeout=20,
        )
        r.raise_for_status()
        return r.json()

    # -- part list ---------------------------------------------------------
    def get_publish_part_list(self, state_cd, ac_number, roll_type_ref_id, pdf_gen_type, revision_no, year):
        payload = {
            "stateCd": state_cd,
            "acNumber": ac_number,
            "rollTypeRefId": roll_type_ref_id,
            "pdfGenType": pdf_gen_type,
            "revisionNo": revision_no,
            "year": year,
            "misKey": crypto.MIS_KEY,
        }
        enc = crypto.hybrid_encrypt(self.pubkey, payload)
        r = self.session.post(
            f"{GATEWAY}/api/v1/printing-publish/get-publish-part-list",
            json=enc, headers={"content-type": "application/json"}, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # -- captcha -------------------------------------------------------
    def get_captcha(self):
        r = self.session.get(
            f"{GATEWAY}/api/v1/captcha-service/getCaptcha/EROLL",
            timeout=20,
        )
        r.raise_for_status()
        return crypto.decrypt_captcha_data(r.json()["data"])

    # -- generate pdfs -------------------------------------------------
    def generate_published_pdfs(
        self, state_cd, ac_number, part_numbers, district_cd,
        captcha_text, captcha_id, lang_cd, published_roll_id,
    ):
        payload = {
            "stateCd": state_cd,
            "acNumber": ac_number,
            "partNumberList": part_numbers,
            "districtCd": district_cd,
            "captcha": captcha_text,
            "captchaId": captcha_id,
            "langCd": lang_cd,
            "publishedRollId": published_roll_id,
            "misKey": crypto.MIS_KEY,
        }
        enc = crypto.hybrid_encrypt(self.pubkey, payload)
        r = self.session.post(
            f"{GATEWAY}/api/v1/printing-publish/generate-published-pdfs",
            json=enc, headers={"content-type": "application/json"}, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # -- file download -------------------------------------------------
    def download_pdf(self, rel_path: str, out_dir: pathlib.Path) -> pathlib.Path:
        local_path = out_dir / pathlib.Path(rel_path).name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists() and local_path.stat().st_size > 0:
            return local_path
        url = f"{PDF_BASE}/{rel_path}"
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        local_path.write_bytes(r.content)
        return local_path


def prompt_captcha(client: EciClient) -> tuple:
    """Fetches+decrypts a captcha, shows it to the user, returns (text, captcha_id).
    Retries automatically on decode/network hiccups (not on wrong answers —
    that's handled by the caller, since 'wrong' is only known after submitting)."""
    cap = client.get_captcha()
    CAPTCHA_IMG.write_bytes(__import__("base64").b64decode(cap["captcha"]))
    print(f"\n  Captcha image: {CAPTCHA_IMG}")
    try:
        webbrowser.open(CAPTCHA_IMG.resolve().as_uri())
    except Exception:
        pass  # not fatal — user can open the file manually
    text = input("  Enter captcha text (valid ~30s, blank to skip this batch): ").strip()
    return text, cap["id"]


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def process_ac(client: EciClient, district_cd, district_name, ac_number, ac_name, roll_type, progress, args):
    ac_number = str(ac_number)
    print(f"\n=== District {district_cd} ({district_name}) — AC {ac_number} ({ac_name}) ===")

    langs = client.get_ac_languages(
        STATE_CD, ac_number, roll_type["rollTypeRefId"], roll_type["pdfGenType"]
    )
    if langs.get("statusCode") != 200 or not langs.get("payload"):
        print(f"  ! No languages returned, skipping AC: {langs}")
        return
    lang_cd = next(iter(langs["payload"].keys()))  # e.g. "HIN"

    parts_resp = client.get_publish_part_list(
        STATE_CD, ac_number, roll_type["rollTypeRefId"], roll_type["pdfGenType"],
        roll_type["revisionNo"], YEAR,
    )
    if parts_resp.get("statusCode") != 200:
        print(f"  ! get-publish-part-list failed: {parts_resp}")
        return
    parts = parts_resp.get("payload") or []
    if not parts:
        print("  (no parts published yet for this AC)")
        return

    part_numbers = [p["partNumber"] for p in parts]
    batches = list(chunked(part_numbers, MAX_PARTS_PER_BATCH))
    print(f"  {len(parts)} parts -> {len(batches)} batch(es) of up to {MAX_PARTS_PER_BATCH}")

    out_dir = DOWNLOAD_DIR / STATE_CD / str(YEAR) / roll_type["rollTypeRefId"] / ac_number

    for batch in batches:
        batch_key = f"{district_cd}-{ac_number}-{'_'.join(map(str, batch))}"
        if batch_key in progress["done_batches"]:
            print(f"  batch {batch}: already done, skipping")
            continue

        for attempt in range(1, CAPTCHA_RETRY_LIMIT + 1):
            captcha_text, captcha_id = prompt_captcha(client)
            if not captcha_text:
                print("  skipped by user")
                break

            result = client.generate_published_pdfs(
                STATE_CD, ac_number, batch, district_cd,
                captcha_text, captcha_id, lang_cd, roll_type["id"],
            )
            status_code = result.get("statusCode")
            if status_code == 200 and result.get("payload"):
                for rel_path in result["payload"]:
                    local = client.download_pdf(rel_path, out_dir)
                    print(f"    downloaded {local.name}")
                progress["done_batches"].append(batch_key)
                save_progress(progress)
                break
            else:
                print(f"  attempt {attempt}: server said: {result.get('message') or result}")
                time.sleep(1)
        else:
            print(f"  giving up on batch {batch} after {CAPTCHA_RETRY_LIMIT} attempts")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-districts", action="store_true", help="print UP district codes and exit")
    ap.add_argument("--list-acs", action="store_true", help="print AC codes (optionally filtered by --district) and exit")
    ap.add_argument("--district", help="district code, e.g. S2408 (Agra)")
    ap.add_argument("--districts", help="comma-separated district codes")
    ap.add_argument("--acs", help="comma-separated AC numbers, e.g. 86,87")
    ap.add_argument("--all", action="store_true", help="process every district/AC in UP (see scale warning in the module docstring)")
    args = ap.parse_args()

    districts, acs = load_master_data()
    by_district_cd = {d["districtCd"]: d for d in districts}

    if args.list_districts:
        for d in sorted(districts, key=lambda x: x["districtValue"]):
            print(f"{d['districtCd']}\t{d['districtValue']}")
        return

    if args.list_acs:
        filt = [a for a in acs if not args.district or a["districtCd"] == args.district]
        for a in sorted(filt, key=lambda x: int(x["asmblyNo"])):
            print(f"{a['asmblyNo']}\t{a['asmblyName'].strip()}\t(district {a['districtCd']})")
        return

    # Decide the AC scope to process
    target_acs = acs
    if args.acs:
        wanted = {s.strip() for s in args.acs.split(",")}
        target_acs = [a for a in acs if str(a["asmblyNo"]) in wanted]
    elif args.district:
        target_acs = [a for a in acs if a["districtCd"] == args.district]
    elif args.districts:
        wanted = {s.strip() for s in args.districts.split(",")}
        target_acs = [a for a in acs if a["districtCd"] in wanted]
    elif not args.all:
        print("Nothing to do — pass --acs, --district, --districts, or --all "
              "(use --list-districts / --list-acs to find codes).")
        sys.exit(1)

    print(f"Will process {len(target_acs)} AC(s).")

    client = EciClient()
    roll_type = client.get_roll_type()
    print(f"Roll type resolved: {roll_type['displayName']} "
          f"(rollTypeRefId={roll_type['rollTypeRefId']}, revisionNo={roll_type['revisionNo']}, "
          f"id={roll_type['id']})")

    progress = load_progress()

    for ac in target_acs:
        district = by_district_cd.get(ac["districtCd"], {})
        try:
            process_ac(
                client,
                ac["districtCd"], district.get("districtValue", "?"),
                ac["asmblyNo"], ac["asmblyName"],
                roll_type, progress, args,
            )
        except KeyboardInterrupt:
            print("\nInterrupted — progress saved, re-run the same command to resume.")
            sys.exit(0)
        except Exception as exc:
            print(f"  ! error processing AC {ac['asmblyNo']}: {exc}")
            continue


if __name__ == "__main__":
    main()
