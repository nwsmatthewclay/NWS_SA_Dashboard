#!/usr/bin/env python3
"""Download compact BTV-area GRIB2 subsets from NOMADS.

The downloader uses the latest products recorded in model_manifest.json
(or explicit filenames), requests a small set of forecast fields, and
checks that the response is a GRIB file before writing it. It never
downloads whole CONUS GRIB files.

RAP: full-domain pressure-level product, cropped to the BTV region.
HRRR: hourly 2-D product, cropped to the same region.

NOMADS can reject combinations that are not present in a selected file;
the script preserves the server response as an error and never labels an
HTML error page as meteorological data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE = "https://nomads.ncep.noaa.gov/cgi-bin"
REGION = {"leftlon": -75.2, "rightlon": -71.2, "toplat": 45.3, "bottomlat": 42.3}

RAP_VARIABLES = ["TMP", "RH", "HGT", "UGRD", "VGRD"]
RAP_LEVELS = [
    1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 725,
    700, 675, 650, 625, 600, 575, 550, 525, 500, 475, 450, 425,
    400, 375, 350, 325, 300, 275, 250, 225, 200, 175, 150, 125, 100,
]
HRRR_VARIABLES = ["CAPE", "CIN", "PWAT", "DPT", "TMP", "UGRD", "VGRD",
                  "REFC", "GUST", "HPBL", "VIS", "PRATE", "APCP"]
HRRR_LEVELS = [
    "2_m_above_ground", "10_m_above_ground", "80_m_above_ground",
    "surface", "mean_sea_level", "entire_atmosphere",
    "0-3000_m_above_ground", "925_mb", "850_mb", "700_mb", "500_mb",
]


def filter_url(model: str, product: dict) -> str:
    date = product["date"]
    cycle = product["cycle_utc"][11:13]
    forecast = int(product["forecast_hour"])
    if model == "RAP":
        script = "filter_rap.pl"
        filename = f"rap.t{cycle}z.wrfprs.f{forecast:02d}.grib2"
        directory = f"/rap.{date}"
        variables = RAP_VARIABLES
        levels = [f"{level}_mb" for level in RAP_LEVELS]
    else:
        script = "filter_hrrr_2d.pl"
        filename = f"hrrr.t{cycle}z.wrfsfcf{forecast:02d}.grib2"
        directory = f"/hrrr.{date}/conus"
        variables = HRRR_VARIABLES
        levels = HRRR_LEVELS

    params = {"file": filename, "dir": directory, "subregion": "1", **REGION}
    params.update({f"var_{name}": "on" for name in variables})
    params.update({f"lev_{name}": "on" for name in levels})
    return f"{BASE}/{script}?{urlencode(params)}"


def download(session, model, product, output_dir):
    url = filter_url(model, product)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{model.lower()}_{product['date']}_{product['cycle_utc'][11:13]}_f{product['forecast_hour']:02d}_btv.grib2"
    destination = output_dir / filename
    response = session.get(url, stream=True, timeout=(20, 180))
    response.raise_for_status()
    first = next(response.iter_content(chunk_size=4), b"")
    if first != b"GRIB":
        sample = first + next(response.iter_content(chunk_size=512), b"")
        raise RuntimeError(f"NOMADS did not return GRIB data for {model}: {sample[:300]!r}")
    temp = destination.with_suffix(destination.suffix + ".part")
    total = 0
    with temp.open("wb") as stream:
        stream.write(first)
        total += len(first)
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                stream.write(chunk)
                total += len(chunk)
    if total < 1000:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Subset unexpectedly small ({total} bytes): {url}")
    temp.replace(destination)
    return {"model": model, "file": str(destination), "bytes": total, "request_url": url,
            "cycle_utc": product["cycle_utc"], "forecast_hour": product["forecast_hour"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="mesoscale/data/model_manifest.json")
    parser.add_argument("--output-dir", default="mesoscale/data/grib")
    parser.add_argument("--model", choices=["RAP", "HRRR", "both"], default="both")
    parser.add_argument("--forecast-hour", type=int, default=0)
    parser.add_argument("--cycle", help="Optional cycle ISO timestamp, e.g. 2026-11-12T12:00:00Z")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers.update({"User-Agent": "BTV-Mesoscale-Detective/0.1 (NWS forecast office research)"})
    models = ["RAP", "HRRR"] if args.model == "both" else [args.model]
    results, errors = [], []
    for model in models:
        products = manifest.get("models", {}).get(model, {}).get("latest_products", [])
        match = next((p for p in products
                      if p.get("forecast_hour") == args.forecast_hour
                      and (not args.cycle or p.get("cycle_utc") == args.cycle)
                      and (p.get("product") == "pressure_levels_full_domain" if model == "RAP"
                           else p.get("product") == "surface")), None)
        if not match:
            errors.append({"model": model, "error": "No matching product in manifest"})
            continue
        try:
            results.append(download(session, model, match, Path(args.output_dir)))
        except Exception as exc:
            errors.append({"model": model, "error": f"{type(exc).__name__}: {exc}"})

    report = {"results": results, "errors": errors}
    print(json.dumps(report, indent=2))
    if errors or not results:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
