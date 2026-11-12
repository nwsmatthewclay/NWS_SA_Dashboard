"""Unit-aware vertical-profile diagnostics for BTV Mesoscale Detective.

Input arrays must describe one collocated model column. This module does
not fetch data, guess missing levels, or silently fill missing values.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import metpy.calc as mpcalc
from metpy.units import units


def _finite_profile(pressure_hpa, temperature_k, dewpoint_k, height_m,
                    u_ms=None, v_ms=None):
    p = np.asarray(pressure_hpa, dtype=float)
    t = np.asarray(temperature_k, dtype=float)
    td = np.asarray(dewpoint_k, dtype=float)
    z = np.asarray(height_m, dtype=float)
    arrays = [p, t, td, z]
    if u_ms is not None:
        arrays.extend([np.asarray(u_ms, dtype=float), np.asarray(v_ms, dtype=float)])
    if any(a.ndim != 1 for a in arrays) or len({a.size for a in arrays}) != 1:
        raise ValueError("All profile arrays must be one-dimensional and equal length")
    mask = np.ones(p.shape, dtype=bool)
    for a in arrays:
        mask &= np.isfinite(a)
    if mask.sum() < 5:
        raise ValueError("At least five complete finite levels are required")
    arrays = [a[mask] for a in arrays]
    order = np.argsort(arrays[0])[::-1]
    arrays = [a[order] for a in arrays]
    if np.any(np.diff(arrays[0]) >= 0):
        raise ValueError("Pressure levels must be unique and strictly decrease upward")
    if np.any(np.diff(arrays[3]) <= 0):
        # Some model grids can have tiny height inversions from interpolation;
        # reject rather than silently sorting winds and thermodynamics separately.
        raise ValueError("Geopotential height must increase upward")
    return arrays


def _number(quantity, target_unit):
    try:
        value = quantity.to(target_unit).magnitude
        return float(np.asarray(value))
    except Exception:
        return None


def diagnose_profile(pressure_hpa, temperature_k, dewpoint_k, height_m,
                     u_ms=None, v_ms=None):
    """Return parcel, moisture, lapse-rate and wind diagnostics for a column.

    Height is metres AGL. Wind components are earth-relative m/s.
    All returned values are JSON-safe numbers or None, with a quality
    section documenting unavailable diagnostics.
    """
    has_wind = u_ms is not None and v_ms is not None
    arrays = _finite_profile(pressure_hpa, temperature_k, dewpoint_k, height_m,
                             u_ms if has_wind else None,
                             v_ms if has_wind else None)
    p, t, td, z = arrays[:4]
    out: dict[str, Any] = {
        "quality": {"profile_levels_used": int(len(p)), "wind_available": has_wind,
                    "warnings": []},
        "units": {"cape": "J kg-1", "pwat": "mm", "lcl": "m AGL",
                  "shear": "kt", "srh": "m2 s-2", "lapse_rate": "K km-1"},
        "diagnostics": {},
    }
    pq = p * units.hPa
    tq = t * units.kelvin
    tdq = td * units.kelvin
    zq = z * units.meter

    try:
        parcel = mpcalc.parcel_profile(pq, tq[0], tdq[0])
        cape, cin = mpcalc.cape_cin(pq, tq, tdq, parcel)
        out["diagnostics"]["sbcape_jkg"] = _number(cape, "joule / kilogram")
        out["diagnostics"]["sbcin_jkg"] = _number(cin, "joule / kilogram")
    except Exception as exc:
        out["quality"]["warnings"].append(f"surface_parcel: {type(exc).__name__}: {exc}")

    try:
        cape, cin = mpcalc.mixed_layer_cape_cin(pq, tq, tdq, depth=100 * units.hPa)
        out["diagnostics"]["mlcape_jkg"] = _number(cape, "joule / kilogram")
        out["diagnostics"]["mlcin_jkg"] = _number(cin, "joule / kilogram")
    except Exception as exc:
        out["quality"]["warnings"].append(f"mixed_layer_parcel: {type(exc).__name__}: {exc}")

    try:
        cape, cin = mpcalc.most_unstable_cape_cin(pq, tq, tdq, depth=300 * units.hPa)
        out["diagnostics"]["mucape_jkg"] = _number(cape, "joule / kilogram")
        out["diagnostics"]["mucin_jkg"] = _number(cin, "joule / kilogram")
    except Exception as exc:
        out["quality"]["warnings"].append(f"most_unstable_parcel: {type(exc).__name__}: {exc}")

    try:
        lcl_pressure, _ = mpcalc.lcl(pq[0], tq[0], tdq[0])
        lcl_height = np.interp(float(lcl_pressure.to("hPa").magnitude), p[::-1], z[::-1])
        out["diagnostics"]["lcl_m_agl"] = float(lcl_height)
    except Exception as exc:
        out["quality"]["warnings"].append(f"lcl: {type(exc).__name__}: {exc}")

    try:
        pw = mpcalc.precipitable_water(pq, tdq)
        out["diagnostics"]["pwat_mm"] = _number(pw, "millimeter")
    except Exception as exc:
        out["quality"]["warnings"].append(f"pwat: {type(exc).__name__}: {exc}")

    # Temperature lapse rate over the first 3 km AGL.
    try:
        if z[0] <= 500 and z[-1] >= 3000:
            t0 = np.interp(0.0, z, t)
            t3 = np.interp(3000.0, z, t)
            out["diagnostics"]["lr03_k_km"] = float(t0 - t3) / 3.0
        else:
            out["diagnostics"]["lr03_k_km"] = None
            out["quality"]["warnings"].append("0-3 km lapse rate unavailable: profile does not span layer")
    except Exception as exc:
        out["quality"]["warnings"].append(f"lr03: {type(exc).__name__}: {exc}")

    if has_wind:
        u, v = arrays[4], arrays[5]
        uq = u * units("m/s")
        vq = v * units("m/s")
        try:
            du, dv = mpcalc.bulk_shear(pq, uq, vq, height=zq, depth=6 * units.km)
            shear = mpcalc.wind_speed(du, dv)
            out["diagnostics"]["bs06_kt"] = _number(shear, "knot")
        except Exception as exc:
            out["quality"]["warnings"].append(f"bulk_shear: {type(exc).__name__}: {exc}")
        try:
            right, left, mean = mpcalc.bunkers_storm_motion(pq, uq, vq, zq)
            srh, _, _ = mpcalc.storm_relative_helicity(
                zq, uq, vq, depth=1 * units.km,
                storm_u=right[0], storm_v=right[1]
            )
            out["diagnostics"]["srh01_m2s2"] = _number(srh, "meter ** 2 / second ** 2")
            out["diagnostics"]["bunkers_right_motion_kt"] = [
                _number(right[0], "knot"), _number(right[1], "knot")
            ]
        except Exception as exc:
            out["quality"]["warnings"].append(f"srh01: {type(exc).__name__}: {exc}")
    else:
        out["diagnostics"]["bs06_kt"] = None
        out["diagnostics"]["srh01_m2s2"] = None

    return out
