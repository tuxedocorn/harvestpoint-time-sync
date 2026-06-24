"""
Harvestpoint → Smartsheet Time Entry Sync
Pulls completed time entries from Harvestpoint (Firebase) and pushes to Smartsheet.
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
DAYS_BACK = 7  # Change this to pull more or fewer days

FIREBASE_API_KEY   = os.getenv("FIREBASE_API_KEY")
HARVESTPOINT_EMAIL = os.getenv("HARVESTPOINT_EMAIL")
HARVESTPOINT_PASS  = os.getenv("HARVESTPOINT_PASS")
FIREBASE_DB_URL    = "https://harvest-point.firebaseio.com"
ORG_ID             = "buffalopacking-2022"

SMARTSHEET_TOKEN   = os.getenv("SMARTSHEET_TOKEN")
SHEET_NAME         = "Harvestpoint Time Entries"

# ── Firebase Auth ─────────────────────────────────────────────────────────────
def get_firebase_token():
    """Log into Firebase with email/password, return access token."""
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
    """Pull all time entries from Firebase for the last DAYS_BACK days."""
    # Calculate start timestamp in seconds (Harvestpoint stores as Unix seconds)
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
    """Keep only completed Start Task records and build clean rows."""
    rows = []
    skipped = 0

    for e in entries:
        # Only process completed Start Task records
        if e.get("timeEntryType") != "Start Task":
            continue
        if not e.get("endDateTime"):
            skipped += 1
            continue

        start_ts = e.get("startDateTime", 0)
        end_ts   = e.get("endDateTime", 0)

        # Convert Unix seconds to readable datetimes
        # Harvestpoint stores as seconds (float), not milliseconds
        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        end_dt   = datetime.fromtimestamp(end_ts,   tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        # Calculate hours worked
        hours = round((end_ts - start_ts) / 3600, 2)

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
    {"title": "Note",           "type": "TEXT_NUMBER"},
    {"title": "Entry Mode",     "type": "TEXT_NUMBER"},
    {"title": "Time Entry Key", "type": "TEXT_NUMBER"},
]


def get_or_create_sheet():
    """Find existing sheet by name or create a new one. Returns (sheet_id, column_map)."""
    # Search for existing sheet
    resp = requests.get(f"{SS_BASE}/sheets", headers=SS_HEADERS)
    resp.raise_for_status()
    sheets = resp.json().get("data", [])

    for sheet in sheets:
        if sheet["name"] == SHEET_NAME:
            sheet_id = sheet["id"]
            print(f"✓ Found existing Smartsheet: '{SHEET_NAME}' (id={sheet_id})")
            return sheet_id, get_column_map(sheet_id)

    # Create new sheet
    payload = {"name": SHEET_NAME, "columns": COLUMN_DEFS}
    resp = requests.post(f"{SS_BASE}/sheets", headers=SS_HEADERS, json=payload)
    resp.raise_for_status()
    sheet_id = resp.json()["result"]["id"]
    print(f"✓ Created new Smartsheet: '{SHEET_NAME}' (id={sheet_id})")
    return sheet_id, get_column_map(sheet_id)


def get_column_map(sheet_id):
    """Return a dict of column title → column id."""
    resp = requests.get(f"{SS_BASE}/sheets/{sheet_id}", headers=SS_HEADERS)
    resp.raise_for_status()
    columns = resp.json().get("columns", [])
    return {col["title"]: col["id"] for col in columns}


def push_rows(sheet_id, column_map, rows):
    """Add all rows to the Smartsheet."""
    if not rows:
        print("No rows to push.")
        return

    ss_rows = []
    for row in rows:
        cells = []
        for col_title, value in row.items():
            col_id = column_map.get(col_title)
            if col_id:
                cells.append({"columnId": col_id, "value": value})
        ss_rows.append({"toBottom": True, "cells": cells})

    # Smartsheet allows max 500 rows per request — chunk if needed
    chunk_size = 500
    for i in range(0, len(ss_rows), chunk_size):
        chunk = ss_rows[i:i + chunk_size]
        resp = requests.post(
            f"{SS_BASE}/sheets/{sheet_id}/rows",
            headers=SS_HEADERS,
            json=chunk
        )
        resp.raise_for_status()

    print(f"✓ Pushed {len(rows)} rows to Smartsheet")


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
    push_rows(sheet_id, column_map, rows)

    print(f"\n✓ Sync complete!")


if __name__ == "__main__":
    main()
