"""
enrich.py — reverse-geocode places via BigDataCloud (free, no API key, parallel)
Called by .github/workflows/enrich-places.yml
Reads  /tmp/places.json   (list of {id, name, lat, lng, _cid})
Writes /tmp/enriched.json (list of {id, address?, ...})
"""
import json
import os
import sys
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed


def geocode(place):
    pid = place.get("id", "")
    lat = place.get("lat")
    lng = place.get("lng")
    if not lat or not lng:
        return {"id": pid}
    url = (
        "https://api.bigdatacloud.net/data/reverse-geocode-client"
        f"?latitude={lat}&longitude={lng}&localityLanguage=en"
    )
    try:
        with urlopen(Request(url, headers={"User-Agent": "PlanMyTrip/1.0"}), timeout=12) as r:
            d = json.loads(r.read())
        # Prefer informative hierarchy (city / region / country)
        infos = (d.get("localityInfo") or {}).get("informative") or []
        names = [
            i["name"]
            for i in infos
            if isinstance(i, dict)
            and i.get("name")
            and isinstance(i.get("order"), (int, float))
            and i["order"] <= 4
        ]
        if names:
            return {"id": pid, "address": ", ".join(names[:4])}
        # Fallback: locality / city / country fields
        parts = []
        for key in ("locality", "city", "principalSubdivision", "countryName"):
            v = d.get(key, "") or ""
            if v and v not in parts:
                parts.append(v)
        addr = ", ".join(parts[:3])
        return {"id": pid, "address": addr} if addr else {"id": pid}
    except Exception as exc:
        name = place.get("name", "")
        print(f"  geocode error [{name}]: {exc}", file=sys.stderr)
        return {"id": pid}


def main():
    try:
        with open("/tmp/places.json", encoding="utf-8") as f:
            places = json.load(f)
        print(f"Loaded {len(places)} places")
    except Exception as exc:
        print(f"FATAL: cannot read places.json: {exc}", file=sys.stderr)
        json.dump([], open("/tmp/enriched.json", "w"))
        sys.exit(0)  # exit 0 so the workflow doesn't fail

    enriched = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(geocode, p): p for p in places}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                enriched.append(fut.result())
            except Exception as exc:
                enriched.append({"id": futs[fut].get("id", "")})
                print(f"  future error: {exc}", file=sys.stderr)
            if done % 30 == 0:
                print(f"  geocoded {done}/{len(places)}")

    addr_c = sum(1 for r in enriched if r.get("address"))
    print(f"Done: {len(enriched)} places | {addr_c} with address")

    with open("/tmp/enriched.json", "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False)
    print(f"Wrote {os.path.getsize('/tmp/enriched.json')} bytes to /tmp/enriched.json")


if __name__ == "__main__":
    main()
