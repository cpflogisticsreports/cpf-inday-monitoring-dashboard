#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_pagasa.py
================
Fetch the latest PAGASA Severe Weather (Tropical Cyclone) Bulletin for a target
storm and regenerate `latest_inday_pagasa.json` for the CPF INDAY dashboard.

SOURCE OF TRUTH
    https://www.pagasa.dost.gov.ph/tropical-cyclone/severe-weather-bulletin

DESIGN PRINCIPLES (accuracy first)
  1. PAGASA only. No model data, no invented values.
  2. Auto-update ONLY the fields we can extract with high confidence:
       - current: position text + EXACT eye lat/long (when PAGASA prints it),
         winds, gusts, pressure, movement, wind extent, category, name
       - meta: bulletin number + issue time + next-bulletin time
       - tcws.highest, marine.galeWarning, marine.waves, gusts.today/thu/fri
     PRESERVE everything else from the existing JSON (forecast waypoints,
     rainfall outlook, satellite block, CPF-facing notes). Those are separate
     PAGASA products or curated interpretation and are not auto-derived here.
  3. FAIL-SAFE: if the target storm or the core fields cannot be parsed, DO NOT
     overwrite the file. Exit 0 without writing so the dashboard keeps the last
     verified data. Never publish a mis-parse.
  4. Commit only when the meaningful data actually changed (no timestamp churn).
  5. EXACT eye coordinates are flagged latlonExact:true. Any derived value is
     flagged approximate. Missing values become
     "Not specified in latest PAGASA bulletin".

Environment overrides:
    STORM_NAME   (default "INDAY")   – which storm to track
    OUT_PATH     (default "latest_inday_pagasa.json")
    PAGASA_URL   (default official bulletin URL)

Usage:
    python scripts/update_pagasa.py            # fetch live, write if changed
    python scripts/update_pagasa.py --dry-run  # print parsed JSON, do not write
    python scripts/update_pagasa.py --from-file page.html   # parse a saved page
"""

import os, re, sys, json, html, math, argparse, datetime, urllib.request, urllib.error, copy

PAGASA_URL = os.environ.get(
    "PAGASA_URL",
    "https://www.pagasa.dost.gov.ph/tropical-cyclone/severe-weather-bulletin",
)
STORM_NAME = os.environ.get("STORM_NAME", "INDAY").strip().upper()
OUT_PATH   = os.environ.get("OUT_PATH", "latest_inday_pagasa.json")
# PRIMARY source: the stable, text-based bulletin PDF (predictable URL, no JS,
# and it contains the exact Track & Intensity Forecast table).
PDF_URL    = os.environ.get("PDF_URL",
             "https://pubfiles.pagasa.dost.gov.ph/tamss/weather/bulletin_%s.pdf" % STORM_NAME.lower())

# --------------------------------------------------------------------------- #
#  Default curated blocks (used only if OUT_PATH does not yet exist).          #
#  On normal runs we load the existing file and preserve these.               #
# --------------------------------------------------------------------------- #
DEFAULT = {
  "meta": {"source": "PAGASA"},
  "current": {"name": STORM_NAME, "intl": "", "cat": "Tropical Cyclone",
              "ninth": "", "positionRef": "", "lat": 0, "lon": 0, "latlonExact": False,
              "winds": "Not specified in latest PAGASA bulletin",
              "gust": "Not specified in latest PAGASA bulletin",
              "pressure": "Not specified in latest PAGASA bulletin",
              "movement": "Not specified in latest PAGASA bulletin",
              "windExtent": "Not specified in latest PAGASA bulletin",
              "entered": "", "landfallPH": "PH landfall unlikely", "outlook": ""},
  "forecast": [],
  "par": [[5,115],[15,115],[21,120],[25,120],[25,135],[5,135]],
  "tcws": {"highest": "Not specified in latest PAGASA bulletin", "signal1": [], "note": ""},
  "gusts": {"today": "", "thu": "", "fri": ""},
  "rainfall": {"tc": [], "monsoon": [], "note": ""},
  "marine": {"galeWarning": "", "waves": [], "advice": ""},
  "satellite": {
      "himawari": "JMA Himawari-9 (real-time, ~10-min)",
      "base": "Esri World Imagery (Maxar/Earthstar) satellite base",
      "note": "Satellite imagery is visual context only. Cloud position/appearance must not be used to infer storm movement — the official center and track come from PAGASA."},
  "changeLog": []
}

# --------------------------------------------------------------------------- #
#  Compass helpers (word + abbreviation forms)                                 #
# --------------------------------------------------------------------------- #
_DIRS = {
  "NORTH":0,"N":0,"NORTHNORTHEAST":22.5,"NNE":22.5,"NORTHEAST":45,"NE":45,
  "EASTNORTHEAST":67.5,"ENE":67.5,"EAST":90,"E":90,"EASTSOUTHEAST":112.5,"ESE":112.5,
  "SOUTHEAST":135,"SE":135,"SOUTHSOUTHEAST":157.5,"SSE":157.5,"SOUTH":180,"S":180,
  "SOUTHSOUTHWEST":202.5,"SSW":202.5,"SOUTHWEST":225,"SW":225,"WESTSOUTHWEST":247.5,"WSW":247.5,
  "WEST":270,"W":270,"WESTNORTHWEST":292.5,"WNW":292.5,"NORTHWEST":315,"NW":315,
  "NORTHNORTHWEST":337.5,"NNW":337.5,
}
def bearing_to_deg(text):
    key = re.sub(r"[^A-Za-z]", "", text or "").upper().replace("WARD", "")
    return _DIRS.get(key)

def clean_dir(s):
    s = (s or "").strip().rstrip(".").replace("-", " ")
    s = re.sub(r"ward\b", "", s, flags=re.I).strip()
    return " ".join(w.capitalize() for w in s.split()) or s

# --------------------------------------------------------------------------- #
#  Networking + HTML → text                                                    #
# --------------------------------------------------------------------------- #
def fetch_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CPF-Logistics-Bot",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "ignore")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
    raise RuntimeError("fetch failed after retries: %s" % last)

def fetch_pdf_text(url):
    """Download the bulletin PDF and extract its text (needs `pypdf`)."""
    import io
    from pypdf import PdfReader
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CPF-Logistics-Bot",
        "Accept": "application/pdf",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)

def fetch_source():
    """Return normalized bulletin text. Tries the stable PDF first (reliable on
    CI runners), then the HTML page as a fallback."""
    try:
        txt = fetch_pdf_text(PDF_URL)
        if STORM_NAME in txt.upper() and "center of the eye" in txt.lower():
            print("Source: PDF (%s)" % PDF_URL)
            return html_to_text(txt)
        print("::warning::PDF fetched but no usable %s bulletin found; trying HTML page." % STORM_NAME)
    except Exception as e:
        print("::warning::PDF fetch/parse failed (%s); trying HTML page." % e)
    print("Source: HTML page (%s)" % PAGASA_URL)
    return html_to_text(fetch_html(PAGASA_URL))

def html_to_text(h):
    h = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<br\s*/?>", "\n", h)
    h = re.sub(r"(?is)</(p|div|li|tr|h[1-6]|td)>", "\n", h)
    t = re.sub(r"(?s)<[^>]+>", " ", h)
    t = html.unescape(t)
    t = t.replace("\u00a0", " ").replace("\u2013", "-").replace("\u2014", "-")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return t

# --------------------------------------------------------------------------- #
#  Parsing                                                                     #
# --------------------------------------------------------------------------- #
POS_RE = re.compile(
    r"center of the eye of\s+(?P<name>.+?)\s+was estimated"
    r"(?:\s+based on all available data)?\s+at\s+"
    r"(?P<dist>[\d,]+)\s*(?:km|kilometers)\s+"
    r"(?P<brg>[A-Za-z ]+?)\s+of\s+"
    r"(?P<land>[A-Za-z .,'\-]+?)"
    r"\s*(?=\(|,?\s*packing|\.|;|\bwith\b|\bmoving\b|\n|$)",
    re.IGNORECASE | re.DOTALL,
)
COORD_RE = re.compile(r"\(\s*([\d.]+)\s*(?:°|deg)?\s*N\s*,\s*([\d.]+)\s*(?:°|deg)?\s*E\s*\)", re.I)

# --- Exact forecast positions from the PDF "TRACK AND INTENSITY FORECAST" table ---
CAT_MAP = {"TD": "Tropical Depression", "TS": "Tropical Storm",
           "STS": "Severe Tropical Storm", "TY": "Typhoon", "STY": "Super Typhoon"}
FC_ROW = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+"   # time, date
    r"(\d{1,2}\.\d)\s+(\d{2,3}\.\d)\s+"                              # lat, lon
    r"(.+?)\s+(\d{2,3})\s+(TD|TS|STS|TY|STY)\s+"                     # location, MSW, cat
    r"([NSEW]{1,3})\s+(\d{1,3})",                                    # move dir, speed
    re.IGNORECASE | re.DOTALL)

def parse_forecast_table(text):
    """Return a list of exact forecast points, or [] if the table isn't present."""
    seg = text
    a = re.search(r"TRACK AND INTENSITY FORECAST", text, re.I)
    b = re.search(r"TROPICAL CYCLONE WIND SIGNALS", text, re.I)
    if a:
        seg = text[a.end(): (b.start() if b else len(text))]
    rows = []
    for m in FC_ROW.finditer(seg):
        tstr, dstr, la, lo, loc, msw, cat, mdir, mspd = m.groups()
        lat, lon = float(la), float(lo)
        if not (0 < lat < 45 and 100 < lon < 160):
            continue  # sanity guard
        try:
            dt = datetime.datetime.strptime(dstr.strip(), "%d %B %Y")
            day, dshort = dt.strftime("%a"), dt.strftime("%d %b")
        except Exception:
            day, dshort = "", dstr.strip()
        tcompact = tstr.replace(" ", "").replace(":00", "")
        rows.append({
            "label": ("%s %s" % (day, tcompact)).strip(),
            "when": ("%s · %s (%s)" % (dshort, tstr, day)).strip(" (·)"),
            "lat": lat, "lon": lon, "latlonExact": True,
            "ref": re.sub(r"\s+", " ", loc).strip(" .,"),
            "cat": CAT_MAP.get(cat.upper(), cat),
            "winds": "%s km/h" % msw,
            "gust": "Not specified in latest PAGASA bulletin",
            "move": "%s · %s km/h" % (mdir.upper(), mspd),
            "place": "PAGASA forecast position (exact).",
        })
    return rows if len(rows) >= 3 else []

def _search(pat, text, group=1, flags=re.I):
    m = re.search(pat, text, flags)
    return m.group(group).strip() if m else None

def parse_bulletin(text, storm):
    """Return a dict of high-confidence fields for `storm`, or None if the storm
    or the essential position line can't be found (=> caller must not overwrite)."""
    storm = storm.upper()

    # locate every per-storm position line; pick the target storm's block
    matches = list(POS_RE.finditer(text))
    target = None
    for i, m in enumerate(matches):
        if storm in m.group("name").upper():
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            target = (m, text[start:end])
            break
    if not target:
        return None  # storm not present in current bulletin -> fail-safe

    m, section = target

    # ---- name / category ----
    raw = re.sub(r"[\u201c\u201d\"']", "", m.group("name")).strip()
    up = raw.upper()
    idx = up.rfind(storm)
    category = (raw[:idx].strip() if idx > 0 else raw.replace(storm, "").strip()) or "Tropical Cyclone"

    # ---- position + EXACT coordinates (if PAGASA printed them) ----
    dist = m.group("dist")
    brg  = " ".join(w.capitalize() for w in m.group("brg").split())
    land = m.group("land").strip().rstrip(",")
    position_ref = "%s km %s of %s" % (dist, brg, land)
    if re.search(r"inside\s+PAR|within\s+the\s+Philippine\s+Area", section, re.I):
        position_ref += " (inside PAR)"

    lat = lon = None
    exact = False
    cm = COORD_RE.search(text[m.end(): m.end() + 80])
    if cm:
        lat, lon, exact = float(cm.group(1)), float(cm.group(2)), True

    # ---- intensity / movement / extent (within the storm's own section) ----
    winds = _search(r"maximum sustained winds of\s*([\d]+)", section)
    gust  = _search(r"gustiness of up to\s*([\d]+)", section)
    pres  = _search(r"central pressure of\s*([\d]+)\s*hPa", section)
    ext   = _search(r"up to\s*([\d]+)\s*km\s+from\s+(?:its|the)\s+cent", section)
    mv = (re.search(r"Present Movement\s+([A-Za-z\- ]+?)\s+at\s+([\d]+)\s*km/?h", section, re.I)
          or re.search(r"moving\s+([A-Za-z\- ]+?)\s+at\s+(?:the speed of\s+)?([\d]+)\s*(?:km/?h|kph|kilometers per hour)", section, re.I))

    # ---- shared advisory fields (search whole page) ----
    highest = _search(r"highest[^.]{0,120}?Signal\s+No\.?\s*([\d]+(?:\s*or\s*[\d]+)?)", text)
    gale_m = re.search(r"Gale Warning(?:\s*No\.?\s*(\d+))?\s*is in effect over\s+([^.]+)\.", text, re.I)
    gale_no2 = _search(r"Gale Warning No\.?\s*(\d+)\s+issued", text)

    forecast = parse_forecast_table(text)

    waves = []
    for wm in re.finditer(r"Up to\s*([\d.]+)\s*m\s*[:\-]?\s*(.+?)(?=(?:Up to\s*[\d.]+\s*m)|\n|$)",
                          text, re.I | re.S):
        where = re.sub(r"\s+", " ", wm.group(2)).strip(" .;")
        if 4 < len(where) < 400:
            waves.append({"h": "Up to %s m" % wm.group(1), "where": where})
        if len(waves) >= 4:
            break

    today = _search(r"Today[:\s]+([^\n]+?)(?=\s*(?:Tomorrow|Friday|\n|$))", text)
    thu   = _search(r"Tomorrow\s*\([^)]*\)[:\s]+([^\n]+?)(?=\s*(?:Friday|\n|$))", text)
    fri   = _search(r"Friday\s*\([^)]*\)[:\s]+([^\n]+?)(?=\s*(?:\n|$))", text)

    issued = _search(r"Issued at\s*([0-9]{1,2}:[0-9]{2}\s*[APap]\.?[Mm]\.?\s*,?\s*"
                     r"[0-9]{1,2}\s+\w+\s+[0-9]{4})", text)
    bno    = _search(r"Tropical Cyclone Bulletin\s*(?:No\.?|Nr\.?|#)?\s*([0-9]+[A-Za-z\-]*)", text)
    nextb  = _search(r"next\s+(?:tropical cyclone\s+)?bulletin\s+will be issued at\s*([^.\n]+)", text)

    return {
        "storm": storm, "category": category,
        "position_ref": position_ref, "lat": lat, "lon": lon, "exact": exact,
        "winds": winds, "gust": gust, "pres": pres, "ext": ext,
        "move_dir": (mv.group(1) if mv else None), "move_spd": (mv.group(2) if mv else None),
        "highest": highest,
        "gale_no": (gale_m.group(1) if gale_m and gale_m.group(1) else gale_no2),
        "gale_where": (re.sub(r"\s+", " ", gale_m.group(2)).strip() if gale_m else None),
        "waves": waves, "today": today, "thu": thu, "fri": fri,
        "issued": issued, "bno": bno, "nextb": nextb, "forecast": forecast,
    }

# --------------------------------------------------------------------------- #
#  Formatting helpers                                                          #
# --------------------------------------------------------------------------- #
_MONTHS = {"JANUARY":"Jan","FEBRUARY":"Feb","MARCH":"Mar","APRIL":"Apr","MAY":"May",
           "JUNE":"Jun","JULY":"Jul","AUGUST":"Aug","SEPTEMBER":"Sep","OCTOBER":"Oct",
           "NOVEMBER":"Nov","DECEMBER":"Dec"}
def fmt_issued(issued):
    """'8:00 PM, 08 July 2026' -> '8:00 PM · 08 Jul 2026' (best effort)."""
    if not issued:
        return None
    s = re.sub(r"\s+", " ", issued).replace(".", "").strip()
    m = re.match(r"([0-9]{1,2}:[0-9]{2})\s*([APap][Mm])\s*,?\s*([0-9]{1,2})\s+(\w+)\s+([0-9]{4})", s)
    if not m:
        return s
    hhmm, ap, dd, mon, yyyy = m.groups()
    mon = _MONTHS.get(mon.upper(), mon[:3].title())
    return "%s %s · %s %s %s" % (hhmm, ap.upper(), dd.zfill(2), mon, yyyy)

def ns(v, unit=""):  # "not specified" wrapper for numbers
    return ("%s%s" % (v, unit)) if v else "Not specified in latest PAGASA bulletin"

# --------------------------------------------------------------------------- #
#  Merge parsed fields into the existing JSON                                  #
# --------------------------------------------------------------------------- #
def build_json(parsed, base):
    data = copy.deepcopy(base) if base else copy.deepcopy(DEFAULT)
    old_current = copy.deepcopy(data.get("current", {}))

    cur = data.setdefault("current", {})
    cur["name"] = parsed["storm"]
    cur["cat"]  = parsed["category"]
    cur["positionRef"] = parsed["position_ref"]
    if parsed["exact"] and parsed["lat"] is not None:
        cur["lat"], cur["lon"], cur["latlonExact"] = parsed["lat"], parsed["lon"], True
    # (if PAGASA didn't print decimals this run, keep the previous plotted point)
    cur["winds"]      = ns(parsed["winds"], " km/h")
    cur["gust"]       = ns(parsed["gust"], " km/h")
    cur["pressure"]   = ("%s hPa" % parsed["pres"]) if parsed["pres"] else \
                        cur.get("pressure", "Not specified in latest PAGASA bulletin")
    cur["windExtent"] = ("Up to %s km from center" % parsed["ext"]) if parsed["ext"] else \
                        cur.get("windExtent", "Not specified in latest PAGASA bulletin")
    if parsed["move_dir"] and parsed["move_spd"]:
        cur["movement"] = "%s · %s km/h" % (clean_dir(parsed["move_dir"]), parsed["move_spd"])

    meta = data.setdefault("meta", {})
    meta["source"] = "PAGASA"
    if parsed["bno"]:
        meta["bulletinNo"] = "Tropical Cyclone Bulletin No. %s" % parsed["bno"]
    label = fmt_issued(parsed["issued"])
    if label:
        meta["issuedLabel"] = label
        meta["asOf"] = "position as of %s" % re.sub(r" · .*", "", label)
    if parsed["nextb"]:
        meta["nextBulletin"] = re.sub(r"\s+", " ", parsed["nextb"]).strip()
    meta["sourceUrl"] = PAGASA_URL
    meta["verifiedNote"] = ("Auto-generated from the official PAGASA bulletin. Current "
        "conditions, signals, gale/marine and gust areas are parsed live; forecast "
        "waypoints, rainfall outlook and satellite notes are preserved/curated.")

    if parsed["highest"]:
        data.setdefault("tcws", {})["highest"] = "No. %s (possible)" % re.sub(r"\s+", " ", parsed["highest"])

    mar = data.setdefault("marine", {})
    if parsed["gale_where"]:
        gno = (" No. %s" % parsed["gale_no"]) if parsed["gale_no"] else ""
        mar["galeWarning"] = "Gale Warning%s in effect — %s." % (gno, parsed["gale_where"])
    if parsed["waves"]:
        mar["waves"] = parsed["waves"]

    g = data.setdefault("gusts", {})
    if parsed["today"]: g["today"] = parsed["today"].strip(" .")
    if parsed["thu"]:   g["thu"]   = parsed["thu"].strip(" .")
    if parsed["fri"]:   g["fri"]   = parsed["fri"].strip(" .")

    # Exact forecast track from the PDF table (only replace when parsed cleanly)
    if parsed.get("forecast"):
        data["forecast"] = parsed["forecast"]

    # ----- change log: diff meaningful current fields vs previous file -----
    changes = []
    def diff(label, a, b, direction="up"):
        if a and b and str(a) != str(b):
            changes.append({"t": label, "from": str(a), "to": str(b), "dir": direction})
    diff("Position", old_current.get("positionRef"), cur.get("positionRef"))
    diff("Winds",    old_current.get("winds"),       cur.get("winds"))
    diff("Gusts",    old_current.get("gust"),        cur.get("gust"))
    diff("Movement", old_current.get("movement"),    cur.get("movement"), "dn")
    diff("Category", old_current.get("cat"),         cur.get("cat"))
    if changes:
        data["changeLog"] = changes
    return data

# --------------------------------------------------------------------------- #
#  Compare (ignoring the volatile autoUpdated timestamp)                       #
# --------------------------------------------------------------------------- #
def meaningful(d):
    d = copy.deepcopy(d)
    d.get("meta", {}).pop("autoUpdated", None)
    return json.dumps(d, sort_keys=True, ensure_ascii=False)

# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print result, do not write")
    ap.add_argument("--from-file", help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    try:
        if args.from_file:
            raw = open(args.from_file, encoding="utf-8", errors="ignore").read()
            text = html_to_text(raw)
        else:
            text = fetch_source()
    except Exception as e:
        print("::warning::Could not fetch PAGASA source (%s). Keeping last verified file." % e)
        return 0

    parsed = parse_bulletin(text, STORM_NAME)
    if not parsed:
        print("::notice::Storm %s not found in the current PAGASA bulletin "
              "(may have exited PAR / no active bulletin). Keeping last verified file." % STORM_NAME)
        return 0
    if not (parsed["winds"] or parsed["position_ref"]):
        print("::warning::Parsed block missing core fields — not overwriting (fail-safe).")
        return 0

    base = None
    if os.path.exists(OUT_PATH):
        try:
            base = json.load(open(OUT_PATH, encoding="utf-8"))
        except Exception:
            base = None

    new = build_json(parsed, base)

    if base and meaningful(base) == meaningful(new):
        print("No change vs current %s — nothing to commit." % OUT_PATH)
        return 0

    new.setdefault("meta", {})["autoUpdated"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")

    if args.dry_run:
        print(json.dumps(new, indent=2, ensure_ascii=False))
        return 0

    d = os.path.dirname(OUT_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("Updated %s (bulletin: %s | %s | winds %s)" % (
        OUT_PATH, new["meta"].get("issuedLabel", "?"),
        new["current"].get("positionRef", "?"), new["current"].get("winds", "?")))
    return 0

if __name__ == "__main__":
    sys.exit(main())
