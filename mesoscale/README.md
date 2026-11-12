# BTV Mesoscale Detective

Standalone interactive mesoscale analysis dashboard for the NWS Burlington forecast area.

## Current status

- `../mesoscale.html`: interactive map-first interface foundation.
- Map click selects a point; station markers select KBTV, KPBG, KMSS, KSLK, KMPV, KRUT, KEFK and 1V4.
- RAP/HRRR selector, forecast-hour control, diagnostic-layer selector and transect drawing controls are wired into the interface.
- Meteorological values are intentionally blank until a verified data pipeline is connected. The page must never substitute illustrative values for model data.

## Architecture

1. **Acquisition:** obtain RAP pressure/native-level and surface GRIB2; obtain HRRR pressure-level and surface GRIB2 where available.
2. **Subsetting:** crop the source grids to a padded BTV CWA domain before expensive calculations or serialization.
3. **Processing:** use xarray/cfgrib, MetPy, NumPy and SciPy for unit-aware diagnostics, vertical interpolation and horizontal derivatives.
4. **Products:** write compact, versioned JSON/NetCDF products with source model, cycle, forecast hour, valid time, units, grid geometry and quality flags.
5. **Frontend:** Leaflet handles map selection and overlays; Plotly renders profiles, hodographs, cross-sections and time series.

## Model roles

- **RAP is the full-column workhorse.** Use RAP pressure/native-level data for soundings, hodographs, vertical cross-sections, lapse rates, deep-layer shear, SRH and forcing diagnostics.
- **HRRR is the high-resolution companion.** Use HRRR for near-surface and surface fields, mesoscale gradients, reflectivity-related fields and high-resolution forecast context. Do not assume HRRR provides the same complete 3-D pressure-level fields as RAP; its standard operational pressure-level output is limited compared with RAP.
- **Comparisons must be field-aware.** Only compare fields that exist in both models, use compatible definitions/units, and can be mapped to a common valid time and grid. Label native grid spacing and interpolation method.

## Initial BTV domain

Use a padded rectangle around Vermont and northern New York, approximately:
- west: -75.2
- east: -71.2
- south: 42.3
- north: 45.3

The map is for analysis and display; the actual CWA boundary should be supplied as a GeoJSON overlay rather than inferred from this rectangle.

## Data contract

Every generated field must include:
- `model`, `model_cycle_utc`, `forecast_hour`, `valid_time_utc`
- `field`, `level`, `units`, `description`
- `grid` (projection, dimensions, coordinates or transform)
- `source_file`, `created_utc`, `quality`

Never silently mix analysis time and forecast valid time. Never label model-derived fields as observations. Surface observations remain a separate layer with station ID, observation timestamp and age.

## Development sequence

1. Validate model availability and GRIB inventories for both models.
2. Implement RAP full-column ingestion and point-profile extraction.
3. Implement HRRR surface/near-surface ingestion and common-grid comparison.
4. Add horizontal diagnostics (frontogenesis, moisture convergence, advection and omega).
5. Add transect sampling and cross-section rendering.
6. Add caching, stale-data checks, automated tests and operational status reporting.

## Running locally

Open `mesoscale.html` through a local static web server. The interface currently uses public Leaflet/Plotly CDNs and OpenStreetMap tiles. The model data endpoint is not connected yet.

## Implemented ingestion components

- `discover_models.py` checks current/recent NOMADS production directories and records available RAP and HRRR cycle/forecast products. With `--index-latest`, it retrieves the GRIB index sidecars to inspect available messages.
- `download_subset.py` requests compact BTV geographic subsets from NOMADS instead of pulling full CONUS files. It validates the GRIB magic header before saving.
- `process_grib.py` decodes GRIB2 with cfgrib, crops 2-D and multi-level grids to the BTV analysis domain, and writes gzip-compressed JSON with grid geometry, units, level coordinates and valid-time metadata.
- `diagnostics.py` provides unit-aware MetPy profile calculations for SBCAPE/CIN, MLCAPE/CIN, MUCAPE/CIN, LCL, PWAT, 0-3 km lapse rate, 0-6 km bulk shear and 0-1 km SRH. It rejects incomplete or physically inconsistent columns instead of silently filling data.
- `tests/test_profile_validation.py` covers core profile input validation.

### Initial model products

The first implementation uses RAP full-domain pressure-level GRIB2 and HRRR hourly 2-D GRIB2. HRRR 2-D provides surface/column and selected pressure-level fields, not a substitute for a complete 3-D upper-air sounding. RAP remains the source for full-column diagnostics.

### Developer commands

```bash
python mesoscale/discover_models.py --days-back 2 --index-latest
python mesoscale/download_subset.py --model RAP --forecast-hour 0
python mesoscale/download_subset.py --model HRRR --forecast-hour 0
python mesoscale/process_grib.py mesoscale/data/grib/rap_YYYYMMDD_HH_f00_btv.grib2 --model RAP --cycle 2026-11-12T12:00:00Z --forecast-hour 0 --output mesoscale/data/fields/rap_f00.json.gz
pytest mesoscale/tests
```

The exact discovered filenames and initialization time must be used when running the processing commands. The example cycle and filename above are placeholders, not a claim that a specific model run is available.


## RRFS support (new)

The model discovery service now includes the operational RRFS deterministic CONUS 3-km products. It discovers the pressure-level, 2-D surface, and sub-hourly 2-D file families from the NCEP production directory structure and records their .idx inventories.

- Pressure levels: rrfs.tCCz.prslev.3km.fFFF.conus.grib2
- 2-D fields: rrfs.tCCz.2dfld.3km.fFFF.conus.grib2
- Sub-hourly 2-D fields: rrfs.tCCz.2dfld.3km.subh.fFFF.conus.grib2

The RRFS selector is available in mesoscale.html. Discovery is wired into the scheduled manifest workflow. RRFS regional download/decoding is intentionally not enabled yet: its operational product families and NOMADS subsetting method need to be validated against current inventories before an automated downloader is allowed to fetch large CONUS files. The 3-km RRFS pressure-level file is the intended source for column diagnostics; the 2-D file supplies near-term fields such as composite reflectivity, CAPE/CIN, gusts, visibility and precipitation diagnostics.

The RRFS/REFS transition and current product naming are documented in the NWS implementation notice and NCEP RRFS product inventory.
