"""
Geotab Active Device Plan — Change Alert
=========================================

Watches the "Geotab Devices" table in Zoho Analytics
(https://analytics.zoho.com/workspace/953790000013364003/view/953790000054827102),
which refreshes every ~3 hours, and emails you whenever any device's
`activeDevicePlan_name` changes between two runs.

THE ROOT CAUSE OF THE 401 (found — read this first)
----------------------------------------------------
ZOHO_WORKDRIVE_FOLDER_ID was never a folder. The diagnostic came back with:

    "name": "GoZen Workdrive ", "type": "team", "is_folder": false,
    "parent_id": "-1", "files_count": 220298, "size": "1.50 TB"

`8w1iccca22f6b282d4565b0689768dfb0e44f` is your whole WorkDrive TEAM — the
org-level container that team folders live under. It cannot hold files
directly, which accounts for every symptom at once: listing it returned 0
items, creating a folder in it returned R602 "Invalid parent Id", and both
upload endpoints returned 401 F900. The token, its scopes, and the account's
permissions were never the problem.

To fix: run `workdrive_find_folder.py` to walk from the team down to a real
folder (or create one), confirm it accepts an upload with --test, and put
that ID in .env as ZOHO_WORKDRIVE_FOLDER_ID. Then run this script with
--retry-upload to push the already-cached snapshot without paying for
another Analytics export.

This version also checks the target up front (one API call) and refuses to
start a 73 MB export when the destination can't receive it.

EARLIER: THE WRITE-TIMEOUT FIX
-------------------------------
Second round of fixes. The 401 is gone — the token's scope turned out to be
`WorkDrive.files.ALL WorkDrive.teamfolders.ALL` all along, so the 401 was
purely the wrong endpoint (see item 1 below), not a permission problem. The
next run got all the way to the upload and then died with:

    urllib3.exceptions.ProtocolError: ('Connection aborted.',
        TimeoutError('The write operation timed out'))

That is not an API error. Zoho never replied at all — the TCP connection
stopped accepting our bytes while Python was still pushing the 3 MB body
into it, and the socket eventually gave up on the *send*. Causes, roughly in
order of how often they turn out to be it: a corporate proxy or TLS-
inspecting firewall interrupting a long outbound POST, a flaky link, or Zoho
briefly stalling. Nothing in the script can prevent it; what the script CAN
do is stop treating it as fatal:

  * The upload now retries 4 times (5s / 15s / 45s backoff) with a 180s
    send/read timeout instead of one 600s attempt that fails once and dies.
  * If all 4 fail, it automatically falls back to the chunked upload API,
    which lives on a different host (upload.zoho.com) and sends the body in
    pieces — that sometimes slips past a proxy that kills one long POST.
  * The snapshot is now cached to `last_snapshot_cache.csv` BEFORE the
    upload is attempted, and `--retry-upload` pushes just that file. So a
    network failure on the last step no longer costs you another 73 MB
    Analytics export to get back to where you were.
  * If everything still fails, the error message says explicitly that this
    is a network problem and not a credentials problem, and what to try.

If it keeps happening, the diagnostic worth doing is one run from a phone
hotspot. If it works there and not on your office network, it's the proxy,
and the fix is a network-side one (allow outbound POSTs to
www.zohoapis.com / upload.zoho.com, or point HTTPS_PROXY at the proxy
explicitly so requests negotiates with it instead of being intercepted).

FIRST ROUND OF FIXES (the 401)
-------------------------------
The earlier run failed at the same step with:

    ERROR: WorkDrive create-upload-session failed 401:
    {"errors":[{"id":"F900","title":"Authorization check failed."}]}

The cause was item 1. (Item 2 was my first guess and it was wrong — the
token's scope was fine; keeping it here because it's still the right thing to
check if a 401 ever comes back.)

1. WRONG UPLOAD API FOR THIS FILE SIZE.  <-- this was the actual 401
   The previous version used WorkDrive's *chunked* upload protocol
   (uploadsession/create -> stream/upload -> uploadsession/commit). That
   protocol exists for files over 250 MB. Your export is 73 MB, so it can
   go through WorkDrive's ordinary single-shot upload endpoint, which is
   one HTTP call and far fewer things to get wrong:

       POST https://www.zohoapis.com/workdrive/api/v1/upload
       multipart/form-data: content=<file bytes>, parent_id=<folder>,
                            filename=<name>, override-name-exist=true

   `override-name-exist=true` means "if a file with this name already
   exists here, save this as a new top version of it" — which is exactly
   the behaviour this script needs, and it also works on the first run
   when the file doesn't exist yet. Chunked upload is still in here as an
   automatic fallback, but only for files above 240 MB.

2. A MISSING WRITE SCOPE — RULED OUT, but worth re-checking on any future 401.
   Look at the order of your log lines: the folder *listing* succeeded
   (it correctly reported "Geotab_Devices.csv not found"), and then the
   *write* failed with 401. Reading worked, writing didn't — which points
   at the refresh token's scope, not at the URL, the folder ID, or the
   client secret. Per Zoho's own API spec, listing a folder needs
   `WorkDrive.files.READ` and uploading needs `WorkDrive.files.CREATE`.
   If your grant was READ-only, this is precisely what you'd see.

   Fix: regenerate the WorkDrive refresh token at api-console.zoho.com
   with scope

       WorkDrive.files.ALL

   (or, spelled out: WorkDrive.files.CREATE,WorkDrive.files.READ,WorkDrive.files.UPDATE)

   Second possibility, if the scope turns out to be right: the WorkDrive
   *user* who authorized the token only has Viewer access to that folder.
   A Viewer in a Team Folder can list files but cannot upload. Check the
   folder's sharing settings and make that user an Editor.

   Run `workdrive_probe.py` (shipped alongside this file) to tell those
   two apart in about five seconds — it does token -> list -> tiny upload
   and prints a verdict.

Also changed, while I was in here:

3. The folder listing is now paginated. It previously read only whatever
   Zoho returned in one default page, so in a folder with many files it
   could have said "not found" about a file that was actually there.
4. By default the file kept in WorkDrive is now a 3-column snapshot
   (serial, plan, customer) instead of the whole 73 MB export — that's
   all the diff needs, and it turns a 73 MB upload/download every three
   hours into a ~3 MB one. Pass --full-export to keep the old byte-for-byte
   behaviour.

HOW THE COMPARISON WORKS
-------------------------
Zoho Analytics does not keep "the previous version" of a report around for
you to diff against, so this script keeps that history as a CSV in Zoho
WorkDrive:

  1. Pull the CURRENT table from Zoho Analytics (bulk export API).
  2. Download the snapshot file sitting in the WorkDrive folder — that IS
     last run's report.
  3. For every serial number present in BOTH files, compare
     `activeDevicePlan_name`. Any serial whose plan differs goes on the
     "changed" list. A brand-new serial, or one that disappeared, is logged
     but is NOT treated as a plan change.
  4. If the changed list is non-empty, email it as a table.
  5. Upload this run's snapshot over the old one, so the next run compares
     against this one.

Step 5 always runs (even when nothing changed) unless you pass --dry-run.

SETUP
-----
  pip install requests python-dotenv

  1) Zoho Analytics OAuth credentials (Zoho API Console -> Server-based
     Applications), scope ZohoAnalytics.data.read (or .ALL):
       ZOHO_ORG_ID
       ZOHO_CLIENT_ID_ANALYTICS
       ZOHO_CLIENT_SECRET_ANALYTICS
       ZOHO_CLIENT_REFRESH_TOKEN_ANALYTICS

  2) Zoho WorkDrive OAuth credentials. Same Client ID/Secret as Analytics
     is fine, as long as the refresh token covers BOTH scopes:
       ZOHO_CLIENT_ID_WORKDRIVE
       ZOHO_CLIENT_SECRET_WORKDRIVE
       ZOHO_WORKDRIVE_REFRESH_TOKEN     <- needs WorkDrive.files.ALL
       ZOHO_WORKDRIVE_FOLDER_ID         <- from workdrive.zoho.com/folder/<THIS>
       # WORKDRIVE_REPORT_FILENAME=Geotab_Devices_plan_snapshot.csv  (default)

  3) Email (SMTP):
       SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD
       EMAIL_FROM / EMAIL_TO (comma-separated)

  Put these in a .env next to this script. Note: the credentials currently
  hardcoded as fallbacks below have been shared in a chat, so treat them as
  burned — regenerate the WorkDrive refresh token and the Gmail app
  password, put the new ones in .env only, and delete the fallbacks.

RUN
---
  python Device_plan_geotab.py                   # normal run
  python Device_plan_geotab.py --dry-run         # diff only; no email, no WorkDrive write
  python Device_plan_geotab.py --retry-upload    # push the cached snapshot only, after a
                                                 #   network failure on the upload step
  python Device_plan_geotab.py --full-export     # store the whole export, not a snapshot
  python Device_plan_geotab.py --verbose

SCHEDULING
----------
      0 */3 * * *  cd /path/to/script && /usr/bin/python3 Device_plan_geotab.py >> geotab_alert.log 2>&1
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def env(name: str, default: str = "") -> str:
    """os.getenv, except a variable that is set-but-empty counts as unset.

    This matters specifically on GitHub Actions. A workflow line like

        ZOHO_ORG_ID: ${{ secrets.ZOHO_ORG_ID }}

    still DEFINES the variable when that secret doesn't exist — it just
    defines it as "". Plain env("ZOHO_ORG_ID", "67409019") then returns
    "" rather than the default, so a value visibly present in this file looks
    missing at runtime. Treating blank as absent makes the hardcoded defaults
    behave the way anyone reading them would expect.

    Values are stripped, which also absorbs the trailing newline that sneaks
    into copy-pasted secrets.
    """
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


WORKSPACE_ID = "953790000013364003"
DEVICES_VIEW_ID = "953790000054827102"

ZOHO_ORG_ID = env("ZOHO_ORG_ID", "67409019")
ZOHO_CLIENT_ID = env("ZOHO_CLIENT_ID_ANALYTICS", "")
ZOHO_CLIENT_SECRET = env("ZOHO_CLIENT_SECRET_ANALYTICS", "")
ZOHO_REFRESH_TOKEN = env("ZOHO_CLIENT_REFRESH_TOKEN_ANALYTICS", "")
ZOHO_ACCOUNTS_DOMAIN = env("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.com")
ZOHO_ANALYTICS_DOMAIN = env("ZOHO_ANALYTICS_DOMAIN", "analyticsapi.zoho.com")

# Column this script watches for changes.
PLAN_COL = "activeDevicePlan_name"
PLAN_COL_FUZZY = (["active", "device", "plan"], ["rate"])

SERIAL_COL_CANDIDATES = [
    "serial", "Serial", "device_serialNumber", "Serial Number", "serialNumber",
]
SERIAL_COL_FUZZY = (["serial"], [])

CUSTOMER_COL_CANDIDATES = ["userContact_userCompany_name", "Customer", "Customer Name"]
CUSTOMER_COL_FUZZY = (["usercompany", "name"], ["partner"])

# Header written into the WorkDrive snapshot. These names are deliberately
# ones resolve_column() already recognizes, so a snapshot and a full export
# are both readable by the same code path.
SNAPSHOT_HEADER = ["serial", PLAN_COL, "userContact_userCompany_name"]

# Zoho WorkDrive.
WORKDRIVE_CLIENT_ID = env("ZOHO_CLIENT_ID_WORKDRIVE", "")
WORKDRIVE_CLIENT_SECRET = env("ZOHO_CLIENT_SECRET_WORKDRIVE", "")
WORKDRIVE_REFRESH_TOKEN = env("ZOHO_WORKDRIVE_REFRESH_TOKEN", "")
WORKDRIVE_ACCOUNTS_DOMAIN = env("ZOHO_WORKDRIVE_ACCOUNTS_DOMAIN", ZOHO_ACCOUNTS_DOMAIN)
WORKDRIVE_API_DOMAIN = env("ZOHO_WORKDRIVE_API_DOMAIN", "www.zohoapis.com")
WORKDRIVE_DOWNLOAD_DOMAIN = env("ZOHO_WORKDRIVE_DOWNLOAD_DOMAIN", "download.zoho.com")
WORKDRIVE_UPLOAD_DOMAIN = env("ZOHO_WORKDRIVE_UPLOAD_DOMAIN", "upload.zoho.com")
# The TEAM id (8w1iccca22f6b282d4565b0689768dfb0e44f) used to be the fallback
# here, which was the original bug: a team can't hold files. The fallback is
# now the 'Automation' team folder, which is verified to resolve and list.
# ZOHO_WORKDRIVE_FOLDER_ID in .env still overrides it.
WORKDRIVE_TEAM_ID = env("ZOHO_WORKDRIVE_TEAM_ID", "8w1iccca22f6b282d4565b0689768dfb0e44f")
WORKDRIVE_FOLDER_ID = env("ZOHO_WORKDRIVE_FOLDER_ID", "6cjtx4278b337cf294d9282e414d0f6777802")
WORKDRIVE_FOLDER_ID_SOURCE = (".env / environment"
                              if (os.getenv("ZOHO_WORKDRIVE_FOLDER_ID") or "").strip()
                              else "built-in default in this file")
WORKDRIVE_REPORT_FILENAME = env(
    "WORKDRIVE_REPORT_FILENAME",
    "Geotab_Devices_plan_snapshot.csv.gz" if env("GZIP_SNAPSHOT", "1").lower()
    not in ("0", "false", "no") else "Geotab_Devices_plan_snapshot.csv")

# WorkDrive's single-shot upload endpoint handles up to 250 MB. Above that you
# must use the chunked API — and Zoho told us its exact lower bound when we
# tried to use it for a 3 MB file:
#   "Chunk Upload is not allowed for this file size.
#    Allowed minimum file size is 1073741824 Bytes."
# So chunked upload is for files >= 1 GiB ONLY. It is not a fallback for small
# files, which is what an earlier version of this script wrongly used it as.
CHUNK_UPLOAD_MIN = 1024 ** 3

# Store the snapshot gzipped. This is not premature optimisation — uploads of
# the 3.5 MB plain CSV consistently stall mid-send from this network while
# tiny POSTs answer instantly, so shrinking the request body ~5x (to ~0.7 MB)
# is the most likely thing to get it through. Reading handles both forms by
# sniffing the gzip magic bytes, so an existing plain-CSV snapshot still works.
GZIP_SNAPSHOT = env("GZIP_SNAPSHOT", "1").lower() not in ("0", "false", "no")

# The snapshot is written here just before the upload is attempted, so that a
# network failure on the WorkDrive step doesn't throw away the Analytics pull
# that produced it. `--retry-upload` pushes this file and nothing else.
LOCAL_SNAPSHOT_CACHE = env(
    "LOCAL_SNAPSHOT_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_snapshot_cache.csv"))

# SMTP email.
SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USERNAME = env("SMTP_USERNAME", "")
SMTP_PASSWORD = env("SMTP_PASSWORD", "")
EMAIL_FROM = env("EMAIL_FROM", SMTP_USERNAME)
# Always a LIST, even when it comes from a hardcoded default. Assigning a bare
# string here is a trap: ", ".join("a@b.com") yields "a, @, b, ., c, o, m", so
# the To: header comes out as separated characters.
EMAIL_TO = [addr.strip()
            for addr in env("EMAIL_TO", "nandhinipv@zenduit.com").split(",")
            if addr.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("geotab_plan_change_alert")


def check_config(*required: tuple) -> None:
    """Validate the RESOLVED configuration values, not environment variable
    names. Each item is (name, value).

    This distinction caused a confusing GitHub Actions failure: three settings
    (ZOHO_ORG_ID, SMTP_HOST, EMAIL_TO) were hardcoded as literals at the top of
    this file, but the check called os.getenv() on their names — so it reported
    them as "missing" while their values sat a few lines above. Checking the
    value covers both ways of supplying it: environment/.env, or a literal
    default in the code.
    """
    missing = [name for name, value in required if not value]
    if missing:
        sys.exit("ERROR: missing required configuration: " + ", ".join(missing) + "\n"
                 "\nEach of these can come from EITHER place:\n"
                 "  * an environment variable of that name (locally: .env; on GitHub\n"
                 "    Actions: a repository secret or variable mapped in the workflow's\n"
                 "    env: block)\n"
                 "  * a hardcoded default in the Configuration block at the top of this\n"
                 "    file — fine for non-secrets like the org id, SMTP host or recipient\n"
                 "    list; never for tokens, client secrets or passwords.\n")


SCOPE_HINT = (
    "\n"
    "Read the message text above first — if it names a file size or a minimum, that\n"
    "is the actual complaint and the rest of this note doesn't apply.\n"
    "\n"
    "Things already ruled out for this setup, so you don't re-chase them:\n"
    "  * scope: the token reports WorkDrive.files.ALL WorkDrive.teamfolders.ALL\n"
    f"  * destination: {WORKDRIVE_FOLDER_ID} resolves to a real workspace and lists\n"
    "    its contents fine — re-check any time with --verify-folder\n"
    "  * the chunked-session upload API: Zoho refuses it below 1 GiB, so it is not an\n"
    "    alternative for a file this size\n"
    "\n"
    "If none of the above fits, run --probe-upload-size to see how large a request\n"
    "body this network will carry, or --local-history to keep the snapshot on this\n"
    "machine and skip WorkDrive entirely.\n"
)


# ==========================================================================
# Zoho Analytics — bulk export
# ==========================================================================
def zoho_access_token() -> str:
    if not (ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN):
        sys.exit("ERROR: set ZOHO_CLIENT_ID_ANALYTICS / ZOHO_CLIENT_SECRET_ANALYTICS / "
                 "ZOHO_CLIENT_REFRESH_TOKEN_ANALYTICS. See the module docstring.")
    resp = requests.post(f"https://{ZOHO_ACCOUNTS_DOMAIN}/oauth/v2/token", data={
        "grant_type": "refresh_token",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "refresh_token": ZOHO_REFRESH_TOKEN,
    }, timeout=60)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        sys.exit(f"ERROR: no Zoho access_token: {resp.text[:300]}")
    return token


def _zoho_headers(token: str) -> dict:
    return {"Authorization": f"Zoho-oauthtoken {token}", "ZANALYTICS-ORGID": ZOHO_ORG_ID}


_CONNECT_TIMEOUT = 10


def _zoho_get(url: str, params: dict, headers: dict, read_timeout: int, what: str):
    log.info("  -> %s ...", what)
    try:
        return requests.get(url, params=params, headers=headers, timeout=(_CONNECT_TIMEOUT, read_timeout))
    except requests.exceptions.ConnectTimeout:
        sys.exit(f"ERROR: {what} — could not even establish a connection within {_CONNECT_TIMEOUT}s. "
                 f"This points to a network/proxy/firewall issue reaching {ZOHO_ANALYTICS_DOMAIN}, "
                 f"not a Zoho server-side problem.")
    except requests.exceptions.ReadTimeout:
        sys.exit(f"ERROR: {what} — connected, but got no response within {read_timeout}s. Could be "
                 f"Zoho running slow, or a proxy silently holding the connection open. Try again; "
                 f"if it keeps happening, try from a different network to rule out a proxy.")
    except requests.exceptions.ConnectionError as exc:
        sys.exit(f"ERROR: {what} — connection failed: {exc}")


def _zoho_create_job(token: str, view_id: str) -> str:
    url = f"https://{ZOHO_ANALYTICS_DOMAIN}/restapi/v2/bulk/workspaces/{WORKSPACE_ID}/views/{view_id}/data"
    params = {"CONFIG": json.dumps({"responseFormat": "csv"}, separators=(",", ":"))}
    resp = _zoho_get(url, params, _zoho_headers(token), 60, f"create export job (view {view_id})")
    if resp.status_code >= 400:
        sys.exit(f"ERROR: Zoho create export job (view {view_id}) {resp.status_code}: {resp.text[:500]}")
    job_id = (resp.json().get("data") or {}).get("jobId")
    if not job_id:
        sys.exit(f"ERROR: no jobId for view {view_id}: {resp.text[:300]}")
    return job_id


def _zoho_wait(token: str, job_id: str, timeout_seconds: int = 300) -> str:
    url = f"https://{ZOHO_ANALYTICS_DOMAIN}/restapi/v2/bulk/workspaces/{WORKSPACE_ID}/exportjobs/{job_id}"
    start = time.time()
    deadline = start + timeout_seconds
    last_log = start
    while time.time() < deadline:
        resp = _zoho_get(url, {"responseFormat": "json"}, _zoho_headers(token), 60,
                         f"poll export job {job_id}")
        resp.raise_for_status()
        info = resp.json().get("data") or {}
        if info.get("jobStatus") == "JOB COMPLETED" or str(info.get("jobCode")) == "1004":
            return info["downloadUrl"]
        if str(info.get("jobCode")) in ("1003", "1005"):
            sys.exit(f"ERROR: Zoho export job {job_id} failed: {info}")
        now = time.time()
        if now - last_log >= 30:
            log.info("  ...still waiting on export job %s (%.0fs elapsed, timeout at %ds)",
                     job_id, now - start, timeout_seconds)
            last_log = now
        time.sleep(2)
    sys.exit(f"ERROR: Zoho export job {job_id} timed out after {timeout_seconds}s.")


def _zoho_download(token: str, url: str) -> str:
    log.info("  -> downloading export content (this took ~10 min last time; 73 MB) ...")
    try:
        resp = requests.get(url, headers={**_zoho_headers(token), "Accept-Encoding": "identity"},
                            timeout=(_CONNECT_TIMEOUT, 180))
    except requests.exceptions.Timeout:
        sys.exit("ERROR: download of export content timed out. Zoho running slow, or a proxy "
                 "silently holding the connection open — try again.")
    except requests.exceptions.ConnectionError as exc:
        sys.exit(f"ERROR: download of export content failed: {exc}")
    resp.raise_for_status()
    text = resp.text
    return text[1:] if text and text[0] == "﻿" else text


def zoho_export_view_raw(token: str, view_id: str, label: str, timeout_seconds: int = 300) -> str:
    log.info("Zoho Analytics: exporting %s ...", label)
    text = _zoho_download(token, _zoho_wait(token, _zoho_create_job(token, view_id), timeout_seconds))
    log.info("  got %d byte(s)", len(text))
    return text


def parse_csv_rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def resolve_column(rows: list[dict], exact: list[str], fuzzy_all: list[str],
                   fuzzy_none: list[str], label: str, required: bool = True) -> str:
    keys = list(rows[0].keys()) if rows else []
    for c in exact:
        if c in keys:
            return c
    for k in keys:
        kl = (k or "").lower()
        if all(t in kl for t in fuzzy_all) and not any(t in kl for t in fuzzy_none):
            log.info("  %s -> fuzzy column '%s'", label, k)
            return k
    if required:
        sys.exit(f"ERROR: could not resolve the '{label}' column. Available columns: {keys}\n"
                 f"Add the exact name to the relevant *_COL_CANDIDATES list near the top of this "
                 f"script and rerun.")
    log.info("  %s -> not present (optional). Available: %s", label, keys)
    return ""


def build_serial_plan_map(rows: list[dict], context_label: str) -> dict[str, dict]:
    """serial -> {'plan': ..., 'customer': ...}. Column names are resolved
    independently per file, so a snapshot and a full export both work."""
    if not rows:
        return {}
    serial_col = resolve_column(rows, SERIAL_COL_CANDIDATES, *SERIAL_COL_FUZZY,
                                f"serial number ({context_label})")
    plan_col = resolve_column(rows, [PLAN_COL], *PLAN_COL_FUZZY,
                              f"active device plan ({context_label})")
    customer_col = resolve_column(rows, CUSTOMER_COL_CANDIDATES, *CUSTOMER_COL_FUZZY,
                                  f"customer name ({context_label})", required=False)

    mapping: dict[str, dict] = {}
    blank_serials = 0
    for r in rows:
        serial = str(r.get(serial_col) or "").strip()
        if not serial:
            blank_serials += 1
            continue
        mapping[serial] = {
            "plan": str(r.get(plan_col) or "").strip(),
            "customer": str(r.get(customer_col) or "").strip() if customer_col else "",
        }
    if blank_serials:
        log.warning("(%s) skipped %d row(s) with a blank serial number.", context_label, blank_serials)
    log.info("(%s) %d device(s) with a usable serial number.", context_label, len(mapping))
    return mapping


def encode_payload(text: str) -> bytes:
    """CSV text -> the bytes we actually store. gzip when enabled, and always
    gzip when the target filename says .gz so the two can't disagree."""
    if GZIP_SNAPSHOT or WORKDRIVE_REPORT_FILENAME.lower().endswith(".gz"):
        return gzip.compress(text.encode("utf-8"), 9)
    return text.encode("utf-8")


def decode_payload(raw: bytes) -> str:
    """Bytes we read back -> CSV text. Sniffs the gzip magic number rather
    than trusting the file extension, so a snapshot written by any version of
    this script (plain or gzipped) is readable."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    return text[1:] if text and text[0] == "﻿" else text


def build_snapshot_csv(mapping: dict[str, dict]) -> str:
    """The small file that gets stored in WorkDrive: three columns, one row
    per serial. ~3 MB for 80k devices instead of 73 MB."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SNAPSHOT_HEADER)
    for serial in sorted(mapping):
        row = mapping[serial]
        writer.writerow([serial, row.get("plan", ""), row.get("customer", "")])
    return buf.getvalue()


# ==========================================================================
# Zoho WorkDrive
# ==========================================================================
def workdrive_access_token() -> str:
    if not (WORKDRIVE_CLIENT_ID and WORKDRIVE_CLIENT_SECRET and WORKDRIVE_REFRESH_TOKEN):
        sys.exit("ERROR: set ZOHO_CLIENT_ID_WORKDRIVE / ZOHO_CLIENT_SECRET_WORKDRIVE / "
                 "ZOHO_WORKDRIVE_REFRESH_TOKEN. See the module docstring's SETUP section.")
    if not WORKDRIVE_FOLDER_ID:
        sys.exit("ERROR: set ZOHO_WORKDRIVE_FOLDER_ID to the WorkDrive folder that holds "
                 f"{WORKDRIVE_REPORT_FILENAME}.")
    resp = requests.post(f"https://{WORKDRIVE_ACCOUNTS_DOMAIN}/oauth/v2/token", data={
        "grant_type": "refresh_token",
        "client_id": WORKDRIVE_CLIENT_ID,
        "client_secret": WORKDRIVE_CLIENT_SECRET,
        "refresh_token": WORKDRIVE_REFRESH_TOKEN,
    }, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        sys.exit(f"ERROR: no WorkDrive access_token: {resp.text[:300]}")
    # When Zoho echoes the granted scope, log it — it makes a later 401
    # instantly diagnosable instead of guesswork.
    if payload.get("scope"):
        log.info("WorkDrive token scope: %s", payload["scope"])
        if "WorkDrive.files.CREATE" not in payload["scope"] and ".ALL" not in payload["scope"]:
            log.warning("WorkDrive token does NOT appear to include a create scope — the upload "
                        "step at the end of this run will most likely fail with 401 F900. "
                        "Regenerate the refresh token with scope WorkDrive.files.ALL.")
    return token


def _wd_headers(token: str) -> dict:
    return {"Authorization": f"Zoho-oauthtoken {token}"}


def list_team_folders(token: str, team_id: str) -> list[tuple[str, str]]:
    """[(id, name)] of the team folders in a team. Returns [] rather than
    raising — it's used inside error paths where a failure here shouldn't
    replace the more useful message we're already about to print."""
    try:
        resp = requests.get(f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/teams/{team_id}/teamfolders",
                            headers=_wd_headers(token), params={"page[limit]": 100}, timeout=60)
        if resp.status_code >= 400:
            log.debug("team folder listing failed %s: %s", resp.status_code, resp.text[:300])
            return []
        data = resp.json().get("data") or []
        if isinstance(data, dict):  # a single team folder comes back unwrapped
            data = [data]
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") or {}
            out.append((item.get("id"), attrs.get("name") or "(unnamed)"))
        return out
    except (requests.exceptions.RequestException, ValueError) as exc:
        log.debug("team folder listing errored: %s", exc)
        return []


def create_workdrive_folder(token: str, parent_id: str, name: str) -> str:
    resp = requests.post(
        f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/files",
        headers={**_wd_headers(token), "Content-Type": "application/vnd.api+json",
                 "Accept": "application/vnd.api+json"},
        data=json.dumps({"data": {"attributes": {"name": name, "parent_id": parent_id},
                                  "type": "files"}}),
        timeout=60)
    if resp.status_code >= 400:
        sys.exit(f"ERROR: could not create folder {name!r} under {parent_id}: "
                 f"HTTP {resp.status_code} {resp.text[:600]}")
    data = resp.json().get("data") or {}
    new_id = data.get("id") or (data.get("attributes") or {}).get("resource_id")
    if not new_id:
        sys.exit(f"ERROR: folder created but no id came back: {resp.text[:600]}")
    return str(new_id)


def update_env_file(folder_id: str) -> bool:
    """Rewrite (or add) ZOHO_WORKDRIVE_FOLDER_ID in the .env next to this
    script. Returns True if the file was written."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    key = "ZOHO_WORKDRIVE_FOLDER_ID"
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={folder_id}"
                replaced = True
        if not replaced:
            lines.append(f"{key}={folder_id}")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"  {'updated' if replaced else 'added'} {key} in {env_path}")
        return True
    except OSError as exc:
        print(f"  could not write {env_path}: {exc}")
        return False


def setup_folder_wizard() -> None:
    """Interactive: walk from the team down to a usable folder, seed it, and
    save the ID. Exists so this can be done by running THIS script, without
    switching files or hand-editing .env."""
    token = workdrive_access_token()
    print()
    print("This will find (or create) a WorkDrive folder for the plan snapshot and save")
    print("its ID to .env. Ctrl-C to back out at any point.")
    print()

    team_id = WORKDRIVE_TEAM_ID
    folders = list_team_folders(token, team_id)
    if not folders:
        sys.exit(f"Could not list team folders for team {team_id}.\n"
                 "Open WorkDrive in a browser, go into the folder you want to use, copy the\n"
                 "ID from the end of the URL (.../folders/<THIS PART>), and set it in .env as\n"
                 "ZOHO_WORKDRIVE_FOLDER_ID. Then rerun with --verify-folder to check it.")

    print(f"Team folders in {team_id}:\n")
    for i, (fid, name) in enumerate(folders, start=1):
        print(f"  [{i:>2}]  {name}   ({fid})")
    print()
    while True:
        raw = input(f"Pick a team folder [1-{len(folders)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(folders):
            parent_id, parent_name = folders[int(raw) - 1]
            break
        print("  not a valid number, try again")

    print()
    sub = input("Name for a new subfolder to hold the snapshot "
                "[Geotab Alerts, or blank to use the team folder root]: ").strip()
    if sub == "":
        target_id, target_desc = parent_id, f"{parent_name} (team folder root)"
    else:
        target_id = create_workdrive_folder(token, parent_id, sub)
        target_desc = f"{parent_name} / {sub}"
        print(f"  created: {target_desc}  ({target_id})")

    # Prove it works by uploading the real thing rather than a throwaway probe:
    # the cached snapshot if we have one (which also seeds history for the next
    # run), otherwise a header-only snapshot that the next run harmlessly
    # replaces. Either way no junk file is left behind to clean up.
    if os.path.exists(LOCAL_SNAPSHOT_CACHE):
        with open(LOCAL_SNAPSHOT_CACHE, "r", encoding="utf-8", newline="") as fh:
            payload = fh.read()
        print(f"\nUploading the cached snapshot ({len(payload) / 1024 / 1024:.1f} MB) as a real test ...")
    else:
        payload = ",".join(SNAPSHOT_HEADER) + "\n"
        print("\nUploading a header-only snapshot as a test (no cache to seed with) ...")

    new_id = upload_workdrive_file(token, target_id, WORKDRIVE_REPORT_FILENAME,
                                   encode_payload(payload))
    print(f"  upload OK — '{WORKDRIVE_REPORT_FILENAME}' is now in {target_desc} "
          f"(file id {new_id or 'not returned'})")

    print()
    print(f"  ZOHO_WORKDRIVE_FOLDER_ID={target_id}")
    print()
    if input("Save that to .env for you? [Y/n]: ").strip().lower() in ("", "y", "yes"):
        if update_env_file(target_id):
            print("\nDone. From now on just run the script normally:")
            print("    python Device_plan_geotab.py")
            return
    print("\nNot saved — put the line above into your .env by hand, then run:")
    print("    python Device_plan_geotab.py")


def verify_workdrive_target(token: str, resource_id: str) -> str:
    """Check up front that ZOHO_WORKDRIVE_FOLDER_ID is a FOLDER we can write
    into, and fail immediately with a useful message if it isn't. Worth the
    one extra API call: without this check, a wrong ID here fails only at the
    very end of the run, after a 73 MB export, with an opaque 401.

    Returns 'files' or 'teamfolders' — which listing endpoint to use."""
    resp = requests.get(f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/files/{resource_id}",
                        headers=_wd_headers(token), timeout=60)
    if resp.status_code >= 400:
        sys.exit(f"ERROR: cannot read WorkDrive resource {resource_id}: "
                 f"HTTP {resp.status_code} {resp.text[:400]}\n"
                 "Check ZOHO_WORKDRIVE_FOLDER_ID. Run workdrive_find_folder.py to list "
                 "usable folder IDs.")
    attrs = ((resp.json().get("data") or {}).get("attributes") or {})
    kind = str(attrs.get("type") or "").lower()
    log.info("WorkDrive target: %r (type=%s) — id %s from %s",
             attrs.get("name"), kind or "?", resource_id, WORKDRIVE_FOLDER_ID_SOURCE)

    # Only "this is the whole team" is a hard error. Note what is NOT tested
    # here: parent_id == "-1". A team folder sits directly under the team and
    # may well report that too, and a team folder IS a valid upload parent —
    # rejecting on parent_id would lock out the very IDs we're telling people
    # to use.
    if kind == "team" or attrs.get("resource_type") == 102:
        # Don't just say "go find a folder" — look them up and print them, so
        # this run gives the answer instead of another instruction.
        listing = ""
        folders = list_team_folders(token, resource_id)
        if folders:
            rows = "\n".join(f"    {fid}   {name}" for fid, name in folders[:40])
            listing = ("\nTeam folders available under it (any of these can be "
                       "ZOHO_WORKDRIVE_FOLDER_ID,\nor a subfolder inside one):\n\n" + rows + "\n")

        sys.exit(
            f"ERROR: {resource_id} is not a folder — it is your whole WorkDrive TEAM "
            f"({attrs.get('name')!r}, {attrs.get('storage_info', {}).get('files_count', '?')} files).\n"
            "\n"
            "A team is the org-level container that team folders live under, so it can't\n"
            "hold files directly. That is why uploads into it return 401 F900 and why\n"
            "creating a folder in it returns R602 'Invalid parent Id' — the token and its\n"
            "scopes are fine, the parent just isn't a real parent.\n"
            f"{listing}"
            "\n"
            "Easiest fix — rerun THIS script with one flag and answer two questions:\n"
            "\n"
            "    python Device_plan_geotab.py --setup-folder\n"
            "\n"
            "It lists those folders, optionally creates a subfolder, proves an upload\n"
            "works, and writes ZOHO_WORKDRIVE_FOLDER_ID into your .env. After that,\n"
            "normal runs just work.\n")

    # 'workspace' is what a team folder reports as. It accepts uploads fine
    # even though is_folder is False, so it belongs on this list — warning
    # about it was noise.
    if not (attrs.get("is_folder") or kind in ("folder", "teamfolder", "team_folder",
                                               "workspace", "library")):
        log.warning("WorkDrive target reports type=%r / is_folder=%s, which doesn't look like "
                    "something that can hold files. Continuing anyway — but if the upload is "
                    "refused, this is the first thing to re-check.", kind, attrs.get("is_folder"))

    return "teamfolders" if kind in ("teamfolder", "team_folder") else "files"


def find_workdrive_file(token: str, folder_id: str, filename: str,
                        endpoint: str = "files") -> str | None:
    """Lists the folder's contents, page by page (the API returns a bounded
    page, so an unpaginated single call could miss the file entirely in a
    busy folder), and matches by name. Returns the resource id or None."""
    # An ordinary folder is listed via /files/{id}/files and a team folder via
    # /teamfolders/{id}/files. We guess from the resource's type, but the guess
    # is cheap to get wrong, so fall back to the other one instead of dying.
    alternate = "teamfolders" if endpoint == "files" else "files"
    url = f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/{endpoint}/{folder_id}/files"
    limit = 50
    offset = 0
    seen = 0
    while True:
        resp = requests.get(url, headers=_wd_headers(token),
                            params={"page[limit]": limit, "page[offset]": offset}, timeout=60)
        if resp.status_code >= 400:
            if alternate:
                log.info("Listing via /%s returned HTTP %d — retrying via /%s ...",
                         endpoint, resp.status_code, alternate)
                endpoint, alternate = alternate, ""
                url = f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/{endpoint}/{folder_id}/files"
                offset = 0
                seen = 0
                continue
            sys.exit(f"ERROR: WorkDrive list-folder-contents for folder {folder_id} failed "
                     f"{resp.status_code}: {resp.text[:800]}\n"
                     f"(Check ZOHO_WORKDRIVE_FOLDER_ID, and that the refresh token has "
                     f"WorkDrive.files.READ.)")
        items = resp.json().get("data") or []
        for item in items:
            name = (item.get("attributes") or {}).get("name")
            if name == filename:
                log.info("Found '%s' in WorkDrive (resource id %s).", filename, item.get("id"))
                return item.get("id")
        seen += len(items)
        if len(items) < limit:
            log.info("Listed %d item(s) in the WorkDrive folder; no '%s' among them.", seen, filename)
            return None
        offset += limit


def download_workdrive_file_text(token: str, resource_id: str) -> str:
    url = f"https://{WORKDRIVE_DOWNLOAD_DOMAIN}/v1/workdrive/download/{resource_id}"
    resp = requests.get(url, headers=_wd_headers(token), timeout=300)
    if resp.status_code >= 400:
        extra = ""
        if "OAUTHSCOPE" in (resp.text or "").upper() or resp.status_code in (401, 403):
            extra = (
                "\n"
                "This is the one endpoint your token can't reach, and it's a documented quirk\n"
                "rather than a mistake in your setup. Zoho's own API spec lists exactly two\n"
                "endpoints as needing the LEGACY ZohoFiles scopes on top of the WorkDrive ones,\n"
                "and they're the two that live on their own hosts:\n"
                "\n"
                "    GET  download.zoho.com/v1/workdrive/download/{id}\n"
                "         -> WorkDrive.files.READ  AND  ZohoFiles.files.READ\n"
                "    POST upload.zoho.com/workdrive-api/v1/stream/upload\n"
                "         -> WorkDrive.files.CREATE AND ZohoFiles.files.CREATE\n"
                "\n"
                "Everything on www.zohoapis.com (listing, verifying, and the upload this script\n"
                "actually uses) is happy with WorkDrive.* alone — which is why every other step\n"
                "works. To enable this one, regenerate the WorkDrive refresh token with:\n"
                "\n"
                "    WorkDrive.files.ALL,WorkDrive.teamfolders.ALL,ZohoFiles.files.ALL\n"
                "\n"
                "You only need it to read history on a machine that has no local copy — e.g. if\n"
                "you move this job to another box. On THIS machine the script now reads history\n"
                f"from {LOCAL_SNAPSHOT_CACHE} and never calls download at all.\n")
        # Deliberately a warning, not a fatal error. Not being able to read
        # last run's file means we can't alert on changes THIS cycle, but the
        # run can still write history so the next cycle works. Dying here
        # would leave the folder empty forever on a machine that can't
        # download — self-healing beats correct-and-stuck.
        log.warning("WorkDrive download of file %s failed %d: %s%s",
                    resource_id, resp.status_code, resp.text[:400], extra)
        return ""
    # .content, not .text: the stored snapshot is usually gzipped now, and
    # decode_payload works out which it is from the bytes themselves.
    return decode_payload(resp.content)


def _extract_resource_id(payload, fallback: str = "") -> str:
    """WorkDrive's upload response nests the new file's id a couple of layers
    down and the exact shape varies between endpoints, so check the plausible
    spots rather than assume one."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            attrs = data.get("attributes") or {}
            for key in ("resource_id", "ResourceId", "id"):
                if attrs.get(key):
                    return str(attrs[key])
            if data.get("id"):
                return str(data["id"])
        if payload.get("resource_id"):
            return str(payload["resource_id"])
    return fallback


def _upload_failed(resp, step: str):
    hint = SCOPE_HINT if resp.status_code in (401, 403) else ""
    sys.exit(f"ERROR: WorkDrive {step} failed {resp.status_code}: {resp.text[:800]}{hint}")


class UploadNetworkError(Exception):
    """The upload never got a reply — connection dropped, reset, or timed out
    mid-send. Distinct from an HTTP error response, which means Zoho DID
    answer and is telling us something."""


def simple_upload_workdrive_file(token: str, folder_id: str, filename: str, data: bytes) -> str:
    """WorkDrive's ordinary upload endpoint — one call, good up to 250 MB.
    override-name-exist=true makes this idempotent by name: first run creates
    the file, every later run saves a new top version of the same file.

    Retries on network failure. A 'write operation timed out' here means the
    far end stopped reading our body partway through — a flaky link, a proxy,
    or Zoho hiccuping — and is almost always fine on the next attempt, so
    losing a whole 73 MB Analytics pull over it would be silly."""
    url = f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/upload"
    backoffs = [5, 15, 45, 0]  # 4 attempts; last entry unused
    last_error = ""
    for attempt, wait in enumerate(backoffs, start=1):
        log.info("WorkDrive: uploading '%s' (%.1f MB) to folder %s (attempt %d/%d) ...",
                 filename, len(data) / 1024 / 1024, folder_id, attempt, len(backoffs))
        try:
            resp = requests.post(
                url,
                headers=_wd_headers(token),
                # Zoho asks for the filename URL-encoded in UTF-8.
                data={"parent_id": folder_id,
                      "filename": quote(filename, safe=""),
                      "override-name-exist": "true"},
                files={"content": (filename, data, "text/csv")},
                # Shorter than before on purpose: a stalled send is better
                # detected and retried than waited out for 10 minutes.
                timeout=(_CONNECT_TIMEOUT, 180),
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("  upload attempt %d failed at the network level: %s", attempt, last_error)
            if attempt < len(backoffs):
                log.info("  retrying in %ds ...", wait)
                time.sleep(wait)
            continue

        if resp.status_code >= 400:
            # Zoho answered. Retrying a real HTTP error is pointless.
            _upload_failed(resp, "upload")
        log.info("  upload accepted (HTTP %d).", resp.status_code)
        try:
            return _extract_resource_id(resp.json())
        except ValueError:
            return ""

    raise UploadNetworkError(last_error)


# ---- chunked upload: only used for files above SIMPLE_UPLOAD_LIMIT -------
def create_workdrive_upload_session(token: str, size: int, filename: str, folder_id: str) -> str:
    url = f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/uploadsession/create"
    params = {"size": str(size), "file_name": filename, "parent_id": folder_id,
              "name_conflict": "update"}
    resp = requests.post(url, params=params, headers=_wd_headers(token), timeout=60)
    if resp.status_code >= 400:
        _upload_failed(resp, "create-upload-session")
    payload = resp.json()
    for candidate in (payload, payload.get("data")):
        if isinstance(candidate, list) and candidate:
            candidate = candidate[0]
        if isinstance(candidate, dict):
            for key in ("upload_id", "uploadId"):
                if candidate.get(key):
                    return candidate[key]
            attrs = candidate.get("attributes") or {}
            for key in ("upload_id", "uploadId"):
                if attrs.get(key):
                    return attrs[key]
    sys.exit("ERROR: WorkDrive create-upload-session succeeded but no upload_id was found in the "
             f"response: {resp.text[:800]}")


def upload_workdrive_chunks(token: str, upload_id: str, data: bytes) -> None:
    chunk_size = 64 * 1024 * 1024
    total = len(data)
    offset = 0
    while offset < total:
        end = min(offset + chunk_size, total) - 1
        chunk = data[offset:end + 1]
        url = f"https://{WORKDRIVE_UPLOAD_DOMAIN}/workdrive-api/v1/stream/upload"
        headers = {
            **_wd_headers(token),
            "upload-id": upload_id,
            "Content-Range": f"bytes {offset}-{end}/{total}",
            "x-streammode": "1",
            "Content-Length": str(len(chunk)),
        }
        resp = requests.post(url, headers=headers, data=chunk, timeout=(_CONNECT_TIMEOUT, 600))
        if resp.status_code >= 400:
            _upload_failed(resp, f"chunk upload (bytes {offset}-{end}/{total})")
        log.info("  WorkDrive upload: %d / %d bytes sent", end + 1, total)
        offset = end + 1


def commit_workdrive_upload_session(token: str, upload_id: str, filename: str, folder_id: str) -> str:
    url = f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/uploadsession/commit"
    params = {"upload_id": upload_id, "parent_id": folder_id, "file_name": filename,
              "name_conflict": "update"}
    resp = requests.post(url, params=params, headers=_wd_headers(token), timeout=120)
    if resp.status_code >= 400:
        _upload_failed(resp, "commit-upload-session")
    try:
        return _extract_resource_id(resp.json())
    except ValueError:
        return ""


def chunked_upload_workdrive_file(token: str, folder_id: str, filename: str, data: bytes) -> str:
    upload_id = create_workdrive_upload_session(token, len(data), filename, folder_id)
    upload_workdrive_chunks(token, upload_id, data)
    return commit_workdrive_upload_session(token, upload_id, filename, folder_id)


def _multipart_body(fields: dict, filename: str, content: bytes) -> tuple[bytes, str]:
    """Hand-build the multipart body so it can be streamed with chunked
    transfer encoding (requests only does that when handed an iterator, and
    an iterator can't be multipart-encoded by requests for us)."""
    boundary = "----geotabPlanSnapshotBoundary7d91"
    out = bytearray()
    for key, value in fields.items():
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n").encode("utf-8")
    out += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="content"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    out += content
    out += f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(out), boundary


def streamed_upload_workdrive_file(token: str, folder_id: str, filename: str, data: bytes) -> str:
    """Same endpoint, but the body goes out in 32 KB chunks with
    Transfer-Encoding: chunked instead of one Content-Length blob.

    Why bother: a middlebox that stalls on a multi-megabyte POST sometimes
    passes the identical bytes when they arrive as a chunked stream, because
    it stops trying to buffer the whole body before forwarding it. Cheap to
    try, and it's the difference between working and not if that's the wall
    we're hitting."""
    body, boundary = _multipart_body(
        {"parent_id": folder_id, "filename": quote(filename, safe=""),
         "override-name-exist": "true"}, filename, data)

    def gen():
        for i in range(0, len(body), 32768):
            yield body[i:i + 32768]

    log.info("WorkDrive: retrying '%s' as a chunked stream (%.2f MB in 32 KB pieces) ...",
             filename, len(data) / 1024 / 1024)
    resp = requests.post(
        f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/upload",
        headers={**_wd_headers(token),
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=gen(), timeout=(_CONNECT_TIMEOUT, 180))
    if resp.status_code >= 400:
        _upload_failed(resp, "chunked-stream upload")
    log.info("  chunked-stream upload accepted (HTTP %d).", resp.status_code)
    try:
        return _extract_resource_id(resp.json())
    except ValueError:
        return ""


def upload_workdrive_file(token: str, folder_id: str, filename: str, data: bytes) -> str:
    """Takes BYTES (already gzipped, if gzipping is on) rather than text."""
    if len(data) >= CHUNK_UPLOAD_MIN:
        log.info("WorkDrive: %.0f MB is at or above the 1 GiB chunked-upload minimum — "
                 "using the chunked session API.", len(data) / 1024 / 1024)
        return chunked_upload_workdrive_file(token, folder_id, filename, data)

    try:
        return simple_upload_workdrive_file(token, folder_id, filename, data)
    except UploadNetworkError as exc:
        # NOT falling back to the chunked session API here. Zoho rejects it
        # outright below 1 GiB ("Allowed minimum file size is 1073741824
        # Bytes"), so for a file this size that fallback could only ever add a
        # confusing second error. Streaming the same request with chunked
        # transfer encoding is the fallback that might actually help.
        log.warning("Single-shot upload failed at the network level every time (%s).", exc)
        try:
            return streamed_upload_workdrive_file(token, folder_id, filename, data)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc2:
            sys.exit(
                f"ERROR: could not upload the {len(data) / 1024 / 1024:.2f} MB snapshot to "
                "WorkDrive. Zoho never replied — the connection died mid-send both as a normal\n"
                "POST and as a chunked stream.\n"
                f"  normal POST:     {exc}\n"
                f"  chunked stream:  {type(exc2).__name__}: {exc2}\n"
                "\n"
                "This is not credentials, scopes, or the folder — all of those were verified\n"
                "earlier in this run. Small POSTs to the same endpoint get answered instantly\n"
                "while multi-megabyte ones stall, which is the signature of something between\n"
                "this machine and Zoho refusing to carry a large request body.\n"
                "\n"
                "Next steps, cheapest first:\n"
                "  1. python Device_plan_geotab.py --probe-upload-size\n"
                "     Uploads 10 KB, 100 KB, 500 KB, 1 MB, 2 MB and 4 MB and reports where it\n"
                "     starts failing. That tells us the actual ceiling in one run.\n"
                "  2. python Device_plan_geotab.py --local-history\n"
                "     Keeps the history file on this machine instead of WorkDrive and skips the\n"
                "     upload entirely. The alerting works fully; you just lose the ability to\n"
                "     run it from a different machine. This is the pragmatic option if the\n"
                "     network can't be changed.\n"
                "  3. Try one run from a phone hotspot. If it succeeds there, it's your\n"
                "     office network/proxy, and the fix is to allow large outbound POSTs to\n"
                f"     {WORKDRIVE_API_DOMAIN} (or set HTTPS_PROXY so requests negotiates with\n"
                "     the proxy properly instead of being silently intercepted).\n"
                f"\nThe snapshot is safe at {LOCAL_SNAPSHOT_CACHE} — nothing was lost. Once the\n"
                "path works, 'python Device_plan_geotab.py --retry-upload' pushes it without\n"
                "another Analytics export.\n")


def probe_upload_size(token: str, folder_id: str) -> None:
    """Upload increasing payloads to find the size at which this network stops
    completing a POST. All probes reuse one filename, so at most one throwaway
    file is left in the folder."""
    name = "_upload_size_probe.csv"
    print("\nProbing how large a request body this network will carry to WorkDrive.")
    print("(all probes overwrite one file, so at most one stray file is left behind)\n")
    print(f"  {'payload':>10}   {'result':<24} {'seconds':>8}")
    print(f"  {'-' * 10}   {'-' * 24} {'-' * 8}")
    biggest_ok = 0
    for size in (10 * 1024, 100 * 1024, 500 * 1024, 1024 * 1024, 2 * 1024 * 1024, 4 * 1024 * 1024):
        # Compressible filler, so gzipped payloads of this size behave similarly.
        body = ("serial,plan\n" + "G00000000,Base\n" * (size // 15))[:size].encode("utf-8")
        started = time.time()
        try:
            resp = requests.post(
                f"https://{WORKDRIVE_API_DOMAIN}/workdrive/api/v1/upload",
                headers=_wd_headers(token),
                data={"parent_id": folder_id, "filename": quote(name, safe=""),
                      "override-name-exist": "true"},
                files={"content": (name, body, "text/csv")},
                timeout=(_CONNECT_TIMEOUT, 120))
            outcome = f"HTTP {resp.status_code}"
            if resp.status_code < 400:
                outcome += " ok"
                biggest_ok = max(biggest_ok, size)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            outcome = type(exc).__name__
        print(f"  {size / 1024:9.0f}K   {outcome:<24} {time.time() - started:8.1f}")

    print()
    if biggest_ok == 0:
        print("Nothing got through, not even 10 KB — so this isn't about size after all.")
        print("Re-run --verify-folder and check whether the folder ID still resolves.")
    elif biggest_ok >= 4 * 1024 * 1024:
        print("Everything got through, including 4 MB. The earlier failures were then")
        print("intermittent rather than a hard ceiling — just rerun with --retry-upload.")
    else:
        print(f"Largest body that completed: {biggest_ok / 1024:.0f} KB.")
        gz_note = "gzipped (~0.7 MB)" if GZIP_SNAPSHOT else "un-gzipped (~3.5 MB)"
        print(f"Your snapshot is currently {gz_note}.")
        if GZIP_SNAPSHOT and biggest_ok >= 1024 * 1024:
            print("That should fit under the ceiling — run --retry-upload and it ought to land.")
        else:
            print("That won't fit. Either raise the limit on the network side, or run with")
            print("--local-history to keep the history file on this machine instead.")


# ==========================================================================
# Diff
# ==========================================================================
def diff_plans(previous: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    changes = []
    for serial, cur in current.items():
        prev = previous.get(serial)
        if prev is None:
            continue
        old_plan = (prev.get("plan") or "").strip()
        new_plan = (cur.get("plan") or "").strip()
        if old_plan != new_plan:
            changes.append({
                "serial": serial,
                "customer": cur.get("customer") or prev.get("customer") or "",
                "old_plan": old_plan or "(blank)",
                "new_plan": new_plan or "(blank)",
            })
    changes.sort(key=lambda c: (c["customer"], c["serial"]))
    return changes


# ==========================================================================
# Email
# ==========================================================================
def send_email(changes: list[dict]) -> None:
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and EMAIL_FROM and EMAIL_TO):
        sys.exit("ERROR: set SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD / EMAIL_FROM / EMAIL_TO "
                 "to send the change alert. See the module docstring's SETUP section.")

    subject = f"Geotab active device plan change alert — {len(changes)} device(s) changed"

    text_lines = [f"{len(changes)} device(s) changed active plan:\n"]
    rows_html = []
    for c in changes:
        text_lines.append(f"  - {c['serial']} ({c['customer']}): {c['old_plan']} -> {c['new_plan']}")
        rows_html.append(
            f"<tr><td style='padding:4px 10px;border:1px solid #ccc'>{c['serial']}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ccc'>{c['customer']}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ccc'>{c['old_plan']}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ccc'>{c['new_plan']}</td></tr>"
        )
    text_body = "\n".join(text_lines)
    html_body = f"""
    <html><body>
      <p>{len(changes)} device(s) changed active plan since the last run:</p>
      <table style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
        <tr style="background:#eee">
          <th style="padding:4px 10px;border:1px solid #ccc">Serial Number</th>
          <th style="padding:4px 10px;border:1px solid #ccc">Customer</th>
          <th style="padding:4px 10px;border:1px solid #ccc">Previous Plan</th>
          <th style="padding:4px 10px;border:1px solid #ccc">Current Plan</th>
        </tr>
        {''.join(rows_html)}
      </table>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    log.info("Sending email to %s ...", ", ".join(EMAIL_TO))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    log.info("Email sent.")


# ==========================================================================
# Main
# ==========================================================================
def main():
    parser = argparse.ArgumentParser(description="Alert by email when a Geotab device's active "
                                                 "device plan changes between runs, using a CSV in "
                                                 "Zoho WorkDrive as history.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the diff only. Do not send email, do not touch WorkDrive.")
    parser.add_argument("--full-export", action="store_true",
                        help="Store the entire Zoho export in WorkDrive (73 MB) instead of the "
                             "3-column snapshot the diff actually needs.")
    parser.add_argument("--retry-upload", action="store_true",
                        help="Skip Analytics and the diff entirely; just push the locally cached "
                             f"snapshot ({LOCAL_SNAPSHOT_CACHE}) to WorkDrive. Use this after an "
                             "upload died on a network error, so you don't pay for another 73 MB "
                             "export.")
    parser.add_argument("--setup-folder", action="store_true",
                        help="Interactive: pick or create the WorkDrive folder for the snapshot, "
                             "prove an upload works, and save the ID to .env. Run this once.")
    parser.add_argument("--verify-folder", action="store_true",
                        help="Just check that ZOHO_WORKDRIVE_FOLDER_ID is a usable folder and exit.")
    parser.add_argument("--local-history", action="store_true",
                        help="Keep the previous-run snapshot on THIS machine "
                             f"({LOCAL_SNAPSHOT_CACHE}) and never touch WorkDrive. The alerting "
                             "works exactly the same; you just can't run it from another machine.")
    parser.add_argument("--test-email", action="store_true",
                        help="Send one sample alert with fake rows, to prove SMTP and EMAIL_TO "
                             "work without waiting for a real plan change. Touches nothing else.")
    parser.add_argument("--probe-upload-size", action="store_true",
                        help="Upload payloads of increasing size and report where this network "
                             "stops completing the POST. Diagnostic only.")
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging.")
    args = parser.parse_args()
    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Check configuration before doing anything expensive. A scheduled run
    # should not discover that SMTP_PASSWORD is unset only after spending
    # three minutes pulling 73 MB out of Analytics.
    WORKDRIVE_CFG = (("ZOHO_CLIENT_ID_WORKDRIVE", WORKDRIVE_CLIENT_ID),
                     ("ZOHO_CLIENT_SECRET_WORKDRIVE", WORKDRIVE_CLIENT_SECRET),
                     ("ZOHO_WORKDRIVE_REFRESH_TOKEN", WORKDRIVE_REFRESH_TOKEN),
                     ("ZOHO_WORKDRIVE_FOLDER_ID", WORKDRIVE_FOLDER_ID))
    ANALYTICS_CFG = (("ZOHO_ORG_ID", ZOHO_ORG_ID),
                     ("ZOHO_CLIENT_ID_ANALYTICS", ZOHO_CLIENT_ID),
                     ("ZOHO_CLIENT_SECRET_ANALYTICS", ZOHO_CLIENT_SECRET),
                     ("ZOHO_CLIENT_REFRESH_TOKEN_ANALYTICS", ZOHO_REFRESH_TOKEN))
    EMAIL_CFG = (("SMTP_HOST", SMTP_HOST), ("SMTP_USERNAME", SMTP_USERNAME),
                 ("SMTP_PASSWORD", SMTP_PASSWORD), ("EMAIL_FROM", EMAIL_FROM),
                 ("EMAIL_TO", EMAIL_TO))

    if args.test_email:
        check_config(*EMAIL_CFG)
    elif args.setup_folder or args.verify_folder or args.probe_upload_size or args.retry_upload:
        check_config(*WORKDRIVE_CFG)
    elif args.local_history:
        check_config(*ANALYTICS_CFG, *EMAIL_CFG)
    elif args.dry_run:
        # No email is sent and nothing is written, so SMTP config is irrelevant.
        check_config(*ANALYTICS_CFG, *WORKDRIVE_CFG)
    else:
        check_config(*ANALYTICS_CFG, *WORKDRIVE_CFG, *EMAIL_CFG)

    if args.test_email:
        # The email path has never actually run — no plan change has happened
        # yet — so this exercises it on demand with two fake rows rather than
        # waiting for a real change to discover EMAIL_TO is unset.
        if not EMAIL_TO:
            sys.exit("ERROR: EMAIL_TO is empty, so there is nobody to send to. Add it to .env:\n"
                     "    EMAIL_TO=you@example.com\n"
                     "    (comma-separate for several recipients)\n"
                     "Without it, the first real plan change would fail at this step.")
        log.info("Sending a SAMPLE alert (fake rows) to confirm SMTP works ...")
        send_email([
            {"serial": "SAMPLE-0001", "customer": "Example Logistics Inc",
             "old_plan": "ProPlus", "new_plan": "Suspended"},
            {"serial": "SAMPLE-0002", "customer": "Example Haulage Ltd",
             "old_plan": "Base", "new_plan": "Regulatory"},
        ])
        log.info("If that arrived, the alerting path is proven end to end.")
        return

    if args.probe_upload_size:
        probe_upload_size(workdrive_access_token(), WORKDRIVE_FOLDER_ID)
        return

    if args.setup_folder:
        setup_folder_wizard()
        return

    if args.verify_folder:
        endpoint = verify_workdrive_target(workdrive_access_token(), WORKDRIVE_FOLDER_ID)
        log.info("Target looks usable (listing endpoint: /%s). Nothing else done.", endpoint)
        return

    if args.retry_upload:
        if not os.path.exists(LOCAL_SNAPSHOT_CACHE):
            sys.exit(f"ERROR: --retry-upload needs {LOCAL_SNAPSHOT_CACHE}, which doesn't exist. "
                     f"Run a normal cycle first.")
        with open(LOCAL_SNAPSHOT_CACHE, "r", encoding="utf-8", newline="") as fh:
            payload = encode_payload(fh.read())
        log.info("--retry-upload: pushing the cached snapshot (%.2f MB%s) — no Analytics pull.",
                 len(payload) / 1024 / 1024, ", gzipped" if GZIP_SNAPSHOT else "")
        wd_token = workdrive_access_token()
        verify_workdrive_target(wd_token, WORKDRIVE_FOLDER_ID)
        new_id = upload_workdrive_file(wd_token, WORKDRIVE_FOLDER_ID, WORKDRIVE_REPORT_FILENAME, payload)
        log.info("Updated '%s' in WorkDrive (file id %s).", WORKDRIVE_REPORT_FILENAME,
                 new_id or "id not returned")
        return

    # Check the WorkDrive destination BEFORE the Analytics export, not after.
    # The export costs 73 MB and a few minutes; validating where the result
    # has to land costs one API call. Doing it in the other order (as the
    # previous version did) means a misconfigured destination burns the whole
    # export before saying so.
    wd_token = ""
    endpoint = "files"
    if args.local_history:
        log.info("--local-history: WorkDrive is not used this run; history lives at %s",
                 LOCAL_SNAPSHOT_CACHE)
    else:
        wd_token = workdrive_access_token()
        log.info("Zoho WorkDrive: authenticated.")
        endpoint = verify_workdrive_target(wd_token, WORKDRIVE_FOLDER_ID)

    zoho_token = zoho_access_token()
    log.info("Zoho Analytics: authenticated.")
    current_text = zoho_export_view_raw(zoho_token, DEVICES_VIEW_ID, "Geotab Devices")
    current_rows = parse_csv_rows(current_text)
    if not current_rows:
        sys.exit("ERROR: Zoho Analytics returned zero rows for the Geotab Devices view; aborting "
                 "(refusing to treat an empty pull as '0 devices' and overwrite the WorkDrive file).")

    current = build_serial_plan_map(current_rows, "current Zoho pull")
    if not current:
        sys.exit("ERROR: no rows had a usable serial number; aborting without touching WorkDrive.")

    previous: dict[str, dict] = {}
    if args.local_history:
        if os.path.exists(LOCAL_SNAPSHOT_CACHE):
            with open(LOCAL_SNAPSHOT_CACHE, "r", encoding="utf-8", newline="") as fh:
                previous = build_serial_plan_map(parse_csv_rows(fh.read()), "local history file")
        else:
            log.info("No local history at %s yet — this run creates it; the next run will have "
                     "something to compare against.", LOCAL_SNAPSHOT_CACHE)
    elif os.path.exists(LOCAL_SNAPSHOT_CACHE):
        # Read history from the local copy when we have one, and don't call the
        # download endpoint at all. Two reasons: it's the same content this
        # machine uploaded last run (and if an upload ever failed, it's the
        # FRESHER of the two), and downloading from WorkDrive needs an OAuth
        # scope this token doesn't have — see the warning text below.
        with open(LOCAL_SNAPSHOT_CACHE, "r", encoding="utf-8", newline="") as fh:
            previous = build_serial_plan_map(parse_csv_rows(fh.read()),
                                             "local copy of last run's snapshot")
    else:
        existing_file_id = find_workdrive_file(wd_token, WORKDRIVE_FOLDER_ID,
                                               WORKDRIVE_REPORT_FILENAME, endpoint)
        if existing_file_id:
            previous_text = download_workdrive_file_text(wd_token, existing_file_id)
            if previous_text:
                previous = build_serial_plan_map(parse_csv_rows(previous_text),
                                                 "previous WorkDrive file")
            else:
                log.warning("Could not read last run's snapshot, so no change alert can be sent "
                            "for this cycle. This run still writes history, and the next run will "
                            "compare against the local copy — so this fixes itself.")
        else:
            log.info("'%s' not found in the WorkDrive folder (first run, or it was moved/renamed) — "
                     "nothing to compare against yet. This run's export will become the file for "
                     "next time.", WORKDRIVE_REPORT_FILENAME)

    new_serials = sorted(set(current) - set(previous))
    missing_serials = sorted(set(previous) - set(current))
    if previous and new_serials:
        log.info("%d new serial(s) not in the previous file (not treated as a plan change): %s",
                 len(new_serials), new_serials[:20])
    if previous and missing_serials:
        log.info("%d serial(s) from the previous file are absent this run (not treated as a "
                 "plan change): %s", len(missing_serials), missing_serials[:20])

    changes = diff_plans(previous, current) if previous else []
    if changes:
        log.info("%d device(s) changed active plan:", len(changes))
        for c in changes:
            log.info("  %s (%s): '%s' -> '%s'", c["serial"], c["customer"], c["old_plan"], c["new_plan"])
    else:
        log.info("No active device plan changes detected.")

    if changes:
        if args.dry_run:
            log.info("--dry-run set: not sending email.")
        else:
            send_email(changes)

    if args.dry_run:
        log.info("--dry-run set: not writing history anywhere.")
        return

    text = current_text if args.full_export else build_snapshot_csv(current)

    # Write the local copy first, always. In --local-history mode this IS the
    # history; otherwise it's the safety net that makes --retry-upload possible.
    try:
        with open(LOCAL_SNAPSHOT_CACHE, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        log.info("Wrote this run's snapshot to %s", LOCAL_SNAPSHOT_CACHE)
    except OSError as exc:
        if args.local_history:
            sys.exit(f"ERROR: --local-history can't write {LOCAL_SNAPSHOT_CACHE}: {exc}")
        log.warning("Could not write the local snapshot cache (%s) — continuing anyway.", exc)

    if args.local_history:
        log.info("--local-history: done. Next run will diff against that file.")
        return

    payload = encode_payload(text)
    log.info("Uploading %s (%.2f MB%s).", WORKDRIVE_REPORT_FILENAME, len(payload) / 1024 / 1024,
             f", gzipped from {len(text.encode('utf-8')) / 1024 / 1024:.2f} MB" if GZIP_SNAPSHOT else "")
    new_id = upload_workdrive_file(wd_token, WORKDRIVE_FOLDER_ID, WORKDRIVE_REPORT_FILENAME, payload)
    log.info("Updated '%s' in WorkDrive (%s, file id %s).", WORKDRIVE_REPORT_FILENAME,
             "full export" if args.full_export else "snapshot", new_id or "id not returned")


if __name__ == "__main__":
    main()
