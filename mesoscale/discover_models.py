#!/usr/bin/env python3
"""Discover recent RAP/HRRR GRIB2 products and inspect their indexes.

This is a discovery/metadata step, not a bulk downloader. It probes the
official NCEP production directory listings, selects recent files using
published naming conventions, and records GRIB index metadata so the next
stage can request only the needed fields and BTV subregion.

Examples:
  python mesoscale/discover_models.py
  python mesoscale/discover_models.py --days-back 2 --output mesoscale/data/model_manifest.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com"
HEADERS = {"User-Agent": "BTV-Mesoscale-Detective/0.1 (NWS forecast office research)"}
TIMEOUT = 25


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def get_listing(session: requests.Session, url: str) -> list[str]:
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    parser = Links()
    parser.feed(response.text)
    return [urljoin(url, href) for href in parser.hrefs]


def index_summary(session: requests.Session, grib_url: str) -> dict:
    idx_url = grib_url + ".idx"
    response = session.get(idx_url, timeout=TIMEOUT)
    response.raise_for_status()
    lines = [line.strip() for line in response.text.splitlines() if line.strip()]
    messages = []
    for line in lines:
        parts = line.split(":")
        if len(parts) >= 5:
            messages.append({
                "message": parts[0],
                "byte_offset": int(parts[1]) if parts[1].isdigit() else None,
                "reference": parts[2],
                "level": parts[3],
                "description": ":".join(parts[4:]),
            })
    return {"url": idx_url, "message_count": len(messages), "messages": messages}


def discover_model(session, model, days_back):
    found = []
    today = datetime.now(timezone.utc).date()
    for day_offset in range(days_back + 1):
        day = today - timedelta(days=day_offset)
        date_str = day.strftime("%Y%m%d")
        if model == "RAP":
            directory = f"{BASE}/rap/prod/rap.{date_str}/"
            pattern = re.compile(r"rap\.t(\d{2})z\.wrfprs\.f(\d{2,3})\.grib2$")
            role = "pressure_levels_full_domain"
        else:
            directory = f"{BASE}/hrrr/prod/hrrr.{date_str}/conus/"
            pattern = re.compile(r"hrrr\.t(\d{2})z\.(wrfprsf|wrfsfcf)(\d{2,3})\.grib2$")
            role = None
        try:
            urls = get_listing(session, directory)
        except requests.RequestException as exc:
            found.append({"date": date_str, "directory": directory,
                          "error": f"{type(exc).__name__}: {exc}"})
            continue
        for url in urls:
            filename = url.rsplit("/", 1)[-1]
            match = pattern.match(filename)
            if not match:
                continue
            if model == "RAP":
                cycle, forecast = match.groups()
                product_role = role
            else:
                cycle, family, forecast = match.groups()
                product_role = "pressure_levels" if family == "wrfprsf" else "surface"
            found.append({
                "model": model,
                "date": date_str,
                "cycle_utc": f"{date_str}T{cycle}:00:00Z",
                "forecast_hour": int(forecast),
                "product": product_role,
                "filename": filename,
                "url": url,
            })
    return found



def discover_rrfs(session, days_back):
    """Discover deterministic 3-km CONUS RRFS pressure and 2-D products."""
    found = []
    today = datetime.now(timezone.utc).date()
    pattern = re.compile(
        r"rrfs\\.t(\\d{2})z\\.(prslev|2dfld|2dfld\\.subh)\\.3km\\.f(\\d{3})\\.conus\\.grib2$"
    )
    for day_offset in range(days_back + 1):
        date_str = (today - timedelta(days=day_offset)).strftime("%Y%m%d")
        day_dir = f"{BASE}/rrfs/prod/rrfs.{date_str}/"
        try:
            cycle_dirs = get_listing(session, day_dir)
        except requests.RequestException as exc:
            found.append({"date": date_str, "directory": day_dir,
                          "error": f"{type(exc).__name__}: {exc}"})
            continue
        for cycle_dir in cycle_dirs:
            cycle_name = cycle_dir.rstrip("/").rsplit("/", 1)[-1]
            if not re.fullmatch(r"\\d{2}", cycle_name):
                continue
            try:
                urls = get_listing(session, cycle_dir)
            except requests.RequestException as exc:
                found.append({"date": date_str, "directory": cycle_dir,
                              "error": f"{type(exc).__name__}: {exc}"})
                continue
            for url in urls:
                filename = url.rsplit("/", 1)[-1]
                match = pattern.match(filename)
                if not match:
                    continue
                cycle, family, forecast = match.groups()
                found.append({
                    "model": "RRFS", "date": date_str,
                    "cycle_utc": f"{date_str}T{cycle}:00:00Z",
                    "forecast_hour": int(forecast),
                    "product": {"prslev": "pressure_levels_3km",
                                "2dfld": "surface_2d_3km",
                                "2dfld.subh": "surface_subhourly_3km"}[family],
                    "resolution_km": 3, "domain": "conus",
                    "filename": filename, "url": url,
                })
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=2,
                        help="Search today and this many previous UTC dates")
    parser.add_argument("--output", default="mesoscale/data/model_manifest.json")
    parser.add_argument("--index-latest", action="store_true",
                        help="Fetch .idx metadata for newest discovered product in each model family")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": "NCEP NOMADS operational production directories",
        "domain": {"west": -75.2, "east": -71.2, "south": 42.3, "north": 45.3},
        "models": {},
        "errors": [],
    }
    for model in ("RAP", "HRRR"):
        products = discover_model(session, model, max(0, args.days_back))
        valid = [p for p in products if p.get("url")]
        valid.sort(key=lambda p: (p["cycle_utc"], p["forecast_hour"], p["product"]), reverse=True)
        if args.index_latest:
            # One index for the latest available product in each distinct HRRR family;
            # one latest pressure-level index for RAP.
            chosen = []
            families = sorted({p["product"] for p in valid})
            for family in families:
                item = next((p for p in valid if p["product"] == family), None)
                if item:
                    chosen.append(item)
            for item in chosen:
                try:
                    item["index"] = index_summary(session, item["url"])
                except requests.RequestException as exc:
                    item["index_error"] = f"{type(exc).__name__}: {exc}"
        report["models"][model] = {
            "products_found": len(valid),
            "latest_products": valid[:40],
            "directory_errors": [p for p in products if p.get("error")],
        }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for model, data in report["models"].items():
        print(f"{model}: {data['products_found']} product(s) discovered")
        for item in data["latest_products"][:3]:
            print(f"  {item['cycle_utc']} F{item['forecast_hour']:02d} {item['product']} {item['filename']}")
    print(f"Manifest written: {output}")
    if not any(report["models"][m]["products_found"] for m in report["models"]):
        print("No products discovered. Check NOMADS reachability and directory conventions.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
