"""
enrich.py — Google Maps internal /maps/preview/place endpoint (NO Places API, NO key).

Each place in the imported list carries two decimal IDs (the Feature ID parts,
from entitylist/getlist loc[6]). We rebuild the FID `0xHEX1:0xHEX2`, query the
public /maps/preview/place endpoint, and parse the rich data array:

  d6[39]  full formatted address
  d6[166] short locality ("Shibuya, Tokyo, Japan")
  d6[4][7] rating (float 1-5)
  d6[13][0] category ("Perfume store", "Japanese restaurant", "Cafe")
  d6[178][0][0] phone ("+81 3-6804-5470")
  d6[7][1] website domain ("retaw.tokyo")
  d6[203] weekly opening hours
  d6[11]  exact place name
  d6[78]  Google Place ID (ChIJ...)

Target runtime: <2 minutes for 150 places (8 parallel workers).
"""
import datetime
import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]


def to_hex(n):
    """Decimal (possibly signed 64-bit) → unsigned hex string."""
    n = int(n)
    if n < 0:
        n += 2 ** 64
    return format(n, "x")


def _g(arr, i):
    try:
        return arr[i]
    except Exception:
        return None


def extract_place(data):
    """Parse the /maps/preview/place response array → flat dict."""
    try:
        d6 = data[6]
    except Exception:
        return {}
    out = {}

    # ── exact name ──
    name = _g(d6, 11)
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()

    # ── address: prefer full formatted (d6[39]); fall back to short (d6[166]) ──
    addr = _g(d6, 39)
    if isinstance(addr, str) and addr.strip():
        out["address"] = addr.strip()
    else:
        short = _g(d6, 166)
        if isinstance(short, str) and short.strip():
            out["address"] = short.strip()

    # ── country (explicit) — d6[2][0] full name, d6[88][2][1] ISO code ──
    # This makes country grouping work for EVERY country, not a hardcoded list.
    comp = _g(d6, 2)
    if isinstance(comp, list) and comp and isinstance(comp[0], str) and comp[0].strip():
        out["country"] = comp[0].strip()
    try:
        cc = d6[88][2][1]
        if isinstance(cc, str) and len(cc) == 2:
            out["countryCode"] = cc.upper()
    except Exception:
        pass

    # ── rating: d6[4][7] ──
    r4 = _g(d6, 4)
    if isinstance(r4, list) and len(r4) > 7 and isinstance(r4[7], (int, float)):
        if 1.0 <= r4[7] <= 5.0:
            out["rating"] = round(float(r4[7]), 1)

    # ── category / place type: d6[13][0] ──
    t13 = _g(d6, 13)
    if isinstance(t13, list) and t13 and isinstance(t13[0], str):
        out["placeType"] = t13[0].strip()

    # ── phone: d6[178][0][0] ──
    p178 = _g(d6, 178)
    try:
        phone = p178[0][0]
        if isinstance(phone, str) and phone.strip():
            out["phone"] = phone.strip()
    except Exception:
        pass

    # ── website: d6[7][1] (clean domain) ──
    w7 = _g(d6, 7)
    if isinstance(w7, list) and len(w7) > 1 and isinstance(w7[1], str) and w7[1].strip():
        site = w7[1].strip()
        out["website"] = site if site.startswith("http") else "https://" + site

    # ── Google Place ID: d6[78] ──
    pid = _g(d6, 78)
    if isinstance(pid, str) and pid.startswith("ChIJ"):
        out["placeId"] = pid

    # ── opening hours: d6[203] → today's hours string ──
    h = _g(d6, 203)
    if isinstance(h, list):
        today_name = WEEKDAYS[datetime.date.today().weekday()]
        for el in h:
            try:
                day = el[0]
                if day[0] == today_name:
                    hrs = day[3][0][0]  # e.g. "12–7:30 PM"
                    if isinstance(hrs, str) and hrs.strip():
                        out["todayHours"] = hrs.strip()
                    break
            except Exception:
                continue

    return out


def fetch_one(place):
    pid = place.get("id", "")
    cid1 = place.get("_cid1")
    cid2 = place.get("_cid2")
    lat = place.get("lat")
    lng = place.get("lng")
    if cid1 is None or cid2 is None or lat is None or lng is None:
        return {"id": pid}
    fid = "0x" + to_hex(cid1) + ":0x" + to_hex(cid2)
    pb = (
        f"!1m18!1s{fid}!3m12!1m3!1d1000!2d{lng}!3d{lat}"
        f"!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1"
        f"!4m2!3d{lat}!4d{lng}!5e0!12m1!1e1"
    )
    url = "https://www.google.com/maps/preview/place?authuser=0&hl=en&gl=us&pb=" + pb
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
        start = txt.index("[")
        data = json.loads(txt[start:])
        result = extract_place(data)
        result["id"] = pid
        fields = [k for k in ("address", "rating", "phone", "website", "todayHours", "placeType") if result.get(k)]
        print(f"  [{place.get('name','')[:25]}] → {', '.join(fields) or '(no data)'}")
        return result
    except Exception as exc:
        print(f"  ERR [{place.get('name','')[:20]}]: {exc}", file=sys.stderr)
        return {"id": pid}


def main():
    try:
        with open("/tmp/places.json", encoding="utf-8") as f:
            places = json.load(f)
        print(f"Loaded {len(places)} places")
        if places:
            p0 = places[0]
            print(f"  sample: {p0.get('name')} cid=({p0.get('_cid1')},{p0.get('_cid2')}) "
                  f"lat={p0.get('lat')} lng={p0.get('lng')}")
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        json.dump([], open("/tmp/enriched.json", "w"))
        sys.exit(0)

    print("Fetching place details via /maps/preview/place (8 workers)...")
    enriched = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_one, p): p for p in places}
        for fut in as_completed(futs):
            try:
                enriched.append(fut.result())
            except Exception:
                p = futs[fut]
                enriched.append({"id": p.get("id", "")})

    addr_c = sum(1 for r in enriched if r.get("address"))
    rate_c = sum(1 for r in enriched if r.get("rating"))
    phone_c = sum(1 for r in enriched if r.get("phone"))
    web_c = sum(1 for r in enriched if r.get("website"))
    hours_c = sum(1 for r in enriched if r.get("todayHours"))
    type_c = sum(1 for r in enriched if r.get("placeType"))
    print(f"Final: {len(enriched)} places | addr={addr_c} rating={rate_c} "
          f"phone={phone_c} web={web_c} hours={hours_c} type={type_c}")

    with open("/tmp/enriched.json", "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False)
    print(f"Wrote {os.path.getsize('/tmp/enriched.json')} bytes")


if __name__ == "__main__":
    main()
