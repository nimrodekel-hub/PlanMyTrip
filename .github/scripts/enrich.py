"""
enrich.py — BigDataCloud (address) + Overpass/OSM (phone, hours, website, type)
Target runtime: <3 minutes for 150 places.
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── BigDataCloud: reverse-geocode lat/lng → readable address ─────────────────
def bigdatacloud_address(place):
    pid = place.get("id", "")
    lat, lng = place.get("lat"), place.get("lng")
    if not lat or not lng:
        return {"id": pid}
    url = (
        "https://api.bigdatacloud.net/data/reverse-geocode-client"
        f"?latitude={lat}&longitude={lng}&localityLanguage=en"
    )
    try:
        with urlopen(Request(url, headers={"User-Agent": "PlanMyTrip/1.0"}), timeout=10) as r:
            d = json.loads(r.read())

        # Use flat fields — reliable, local-level address
        # locality = neighborhood/ward, city = city, principalSubdivision = state/province
        # countryName = country. Deduplicate while preserving order.
        parts = []
        for key in ("locality", "city", "principalSubdivision", "countryName"):
            v = (d.get(key) or "").strip()
            if v and v not in parts:
                parts.append(v)

        # Deduplicate consecutive duplicates (Tokyo often appears twice)
        addr = ", ".join(parts[:4])
        if addr:
            return {"id": pid, "address": addr}

        # Last resort: display_name from administrative hierarchy
        admin = (d.get("localityInfo") or {}).get("administrative") or []
        admin_names = [
            i.get("name", "") for i in admin
            if isinstance(i, dict) and i.get("name")
            and isinstance(i.get("order"), (int, float))
            and 6 <= i["order"] <= 12  # city/district level, not continent
        ]
        if admin_names:
            return {"id": pid, "address": ", ".join(admin_names[:3])}

        return {"id": pid}
    except Exception as exc:
        print(f"  BDC err [{place.get('name','')}]: {exc}", file=sys.stderr)
        return {"id": pid}


# ── Overpass/OSM: phone, hours, website, type ────────────────────────────────
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

OSM_TYPE_MAP = {
    "restaurant": "Restaurant", "cafe": "Café", "bar": "Bar",
    "fast_food": "Fast Food", "hotel": "Hotel", "hostel": "Hostel",
    "museum": "Museum", "gallery": "Gallery", "theatre": "Theatre",
    "cinema": "Cinema", "hospital": "Hospital", "pharmacy": "Pharmacy",
    "clothes": "Clothing Store", "supermarket": "Supermarket",
    "convenience": "Convenience Store", "bakery": "Bakery",
    "mall": "Shopping Mall", "department_store": "Department Store",
    "shoes": "Shoe Store", "books": "Bookstore", "electronics": "Electronics",
    "toys": "Toy Store", "gift": "Gift Shop",
    "attraction": "Attraction", "viewpoint": "Viewpoint",
    "zoo": "Zoo", "theme_park": "Theme Park",
    "park": "Park", "garden": "Garden",
}


def _name_score(a, b):
    def tok(s):
        return set(re.split(r"[\s\-_\./\\,]+", s.lower())) - {"the", "a", "of", "in", "at"}
    qt, ct = tok(a), tok(b)
    if not qt or not ct:
        return 0.0
    return len(qt & ct) / max(len(qt), len(ct))


def overpass_enrich(place, radius=150):
    lat, lng = place.get("lat"), place.get("lng")
    name = place.get("name", "")
    if not lat or not lng:
        return {}
    query = (
        f"[out:json][timeout:6];"
        f"(node(around:{radius},{lat},{lng})[\"name\"];"
        f"way(around:{radius},{lat},{lng})[\"name\"];);"
        f"out body;"
    )
    try:
        data = urlencode({"data": query}).encode()
        req = Request(OVERPASS_URL, data=data, headers={"User-Agent": "PlanMyTrip/1.0"})
        with urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        elements = resp.get("elements", [])
        # Lower threshold 0.12→0.08 to catch partial matches (e.g. "Harajuku" in "Harajuku Gyoen")
        best_tags, best_score = None, 0.08
        for el in elements:
            tags = el.get("tags", {})
            # Prefer name:en for English comparison (critical for Japanese OSM nodes)
            el_name_en = tags.get("name:en") or tags.get("name:ja_rm") or ""
            el_name_local = tags.get("name") or ""
            score = max(
                _name_score(name, el_name_en) if el_name_en else 0.0,
                _name_score(name, el_name_local) if el_name_local else 0.0,
            )
            if score > best_score:
                best_score, best_tags = score, tags
        if not best_tags:
            return {}
        result = {}
        addr_parts = [best_tags.get(k, "").strip()
                      for k in ("addr:housenumber", "addr:street", "addr:city", "addr:country")
                      if best_tags.get(k)]
        if addr_parts:
            result["address"] = ", ".join(addr_parts)
        phone = (best_tags.get("contact:phone") or best_tags.get("phone")
                 or best_tags.get("contact:mobile"))
        if phone:
            result["phone"] = phone.strip()
        web = best_tags.get("website") or best_tags.get("contact:website") or best_tags.get("url")
        if web:
            result["website"] = web.strip()
        hours = best_tags.get("opening_hours")
        if hours:
            result["todayHours"] = hours.strip()
        for tk in ("amenity", "shop", "tourism", "leisure", "office"):
            raw = best_tags.get(tk, "")
            if raw:
                result["placeType"] = OSM_TYPE_MAP.get(raw, raw.replace("_", " ").title())
                break
        if result:
            print(f"  OSM [{name[:25]}] score={best_score:.2f} "
                  f"ph={'Y' if result.get('phone') else 'N'} "
                  f"wb={'Y' if result.get('website') else 'N'} "
                  f"hr={'Y' if result.get('todayHours') else 'N'}")
        return result
    except Exception as exc:
        print(f"  OSM err [{name[:20]}]: {exc}", file=sys.stderr)
        return {}


def overpass_worker(subset):
    results = {}
    for i, place in enumerate(subset):
        if i > 0:
            time.sleep(0.5)
        results[place.get("id", "")] = overpass_enrich(place)
    return results


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    try:
        with open("/tmp/places.json", encoding="utf-8") as f:
            places = json.load(f)
        print(f"Loaded {len(places)} places")
        if places:
            print(f"  sample: {places[0].get('name')} lat={places[0].get('lat')} lng={places[0].get('lng')}")
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        json.dump([], open("/tmp/enriched.json", "w"))
        sys.exit(0)

    # Phase 1: BigDataCloud — addresses in parallel (~15s)
    print("Phase 1: BigDataCloud addresses (parallel, 20 workers)...")
    enriched_map = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(bigdatacloud_address, p): p for p in places}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                enriched_map[r["id"]] = r
            except Exception:
                p = futs[fut]
                enriched_map[p.get("id", "")] = {"id": p.get("id", "")}
    addr_c = sum(1 for r in enriched_map.values() if r.get("address"))
    # Show sample addresses for verification
    samples = [(r.get("address",""), places[i].get("name",""))
               for i, r in enumerate(list(enriched_map.values())[:3]) if r.get("address")]
    for addr, nm in samples:
        print(f"  sample address [{nm[:20]}]: {addr}")
    print(f"  Phase 1 done: {addr_c}/{len(places)} addresses")

    # Phase 2: Overpass — phone/hours/website via 2 parallel workers (~90s)
    print("Phase 2: Overpass/OSM phone+hours+website (2 workers, 5s timeout)...")
    mid = len(places) // 2
    subsets = [places[:mid], places[mid:]]
    osm_map = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs2 = [ex.submit(overpass_worker, s) for s in subsets]
        for fut in as_completed(futs2):
            try:
                osm_map.update(fut.result())
            except Exception as exc:
                print(f"  OSM worker err: {exc}", file=sys.stderr)

    osm_hits = 0
    for pid, osm_data in osm_map.items():
        if not osm_data:
            continue
        osm_hits += 1
        r = enriched_map.get(pid, {"id": pid})
        for k, v in osm_data.items():
            if v and not r.get(k):
                r[k] = v
        enriched_map[pid] = r
    print(f"  Phase 2 done: OSM data for {osm_hits}/{len(places)} places")

    enriched = list(enriched_map.values())
    addr_c  = sum(1 for r in enriched if r.get("address"))
    phone_c = sum(1 for r in enriched if r.get("phone"))
    web_c   = sum(1 for r in enriched if r.get("website"))
    hours_c = sum(1 for r in enriched if r.get("todayHours"))
    print(f"Final: {len(enriched)} places | addr={addr_c} phone={phone_c} web={web_c} hours={hours_c}")

    with open("/tmp/enriched.json", "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False)
    print(f"Wrote {os.path.getsize('/tmp/enriched.json')} bytes to /tmp/enriched.json")


if __name__ == "__main__":
    main()
