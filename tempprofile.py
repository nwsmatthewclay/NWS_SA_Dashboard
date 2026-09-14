"""
tempprofile.py
==============
Generates the Mount Mansfield low-level "pseudo sounding" Skew-T chart
for the winter recreation dashboard (matthewclay88/severe-dashboard).

Purpose
-------
Builds a shallow, low-level temperature/wind profile for the Mount
Mansfield summit area, used to:
  - Determine the rain/snow line at higher elevations
  - Identify warm layers aloft that could cause freezing rain
  - Flag mountain-wave / turbulence potential via a Froude-number
    check and a critical-level scan

Output
------
outputs/vt_pseudo_sounding.png — committed to the repo by the GitHub
Actions workflow (see workflow.yml), then served to the dashboard via
raw.githubusercontent.com. This script does NOT push to Drive or Git
itself — it only writes the PNG locally; the workflow's "Commit
dashboard outputs" step handles getting it into the repo.

Runs with NO Google credentials required — every data source here is
a public NWS/IEM endpoint.

NOTE ON THIS REBUILD
---------------------
This file was reconstructed from chat history after a GitHub account
suspension wiped the original repo. The overall structure, the locked
figure-size fix, the data endpoints, and the plotting approach are all
confirmed from prior sessions. However, a few pieces of exact
tuning were arrived at through trial-and-error against live data and
could not be recovered byte-for-exact:
  - The precise mountain-wave "critical level" scoring thresholds
  - The exact pressure/temperature padding constants for the Skew-T
    y/x limits (values below are reasonable starting points, not
    necessarily the final tuned numbers)
Treat first-run output as a "close but verify" starting point, not a
guaranteed match to the old chart.
"""

import os
import io
import re
import json
import datetime as dt

import requests
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from metpy.plots import SkewT
from metpy.units import units
import metpy.calc as mpcalc

# =====================================================================
# 1. CONSTANTS
# =====================================================================

OUTPUT_DIR = "outputs"
OUTPUT_FILE = "vt_pseudo_sounding.png"

# Locked figure dimensions - DO NOT make these dependent on the data's
# pressure/temperature range. That was the root cause of a layout bug
# where the chart's container kept resizing day-to-day based on live
# data spread. These are fixed constants on purpose.
SKEW_WIDTH_IN = 5.5
SKEW_HEIGHT_IN = 3.0

# Mount Mansfield summit station
MANSFIELD_STATION_ID = "MMNV1"
MANSFIELD_ELEV_FT = 3891

# BUFKIT gives the full tropospheric profile (surface to ~12 hPa);
# this script only wants the shallow LOW-LEVEL layer relevant to
# rain/snow line and freezing-rain analysis. Trim to this height (AGL,
# meters) before computing y-limits or plotting - without this, a full
# profile blows the Skew-T's pressure axis out to near-zero/negative
# values (caught in testing against a live sample).
LOW_LEVEL_MAX_HEIGHT_M = 3500

# NWS RRS SHEF text product (wind/temp obs) via IEM AFOS
RRS_AFOS_URL = (
    "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json"
    "?pil=RRSBTV&limit=1"
)

# BUFKIT RAP model sounding, fetched via IEM's mtarchive for the
# BTV-area grid point closest to Mansfield (no BUFKIT point exists
# for the summit itself, so KBTV is used as the nearest available
# site). NOTE: the correct host is mtarchive.geol.iastate.edu — an
# earlier draft of this file used mtarchive.iastate.edu (missing
# ".geol"), which resolves nowhere and would have silently failed
# every fetch. Verified against a live file at this exact URL pattern.
BUFKIT_BASE_URL = "https://mtarchive.geol.iastate.edu/{yyyy}/{mm}/{dd}/bufkit/{hh}/rap/rap_{site}.buf"
BUFKIT_SITE = "kbtv"

# Wind units: mph throughout (confirmed correction from an earlier
# knots version)
MS_TO_MPH = 2.23694

# Card accent colors (left-border style, matches plot_dashboard_card())
COLOR_PTYPE = "#1c7ed6"
COLOR_FROUDE = "#2f9e6e"
COLOR_FREEZING = "#d9822b"
COLOR_SHEAR = "#7048a3"


# =====================================================================
# 2. DATA FETCHING
# =====================================================================

BUFKIT_MISSING = -9999.0
BUFKIT_STNPRM_KEYS = {
    "SHOW", "LIFT", "SWET", "KINX", "LCLP", "PWAT", "TOTL",
    "CAPE", "LCLT", "CINS", "EQLV", "LFCT", "BRCH",
}


def parse_bufkit_profiles(raw_text):
    """
    Parse every forecast-hour ("STIM") block out of a raw BUFKIT text
    file. Verified against a live sample from mtarchive.geol.iastate.edu.

    Each level's data is written across two physical lines (8 values
    in SNPARM order, then the remaining 2 — CFRL, HGHT).

    Returns a list of profile dicts sorted by forecast_hour ascending;
    each has pressure/temperature/dewpoint/direction/speed_kt/height
    (numpy arrays, surface-first, missing values as NaN) plus
    model_params (BUFKIT's own SHOW/LIFT/SWET/KINX/PWAT/CAPE/CINS/etc,
    computed by the model itself — use these directly, they're not a
    MetPy recalculation).
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


def trim_to_low_level(profile):
    """
    Cut a full-depth BUFKIT profile down to just the levels within
    LOW_LEVEL_MAX_HEIGHT_M of the surface. Always keeps at least the
    first 3 levels even if the height field is malformed, so downstream
    code has enough points to plot rather than crashing outright.
    """
    height_agl = profile["height"] - profile["height"][0]
    mask = height_agl <= LOW_LEVEL_MAX_HEIGHT_M
    if mask.sum() < 3:
        mask = np.zeros_like(mask, dtype=bool)
        mask[:min(3, len(mask))] = True

    trimmed = dict(profile)
    for key in ("pressure", "temperature", "wetbulb", "dewpoint", "theta_e",
                "direction", "speed_kt", "omega", "cfrl", "height"):
        trimmed[key] = profile[key][mask]
    return trimmed


def fetch_bufkit_profile():
    """
    Fetch the most recent RAP BUFKIT sounding for KBTV (nearest
    available BUFKIT point to Mount Mansfield) and return the
    surface-hour (forecast_hour == 0) profile dict.

    Returns None if the archive request or parse fails — caller
    should fall back to the previous cached PNG rather than crash
    the workflow.
    """
    now = dt.datetime.utcnow()
    # RAP runs hourly; step back a couple hours to make sure the
    # archive has posted the file before we ask for it.
    run_time = now - dt.timedelta(hours=2)
    url = BUFKIT_BASE_URL.format(
        yyyy=run_time.strftime("%Y"),
        mm=run_time.strftime("%m"),
        dd=run_time.strftime("%d"),
        hh=run_time.strftime("%H"),
        site=BUFKIT_SITE,
    )

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"WARNING: BUFKIT fetch failed ({url}): {exc}")
        return None

    try:
        profiles = parse_bufkit_profiles(resp.text)
    except Exception as exc:
        print(f"WARNING: BUFKIT parse failed: {exc}")
        return None

    for p in profiles:
        if p["forecast_hour"] == 0:
            return p

    return profiles[0] if profiles else None


# =====================================================================
# 3. DERIVED PARAMETERS
# =====================================================================

def compute_froude_number(wind_speed_ms, brunt_vaisala_freq, barrier_height_m):
    """
    Froude number = U / (N * h)
    U = characteristic wind speed (m/s)
    N = Brunt-Vaisala frequency (1/s)
    h = barrier (mountain) height (m)

    Fr < 1 favors blocked/wave flow (higher turbulence risk);
    Fr > 1 favors flow-over.
    """
    if brunt_vaisala_freq <= 0 or barrier_height_m <= 0:
        return None
    return wind_speed_ms / (brunt_vaisala_freq * barrier_height_m)


def find_critical_level(heights_m, wind_dir_deg, target_dir_deg=270, tolerance_deg=30):
    """
    Scan a profile for a "critical level" - the height at which wind
    direction backs/veers through the barrier-perpendicular direction,
    a classic mountain-wave breaking indicator.

    Returns the height (m) of the first critical level found, or None.
    """
    for h, wd in zip(heights_m, wind_dir_deg):
        diff = abs(((wd - target_dir_deg) + 180) % 360 - 180)
        if diff <= tolerance_deg:
            return h
    return None


def classify_precip_type(surface_temp_f, freezing_level_ft, warm_layer_present):
    """
    Simple P-Type classifier based on surface temp and freezing level
    height relative to the summit. Refine thresholds against real
    cases - these are reasonable defaults, not the original's exact
    tuned cutoffs.
    """
    if surface_temp_f >= 34:
        return "Rain"
    if warm_layer_present:
        return "Freezing Rain" if surface_temp_f <= 32 else "Rain"
    if freezing_level_ft < MANSFIELD_ELEV_FT:
        return "Snow"
    return "Mixed"


# =====================================================================
# 4. PLOTTING
# =====================================================================

def plot_dashboard_card(ax, title, value_text, color, x, y, w, h):
    """
    Draw a single stat card (4px colored left border, bold value,
    gray subtitle) at the given axes-fraction position.
    """
    ax.add_patch(
        plt.Rectangle(
            (x, y), w, h,
            transform=ax.transAxes,
            facecolor="white",
            edgecolor="none",
            zorder=1,
        )
    )
    ax.add_patch(
        plt.Rectangle(
            (x, y), 0.006, h,
            transform=ax.transAxes,
            facecolor=color,
            edgecolor="none",
            zorder=2,
        )
    )
    ax.text(
        x + 0.02, y + h * 0.7, title,
        transform=ax.transAxes, fontsize=7, color="#666666", zorder=3,
    )
    ax.text(
        x + 0.02, y + h * 0.25, value_text,
        transform=ax.transAxes, fontsize=12, fontweight="bold",
        color="#111111", zorder=3,
    )


def plot_pseudo_sounding(profile, params, out_path):
    """
    Render the Skew-T pseudo sounding with the four stat cards
    (P-Type, Froude, Freezing Level, Shear) beneath it.

    `profile` - dict with pressure/temp/dewpoint/u/v arrays
    `params` - dict with computed p_type, froude, freezing_level_ft,
               shear_kt, critical_level_ft
    """
    fig = plt.figure(figsize=(SKEW_WIDTH_IN, SKEW_HEIGHT_IN + 1.2))

    skew = SkewT(fig, rotation=45, rect=(0.1, 0.35, 0.85, 0.6))

    pressure = profile["pressure"] * units.hPa
    temperature = profile["temperature"] * units.degC
    dewpoint = profile["dewpoint"] * units.degC
    u_wind, v_wind = mpcalc.wind_components(
        profile["speed_kt"] * units.knots,
        profile["direction"] * units.deg,
    )

    skew.plot(pressure, temperature, "r")
    skew.plot(pressure, dewpoint, "g")
    skew.plot_barbs(pressure[::3], u_wind[::3], v_wind[::3])

    p_max, p_min = float(np.max(profile["pressure"])), float(np.min(profile["pressure"]))
    observed_range = max(p_max - p_min, 10.0)
    bottom_pressure = p_max + max(0.5 * observed_range, 12.0)
    top_pressure = p_min - max(1.0 * observed_range, 25.0)
    skew.ax.set_ylim(bottom_pressure, top_pressure)

    skew.ax.set_xlabel("Temperature (\u00b0C)", fontsize=8)
    skew.ax.set_ylabel("Pressure (hPa)", fontsize=8)
    skew.ax.tick_params(labelsize=7)

    fig.suptitle(
        f"Mount Mansfield Low-Level Profile — "
        f"{dt.datetime.utcnow().strftime('%Y-%m-%d %H:%MZ')}",
        fontsize=10, fontweight="bold", y=0.98,
    )

    card_ax = fig.add_axes([0, 0, 1, 0.30])
    card_ax.axis("off")

    plot_dashboard_card(
        card_ax, "P-TYPE", params.get("p_type", "\u2014"),
        COLOR_PTYPE, 0.02, 0.05, 0.22, 0.85,
    )
    plot_dashboard_card(
        card_ax, "FROUDE #",
        f'{params.get("froude"):.2f}' if params.get("froude") is not None else "\u2014",
        COLOR_FROUDE, 0.27, 0.05, 0.22, 0.85,
    )
    plot_dashboard_card(
        card_ax, "FREEZING LEVEL",
        f'{params.get("freezing_level_ft", 0):,.0f} ft',
        COLOR_FREEZING, 0.52, 0.05, 0.22, 0.85,
    )
    plot_dashboard_card(
        card_ax, "0-1KM SHEAR",
        f'{params.get("shear_kt", 0):.0f} kt',
        COLOR_SHEAR, 0.77, 0.05, 0.21, 0.85,
    )

    fig.savefig(out_path, dpi=150, bbox_inches=None)
    plt.close(fig)


# =====================================================================
# 5. MAIN
# =====================================================================

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)

    profile = fetch_bufkit_profile()
    if profile is None:
        print("No profile data available this run — leaving previous PNG in place.")
        return

    profile = trim_to_low_level(profile)
    if len(profile["pressure"]) < 3:
        print("Too few low-level points after trimming — leaving previous PNG in place.")
        return

    surface_temp_f = profile["temperature"][0] * 9 / 5 + 32
    height_agl_m = profile["height"] - profile["height"][0]

    # Freezing level: first height (AGL, converted to ft) where temp
    # crosses from above 0C to at/below 0C, scanning upward
    freezing_level_ft = None
    for i in range(len(profile["temperature"]) - 1):
        t0, t1 = profile["temperature"][i], profile["temperature"][i + 1]
        if np.isnan(t0) or np.isnan(t1):
            continue
        if t0 > 0 >= t1:
            frac = t0 / (t0 - t1) if (t0 - t1) != 0 else 0
            h_m = height_agl_m[i] + frac * (height_agl_m[i + 1] - height_agl_m[i])
            freezing_level_ft = float(h_m * 3.28084)
            break
    if freezing_level_ft is None:
        # never crosses freezing in this profile - either all warm or all cold
        freezing_level_ft = 0.0 if profile["temperature"][0] <= 0 else float(height_agl_m[-1] * 3.28084)

    # warm layer aloft: any level > 0C above a surface that's <= 0C
    warm_layer_present = bool(
        profile["temperature"][0] <= 0 and np.nanmax(profile["temperature"]) > 0
    )

    # 0-1km shear, using BUFKIT's own direction/speed columns
    try:
        u, v = mpcalc.wind_components(profile["speed_kt"] * units.knots, profile["direction"] * units.deg)
        u_shear, v_shear = mpcalc.bulk_shear(
            profile["pressure"] * units.hPa, u, v,
            height=height_agl_m * units.m, depth=1000 * units.m,
        )
        shear_kt = float(mpcalc.wind_speed(u_shear, v_shear).to("knots").magnitude)
    except Exception as exc:
        print(f"WARNING: shear calc failed: {exc}")
        shear_kt = 0.0

    # Froude number: needs a bulk wind speed, a stability (Brunt-Vaisala)
    # estimate, and the barrier height. This is a simplified single-layer
    # estimate over the lowest ~1km - refine against real cases if the
    # mountain-wave flagging needs to be more precise.
    try:
        surface_speed_ms = float(profile["speed_kt"][0]) * 0.514444
        dtheta_dz = (profile["temperature"][1] - profile["temperature"][0]) / max(
            height_agl_m[1] - height_agl_m[0], 1.0
        )
        n_squared = max((9.81 / (profile["temperature"][0] + 273.15)) * (dtheta_dz + 0.0098), 1e-6)
        brunt_vaisala = n_squared ** 0.5
        froude = compute_froude_number(surface_speed_ms, brunt_vaisala, MANSFIELD_ELEV_FT * 0.3048)
    except Exception as exc:
        print(f"WARNING: Froude calc failed: {exc}")
        froude = None

    critical_level_m = find_critical_level(height_agl_m, profile["direction"])

    params = {
        "p_type": classify_precip_type(surface_temp_f, freezing_level_ft, warm_layer_present),
        "froude": froude,
        "freezing_level_ft": freezing_level_ft,
        "shear_kt": shear_kt,
        "critical_level_ft": (critical_level_m * 3.28084) if critical_level_m is not None else None,
    }

    plot_pseudo_sounding(profile, params, out_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
