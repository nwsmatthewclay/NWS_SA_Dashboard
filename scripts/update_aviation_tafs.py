#!/usr/bin/env python3
"""Refresh the small same-origin TAF feed used by aviation.html."""
import json
import re
import sys
import time
from pathlib import Path

import requests

SITES = ("KBTV", "KSLK", "KPBG", "KMPV", "KEFK", "KRUT", "KMSS")
URL = "https://aviationweather.gov/api/data/taf"
OUT = Path("data/aviation_tafs.json")
HEADERS = {"User-Agent": "NWS-SABTV-AviationDashboard/1.0 (GitHub Actions; public weather display)"}

def pattern(site):
    return re.compile(
        r"(?<![A-Z0-9])(?:TAF\s+)?(?:(?:AMD|COR)\s+)?"
        + re.escape(site)
        + r"\s+\d{6}Z\s+\d{4}/\d{4}\b",
        re.IGNORECASE,
    )

def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    tafs = {}
    failures = []
    for index, site in enumerate(SITES):
        if index:
            time.sleep(0.35)
        try:
            response = session.get(URL, params={"ids": site, "format": "raw"}, timeout=20)
            response.raise_for_status()
            raw = " ".join(response.text.replace("\r", " ").replace("\n", " ").split()).strip()
            if not raw or not pattern(site).search(raw):
                failures.append(f"{site}: response did not contain a valid raw TAF (HTTP {response.status_code})")
                print(f"{site}: unexpected response: {response.text[:250]!r}", file=sys.stderr)
                continue
            tafs[site] = raw
            print(f"{site}: retrieved {len(raw)} characters")
        except Exception as exc:
            failures.append(f"{site}: {exc}")
    if failures:
        print("TAF refresh incomplete; leaving the last good feed untouched:", file=sys.stderr)
        for failure in failures:
            print(" - " + failure, file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"tafs": tafs}, indent=2) + "\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == payload:
        print("TAFs unchanged; no commit needed.")
        return 0
    OUT.write_text(payload, encoding="utf-8")
    print(f"Wrote {OUT} with {len(tafs)} validated TAFs.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
