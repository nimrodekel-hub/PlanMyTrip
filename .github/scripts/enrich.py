"""
enrich.py — enriches places using BigDataCloud (address) + Overpass/OSM (phone, hours, website, type)
Both services are free with no API key required.

Reads  /tmp/places.json   [{id, name, lat, lng, _cid}]
Writes /tmp/enriched.json [{id, address?, phone?, website?, todayHours?, placeType?}]
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── BigDataCloud: reverse-geocode lat/lng → address ──────────────────────────
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
        with urlopen(Request(url, headers={"User-Agent": "PlanMyTrip/1.0"}), timeout=12) as r:
            d = json.loads(r.read())
        # Prefer informative hierarchy (more human-readable)
        infos = (d.get("localityInfo") or {}).get("informative") or []
        names = [
            i["name"] for i in infos
            if isinstance(i, dict) and i.get("name")
            and isinstance(i.get("order"), (int, float)) and i["order"] <= 4
        ]
        if names:
            return {"id": pid, "address": ", ".join(names[:4])}
        # Fallback to flat fields
        parts = []
        for key in ("locality", "city", "principalSubdivision", "countryName"):
            v = (d.get(key) or "").strip()
            if v and v not in parts:
                parts.append(v)
        addr = ", ".join(parts[:3])
        return {"id": pid, "address": addr} if addr else {"id": pid}
    except Exception as exc:
        print(f"  BDC error [{place.get('name','')}]: {exc}", file=sys.stderr)
        return {"id": pid}


# ── Overpass API: search OSM within radius for matching POI ──────────────────
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

OSM_TYPE_MAP = {
    # amenity values
    "restaurant": "Restaurant", "cafe": "Café", "bar": "Bar",
    "fast_food": "Fast Food", "food_court": "Food Court",
    "hotel": "Hotel", "hostel": "Hostel", "guest_house": "Guest House",
    "museum": "Museum", "gallery": "Gallery", "theatre": "Theatre",
    "cinema": "Cinema", "library": "Library", "place_of_worship": "Place of Worship",
    "hospital": "Hospital", "pharmacy": "Pharmacy", "bank": "Bank",
    "parking": "Parking", "fuel": "Gas Station",
    # shop values
    "clothes": "Clothing Store", "supermarket": "Supermarket",
    "convenience": "Convenience Store", "bakery": "Bakery",
    "mall": "Shopping Mall", "department_store": "Department Store",
    "shoes": "Shoe Store", "books": "Bookstore", "electronics": "Electronics",
    "toys": "Toy Store", "gift": "Gift Shop", "jewelry": "Jewelry",
    # tourism values
    "attraction": "Attraction", "viewpoint": "Viewpoint",
    "artwork": "Artwork", "information": "Information",
    "zoo": "Zoo", "theme_park": "Theme Park",
    # leisure values
    "park": "Park", "garden": "Garden", "beach_resort": "Beach",
    "sports_centre": "Sports Centre", "fitness_centre": "Gym",
}

def _name_score(query, candidate):
    """Simple token-overlap similarity (0-1)."""
    def tokens(s):
        return set(re.split(r"[\s\-_\./\\,]+", s.lower())) - {"the", "a", "of", "in", "at"}
    qt, ct = tokens(query), tokens(candidate)
    if not qt or not ct:
        return 0.0
    return len(qt & ct) / max(len(qt), len(ct))

def overpass_enrich(place, radius=80):
    lat, lng = place.get("lat"), place.get("lng")
    name = place.get("name", "")
    if not lat or not lng:
        return {}

    query = (
        f"[out:json][timeout:15];"
        f"(node(around:{radius},{lat},{lng})[\"name\"];"
        f"way(around:{radius},{lat},{lng})[\"name\"];);"
        f"out body;"
    )
    try:
        data = urlencode({"data": query}).encode()
        req = Request(OVERPASS_URL, data=data, headers={"User-Agent": "PlanMyTrip/1.0"})
        with urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())

        elements = resp.get("elements", [])
        if not elements:
            return {}

        # Find best-matching element by name similarity
        best_tags, best_score = None, 0.1  # min threshold
        for el in elements:
            tags = el.get("tags", {})
            el_name = tags.get("name") or tags.get("name:en") or ""
            score = _name_score(name, el_name) if el_name else 0.0
            if score > best_score:
                best_score, best_tags = score, tags

        if not best_tags:
            return {}

        result = {}

        # Address from OSM addr:* tags
        addr_parts = []
        for k in ("addr:housenumber", "addr:street", "addr:suburb",
                  "addr:city", "addr:state", "addr:country"):
            v = best_tags.get(k, "").strip()
            if v:
                addr_parts.append(v)
        if addr_parts:
            result["address"] = ", ".join(addr_parts)

        # Phone
        phone = (best_tags.get("contact:phone") or best_tags.get("phone")
                 or best_tags.get("contact:mobile"))
        if phone:
            result["phone"] = phone.strip()

        # Website
        web = (best_tags.get("website") or best_tags.get("contact:website")
               or best_tags.get("url"))
        if web:
            result["website"] = web.strip()

        # Opening hours (store as string; UI can display it)
        hours = best_tags.get("opening_hours")
        if hours:
            result["todayHours"] = hours.strip()

        # Place type (map OSM tag → readable English)
        for type_key in ("amenity", "shop", "tourism", "leisure", "office"):
            raw = best_tags.get(type_key, "")
            if raw:
                result["placeType"] = OSM_TYPE_MAP.get(raw, raw.replace("_", " ").title())
                break

        print(f"  OSM match [{name}] score={best_score:.2f}: "
              f"addr={'Y' if result.get('address') else 'N'} "
              f"phone={'Y' if result.get('phone') else 'N'} "
              f"web={'Y' if result.get('website') else 'N'} "
              f"hours={'Y' if result.get('todayHours') else 'N'}")
        return result

    except Exception as exc:
        print(f"  Overpass error [{name}]: {exc}", file=sys.stderr)
        return {}


def overpass_sequential(places_subset):
    """Run Overpass queries sequentially with 1s sleep (rate-limit safe)."""
    results = {}
    for i, place in enumerate(places_subset):
        if i > 0:
            time.sleep(1.05)  # Overpass: max 1 req/s per worker
        pid = place.get("id", "")
        results[pid] = overpass_enrich(place)
    return results


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    try:
        with open("/tmp/places.json", encoding="utf-8") as f:
            places = json.load(f)
        print(f"Loaded {len(places)} places")
    except Exception as exc:
        print(f"FATAL: cannot read places.json: {exc}", file=sys.stderr)
        json.dump([], open("/tmp/enriched.json", "w"))
        sys.exit(0)

    # ── Phase 1: BigDataCloud addresses (parallel, ~15 seconds) ──────────────
    print("Phase 1: BigDataCloud geocoding (parallel)...")
    enriched_map = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(bigdatacloud_address, p): p for p in places}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                enriched_map[r["id"]] = r
            except Exception as exc:
                p = futs[fut]
                enriched_map[p.get("id", "")] = {"id": p.get("id", "")}
            if done % 40 == 0:
                print(f"  BDC: {done}/{len(places)}")
    addr_c = sum(1 for r in enriched_map.values() if r.get("address"))
    print(f"  Phase 1 done: {addr_c}/{len(places)} addresses")

    # ── Phase 2: Overpass/OSM details (2 parallel workers, ~90 seconds) ──────
    print("Phase 2: Overpass/OSM details (2 workers, 1 req/s each)...")
    mid = len(places) // 2
    subsets = [places[:mid], places[mid:]]
    osm_map = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs2 = [ex.submit(overpass_sequential, subset) for subset in subsets]
        for fut in as_completed(futs2):
            try:
                osm_map.update(fut.result())
            except Exception as exc:
                print(f"  Overpass worker error: {exc}", file=sys.stderr)

    # Merge OSM data into enriched_map (only fill missing fields)
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

    # ── Output ────────────────────────────────────────────────────────────────
    enriched = list(enriched_map.values())
    addr_c  = sum(1 for r in enriched if r.get("address"))
    phone_c = sum(1 for r in enriched if r.get("phone"))
    web_c   = sum(1 for r in enriched if r.get("website"))
    hours_c = sum(1 for r in enriched if r.get("todayHours"))
    type_c  = sum(1 for r in enriched if r.get("placeType"))
    print(f"Summary: {len(enriched)} places | "
          f"addr:{addr_c} | phone:{phone_c} | web:{web_c} | "
          f"hours:{hours_c} | type:{type_c}")

    with open("/tmp/enriched.json", "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False)
    print(f"Wrote {os.path.getsize('/tmp/enriched.json')} bytes to /tmp/enriched.json")


if __name__ == "__main__":
    main()
