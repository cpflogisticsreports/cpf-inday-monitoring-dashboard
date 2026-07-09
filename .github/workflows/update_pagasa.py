#!/usr/bin/env python3
import json, os, re, time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from pypdf import PdfReader

STORM_NAME=os.getenv("STORM_NAME","INDAY").lower()
OUT_PATH=Path(os.getenv("OUT_PATH","docs/latest_inday_pagasa.json"))
PAGE_URL="https://www.pagasa.dost.gov.ph/tropical-cyclone/severe-weather-bulletin"
PDF_BASE="https://pubfiles.pagasa.dost.gov.ph/tamss/weather/bulletin/"
ADVISORY_URL="https://pubfiles.pagasa.dost.gov.ph/tamss/weather/advisory.pdf"
GALE_URL="https://pubfiles.pagasa.dost.gov.ph/tamss/weather/gale.pdf"
CAT={"STY":"Super Typhoon","TY":"Typhoon","STS":"Severe Tropical Storm","TS":"Tropical Storm","TD":"Tropical Depression"}
CPF_HUBS=[
{"name":"Gerona Feedmill / Tarlac Hub","lat":15.606,"lon":120.596,"type":"Feedmill"},
{"name":"Ilagan Feedmill / Isabela","lat":17.148,"lon":121.889,"type":"Feedmill"},
{"name":"Metro Manila / NCR","lat":14.5995,"lon":120.9842,"type":"Delivery area"},
{"name":"Cebu","lat":10.3157,"lon":123.8854,"type":"Visayas hub"},
{"name":"Davao","lat":7.1907,"lon":125.4553,"type":"Mindanao hub"},
{"name":"General Santos","lat":6.1164,"lon":125.1716,"type":"Mindanao delivery area"},
{"name":"Cagayan de Oro","lat":8.4542,"lon":124.6319,"type":"Mindanao delivery area"}]

def now_iso(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def fetch(url,binary=False):
    sep="&" if "?" in url else "?"
    req=Request(url+f"{sep}_cb={int(time.time())}",headers={"User-Agent":"CPF-Logistics-PAGASA-Monitor/3.0","Cache-Control":"no-cache","Pragma":"no-cache"})
    data=urlopen(req,timeout=45).read()
    return data if binary else data.decode("utf-8",errors="ignore")
def pdf_text(url):
    data=fetch(url,True)
    if data[:4]!=b"%PDF": raise RuntimeError("Not PDF: "+url)
    return "\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(data)).pages)
def clean(s): return re.sub(r"\s+"," ",(s or "")).strip()
def rx(text,pat,default=""):
    m=re.search(pat,text,flags=re.I|re.S); return clean(m.group(1)) if m else default

def find_latest_pdf():
    html=fetch(PAGE_URL)
    nums=[int(n) for n in re.findall(r"Tropical Cyclone Bulletin\s*#\s*(\d+)",html,re.I)]
    for h in re.findall(r'href=["\']([^"\']+)["\']',html,re.I):
        if STORM_NAME in h.lower() and "tcb" in h.lower() and ".pdf" in h.lower():
            m=re.search(r"tcb(?:%23|#)?(\d+)",h,re.I)
            if m: nums.append(int(m.group(1)))
    start=max(nums) if nums else 20
    for n in range(start+2,0,-1):
        url=f"{PDF_BASE}TCB%23{n}_{STORM_NAME}.pdf"
        try:
            data=fetch(url,True)
            if data[:4]==b"%PDF" and len(data)>1000:
                return url,data
        except Exception:
            pass
    raise RuntimeError("No reachable PAGASA TCB archive PDF found.")

def parse_tcb(text,url):
    no=rx(text,r"TROPICAL CYCLONE BULLETIN\s*(?:NR\.|#)?\s*(\d+)")
    issued=rx(text,r"Issued at\s*([0-9: ]+(?:AM|PM),?\s*\d{1,2}\s+\w+\s+\d{4})")
    valid=rx(text,r"Valid for broadcast until\s*(.*?)\.")
    category=rx(text,r"TROPICAL CYCLONE BULLETIN\s*(?:NR\.|#)?\s*\d+\s*([A-Za-z ]+)\s+INDAY","Typhoon")
    ctime=rx(text,r"Location of Center\s*\((.*?)\)")
    pos=rx(text,r"estimated.*?at\s+(.*?)\s*\(\s*\d{1,2}\.\d")
    coord=re.search(r"\(\s*(\d{1,2}\.\d)\s*°?\s*N,\s*(\d{2,3}\.\d)\s*°?\s*E\s*\)",text)
    lat=float(coord.group(1)) if coord else None; lon=float(coord.group(2)) if coord else None
    wind=rx(text,r"Maximum sustained winds of\s*(\d+)\s*km/h"); gust=rx(text,r"gustiness of up to\s*(\d+)\s*km/h"); pres=rx(text,r"central pressure of\s*(\d+)\s*hPa")
    move=rx(text,r"Present Movement\s*([A-Za-z \-]+ at\s*\d+\s*km/h)"); ms=rx(move,r"(\d+)\s*km/h")
    extent=rx(text,r"extend(?:s)? outwards up to\s*(\d+)\s*km")
    track=[]
    if lat and lon:
        track.append({"label":"Current","forecast_time":f"Current: {ctime}, {issued.split(',')[-1].strip() if issued else ''}","lat":lat,"lon":lon,"position_description":pos,"max_winds_kmh":int(wind) if wind else None,"gustiness_kmh":int(gust) if gust else None,"central_pressure_hpa":int(pres) if pres else None,"category":category,"movement_direction":move.split(" at ")[0].strip() if move else "","movement_speed_kmh":int(ms) if ms else None})
    one=re.sub(r"\s+"," ",text)
    pat=re.compile(r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s+(\d{1,2}\s+\w+\s+\d{4})\s+(\d{1,2}\.\d)\s+(\d{2,3}\.\d)\s+(.*?)\s+(\d{2,3})\s+(STY|TY|STS|TS|TD)\s+([A-Z]+)\s+(\d+)",re.I)
    seen=set()
    for m in pat.finditer(one):
        key=m.group(1,2,3,4)
        if key in seen: continue
        seen.add(key)
        track.append({"forecast_time":f"{m.group(1)}, {m.group(2)}","lat":float(m.group(3)),"lon":float(m.group(4)),"position_description":clean(m.group(5)),"max_winds_kmh":int(m.group(6)),"category":CAT.get(m.group(7).upper(),m.group(7).upper()),"movement_direction":m.group(8).upper(),"movement_speed_kmh":int(m.group(9))})
    sig2=rx(text,r"TCWS No\..*?\n2\s+Wind threat:.*?winds\s+(Batanes)\s+-\s+-")
    sig1=rx(text,r"Wind threat:\s*Strong\s*winds\s*Luzon\s*(.*?)\s+-\s+-\s*Warning lead time")
    tcws=[]
    if sig2: tcws.append({"signal":"TCWS No. 2","areas_raw":sig2,"wind_range_kmh":"62–88","potential_impact":"Minor to moderate threat to life and property."})
    if sig1: tcws.append({"signal":"TCWS No. 1","areas_raw":sig1,"wind_range_kmh":"39–61","potential_impact":"Minimal to minor threat to life and property."})
    storm={"name":"INDAY","international_name":"BAVI","category":category,"current_center_time":ctime,"lat":lat,"lon":lon,"position_description":pos,"max_winds_kmh":int(wind) if wind else None,"gustiness_kmh":int(gust) if gust else None,"central_pressure_hpa":int(pres) if pres else None,"movement":move,"movement_speed_kmh":int(ms) if ms else None,"wind_extent_km":int(extent) if extent else None}
    return {"source_type":"TCB","number":f"Tropical Cyclone Bulletin No. {no}","issued_at":issued,"valid_until":valid,"source_url":url},storm,track,tcws

def parse_pdf_source(url,label):
    try:
        t=pdf_text(url); return [{"source":label,"issued_at":rx(t,r"Issued at:?\s*(.*?)\n"),"details_raw":clean(t)[:5000]}]
    except Exception as e: return [{"source":label,"details_raw":"Unable to fetch/parse: "+str(e)}]

def make_risks():
    return [
{"area_route":"Batanes / Extreme Northern Luzon","commodity_affected":"All dispatch","main_hazard":"TCWS No. 2; gale-force winds; very rough seas","risk_level":"Critical","recommended_dispatch_action":"Suspend dispatch until further notice","remarks":"Do not release trips without official clearance and validated safe passage."},
{"area_route":"Cagayan, Babuyan Islands, Isabela, Apayao, Ilocos Norte, northern Aurora, Catanduanes","commodity_affected":"Feeds, live animals, RM transfers","main_hazard":"TCWS No. 1; strong winds; exposed coastal/upland risk","risk_level":"High","recommended_dispatch_action":"Require route validation before dispatch","remarks":"Validate road passability, wind exposure, receiving readiness, GPS status, and contractor readiness."},
{"area_route":"Northern/eastern seaboards of Luzon and eastern Visayas","commodity_affected":"Sea/RORO-dependent transfers, containers","main_hazard":"Gale warning; rough to very rough seas","risk_level":"Critical","recommended_dispatch_action":"Hold dispatch","remarks":"Hold small seacraft/motorbanca-dependent movement; validate larger vessels with Coast Guard, ports, and shipping lines."},
{"area_route":"Central Luzon, NCR/CALABARZON, Visayas and Mindanao routes not under hold/suspension","commodity_affected":"All commodities","main_hazard":"Habagat/rain/gust exposure and local passability changes","risk_level":"Moderate","recommended_dispatch_action":"Dispatch with caution","remarks":"Coordinate route validation and update Affected Delivery Updates file."}]

def main():
    old={}
    if OUT_PATH.exists():
        try: old=json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception: old={}
    try:
        url,pdf=find_latest_pdf()
        text="\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(pdf)).pages)
        upd,storm,track,tcws=parse_tcb(text,url)
        rainfall=parse_pdf_source(ADVISORY_URL,"Weather Advisory")
        marine=parse_pdf_source(GALE_URL,"Gale Warning")
        risks=make_risks()
        changes=[]
        if old.get("latest_pagasa_update",{}).get("number")!=upd.get("number"):
            changes.append(f"Bulletin updated: {old.get('latest_pagasa_update',{}).get('number','None')} → {upd.get('number')}")
        for label,key in [("Maximum sustained winds","max_winds_kmh"),("Gustiness","gustiness_kmh"),("Central pressure","central_pressure_hpa"),("Movement","movement"),("Wind extent","wind_extent_km")]:
            if old.get("storm",{}).get(key)!=storm.get(key): changes.append(f"{label} changed: {old.get('storm',{}).get(key,'N/A')} → {storm.get(key)}")
        if not changes: changes.append("Checked PAGASA sources; no material change versus last verified JSON.")
        data={"last_checked":now_iso(),"latest_pagasa_update":upd,"storm":storm,"forecast_track":track,"tcws":tcws or [],"rainfall":rainfall,"marine":marine,"regional_advisories":[],"cpf_hubs":CPF_HUBS,"cpf_risk_matrix":risks,"dispatch_recommendations":[{"action":r["recommended_dispatch_action"],"area":r["area_route"],"reason":r["main_hazard"]} for r in risks],"contractor_advisory":"All freight contractors must validate route, weather, road, bridge, and port conditions before dispatch. Keep GPS active and report flooding, landslides, road closures, port cancellations, strong winds, route deviations, or delays immediately.","change_log":changes,"source_urls":{"tcb":url,"weather_advisory":ADVISORY_URL,"gale":GALE_URL},"reliability_notes":["PAGASA is the official source of truth.","Satellite imagery is visual context only and must not override PAGASA values."]}
        if not storm.get("lat") or not track: raise RuntimeError("Extraction incomplete; missing storm coordinates or track.")
        OUT_PATH.parent.mkdir(parents=True,exist_ok=True)
        OUT_PATH.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        print("Wrote",OUT_PATH,"from",url)
    except Exception as e:
        if old:
            old["last_checked"]=now_iso(); old.setdefault("errors",[]).append({"time":now_iso(),"message":str(e)})
            OUT_PATH.write_text(json.dumps(old,ensure_ascii=False,indent=2),encoding="utf-8")
            print("Extraction failed; retained previous JSON:",e)
        else: raise
if __name__=="__main__": main()
