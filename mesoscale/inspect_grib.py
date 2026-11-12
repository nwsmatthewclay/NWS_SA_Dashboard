#!/usr/bin/env python3
"""Inspect GRIB2 files and write a machine-readable inventory.

This is the first ingestion step: it records exactly which fields and
vertical coordinates are present before any diagnostic is calculated.
It deliberately does not infer missing variables or fabricate fields.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import cfgrib


def describe_value(value):
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def inspect_file(path: Path) -> dict:
    result = {
        "source_file": str(path),
        "inspected_utc": datetime.now(timezone.utc).isoformat(),
        "datasets": [],
        "errors": [],
    }
    try:
        datasets = cfgrib.open_datasets(
            str(path),
            backend_kwargs={"indexpath": ""},
        )
    except Exception as exc:
        result["errors"].append(f"open_datasets: {type(exc).__name__}: {exc}")
        return result

    for index, ds in enumerate(datasets):
        variables = {}
        for name, variable in ds.data_vars.items():
            variables[name] = {
                "dims": list(variable.dims),
                "shape": list(variable.shape),
                "units": variable.attrs.get("units"),
                "long_name": variable.attrs.get("long_name"),
                "standard_name": variable.attrs.get("standard_name"),
                "level_type": variable.attrs.get("GRIB_typeOfLevel"),
                "step_type": variable.attrs.get("GRIB_stepType"),
            }
        coordinates = {}
        for name, coord in ds.coords.items():
            values = coord.values
            flat = values.reshape(-1) if getattr(values, "ndim", 0) else [values]
            preview = [describe_value(v) for v in flat[:12]]
            coordinates[name] = {
                "dims": list(coord.dims),
                "shape": list(coord.shape),
                "values_preview": preview,
            }
        attrs = {k: describe_value(v) for k, v in ds.attrs.items()
                 if k in {"GRIB_centre", "GRIB_dataDate", "GRIB_dataTime",
                          "GRIB_forecastTime", "GRIB_stepUnits",
                          "GRIB_typeOfLevel", "GRIB_gridType"}}
        result["datasets"].append({
            "dataset_index": index,
            "attrs": attrs,
            "variables": variables,
            "coordinates": coordinates,
        })
        ds.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="GRIB2 files to inspect")
    parser.add_argument("--output", default="mesoscale_grib_inventory.json")
    args = parser.parse_args()

    reports = []
    for item in args.files:
        path = Path(item)
        if not path.is_file():
            reports.append({"source_file": item, "errors": ["file not found"], "datasets": []})
            continue
        reports.append(inspect_file(path))

    output = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": reports,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, default=str) + "\n", encoding="utf-8")
    failures = sum(bool(report.get("errors")) for report in reports)
    print(f"Wrote {args.output}; inspected {len(reports)} file(s); file-level failures: {failures}")


if __name__ == "__main__":
    main()
