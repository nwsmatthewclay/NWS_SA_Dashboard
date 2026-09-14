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

# NWS RRS SHEF text product (wind/temp obs) via IEM AFOS
RRS_AFOS_URL = (
    "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json"
    "?pil=RRSBTV&limit=1"
)

# BUFKIT RAP model sounding, fetched via IEM's mtarchive for the
# BTV-area grid point closest to Mansfield. Adjust `bufkit_site` if
# the original used a different site identifier.
BUFKIT_BASE_URL = "https://mtarchive.iastate.edu/{yyyy}/{mm}/{dd}/bufkit/{hh}/rap/rap_{site}.buf"
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

def fetch_latest_rrs_obs():
    """
    Pull the most recent RRSBTV SHEF text product via IEM's AFOS API
    and parse out the Mount Mansfield (MMNV1) temperature/wind line.

    Returns a dict: {"temp_f": float, "wind_mph": float,
    "wind_dir": int, "valid": datetime} or None if unavailable.
    """
    try:
        resp = requests.get(RRS_AFOS_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        products = data.get("data", [])
        if not products:
            return None

        # IEM's AFOS list can return multiple pre-blocks with the same
        # timestamp for this product - the real content has, in past
        # sessions, shown up split across blocks. Concatenate all of
        # them defensively rather than trusting the last block alone.
        raw_text = "".join(p.get("data", "") for p in products)

        return _parse_rrs_text(raw_text)
    except Exception as exc:
        print(f"WARNING: could not fetch RRS obs: {exc}")
        return None


def _parse_rrs_text(raw_text):
    """
    Parse SHEF-encoded text for the Mansfield station line.
    SHEF fields of interest: TAIRGZZ (air temp), UDIRG (wind dir),
    UPERG (wind speed peak), USIRG (wind speed sustained).

    This parser is intentionally defensive - SHEF text formatting
    varies run to run. Extend the regex/section matching here once
    you have a live sample to test against.
    """
    result = {"temp_f": None, "wind_mph": None, "wind_dir": None, "valid": None}

    for line in raw_text.splitlines():
        if MANSFIELD_STATION_ID not in line.upper():
            continue
        # Placeholder parse logic - real SHEF decoding needs the
        # actual field layout from a live sample. Left explicit so
        # it fails loudly (returns None fields) rather than silently
        # guessing wrong numbers.
        pass

    return result


def fetch_bufkit_profile():
    """
    Fetch the most recent RAP BUFKIT sounding for the Mansfield-area
    grid point and return arrays of (pressure, temperature, dewpoint,
    u_wind, v_wind) suitable for metpy's SkewT.

    Returns None if the archive request fails (e.g. run not posted
    yet) - caller should fall back to the previous cached PNG rather
    than crash the workflow.
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

    # NOTE: actual BUFKIT text parsing (STN/STIM header, PRES/TMPC/
    # DWPC/DRCT/SKNT columns) was handled by a dedicated parser in the
    # original script. Re-implement using a BUFKIT parsing library
    # (e.g. `bufkit` or a hand-rolled column splitter) once you have a
    # sample file to test against - the column layout must match
    # exactly or the sounding will silently plot garbage.
    raise NotImplementedError(
        "BUFKIT text parsing needs to be re-implemented against a "
        "live sample file - see function docstring."
    )


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
    u_wind = profile["u"] * units("m/s")
    v_wind = profile["v"] * units("m/s")

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

    surface_temp_f = profile["temperature"][0] * 9 / 5 + 32
    freezing_level_ft = 0.0  # TODO: derive from profile once parsing is restored
    warm_layer_present = False  # TODO: scan profile for T > 0C aloft over a sub-freezing surface

    params = {
        "p_type": classify_precip_type(surface_temp_f, freezing_level_ft, warm_layer_present),
        "froude": None,  # TODO: wire compute_froude_number() once N and U are derived
        "freezing_level_ft": freezing_level_ft,
        "shear_kt": 0.0,  # TODO: derive 0-1km shear from u/v arrays
        "critical_level_ft": None,
    }

    plot_pseudo_sounding(profile, params, out_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
