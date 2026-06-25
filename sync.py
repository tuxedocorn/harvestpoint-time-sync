"""
Harvestpoint → Smartsheet Time Entry Sync
Pulls completed time entries from Harvestpoint (Firebase) and upserts to Smartsheet.
- Pulls last 14 days from Firebase
- Updates existing rows by Time Entry Key
- Adds new rows for new entries
- Calculates Week Start (Monday) for each punch
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
DAYS_BACK = 14

FIREBASE_API_KEY   = os.getenv("FIREBASE_API_KEY")
HARVESTPOINT_EMAIL = os.getenv("HARVESTPOINT_EMAIL")
HARVESTPOINT_PASS  = os.getenv("HARVESTPOINT_PASS")
FIREBASE_DB_URL    = "https://harvest-point.firebaseio.com"
ORG_ID             = "buffalopacking-2022"

SMARTSHEET_TOKEN   = os.getenv("SMARTSHEET_TOKEN")
SHEET_NAME         = "Harvestpoint Time Entries"

# ── Firebase Auth ─────────────────────────────────────────────────────────────
def get_firebase_token():
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    payload = {
        "email": HARVESTPOINT_EMAIL,
        "password": HARVESTPOINT_PASS,
        "returnSecureToken": True
    }
    resp = requests.post(url, json=payload)
    resp.raise_for_status()
    token = resp.json().get("idToken")
    print("✓ Firebase auth successful")
    return token


# ── Fetch Time Entries ────────────────────────────────────────────────────────
def fetch_time_entries(token):
    since = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    since_s = since.timestamp()

    url = f"{FIREBASE_DB_URL}/organization/{ORG_ID}/timeEntry.json"
    params = {
        "auth": token,
        "orderBy": '"startDateTime"',
        "startAt": since_s,
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    if not data:
        print("No time entries found for the date range.")
        return []

    entries = list(data.values())
    print(f"✓ Fetched {len(entries)} raw time entries from Harvestpoint")
    return entries


# ── Filter & Transform ────────────────────────────────────────────────────────
def process_entries(entries):
    rows = []
    skipped = 0

    for e in entries:
        if e.get("timeEntryType") != "Start Task":
            continue
        if not e.get("endDateTime"):
            skipped += 1
            continue

        start_ts = e.get("startDateTime", 0)
        end_ts   = e.get("endDateTime", 0)

        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        end_dt   = datetime.fromtimestamp(end_ts,   tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        hours = round((end_ts - start_ts) / 3600, 2)

        # Calculate Monday of the week this punch belongs to
        punch_date = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        week_start = (punch_date - timedelta(days=punch_date.weekday())).strftime("%Y-%m-%d")

        work_task = e.get("workTask", {})

        rows.append({
            "Employee Name":    f"{e.get('employeeFirstName', '')} {e.get('employeeLastName', '')}".strip(),
            "Employee Code":    e.get("employeeCode", "").strip(),
            "Crew":             e.get("crewName", "").strip(),
            "Orchard":          e.get("orchardName", ""),
            "Work Task":        work_task.get("name", ""),
            "Start DateTime":   start_dt,
            "End DateTime":     end_dt,
            "Hours Worked":     hours,
            "Week Start":       week_start,
            "Note":             e.get("note", ""),
            "Entry Mode":       e.get("entryMode", ""),
            "Time Entry Key":   e.get("key", ""),
        })

    print(f"✓ {len(rows)} completed entries ready to sync  ({skipped} skipped — no end time)")
    return rows


# ── Smartsheet ────────────────────────────────────────────────────────────────
SS_BASE = "https://api.smartsheet.com/2.0"
SS_HEADERS = {
    "Authorization": f"Bearer {SMARTSHEET_TOKEN}",
    "Content-Type": "application/json"
}

COLUMN_DEFS = [
    {"title": "Employee Name",  "type": "TEXT_NUMBER", "primary": True},
    {"title": "Employee Code",  "type": "TEXT_NUMBER"},
    {"title": "Crew",           "type": "TEXT_NUMBER"},
    {"title": "Orchard",        "type": "TEXT_NUMBER"},
    {"title": "Work Task",      "type": "TEXT_NUMBER"},
    {"title": "Start DateTime", "type": "TEXT_NUMBER"},
    {"title": "End DateTime",   "type": "TEXT_NUMBER"},
    {"title": "Hours Worked",   "type": "TEXT_NUMBER"},
    {"title": "Week Start",     "type": "TEXT_NUMBER"},
    {"title": "Note",           "type": "TEXT_NUMBER"},
    {"title": "Entry Mode",     "type": "TEXT_NUMBER"},
    {"title": "Time Entry Key", "type": "TEXT_NUMBER"},
]


def get_or_create_sheet():
    resp = requests.get(f"{SS_BASE}/sheets", headers=SS_HEADERS)
    resp.raise_for_status()
    sheets = resp.json().get("data", [])

    for sheet in sheets:
        if sheet["name"] == SHEET_NAME:
            sheet_id = sheet["id"]
            print(f"✓ Found existing Smartsheet: '{SHEET_NAME}' (id={sheet_id})")
            return sheet_id, get_column_map(sheet_id)

    payload = {"name": SHEET_NAME, "columns": COLUMN_DEFS}
    resp = requests.post(f"{SS_BASE}/sheets", headers=SS_HEADERS, json=payload)
    resp.raise_for_status()
    sheet_id = resp.json()["result"]["id"]
    print(f"✓ Created new Smartsheet: '{SHEET_NAME}' (id={sheet_id})")
    time.sleep(2)
    return sheet_id, get_column_map(sheet_id)


def get_column_map(sheet_id):
    resp = requests.get(f"{SS_BASE}/sheets/{sheet_id}", headers=SS_HEADERS)
    resp.raise_for_status()
    columns = resp.json().get("columns", [])
    return {col["title"]: col["id"] for col in columns}


def get_existing_rows(sheet_id, column_map):
    key_col_id = column_map.get("Time Entry Key")
    if not key_col_id:
        return {}

    resp = requests.get(f"{SS_BASE}/sheets/{sheet_id}", headers=SS_HEADERS)
    resp.raise_for_status()
    rows = resp.json().get("rows", [])

    existing = {}
    for row in rows:
        for cell in row.get("cells", []):
            if cell.get("columnId") == key_col_id and cell.get("value"):
                existing[cell["value"]] = row["id"]
    return existing


def upsert_rows(sheet_id, column_map, rows):
    if not rows:
        print("No rows to sync.")
        return

    existing = get_existing_rows(sheet_id, column_map)
    print(f"✓ Found {len(existing)} existing rows in Smartsheet")

    to_insert = []
    to_update = []

    for row in rows:
        entry_key = row.get("Time Entry Key")
        cells = [
            {"columnId": column_map[col], "value": val}
            for col, val in row.items()
            if col in column_map
        ]

        if entry_key in existing:
            to_update.append({"id": existing[entry_key], "cells": cells})
        else:
            to_insert.append({"toBottom": True, "cells": cells})

    if to_insert:
        for i in range(0, len(to_insert), 500):
            resp = requests.post(
                f"{SS_BASE}/sheets/{sheet_id}/rows",
                headers=SS_HEADERS,
                json=to_insert[i:i + 500]
            )
            resp.raise_for_status()
        print(f"✓ Inserted {len(to_insert)} new rows")
    else:
        print("✓ No new rows to insert")

    if to_update:
        for i in range(0, len(to_update), 500):
            resp = requests.put(
                f"{SS_BASE}/sheets/{sheet_id}/rows",
                headers=SS_HEADERS,
                json=to_update[i:i + 500]
            )
            resp.raise_for_status()
        print(f"✓ Updated {len(to_update)} existing rows")
    else:
        print("✓ No existing rows needed updating")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"Harvestpoint → Smartsheet Sync")
    print(f"Pulling last {DAYS_BACK} days of time entries")
    print(f"{'='*50}\n")

    token   = get_firebase_token()
    entries = fetch_time_entries(token)
    rows    = process_entries(entries)

    sheet_id, column_map = get_or_create_sheet()
    upsert_rows(sheet_id, column_map, rows)

    print(f"\n✓ Sync complete!")


if __name__ == "__main__":
    main()
