"""
Mount Mansfield Snow Depth
----------------------------------

Two independent data sources, kept separate on purpose:

1. CHART (Max/Min/Avg/Current-season lines) - the 4 NWS BTV feeds,
   maintained by NWS, matching the season chart on
   https://www.weather.gov/btv/recreation.

2. CURRENT DEPTH - MMNV1 Mount Mansfield COOP observation via IEM.
   This is the live/current snow-depth source used for the status card.

3. HISTORICAL DEPTH / NORMAL / RANK - committed snow-depth.csv.
   The CSV is the single source of truth for the historical comparison,
   including the Average Season normal and season-by-season records.

Source format notes:
    - The NWS .xml files are not real XML - each is a thin
      <data><text>...</text></data> wrapper around a JS array literal
      of [Date.UTC(y,m,d), depth_inches] pairs. Date.UTC's month is
      0-indexed (0=Jan), unlike Python's date().month.
    - The committed snow-depth.csv is wide-format: one row per ski
      season ("2025-2026"), one column per day of the season
      ("9/1".."6/30" - it only tracks Sep-Jun), plus a final
      "Average Season" row with the climatological mean for each day.

Data sources:
    https://www.weather.gov/source/btv/rec/mmn/{current-season}depth.xml
    https://mesonet.agron.iastate.edu/cgi-bin/request/coopobs.py
    https://www.weather.gov/source/btv/rec/mmn/avgdepth.xml
    https://www.weather.gov/source/btv/rec/mmn/maxdepth.xml
    https://www.weather.gov/source/btv/rec/mmn/mindepth.xml
    https://s3.amazonaws.com/matthewparrilla.com/snow-depth.csv

Requirements:
    pip install requests matplotlib
"""

import csv
import io
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import requests

# ---- Chart data sources (NWS, unchanged) ----

CURRENT_URL_TEMPLATE = "https://www.weather.gov/source/btv/rec/mmn/{season}depth.xml"
AVERAGE_URL = "https://www.weather.gov/source/btv/rec/mmn/avgdepth.xml"
MAX_URL = "https://www.weather.gov/source/btv/rec/mmn/maxdepth.xml"
MIN_URL = "https://www.weather.gov/source/btv/rec/mmn/mindepth.xml"

# ---- Historical depth / normal data source (local CSV) ----
# The committed CSV is now the single source of truth for historical
# normal, record high, record low, and day-of-season ranking.
BASE_DIR = Path(__file__).resolve().parent
HISTORY_CSV_PATHS = [
    BASE_DIR / "data" / "snow-depth.csv",
    BASE_DIR / "snow-depth.csv",
]
AVERAGE_ROW_LABEL = "Average Season"

# ---- Current Mount Mansfield snow depth (IEM MMNV1 COOP) ----
# MMNV1 is the Mount Mansfield COOP station used by the profile.
IEM_COOPOBS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/coopobs.py"
MMNV1_STATION = "MMNV1"
IEM_LOOKBACK_DAYS = 14

HEADERS = {
    "User-Agent": (
        "MountMansfieldSnowDepth/1.0 (dashboard status card)"
    ),
}

REPO_OUTPUT_DIR = str(BASE_DIR / "outputs")

STATUS_OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_snow_depth_status.json")
CHART_OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_snow_depth_chart.png")


# =====================================================================
# CHART (NWS feeds - unchanged)
# =====================================================================

def fetch_depth_series(url):
    """
    Download one NWS depth-series file and parse it into a
    {date: depth_inches} dict.
    """

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    text = response.text

    match = re.search(r"<text>(.*)</text>", text, re.DOTALL)

    if not match:
        raise ValueError(f"Could not find <text> payload in {url}")

    inner = match.group(1)

    pairs = re.findall(
        r"Date\.UTC\((\d+),(\d+),(\d+)(?:,\d+)?\),\s*(-?\d+(?:\.\d+)?)",
        inner,
    )

    records = {}

    for year_str, month_str, day_str, value_str in pairs:

        year = int(year_str)
        month = int(month_str) + 1  # JS Date.UTC month is 0-indexed
        day = int(day_str)

        try:
            record_date = date(year, month, day)
        except ValueError:
            # A handful of source rows have used an invalid day (e.g.
            # day 31 in a 30-day month) - skip rather than guess.
            continue

        records[record_date] = float(value_str)

    return records


def plot_snow_depth_chart(current_series, average_series, max_series, min_series):
    """
    Render the Max/Min/Avg/Current-season depth lines as a single PNG,
    matching the season chart on weather.gov/btv/recreation.
    """

    def to_xy(series):
        dates = sorted(series.keys())
        return dates, [series[d] for d in dates]

    max_x, max_y = to_xy(max_series)
    min_x, min_y = to_xy(min_series)
    avg_x, avg_y = to_xy(average_series)
    cur_x, cur_y = to_xy(current_series)

    fig, ax = plt.subplots(figsize=(9.5, 3.2))

    ax.plot(max_x, max_y, color="#4dabf7", linewidth=1.5, label="Max Snow Depth")
    ax.plot(min_x, min_y, color="#5c5cd6", linewidth=1.5, label="Min Snow Depth")
    ax.plot(avg_x, avg_y, color="#37b24d", linewidth=1.5, label="Avg Snow Depth")
    ax.plot(cur_x, cur_y, color="#e03131", linewidth=2.5, label="Current Depth")

    ax.set_ylabel("Inches", fontsize=9)
    ax.set_ylim(bottom=0)

    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))

    ax.tick_params(labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e9ecef", linewidth=0.8)
    ax.set_axisbelow(True)

    ax.legend(
        loc="upper left", fontsize=7.5, frameon=False, ncol=4,
        bbox_to_anchor=(0.0, 1.18),
    )

    fig.tight_layout()

    plt.savefig(CHART_OUTPUT_FILE, dpi=175)
    plt.close(fig)

    print(f"Saved snow depth chart to: {CHART_OUTPUT_FILE}")


# =====================================================================
# HISTORICAL DEPTH / DEPARTURE (committed full-history CSV)
# =====================================================================

def locate_history_csv():
    """Return the first committed historical snow-depth CSV that exists."""

    for path in HISTORY_CSV_PATHS:
        if path.exists():
            return path

    searched = ", ".join(str(path) for path in HISTORY_CSV_PATHS)
    raise FileNotFoundError(
        f"Historical snow-depth CSV not found. Expected one of: {searched}"
    )


def fetch_snow_depth_history():
    """
    Load the committed full-history CSV: one row per ski season back to
    1954, one column per day of the season, plus the final ``Average Season``
    row. This is intentionally local/version-controlled rather than fetched
    from a remote copy so the dashboard's historical standing cannot change
    underneath the workflow.
    """

    csv_path = locate_history_csv()
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        rows = list(reader)

    if not rows or len(rows[0]) < 2:
        raise ValueError(f"Historical snow-depth CSV is empty or malformed: {csv_path}")

    day_labels = [label.strip() for label in rows[0][1:]]
    season_rows = {row[0].strip(): row[1:] for row in rows[1:] if row and row[0].strip()}

    if AVERAGE_ROW_LABEL not in season_rows:
        raise ValueError(f"Historical snow-depth CSV is missing '{AVERAGE_ROW_LABEL}': {csv_path}")

    return day_labels, season_rows


def season_label_for_date(d):
    """
    Ski-season label ("2025-2026") the given calendar date falls in.
    The dataset only tracks Sep 1 - Jun 30; returns None for Jul/Aug
    (the off-season gap between one season ending and the next
    starting - there's genuinely nothing to report, not just a gap
    in an otherwise-continuous series).
    """

    if d.month >= 9:
        return f"{d.year}-{d.year + 1}"

    if d.month <= 6:
        return f"{d.year - 1}-{d.year}"

    return None


def latest_reported_index(day_labels, values, as_of_label):
    """
    Index of the most recent column at or before `as_of_label` ("M/D")
    that has a non-blank value in `values`, walking backward to skip
    gaps in reporting (this dataset has real gaps - scattered blank
    cells, not just zeros).
    """

    start = day_labels.index(as_of_label) if as_of_label in day_labels else len(day_labels) - 1

    for i in range(start, -1, -1):

        if i < len(values) and values[i].strip() != "":
            return i

    return None


def rank_for_day(day_labels, season_rows, day_index, current_depth, exclude_season=None):
    """
    Where the current depth ranks among all historical seasons' depth
    on this same day-of-season (1 = deepest on record for this date).
    Returns (rank, total_seasons_compared, deepest_season_label) or
    (None, 0, None) if there's nothing to compare against.
    """

    comparisons = []

    for season, values in season_rows.items():

        if season == AVERAGE_ROW_LABEL or season == exclude_season:
            continue

        if day_index >= len(values):
            continue

        raw = values[day_index].strip()

        if raw == "":
            continue

        try:
            comparisons.append((season, float(raw)))
        except ValueError:
            continue

    if not comparisons:
        return None, 0, None, None, None

    comparisons.sort(key=lambda pair: pair[1], reverse=True)

    deepest_season = comparisons[0][0]
    record_high_in = comparisons[0][1]
    record_low_in = comparisons[-1][1]

    rank = 1

    for season, value in comparisons:

        if value > current_depth:
            rank += 1

    return rank, len(comparisons), deepest_season, record_high_in, record_low_in

# =====================================================================
# CURRENT DEPTH (IEM MMNV1 COOP)
# =====================================================================

def parse_iem_coop_csv(text):
    """Parse the IEM COOP CSV into rows."""

    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def fetch_current_mansfield_depth(as_of=None):
    """
    Get the latest reported snow depth from the MMNV1 Mount Mansfield COOP
    station. IEM publishes the raw COOP snow-depth observation as ``snowd``.

    We look back a short window because MMNV1 is a daily COOP observation,
    not a continuous automated snow-depth sensor. The latest usable MMNV1
    observation is therefore the correct live/current value available from
    that station.
    """

    as_of = as_of or datetime.now(timezone.utc).date()
    start_date = as_of - timedelta(days=IEM_LOOKBACK_DAYS)

    params = {
        "network": "VT_COOP",
        "stations": MMNV1_STATION,
        "sts": start_date.isoformat(),
        "ets": as_of.isoformat(),
        "what": "download",
        "delim": "comma",
    }

    try:
        response = requests.get(
            IEM_COOPOBS_URL, headers=HEADERS, params=params, timeout=30
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"IEM MMNV1 snow-depth fetch failed: {error}")
        return None

    rows = parse_iem_coop_csv(response.text)
    candidates = []

    for row in rows:
        if row.get("station") != MMNV1_STATION:
            continue

        raw_depth = (row.get("snowd") or row.get("snow_depth") or "").strip()
        if raw_depth in ("", "M", "m", "NA", "null"):
            continue

        try:
            depth = float(raw_depth)
        except ValueError:
            continue

        valid = row.get("valid", "")
        candidates.append((valid, depth, row))

    if not candidates:
        print("IEM MMNV1: no usable snow-depth observation found in lookback window.")
        return None

    candidates.sort(key=lambda item: item[0])
    valid, depth, row = candidates[-1]

    return {
        "depth_in": depth,
        "observed": valid,
        "source": "IEM VT_COOP MMNV1",
    }

def build_snow_depth_observation(as_of=None):
    """
    Current depth from live MMNV1 COOP observations + departure from normal
    and rank against
    70+ years of history (both only computable when the CSV has a
    day-of-season column to compare against, i.e. during the tracked
    Sep-Jun season).

    MMNV1 is attempted unconditionally, regardless of season - it is
    a live station observation and is independent of the CSV's season-only
    column range. Departure/normal/rank are the only pieces
    that gracefully degrade to None off-season, since those
    genuinely depend on a day-of-season column that doesn't exist
    for Jul/Aug.
    """

    as_of = as_of or datetime.now(timezone.utc).date()

    current_result = fetch_current_mansfield_depth(as_of)

    day_labels, season_rows = fetch_snow_depth_history()

    season_label = season_label_for_date(as_of)

    base_result = {
        "station": "Mount Mansfield Stake",
        "source": "MMNV1 (IEM) + committed snow-depth.csv",
        "season": season_label,
        "as_of_date": as_of.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # No CSV day-of-season column to compare against (Jul/Aug) - but
    # MMNV1 may still have a real live current-depth reading, so
    # surface that even though departure/normal/rank can't be
    # computed against a day-of-season column that doesn't exist.
    if season_label is None or season_label not in season_rows:

        if current_result is not None:

            return {
                **base_result,
                "observed_date_label": None,
                "current_observation": current_result["observed"],
                "current_depth_in": current_result["depth_in"],
                "current_depth_source": current_result["source"],
                "normal_depth_in": None,
                "record_high_in": None,
                "record_low_in": None,
                "departure_in": None,
                "departure_text": "Off-season \u2014 normal unavailable",
                "rank": None,
                "rank_of": None,
            }

        return {
            **base_result,
            "observed_date_label": None,
            "current_observation": None,
            "current_depth_in": None,
            "normal_depth_in": None,
            "record_high_in": None,
            "record_low_in": None,
            "departure_in": None,
            "departure_text": "Off-season \u2014 no current tracking",
            "rank": None,
            "rank_of": None,
        }

    as_of_label = f"{as_of.month}/{as_of.day}"

    current_values = season_rows[season_label]
    idx = latest_reported_index(day_labels, current_values, as_of_label)

    if idx is None and current_result is None:

        return {
            **base_result,
            "observed_date_label": None,
            "current_depth_in": None,
            "current_depth_source": None,
            "normal_depth_in": None,
            "record_high_in": None,
            "record_low_in": None,
            "departure_in": None,
            "departure_text": "No data reported yet this season",
            "rank": None,
            "rank_of": None,
        }

    # idx/obs_label are still needed (even when MMNV1 has the current
    # depth) to look up normal_depth_in and rank_for_day() against the
    # right day-of-season column. Fall back to the most recent
    # CSV-reported day if the historical CSV has nothing for today yet.
    if idx is None:
        idx = latest_reported_index(day_labels, current_values, day_labels[-1])

    obs_label = day_labels[idx] if idx is not None else None

    # The current value always comes from MMNV1. We do not fall back to the
    # historical/current-season CSV for the live depth because the CSV is
    # intentionally historical/climatological data for this product.
    if current_result is not None:
        current_depth = current_result["depth_in"]
        current_depth_source = current_result["source"]
        current_observation = current_result["observed"]
    else:
        current_depth = None
        current_depth_source = None
        current_observation = None

    average_values = season_rows.get(AVERAGE_ROW_LABEL, [])
    normal_depth = None

    if idx is not None and idx < len(average_values) and average_values[idx].strip() != "":
        normal_depth = float(average_values[idx])

    if normal_depth is None or current_depth is None:

        departure = None
        departure_text = "Normal unavailable" if current_depth is not None else "No data reported yet this season"

    else:

        departure = current_depth - normal_depth

        if abs(departure) < 0.5:
            departure_text = "Near normal"
        elif departure > 0:
            departure_text = f"+{departure:.0f} in above normal"
        else:
            departure_text = f"{departure:.0f} in below normal"

    if idx is not None and current_depth is not None:
        rank, rank_of, deepest_season, record_high_in, record_low_in = rank_for_day(
            day_labels, season_rows, idx, current_depth, exclude_season=season_label
        )
    else:
        rank, rank_of, deepest_season, record_high_in, record_low_in = None, None, None, None, None

    return {
        **base_result,
        "observed_date_label": obs_label,
        "current_observation": current_observation,
        "current_depth_in": current_depth,
        "current_depth_source": current_depth_source,
        "normal_depth_in": normal_depth,
        "record_high_in": record_high_in,
        "record_low_in": record_low_in,
        "departure_in": departure,
        "departure_text": departure_text,
        "rank": rank,
        "rank_of": rank_of,
        "deepest_season_on_this_date": deepest_season,
    }


# =====================================================================
# MAIN
# =====================================================================

def main():

    os.makedirs(REPO_OUTPUT_DIR, exist_ok=True)

    # Chart: NWS feeds. Current-season URL is generated from the run date.
    season = season_label_for_date(datetime.now(timezone.utc).date())
    current_url = CURRENT_URL_TEMPLATE.format(season=season) if season else None

    try:
        current_series = fetch_depth_series(current_url) if current_url else {}
    except requests.RequestException as error:
        print(f"Current-season NWS snow-depth chart feed unavailable: {error}")
        current_series = {}

    average_series = fetch_depth_series(AVERAGE_URL)
    max_series = fetch_depth_series(MAX_URL)
    min_series = fetch_depth_series(MIN_URL)

    plot_snow_depth_chart(current_series, average_series, max_series, min_series)

    # Current depth: live MMNV1. Historical normal/rank/records: committed CSV.

    status = build_snow_depth_observation()

    print(json.dumps(status, indent=2))

    with open(STATUS_OUTPUT_FILE, "w") as f:
        json.dump(status, f, indent=2)

    print(f"\nSaved status to {STATUS_OUTPUT_FILE}")


if __name__ == "__main__":
    main()
