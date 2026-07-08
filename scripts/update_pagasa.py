#!/usr/bin/env python3
"""
CPF Logistics PAGASA Auto-Updater

Fix applied:
- Uses PAGASA Tropical Cyclone Bulletin webpage as the primary discovery page.
- Finds the latest TCB#N_inday.pdf archive link instead of relying only on stale rolling files.
- Adds cache-busting query parameters.
- Extracts the official PDF using pypdf and writes docs/latest_inday_pagasa.json.
- Retains the previous verified JSON if extraction fails.

PAGASA remains the official source. Satellite imagery is visual context only.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from pypdf import PdfReader
from io import BytesIO

STORM_NAME = os.getenv("STORM_NAME", "INDAY")
OUT_PATH = Path(os.getenv("OUT_PATH", "docs/latest_inday_pagasa.json"))

PAGASA_TCB_PAGE = "https://www.pagasa.dost.gov.ph/tropical-cyclone/severe-weather-bulletin"
PAGASA_WEATHER_ADVISORY = "https://pubfiles.pagasa.dost.gov.ph/tamss/weather/advisory.pdf"
PAGASA_GALE = "https://pubfiles.pagasa.dost.gov.ph/tamss/weather/gale.pdf"

CPF_HUBS = [
    {"name": "Gerona Feedmill / Tarlac Hub", "lat": 15.606, "lon": 120.596, "type": "Feedmill"},
    {"name": "Ilagan Feedmill / Isabela", "lat": 17.148, "lon": 121.889, "type": "Feedmill"},
    {"name": "Metro Manila / NCR", "lat": 14.5995, "lon": 120.9842, "type": "Delivery area"},
    {"name": "Cebu", "lat": 10.3157, "lon": 123.8854, "type": "Visayas hub"},
    {"name": "Davao", "lat": 7.1907, "lon": 125.4553, "type": "Mindanao hub"},
    {"name": "General Santos", "lat": 6.1164, "lon": 125.1716, "type": "Mindanao delivery area"},
    {"name": "Cagayan de Oro", "lat": 8.4542, "lon": 124.6319, "type": "Mindanao delivery area"},
]

CAT = {"STY": "Super Typhoon", "TY": "Typhoon", "STS": "Severe Tropical Storm", "TS": "Tropical Storm", "TD": "Tropical Depression"}

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def get(url, binary=False):
    sep = "&" if "?" in url else "?"
    url2 = f"{url}{sep}_cb={int(time.time())}"
    req = Request(url2, headers={
        "User-Agent": "CPF-Logistics-PAGASA-Monitor/2.0",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    })
    with urlopen(req, timeout=45) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", errors="ignore")

def pdf_text(url):
    data = get(url, binary=True)
    reader = PdfReader(BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def find_latest_tcb_pdf():
    html = get(PAGASA_TCB_PAGE)
    storm = STORM_NAME.lower()
    # Example links: https://pubfiles.../TCB%236_inday.pdf or TCB#6_inday.pdf
    candidates = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        h = href.lower()
        if storm in h and "tcb" in h and ".pdf" in h:
            full = urljoin(PAGASA_TCB_PAGE, href)
            m = re.search(r"tcb(?:%23|#)?(\d+)", h, flags=re.I)
            num = int(m.group(1)) if m else 0
            candidates.append((num, full))
    if not candidates:
        raise RuntimeError("No latest TCB PDF link found on PAGASA bulletin page.")
    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[0][1]

def rx(text, pattern, default=""):
    m = re.search(pattern, text, flags=re.I | re.S)
    return clean(m.group(1)) if m else default

def parse_tcb(text, url):
    bulletin_no = rx(text, r"TROPICAL CYCLONE BULLETIN\s*(?:NR\.|#)?\s*(\d+)")
    issued = rx(text, r"Issued at\s*([0-9: ]+(?:AM|PM),?\s*\d{1,2}\s+\w+\s+\d{4})")
    valid = rx(text, r"Valid for broadcast until\s*(.*?)\.")
    category_name = rx(text, r"TROPICAL CYCLONE BULLETIN.*?\n([A-Za-z ]+)\s+INDAY", "Typhoon").strip()
    center_time = rx(text, r"Location of Center\s*\((.*?)\)")
    pos_desc = rx(text, r"estimated.*?at\s+(.*?)\s*\(\s*\d{1,2}\.\d", "")
    coord = re.search(r"\(\s*(\d{1,2}\.\d)\s*°?\s*N,\s*(\d{2,3}\.\d)\s*°?\s*E\s*\)", text)
    lat = float(coord.group(1)) if coord else None
    lon = float(coord.group(2)) if coord else None
    max_wind = rx(text, r"Maximum sustained winds of\s*(\d+)\s*km/h")
    gust = rx(text, r"gustiness of up to\s*(\d+)\s*km/h")
    pressure = rx(text, r"central pressure of\s*(\d+)\s*hPa")
    movement = rx(text, r"Present Movement\s*([A-Za-z \-]+ at\s*\d+\s*km/h)")
    if not movement:
        movement = rx(text, r"Moving\s*([A-Za-z \-]+ at\s*\d+\s*km/h)")
    move_speed = rx(movement, r"(\d+)\s*km/h")
    extent = rx(text, r"extend(?:s)? outwards up to\s*(\d+)\s*km")

    forecast = []
    one = re.sub(r"\s+", " ", text)
    pattern = re.compile(
        r"(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM))\s+"
        r"(?P<date>\d{1,2}\s+\w+\s+\d{4})\s+"
        r"(?P<lat>\d{1,2}\.\d)\s+"
        r"(?P<lon>\d{2,3}\.\d)\s+"
        r"(?P<pos>.*?)\s+"
        r"(?P<msw>\d{2,3})\s+"
        r"(?P<cat>STY|TY|STS|TS|TD)\s+"
        r"(?P<dir>[A-Z]+)\s+(?P<speed>\d+)",
        flags=re.I
    )
    seen = set()
    for m in pattern.finditer(one):
        key = (m.group("time"), m.group("date"), m.group("lat"), m.group("lon"))
        if key in seen:
            continue
        seen.add(key)
        forecast.append({
            "forecast_time": f"{m.group('time')}, {m.group('date')}",
            "lat": float(m.group("lat")),
            "lon": float(m.group("lon")),
            "position_description": clean(m.group("pos")),
            "max_winds_kmh": int(m.group("msw")),
            "category": CAT.get(m.group("cat").upper(), m.group("cat").upper()),
            "movement_direction": m.group("dir").upper(),
            "movement_speed_kmh": int(m.group("speed")),
        })

    tcws_text = rx(text, r"TROPICAL CYCLONE WIND SIGNALS.*?Wind threat:.*?Strong\s*winds\s*(.*?)\s*-\s*-\s*Warning lead time", "")
    if not tcws_text:
        tcws_text = rx(text, r"TCWS.*?(Batanes.*?)\s*Warning lead time", "")

    storm = {
        "name": "INDAY",
        "international_name": "BAVI",
        "category": category_name,
        "current_center_time": center_time,
        "lat": lat,
        "lon": lon,
        "position_description": pos_desc,
        "max_winds_kmh": int(max_wind) if max_wind else None,
        "gustiness_kmh": int(gust) if gust else None,
        "central_pressure_hpa": int(pressure) if pressure else None,
        "movement": movement,
        "movement_speed_kmh": int(move_speed) if move_speed else None,
        "wind_extent_km": int(extent) if extent else None,
    }

    latest_update = {
        "source_type": "TCB",
        "number": f"Tropical Cyclone Bulletin No. {bulletin_no}",
        "issued_at": issued,
        "valid_until": valid,
        "source_url": url
    }

    # Put current point first for dashboard plotting.
    current_point = {
        "forecast_time": f"Current: {center_time}, {issued.split(',')[-1].strip() if issued else ''}",
        "lat": lat,
        "lon": lon,
        "position_description": pos_desc,
        "max_winds_kmh": storm["max_winds_kmh"],
        "gustiness_kmh": storm["gustiness_kmh"],
        "central_pressure_hpa": storm["central_pressure_hpa"],
        "category": category_name,
        "movement_direction": movement.split(" at ")[0].strip() if movement else "",
        "movement_speed_kmh": storm["movement_speed_kmh"],
        "label": "Current"
    }
    if lat and lon:
        forecast = [current_point] + forecast

    return latest_update, storm, forecast, tcws_text

def parse_advisory():
    try:
        t = pdf_text(PAGASA_WEATHER_ADVISORY)
    except Exception as e:
        return [{"source": "Weather Advisory", "details_raw": f"Unable to fetch/parse advisory: {e}"}]
    no = rx(t, r"WEATHER ADVISORY NO\.\s*(\d+)")
    issued = rx(t, r"Issued at:\s*(.*?)\n")
    rows = []
    if no or issued:
        rows.append({
            "source": f"Weather Advisory No. {no}" if no else "Weather Advisory",
            "issued_at": issued,
            "details_raw": clean(t)[:6000]
        })
    return rows

def parse_gale():
    try:
        t = pdf_text(PAGASA_GALE)
    except Exception as e:
        return [{"source": "Gale Warning", "affected_seaboards_raw": f"Unable to fetch/parse gale warning: {e}"}]
    no = rx(t, r"GALE WARNING NR\.\s*(\d+)")
    issued = rx(t, r"Issued at\s*(.*?)\n")
    valid = rx(t, r"Valid for broadcast until\s*(.*?)\.")
    winds = re.findall(r"\(?\s*(\d+\s*[-–]\s*\d+)\s*\)?\s*/", t)
    waves = re.findall(r"(\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\s*m)", t)
    return [{
        "source": f"Gale Warning No. {no}" if no else "Gale Warning",
        "issued_at": issued,
        "valid_until": valid,
        "affected_seaboards_raw": clean(t)[:6000],
        "wind_speed_kmh_raw": [w + " km/h" for w in winds[:5]],
        "wave_height_m_raw": waves[:5]
    }]

def build_risk_matrix(storm, marine, rainfall):
    marine_raw = " ".join(m.get("affected_seaboards_raw","") for m in marine).lower()
    rainfall_raw = " ".join(r.get("details_raw","") + r.get("areas","") for r in rainfall).lower()
    return [
        {
            "area_route": "North Luzon / TCWS No. 1 areas",
            "commodity_affected": "Feeds, silo/bulk feeds, live animals, RM transfers",
            "main_hazard": "TCWS No. 1; strong winds; exposed coastal/upland route risk",
            "risk_level": "High",
            "recommended_dispatch_action": "Require route validation before dispatch",
            "remarks": "Validate road passability, wind exposure, receiving readiness, GPS status, and contractor readiness before release."
        },
        {
            "area_route": "Eastern/northern seaboards of Luzon and eastern Visayas",
            "commodity_affected": "Sea/RORO-dependent transfers, containers, inter-island deliveries",
            "main_hazard": "Gale warning / rough to very rough seas" if "gale" in marine_raw or "rough" in marine_raw else "Marine advisory watch",
            "risk_level": "Critical",
            "recommended_dispatch_action": "Hold dispatch",
            "remarks": "Hold small seacraft/motorbanca-dependent movement; validate larger vessels with Coast Guard, ports, and shipping lines."
        },
        {
            "area_route": "Ilagan / Isabela corridor",
            "commodity_affected": "Feeds, silo/bulk feeds, RM transfers",
            "main_hazard": "TCWS No. 1 plus Isabela seaboard exposure",
            "risk_level": "High",
            "recommended_dispatch_action": "Require route validation before dispatch",
            "remarks": "Dispatch only after local route, contractor, and receiving confirmation."
        },
        {
            "area_route": "Gerona / Tarlac hub and Central Luzon outbound routes",
            "commodity_affected": "Feeds, food, RM, livestock support",
            "main_hazard": "Downstream northbound/eastbound/western corridors may be affected by wind, rain, or marine advisories",
            "risk_level": "Moderate",
            "recommended_dispatch_action": "Dispatch with caution",
            "remarks": "Coordinate route validation for affected corridors; update Affected Delivery Updates file."
        },
        {
            "area_route": "NCR / CALABARZON / Bataan / Zambales / Bulacan / Pampanga",
            "commodity_affected": "Food, feeds, containers, raw materials",
            "main_hazard": "Habagat rainfall/gust watch; possible flooding/visibility reduction",
            "risk_level": "Moderate",
            "recommended_dispatch_action": "Dispatch with caution",
            "remarks": "Check flood-prone roads, receiving delays, and route restrictions before release."
        },
        {
            "area_route": "MIMAROPA / Palawan / Western Visayas",
            "commodity_affected": "Feeds, food, livestock, RORO-linked movements",
            "main_hazard": "Monsoon rainfall, flooding/landslide-prone roads, and port disruption risk",
            "risk_level": "High",
            "recommended_dispatch_action": "Require route validation before dispatch",
            "remarks": "Coordinate with contractors, local teams, customers, and port/RORO operators before dispatch."
        },
        {
            "area_route": "Mindanao areas under monsoon rainfall/gust watch",
            "commodity_affected": "Feeds, livestock, aqua, food, RM",
            "main_hazard": "Gusty conditions and localized flooding/landslide risk",
            "risk_level": "Moderate",
            "recommended_dispatch_action": "Dispatch with caution",
            "remarks": "Keep GPS active and report road disruptions immediately."
        },
    ]

def main():
    old = {}
    if OUT_PATH.exists():
        try:
            old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            old = {}

    try:
        tcb_url = find_latest_tcb_pdf()
        tcb_text = pdf_text(tcb_url)
        latest_update, storm, forecast, tcws_text = parse_tcb(tcb_text, tcb_url)
        rainfall = parse_advisory()
        marine = parse_gale()
        risk = build_risk_matrix(storm, marine, rainfall)
        change_log = []
        if old.get("latest_pagasa_update", {}).get("number") != latest_update.get("number"):
            change_log.append(f"Bulletin updated: {old.get('latest_pagasa_update', {}).get('number', 'None')} → {latest_update.get('number')}")
        if old.get("storm", {}).get("max_winds_kmh") != storm.get("max_winds_kmh"):
            change_log.append(f"Intensity changed: {old.get('storm', {}).get('max_winds_kmh', 'N/A')} km/h → {storm.get('max_winds_kmh')} km/h")
        if old.get("storm", {}).get("movement") != storm.get("movement"):
            change_log.append(f"Movement changed: {old.get('storm', {}).get('movement', 'N/A')} → {storm.get('movement')}")
        if old.get("storm", {}).get("wind_extent_km") != storm.get("wind_extent_km"):
            change_log.append(f"Wind extent changed: {old.get('storm', {}).get('wind_extent_km', 'N/A')} km → {storm.get('wind_extent_km')} km")
        if not change_log:
            change_log.append("Checked PAGASA sources; no material change versus last verified JSON.")

        data = {
            "last_checked": now_iso(),
            "latest_pagasa_update": latest_update,
            "storm": storm,
            "forecast_track": forecast,
            "tcws": [{
                "signal": "TCWS No. 1",
                "areas_raw": tcws_text or "Not specified in latest PAGASA extraction.",
                "wind_range_kmh": "39–61",
                "potential_impact": "Minimal to minor threat to life and property. Highest possible signal mentioned by PAGASA should be checked in latest bulletin."
            }],
            "rainfall": rainfall,
            "marine": marine,
            "regional_advisories": [],
            "cpf_hubs": CPF_HUBS,
            "cpf_risk_matrix": risk,
            "dispatch_recommendations": [
                {"action": r["recommended_dispatch_action"], "area": r["area_route"], "reason": r["main_hazard"]} for r in risk
            ],
            "contractor_advisory": "All freight contractors must validate route, weather, road, bridge, and port conditions before dispatch. Keep GPS active, confirm driver/helper readiness, secure cargo, and report flooding, landslides, road closures, port cancellations, low visibility, strong winds, route deviations, vehicle issues, or delivery delays immediately. Do not proceed through unsafe flooded or landslide-prone roads without CPF Logistics clearance.",
            "change_log": change_log,
            "source_urls": {
                "tcb": tcb_url,
                "weather_advisory": PAGASA_WEATHER_ADVISORY,
                "gale": PAGASA_GALE
            },
            "reliability_notes": [
                "PAGASA is the official source of truth.",
                "Satellite imagery is visual context only and must not override PAGASA values.",
                "Forecast cone is not plotted unless official cone geometry is available."
            ]
        }

        if not data["storm"].get("lat") or not data["forecast_track"]:
            raise RuntimeError("Extraction missing storm coordinates or forecast track; retaining prior verified JSON.")

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {OUT_PATH} from {tcb_url}")

    except Exception as e:
        if old:
            old["last_checked"] = now_iso()
            old.setdefault("errors", []).append({"time": now_iso(), "message": str(e)})
            OUT_PATH.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Extraction failed; retained previous verified JSON: {e}")
        else:
            raise

if __name__ == "__main__":
    main()
