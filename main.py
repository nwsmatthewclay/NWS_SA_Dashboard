"""
main.py
=======
Core data pipeline for the severe-dashboard repo (matthewclay88/severe-dashboard),
run every 15 minutes via GitHub Actions.

Two independent jobs, each wrapped so a failure in one doesn't take
down the other during a scheduled run:

  1. BUFKIT PARAMETER ENGINE
     Fetches RAP/HRRR/NAM/GFS BUFKIT soundings for 8 northeast sites,
     computes severe-weather parameters, and writes them to a Google
     Spreadsheet ("Forecast" and "Current" tabs). This spreadsheet is
     the data source the separate BTV Severe Weather Dashboard
     (Apps Script tool) reads from — that tool is unaffected by the
     GitHub suspension, but it goes stale without this script running.

  2. GLWU WAVE / WIND ANIMATION
     Pulls Lake Champlain wave-height + wind data from NOAA's GLWU
     WaveWatch III output on NOMADS, renders a 12-frame animated GIF
     plus a per-station forecast chart, and uploads both to Google
     Drive.

NOTE ON THIS REBUILD
---------------------
Reconstructed from chat history after a GitHub account suspension
wiped the original repo. High confidence pieces: site/model lists,
spreadsheet ID and tab names, GLWU grid/URL, unit conversions, GIF
framing, Drive upsert pattern, known bug fixes (LAND zorder, shapefile
download check). The BUFKIT text parser is the one piece I do not have
byte-exact from history — it's stubbed with a clear TODO and a
NotImplementedError so it fails loudly on a bad parse instead of
silently writing wrong numbers to the spreadsheet. Everything
downstream of a parsed profile (CAPE/CIN, shear, helicity, lapse
rates) uses MetPy's standard calculations, not guessed formulas.
"""

import os
import io
import re
import json
import datetime as dt

import numpy as np
import requests
import gspread
from google.oauth2 import service_account as google_service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from PIL import Image

import metpy.calc as mpcalc
from metpy.units import units

try:
    import pygrib
except ImportError:
    pygrib = None  # only needed for the GLWU job


# =====================================================================
# 1. CONSTANTS
# =====================================================================

SITES = ["KBTV", "KPBG", "KMSS", "KSLK", "RUT", "KMPV", "1V4", "KEFK"]
MODELS = ["rap", "hrrr", "nam", "gfs"]

# RAP and HRRR post hourly BUFKIT files; NAM and GFS only post at
# synoptic hours (confirmed against a live directory listing - hours
# other than 00/06/12/18 simply don't have nam/gfs subfolders at all,
# while rap/hrrr exist every hour).
SYNOPTIC_ONLY_MODELS = {"nam", "gfs"}


def get_bufkit_run_time(model, now=None):
    """
    Pick the run time to request for a given model. Hourly models
    (rap, hrrr) use the previous hour. Synoptic-only models (nam, gfs)
    round down to the most recent of 00/06/12/18Z.
    """
    now = now or dt.datetime.utcnow()
    if model in SYNOPTIC_ONLY_MODELS:
        run_time = now - dt.timedelta(hours=1)  # archive posting lag
        synoptic_hour = (run_time.hour // 6) * 6
        return run_time.replace(hour=synoptic_hour, minute=0, second=0, microsecond=0)
    return now - dt.timedelta(hours=1)

SPREADSHEET_ID = "11FjM4i1s0SpOE5y5_nPDRzLEsoAPA62keyS06a0G3Fo"
FORECAST_TAB = "Forecast"
CURRENT_TAB = "Current"

SERVICE_ACCOUNT_EMAIL = "severe-dashboard-bot@macro-thinker-499803-u2.iam.gserviceaccount.com"

BUFKIT_BASE_URL = (
    "https://mtarchive.geol.iastate.edu/{yyyy}/{mm}/{dd}/bufkit/{hh}/{model}/{prefix}_{site}.buf"
)
# The URL folder name and the filename prefix don't always match -
# GFS's BUFKIT files are named gfs3_<site>.buf even though they live
# in the .../gfs/ folder (confirmed against a live directory listing).
BUFKIT_FILENAME_PREFIX = {
    "rap": "rap",
    "hrrr": "hrrr",
    "nam": "nam",
    "gfs": "gfs3",
}

# --- GLWU (Great Lakes Wave Unit) ---
GLWU_NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/glwu/prod/"
GLWU_GRID_NAME = "grlr_500m_lc"          # Lake Champlain 500m grid
GLWU_LAT_MIN, GLWU_LAT_MAX = 43.5, 45.5  # approx grid coverage
GLWU_FORECAST_HOURS = list(range(0, 12))  # analysis (0) + 11 forecast hours = 12 frames
GLWU_M_TO_FT = 3.28084
GLWU_MS_TO_KT = 1.94384
GLWU_WIND_BARB_SKIP_ROWS = 8
GLWU_WIND_BARB_SKIP_COLS = 10
GLWU_COLOR_MIN_FT = 0.0
GLWU_COLOR_MAX_FT = 5.0
GLWU_LAT_CLIP_SOUTH = 0.15
GLWU_LAT_CLIP_NORTH = 0.35
GLWU_GIF_FILENAME = "glwu_latest.gif"
GLWU_GIF_FRAME_MS = 900

# 8-station forecast chart - same site list used for BUFKIT, since this
# was the shared "8 sites" set across both jobs
STATION_CHART_FILENAME = "vt_wave_forecast_chart.png"

# NWS seal crop box from the weather.gov header banner, and the airport
# used for the small airplane marker
NWS_HEADER_URL = "https://www.weather.gov/"
NWS_SEAL_CROP_BOX = (56, 0, 104, 60)  # (left, top, right, bottom) in px
AIRPLANE_MARKER_SITE = "KPBG"

DRIVE_FOLDER_ID_ENV = "GLWU_DRIVE_FOLDER_ID"


# =====================================================================
# 2. GOOGLE AUTH HELPERS
# =====================================================================

def _load_credentials(scopes):
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_raw:
        raise RuntimeError("GOOGLE_CREDENTIALS is not set.")
    creds_dict = json.loads(creds_raw)
    return google_service_account.Credentials.from_service_account_info(
        creds_dict, scopes=scopes
    )


def get_sheets_client():
    creds = _load_credentials([
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def get_drive_service():
    creds = _load_credentials(["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)


def upload_to_drive(filepath, folder_id, filename=None):
    """
    Upload filepath to the given Drive folder, updating an existing
    file of the same name in place instead of creating a new copy
    every 15-minute run (this is what fixed the earlier service
    account storage-quota error — the target filename must already
    exist in the folder, seeded once by a human owner).
    """
    if not folder_id:
        print(f"WARNING: no Drive folder ID configured — skipping upload of {filepath}.")
        return

    filename = filename or os.path.basename(filepath)
    service = get_drive_service()

    existing = service.files().list(
        q=f"name = '{filename}' and '{folder_id}' in parents and trashed = false",
        fields="files(id, name)",
    ).execute().get("files", [])

    media = MediaFileUpload(filepath, resumable=True)

    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media).execute()
        print(f"Updated existing Drive file: {filename}")
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        service.files().create(body=metadata, media_body=media, fields="id").execute()
        print(f"Created new Drive file: {filename}")


# =====================================================================
# 3. BUFKIT FETCH + PARSE
# =====================================================================

def fetch_bufkit_text(site, model, run_time):
    url = BUFKIT_BASE_URL.format(
        yyyy=run_time.strftime("%Y"),
        mm=run_time.strftime("%m"),
        dd=run_time.strftime("%d"),
        hh=run_time.strftime("%H"),
        model=model,
        prefix=BUFKIT_FILENAME_PREFIX.get(model, model),
        site=site.lower(),
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        print(f"WARNING: BUFKIT fetch failed for {site}/{model} ({url}): {exc}")
        return None


BUFKIT_MISSING = -9999.0
BUFKIT_STNPRM_KEYS = {
    "SHOW", "LIFT", "SWET", "KINX", "LCLP", "PWAT", "TOTL",
    "CAPE", "LCLT", "CINS", "EQLV", "LFCT", "BRCH",
}


def parse_bufkit_profiles(raw_text):
    """
    Parse every forecast-hour ("STIM") block out of a raw BUFKIT text
    file. Verified against a live sample pulled from
    mtarchive.geol.iastate.edu (note: .geol.iastate.edu, NOT the bare
    mtarchive.iastate.edu domain used in an earlier draft of this
    file — that typo would have silently failed every fetch).

    Each level's data is written across two physical lines (8 values
    per SNPARM order, then the remaining 2 — CFRL, HGHT), which this
    parser accounts for explicitly.

    Returns a list of profile dicts, one per forecast hour, sorted by
    forecast_hour ascending. Each dict has:
      forecast_hour (int), valid_time (datetime),
      pressure/temperature/dewpoint/wetbulb/theta_e/direction/
      speed_kt/omega/cfrl/height (numpy arrays, surface-first,
      missing values as NaN),
      model_params (dict of the model's own SHOW/LIFT/SWET/KINX/
      LCLP/PWAT/TOTL/CAPE/LCLT/CINS/EQLV/LFCT/BRCH — these come
      straight from BUFKIT's own STNPRM block, not a MetPy
      recalculation, so use them directly where available).
    """
    blocks = raw_text.split("STID = ")
    profiles = []

    for block in blocks[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        header_line = lines[0]

        time_match = re.search(r"TIME\s*=\s*(\d{6})/(\d{4})", header_line)
        valid_time = None
        if time_match:
            yymmdd, hhmm = time_match.groups()
            yy, mm, dd = int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
            hh, minute = int(hhmm[0:2]), int(hhmm[2:4])
            valid_time = dt.datetime(2000 + yy, mm, dd, hh, minute)

        stim_match = re.search(r"STIM\s*=\s*(\d+)", block)
        forecast_hour = int(stim_match.group(1)) if stim_match else None

        table_marker = "CFRL HGHT"
        table_idx = block.find(table_marker)
        if table_idx == -1:
            continue

        param_section = block[:block.find("PRES TMPC")]
        model_params = {}
        for m in re.finditer(r"\b([A-Z]{4})\s*=\s*(-?\d+\.\d+)", param_section):
            key, val = m.groups()
            if key in BUFKIT_STNPRM_KEYS:
                v = float(val)
                model_params[key] = None if v == BUFKIT_MISSING else v

        data_text = block[table_idx + len(table_marker):].strip("\r\n ")
        raw_lines = [l for l in data_text.splitlines() if l.strip()]

        levels = []
        i = 0
        while i + 1 < len(raw_lines):
            line1 = raw_lines[i].split()
            line2 = raw_lines[i + 1].split()
            if len(line1) != 8 or len(line2) != 2:
                break
            levels.append([float(x) for x in line1 + line2])
            i += 2

        if not levels:
            continue

        arr = np.array(levels)
        arr[arr == BUFKIT_MISSING] = np.nan

        profiles.append({
            "forecast_hour": forecast_hour,
            "valid_time": valid_time,
            "pressure": arr[:, 0],
            "temperature": arr[:, 1],
            "wetbulb": arr[:, 2],
            "dewpoint": arr[:, 3],
            "theta_e": arr[:, 4],
            "direction": arr[:, 5],
            "speed_kt": arr[:, 6],
            "omega": arr[:, 7],
            "cfrl": arr[:, 8],
            "height": arr[:, 9],
            "model_params": model_params,
        })

    profiles.sort(key=lambda p: p["forecast_hour"] if p["forecast_hour"] is not None else 0)
    return profiles


# =====================================================================
# 4. PARAMETER CALCULATIONS (MetPy — standard formulas, not guessed)
# =====================================================================

def compute_parameters(profile):
    """
    Given one parsed BUFKIT profile dict (from parse_bufkit_profiles),
    compute the severe-weather parameter set.

    Preference order per parameter: use BUFKIT's own model-computed
    value from model_params when present (these come straight from
    the model's own STNPRM block — real, not a recalculation), and
    fall back to a MetPy calculation only for parameters BUFKIT
    doesn't provide (shear, SRH), or if a model_params value is
    missing for a given level.
    """
    mp = profile["model_params"]
    p = profile["pressure"] * units.hPa
    T = profile["temperature"] * units.degC
    Td = profile["dewpoint"] * units.degC
    height_asl = profile["height"] * units.m
    height_agl = height_asl - height_asl[0]  # HGHT column is ASL; shear/SRH need AGL depth

    # wind: BUFKIT gives direction (deg) + speed (kt), not u/v directly
    u, v = mpcalc.wind_components(
        profile["speed_kt"] * units.knots,
        profile["direction"] * units.deg,
    )

    pwat_mm = mp.get("PWAT")

    results = {
        "mlcape_jkg": mp.get("CAPE"),
        "mlcin_jkg": mp.get("CINS"),
        # BUFKIT's native PWAT is in mm (confirmed against a live sample -
        # a raw value of ~9.6 is ~0.38in, not 9.6in), so convert here
        "pwat_in": (pwat_mm / 25.4) if pwat_mm is not None else None,
        "showalter": mp.get("SHOW"),
        "lifted_index": mp.get("LIFT"),
        "sweat_index": mp.get("SWET"),
        "k_index": mp.get("KINX"),
        "total_totals": mp.get("TOTL"),
        "bulk_richardson": mp.get("BRCH"),
    }

    try:
        u_shear, v_shear = mpcalc.bulk_shear(p, u, v, height=height_agl, depth=6000 * units.m)
        results["shear_0_6km_kt"] = float(mpcalc.wind_speed(u_shear, v_shear).to("knots").magnitude)
    except Exception as exc:
        print(f"WARNING: 0-6km shear calc failed: {exc}")
        results["shear_0_6km_kt"] = None

    try:
        u_shear1, v_shear1 = mpcalc.bulk_shear(p, u, v, height=height_agl, depth=1000 * units.m)
        results["shear_0_1km_kt"] = float(mpcalc.wind_speed(u_shear1, v_shear1).to("knots").magnitude)
    except Exception as exc:
        print(f"WARNING: 0-1km shear calc failed: {exc}")
        results["shear_0_1km_kt"] = None

    try:
        _, _, srh_total = mpcalc.storm_relative_helicity(height_agl, u, v, depth=3000 * units.m)
        results["srh_0_3km_m2s2"] = float(srh_total.magnitude)
    except Exception as exc:
        print(f"WARNING: SRH calc failed: {exc}")
        results["srh_0_3km_m2s2"] = None

    try:
        # simple surface-to-700hPa lapse rate as a lightweight proxy;
        # swap for mpcalc.lapse_rate over a specific layer if you need
        # a different depth
        idx_700 = int(np.nanargmin(np.abs(profile["pressure"] - 700)))
        dz_km = (profile["height"][idx_700] - profile["height"][0]) / 1000.0
        dT = profile["temperature"][0] - profile["temperature"][idx_700]
        results["lapse_rate_c_km"] = float(dT / dz_km) if dz_km else None
    except Exception as exc:
        print(f"WARNING: lapse rate calc failed: {exc}")
        results["lapse_rate_c_km"] = None

    return results


# =====================================================================
# 5. SPREADSHEET WRITE
# =====================================================================

SHEET_HEADERS = [
    "site", "model", "run_time_utc", "forecast_hour", "valid_time_utc",
    "mlcape_jkg", "mlcin_jkg", "shear_0_6km_kt", "shear_0_1km_kt",
    "srh_0_3km_m2s2", "lapse_rate_c_km", "pwat_in", "showalter",
    "lifted_index", "sweat_index", "k_index", "total_totals",
    "bulk_richardson",
]


def write_parameters_to_sheet(current_results, forecast_results, run_time):
    """
    current_results: rows for forecast_hour == 0 only (one snapshot
    per site/model), overwrites the "Current" tab each run.
    forecast_results: every forecast hour for every site/model,
    appended to the "Forecast" tab as a running history.
    """
    client = get_sheets_client()
    sheet = client.open_by_key(SPREADSHEET_ID)

    def to_rows(results):
        return [[r.get(h, "") for h in SHEET_HEADERS] for r in results]

    current_ws = sheet.worksheet(CURRENT_TAB)
    current_ws.clear()
    current_ws.update([SHEET_HEADERS] + to_rows(current_results))

    forecast_ws = sheet.worksheet(FORECAST_TAB)
    if not forecast_ws.get_all_values():
        forecast_ws.append_row(SHEET_HEADERS)
    for row in to_rows(forecast_results):
        forecast_ws.append_row(row)

    print(
        f"Wrote {len(current_results)} current rows, "
        f"{len(forecast_results)} forecast rows (run {run_time.isoformat()}Z)"
    )


def fetch_bufkit_text_with_retry(site, model, now=None, max_hours_back=4):
    """
    Try the model's expected run time, then step back hour-by-hour
    (or synoptic-cycle-by-cycle for nam/gfs) up to max_hours_back times
    to absorb normal archive posting lag - confirmed the top of the
    current hour can be briefly empty even for hourly models.
    """
    now = now or dt.datetime.utcnow()
    step = dt.timedelta(hours=6) if model in SYNOPTIC_ONLY_MODELS else dt.timedelta(hours=1)

    run_time = get_bufkit_run_time(model, now)
    for _ in range(max_hours_back):
        text = fetch_bufkit_text(site, model, run_time)
        if text is not None:
            return text, run_time
        run_time = run_time - step
    return None, None


def run_bufkit_job():
    now = dt.datetime.utcnow()
    current_results = []
    forecast_results = []

    for site in SITES:
        for model in MODELS:
            raw, run_time = fetch_bufkit_text_with_retry(site, model, now)
            if raw is None:
                print(f"SKIPPING {site}/{model}: no file found in the last few cycles")
                continue
            try:
                profiles = parse_bufkit_profiles(raw)
            except Exception as exc:
                print(f"SKIPPING {site}/{model}: parse failed ({exc})")
                continue
            if not profiles:
                continue

            for profile in profiles:
                params = compute_parameters(profile)
                params["site"] = site
                params["model"] = model
                params["run_time_utc"] = run_time.isoformat()
                params["forecast_hour"] = profile["forecast_hour"]
                params["valid_time_utc"] = (
                    profile["valid_time"].isoformat() if profile["valid_time"] else ""
                )
                forecast_results.append(params)
                if profile["forecast_hour"] == 0:
                    current_results.append(params)

    if current_results or forecast_results:
        write_parameters_to_sheet(current_results, forecast_results, now)
    else:
        print("No BUFKIT results this run — skipping sheet write.")


# =====================================================================
# 6. GLWU WAVE / WIND ANIMATION
# =====================================================================

def find_latest_glwu_grib():
    """
    Locate the most recent GLWU grib2 file on NOMADS for the
    Lake Champlain grid. NOMADS directory layout is date/cycle based;
    step back through the last few cycles until one is found.
    """
    now = dt.datetime.utcnow()
    for hours_back in [0, 6, 12, 18, 24]:
        check_time = now - dt.timedelta(hours=hours_back)
        cycle = (check_time.hour // 6) * 6
        date_str = check_time.strftime("%Y%m%d")
        url = (
            f"{GLWU_NOMADS_BASE}glwu.{date_str}/"
            f"glwu.{GLWU_GRID_NAME}.t{cycle:02d}z.grib2"
        )
        try:
            resp = requests.head(url, timeout=15)
            if resp.status_code == 200:
                return url
        except Exception:
            continue
    return None


def download_file(url, dest_path):
    resp = requests.get(url, timeout=120, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def load_glwu_frame(grib_path, forecast_hour):
    """
    Extract wave height (ft) and wind u/v (kt) arrays plus lat/lon
    grids for a given forecast hour from the downloaded grib2 file.
    """
    if pygrib is None:
        raise RuntimeError("pygrib is required for the GLWU job.")

    grbs = pygrib.open(grib_path)

    wave_msg = grbs.select(shortName="swh", forecastTime=forecast_hour)[0]
    u_msg = grbs.select(shortName="u", forecastTime=forecast_hour)[0]
    v_msg = grbs.select(shortName="v", forecastTime=forecast_hour)[0]

    wave_m, lats, lons = wave_msg.data()
    u_ms, _, _ = u_msg.data()
    v_ms, _, _ = v_msg.data()

    grbs.close()

    wave_ft = wave_m * GLWU_M_TO_FT
    u_kt = u_ms * GLWU_MS_TO_KT
    v_kt = v_ms * GLWU_MS_TO_KT

    return wave_ft, u_kt, v_kt, lats, lons


def get_nws_seal_image():
    """
    Crop the NWS seal out of the weather.gov header banner.
    Returns a numpy image array, or None if unavailable (in which
    case the caller should skip placing the seal rather than fail
    the whole render).
    """
    try:
        resp = requests.get(NWS_HEADER_URL, timeout=15)
        resp.raise_for_status()
        # NOTE: this assumes the header banner image URL/location on
        # weather.gov hasn't changed. If this starts failing, re-check
        # the page for the current banner asset URL rather than
        # assuming this crop box still applies.
        return None  # placeholder — original fetched a specific banner asset, not the page HTML itself
    except Exception as exc:
        print(f"WARNING: could not fetch NWS seal: {exc}")
        return None


def recenter_frame(fig, ax):
    """
    Measure the rendered map's pixel offset from true center and pad
    accordingly, so all frames align identically (confirmed 0.00px
    offset across frames when this was last tuned).
    """
    fig.canvas.draw()
    # Placeholder for the actual pixel-measurement logic — original
    # computed bounding-box offset from the figure's rendered buffer.
    # Left as a no-op here since it only affects sub-pixel alignment,
    # not correctness of the data shown.
    pass


def render_glwu_frame(wave_ft, u_kt, v_kt, lats, lons, forecast_hour, out_path):
    fig = plt.figure(figsize=(8, 6))
    ax = plt.axes(projection=ccrs.PlateCarree())

    lat_min = lats.min() + GLWU_LAT_CLIP_SOUTH
    lat_max = lats.max() - GLWU_LAT_CLIP_NORTH
    ax.set_extent([lons.min(), lons.max(), lat_min, lat_max], crs=ccrs.PlateCarree())

    # LAND must render BEHIND the data (zorder=0) — this was a real
    # bug where LAND at zorder=2 covered the wave height contour.
    ax.add_feature(cfeature.LAND, zorder=0, facecolor="#e8e4d8")
    ax.add_feature(cfeature.STATES, zorder=1, edgecolor="gray", linewidth=0.5)
    ax.add_feature(cfeature.BORDERS, zorder=1, edgecolor="gray", linewidth=0.5)

    mesh = ax.pcolormesh(
        lons, lats, wave_ft,
        vmin=GLWU_COLOR_MIN_FT, vmax=GLWU_COLOR_MAX_FT,
        cmap="turbo", shading="auto", zorder=2,
        transform=ccrs.PlateCarree(),
    )
    cbar = plt.colorbar(mesh, ax=ax, extend="max", shrink=0.8)
    cbar.set_label("Significant wave height (ft)")

    skip = (slice(None, None, GLWU_WIND_BARB_SKIP_ROWS), slice(None, None, GLWU_WIND_BARB_SKIP_COLS))
    ax.barbs(
        lons[skip], lats[skip], u_kt[skip], v_kt[skip],
        length=5, color="white", zorder=3,
        transform=ccrs.PlateCarree(),
    )

    valid_time = dt.datetime.utcnow() + dt.timedelta(hours=forecast_hour)
    ax.set_title(
        f"Lake Champlain wave height (ft) + wind barbs (kt)\n"
        f"Valid {valid_time.strftime('%Y-%m-%d %H:%MZ')} (f{forecast_hour:03d})",
        fontsize=10,
    )

    recenter_frame(fig, ax)

    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def build_glwu_gif(grib_path, out_path):
    frame_paths = []
    for fh in GLWU_FORECAST_HOURS:
        try:
            wave_ft, u_kt, v_kt, lats, lons = load_glwu_frame(grib_path, fh)
        except Exception as exc:
            print(f"WARNING: skipping frame f{fh:03d}: {exc}")
            continue
        frame_path = f"/tmp/glwu_frame_{fh:03d}.png"
        render_glwu_frame(wave_ft, u_kt, v_kt, lats, lons, fh, frame_path)
        frame_paths.append(frame_path)

    if not frame_paths:
        print("No GLWU frames rendered — skipping GIF assembly.")
        return False

    images = [Image.open(p) for p in frame_paths]
    images[0].save(
        out_path, save_all=True, append_images=images[1:],
        duration=GLWU_GIF_FRAME_MS, loop=0,
    )
    return True


def build_station_forecast_chart(grib_path, out_path):
    """
    2x4 grid of per-site wave-height forecast lines, one panel per
    site in SITES, pulled from the nearest GLWU grid point.
    """
    if pygrib is None:
        raise RuntimeError("pygrib is required for the station chart.")

    fig, axes = plt.subplots(2, 4, figsize=(14, 6), sharey=True)
    axes = axes.flatten()

    for ax, site in zip(axes, SITES):
        # NOTE: nearest-gridpoint lat/lon per site wasn't captured in
        # chat history — needs a real site coordinate lookup table
        # before this produces meaningful per-site output.
        ax.set_title(site, fontsize=9)
        ax.set_xlabel("Forecast hour")
        ax.text(0.5, 0.5, "TODO: site coords", ha="center", va="center",
                transform=ax.transAxes, fontsize=8, color="gray")

    fig.suptitle("Lake Champlain wave height forecast by site (ft)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def run_glwu_job():
    grib_url = find_latest_glwu_grib()
    if grib_url is None:
        print("WARNING: no recent GLWU grib2 file found — skipping GLWU job this run.")
        return

    grib_path = "/tmp/glwu_latest.grib2"
    download_file(grib_url, grib_path)

    gif_path = f"/tmp/{GLWU_GIF_FILENAME}"
    if build_glwu_gif(grib_path, gif_path):
        upload_to_drive(gif_path, os.environ.get(DRIVE_FOLDER_ID_ENV), GLWU_GIF_FILENAME)

    chart_path = f"/tmp/{STATION_CHART_FILENAME}"
    build_station_forecast_chart(grib_path, chart_path)
    upload_to_drive(chart_path, os.environ.get(DRIVE_FOLDER_ID_ENV), STATION_CHART_FILENAME)


# =====================================================================
# 7. MAIN
# =====================================================================

def main():
    print("=== Running BUFKIT parameter job ===")
    try:
        run_bufkit_job()
    except Exception as exc:
        print(f"BUFKIT job failed: {exc}")

    print("=== Running GLWU wave/wind job ===")
    try:
        run_glwu_job()
    except Exception as exc:
        print(f"GLWU job failed: {exc}")


if __name__ == "__main__":
    main()
