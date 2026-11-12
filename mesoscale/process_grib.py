#!/usr/bin/env python3
"""Crop decoded GRIB2 fields to the BTV domain and publish compact gzip JSON.

The output preserves model, cycle, forecast hour, valid time, units,
level coordinate and grid coordinates. 2-D and multi-level fields remain
separate, so the browser can select a nearest grid point without a server.
"""
from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import cfgrib
import numpy as np

DOMAIN = {"west": -75.2, "east": -71.2, "south": 42.3, "north": 45.3}


def json_values(array, decimals=2):
    arr = np.asarray(array, dtype=float)
    arr = np.where(np.isfinite(arr), np.round(arr, decimals), np.nan)
    return arr.tolist()


def attr_value(value):
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def process_file(path: Path, model: str, cycle: str, forecast_hour: int,
                 output: Path, max_grid_dim: int = 160):
    datasets = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
    fields = {}
    grids = {}
    warnings = []
    try:
        for ds_index, ds in enumerate(datasets):
            lat_name = next((n for n in ("latitude", "lat") if n in ds.coords), None)
            lon_name = next((n for n in ("longitude", "lon") if n in ds.coords), None)
            if not lat_name or not lon_name:
                warnings.append(f"dataset {ds_index}: no latitude/longitude coordinates")
                continue
            lat = np.asarray(ds[lat_name].values)
            lon = np.asarray(ds[lon_name].values)
            if lat.ndim != 2 or lon.ndim != 2:
                warnings.append(f"dataset {ds_index}: expected 2-D lat/lon grid")
                continue
            mask = ((lon >= DOMAIN["west"]) & (lon <= DOMAIN["east"]) &
                    (lat >= DOMAIN["south"]) & (lat <= DOMAIN["north"]))
            if not mask.any():
                continue
            rows, cols = np.where(mask)
            y0, y1 = int(rows.min()), int(rows.max()) + 1
            x0, x1 = int(cols.min()), int(cols.max()) + 1
            lat_crop, lon_crop = lat[y0:y1, x0:x1], lon[y0:y1, x0:x1]
            stride = max(1, int(np.ceil(max(lat_crop.shape) / max_grid_dim)))
            lat_crop, lon_crop = lat_crop[::stride, ::stride], lon_crop[::stride, ::stride]
            grid_id = f"grid_{ds_index}"
            grids[grid_id] = {"latitude": json_values(lat_crop, 4),
                              "longitude": json_values(lon_crop, 4),
                              "shape": list(lat_crop.shape), "stride": stride}

            for name, variable in ds.data_vars.items():
                dims = list(variable.dims)
                y_dim = ds[lat_name].dims[0]
                x_dim = ds[lat_name].dims[1]
                if y_dim not in dims or x_dim not in dims:
                    continue
                # Reorder to all non-horizontal dimensions, then y, x.
                other_dims = [d for d in dims if d not in (y_dim, x_dim)]
                ordered = variable.transpose(*(other_dims + [y_dim, x_dim]))
                arr = np.asarray(ordered.values)
                y_axis, x_axis = ordered.dims.index(y_dim), ordered.dims.index(x_dim)
                slices = [slice(None)] * arr.ndim
                slices[y_axis] = slice(y0, y1, stride)
                slices[x_axis] = slice(x0, x1, stride)
                arr = arr[tuple(slices)]

                level_meta = {}
                for dim in other_dims:
                    if dim in ds.coords:
                        level_meta[dim] = [attr_value(v) for v in np.asarray(ds[dim].values).reshape(-1)]
                short_name = variable.attrs.get("GRIB_shortName", name)
                level_tag = "_".join(f"{k}-{v}" for k, vals in level_meta.items()
                                     for v in vals[:1]) or "2d"
                key = f"{short_name}_{level_tag}_{ds_index}"
                fields[key] = {
                    "short_name": short_name,
                    "variable_name": name,
                    "long_name": variable.attrs.get("long_name"),
                    "units": variable.attrs.get("units"),
                    "dimensions": other_dims,
                    "levels": level_meta,
                    "grid_id": grid_id,
                    "data": json_values(arr, 2),
                }
    finally:
        for ds in datasets:
            ds.close()

    if not fields:
        raise RuntimeError("No gridded variables found inside the BTV geographic domain")
    payload = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "model_cycle_utc": cycle,
        "forecast_hour": forecast_hour,
        "valid_time_utc": None,
        "source_file": path.name,
        "domain": DOMAIN,
        "grids": grids,
        "fields": fields,
        "quality": {"field_count": len(fields), "grid_count": len(grids),
                    "warnings": warnings},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(payload, stream, separators=(",", ":"), allow_nan=False)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("grib_file")
    parser.add_argument("--model", choices=["RAP", "HRRR"], required=True)
    parser.add_argument("--cycle", required=True, help="UTC cycle ISO timestamp")
    parser.add_argument("--forecast-hour", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-grid-dim", type=int, default=160)
    args = parser.parse_args()
    payload = process_file(Path(args.grib_file), args.model, args.cycle,
                           args.forecast_hour, Path(args.output), args.max_grid_dim)
    print(json.dumps({"output": args.output, "fields": payload["quality"]["field_count"],
                      "grids": payload["quality"]["grid_count"],
                      "warnings": payload["quality"]["warnings"]}, indent=2))


if __name__ == "__main__":
    main()
