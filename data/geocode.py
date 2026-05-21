"""
Address geocoding via geopy Nominatim.
Converts a free-text address string to zip_code, city, lat, lng.
Results cached to data/geocode_cache.json to avoid repeated network calls
and to stay within Nominatim's 1-req/s rate limit.
"""

import json
import time
import pathlib
import logging
from typing import Optional

log = logging.getLogger(__name__)

CACHE_PATH = pathlib.Path(__file__).parent / "geocode_cache.json"

TARGET_CITIES = {
    "Berkeley", "Orinda", "Moraga", "Lafayette",
    "Walnut Creek", "Alamo", "Danville", "San Ramon",
}

ZIP_TO_CITY = {
    "94702": "Berkeley",  "94703": "Berkeley",  "94704": "Berkeley",
    "94705": "Berkeley",  "94706": "Berkeley",  "94707": "Berkeley",
    "94708": "Berkeley",  "94709": "Berkeley",  "94710": "Berkeley",
    "94563": "Orinda",
    "94556": "Moraga",
    "94549": "Lafayette",
    "94595": "Walnut Creek", "94596": "Walnut Creek",
    "94597": "Walnut Creek", "94598": "Walnut Creek",
    "94507": "Alamo",
    "94506": "Danville",  "94526": "Danville",
    "94582": "San Ramon", "94583": "San Ramon",
}

_cache: Optional[dict] = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        if CACHE_PATH.exists():
            try:
                _cache = json.loads(CACHE_PATH.read_text())
            except Exception:
                _cache = {}
        else:
            _cache = {}
    return _cache


def _save_cache(cache: dict):
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.warning(f"Could not save geocode cache: {e}")


def geocode(address: str) -> dict:
    """
    Geocode a US address string.

    Returns a dict with keys:
      address, zip_code, city, lat, lng, in_target (bool)
    Returns {} on failure.
    """
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut, GeocoderServiceError

    cache = _load_cache()
    key = address.strip().lower()
    if key in cache:
        return cache[key]

    query = address.strip()
    upper = query.upper()
    if "CA" not in upper and "CALIFORNIA" not in upper:
        query += ", CA, USA"

    geolocator = Nominatim(user_agent="ca-home-pricing-model/1.0")
    try:
        location = geolocator.geocode(query, addressdetails=True, timeout=10)
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        log.warning(f"Geocoding error for '{address}': {e}")
        return {}

    if not location:
        log.warning(f"No geocode result for: {address}")
        return {}

    addr = location.raw.get("address", {})
    postcode = addr.get("postcode", "").strip()[:5]

    # Resolve city: zip mapping is authoritative; fall back to Nominatim city field
    resolved_city = ZIP_TO_CITY.get(postcode)
    if not resolved_city:
        raw_city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("suburb", "")
        )
        for tc in TARGET_CITIES:
            if tc.lower() in raw_city.lower():
                resolved_city = tc
                break

    result = {
        "address":   address.strip(),
        "zip_code":  postcode,
        "city":      resolved_city or "",
        "lat":       float(location.latitude),
        "lng":       float(location.longitude),
        "in_target": resolved_city is not None,
    }

    cache[key] = result
    _save_cache(cache)
    time.sleep(1.1)  # Nominatim: max 1 req/s
    return result
