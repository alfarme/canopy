"""
External API clients: PlantNet and OpenPlantbook.
Uses httpx (already a python-telegram-bot dependency).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ PlantNet


def parse_plantnet_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Parse PlantNet API response into a simplified list of matches.

    Returns list of dicts with keys: scientific_name, common_names, score.
    """
    results = []
    for item in data.get("results", []):
        species = item.get("species", {})
        results.append({
            "scientific_name": species.get("scientificNameWithoutAuthor", "Unknown"),
            "common_names": species.get("commonNames", []),
            "score": item.get("score", 0.0),
        })
    return results


async def identify_plant(image_data_list: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """
    Call PlantNet API to identify a plant from one or more images.

    Args:
        image_data_list: List of (filename, bytes) tuples (max 5).

    Returns:
        List of match dicts (scientific_name, common_names, score), sorted by score desc.

    Raises:
        httpx.HTTPStatusError on API errors.
    """
    api_key = os.environ.get("PLANTNET_API_KEY", "")
    url = "https://my-api.plantnet.org/v2/identify/all"

    files = [
        ("images", (filename, img_bytes, "image/jpeg"))
        for filename, img_bytes in image_data_list[:5]
    ]
    # `data` for multipart MUST be a Mapping (list-of-tuples is only valid for
    # `files`). httpx's encode_request() special-cases a non-Mapping `data` as
    # raw content and skips multipart encoding entirely, producing a sync
    # IteratorByteStream that then fails with "Attempted to send a sync
    # request with an AsyncClient instance." A dict with a list value is how
    # httpx represents a repeated form field (one "organs" per image).
    data = {"organs": ["auto"] * len(files)}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            params={"api-key": api_key},
            files=files,
            data=data,
        )
        resp.raise_for_status()
        return parse_plantnet_results(resp.json())


WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
# Wikipedia's API requires a descriptive User-Agent or it 403s anonymous
# requests; a bare httpx client gets rejected silently as "empty" JSON.
WIKIPEDIA_USER_AGENT = "CanopyBot/1.0 (private hobby project; contact via GitHub alfarme/canopy)"


async def fetch_reference_image(species: str) -> str | None:
    """
    Look up a representative photo for a scientific name via Wikipedia.

    PlantNet's /v2/identify/all response carries no reference images in our
    plan — verified empirically, results only have gbif/powo/score/species
    keys, no `images` field. Wikipedia's pageimages thumbnail is a reliable
    substitute for showing the user a comparison photo per candidate match.

    Returns a public image URL (Telegram fetches it directly, no download
    needed), or None if no thumbnail is found or the lookup fails.
    """
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": WIKIPEDIA_USER_AGENT}) as client:
        try:
            resp = await client.get(WIKIPEDIA_API_URL, params={
                "action": "query",
                "titles": species,
                "prop": "pageimages",
                "pithumbsize": 640,
                "format": "json",
                "redirects": 1,
            })
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                thumb = page.get("thumbnail", {}).get("source")
                if thumb:
                    return thumb
            return None
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            log.warning("event=reference_image_lookup_failed species=%r error=%r", species, exc)
            return None


# ------------------------------------------------------------------ OpenPlantbook


def map_plantbook_to_thresholds(data: dict[str, Any]) -> dict[str, int | None]:
    """
    Map OpenPlantbook API response fields to our thresholds table columns.

    Returns dict with keys matching thresholds table columns.
    """
    return {
        "min_soil_moist": data.get("min_soil_moist"),
        "max_soil_moist": data.get("max_soil_moist"),
        "min_soil_ec": data.get("min_soil_ec"),
        "max_soil_ec": data.get("max_soil_ec"),
        "min_light_lux": data.get("min_light_lux"),
        "max_light_lux": data.get("max_light_lux"),
        "min_temp": data.get("min_temp"),
        "max_temp": data.get("max_temp"),
    }


def has_usable_thresholds(thresholds: dict[str, int | None] | None) -> bool:
    """True only if the mapping exists and has at least one non-None numeric value.

    OpenPlantbook can return a row with some/all fields missing; map_* then
    yields a dict of Nones which is still truthy. Overwriting a plant's
    thresholds with all-None would nullify good values, so callers gate the
    re-seed on this instead of a bare truthiness check.

    Ignores metadata keys like 'match_type' — only checks threshold columns.
    """
    if thresholds is None:
        return False
    threshold_keys = {
        "min_soil_moist", "max_soil_moist", "min_soil_ec", "max_soil_ec",
        "min_light_lux", "max_light_lux", "min_temp", "max_temp",
    }
    return any(
        thresholds.get(k) is not None for k in threshold_keys
    )


# Conservative defaults when OpenPlantbook lookup fails
DEFAULT_THRESHOLDS = {
    "min_soil_moist": 15,
    "max_soil_moist": 65,
    "min_soil_ec": 100,
    "max_soil_ec": 3000,
    "min_light_lux": 200,
    "max_light_lux": 50000,
    "min_temp": 5,
    "max_temp": 35,
}


OPENPLANTBOOK_TOKEN_URL = "https://open.plantbook.io/api/v1/token/"

# Module-level cache: OpenPlantbook uses OAuth2 client_credentials, not a
# static API key. We exchange OPENPLANTBOOK_CLIENT_ID/SECRET for a short-lived
# bearer token once, then reuse it until it's near expiry (default TTL is
# unknown ahead of a real response, so we trust the returned expires_in).
_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_TOKEN_REFRESH_MARGIN = 60  # seconds


async def _get_plantbook_token(client: httpx.AsyncClient) -> str | None:
    """Return a valid OpenPlantbook bearer token, fetching/refreshing as needed."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - _TOKEN_REFRESH_MARGIN:
        return _token_cache["access_token"]

    client_id = os.environ.get("OPENPLANTBOOK_CLIENT_ID", "")
    client_secret = os.environ.get("OPENPLANTBOOK_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        log.warning("event=plantbook_auth_missing_credentials")
        return None

    resp = await client.post(
        OPENPLANTBOOK_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except (ValueError, KeyError) as exc:
        log.warning("event=plantbook_token_malformed_json error=%r", exc)
        return None
    _token_cache["access_token"] = data.get("access_token")
    if not _token_cache["access_token"]:
        log.warning("event=plantbook_token_missing_access_token")
        return None
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["access_token"]


OPENPLANTBOOK_SEARCH_URL = "https://open.plantbook.io/api/v1/plant/search"


async def _search_plantbook_pid(client: httpx.AsyncClient, token: str, species: str) -> str | None:
    """Resolve a scientific name to a real OpenPlantbook pid via the search
    endpoint. The detail endpoint's pid is a community-assigned slug, not a
    simple lowercase-with-spaces form of the binomial (e.g. our own PlantNet
    result "Rhaphidophora tetrasperma" 404s at .../rhaphidophora%20tetrasperma/
    but is findable via search) — guessing the pid directly misses real hits.
    """
    resp = await client.get(
        OPENPLANTBOOK_SEARCH_URL,
        params={"alias": species, "limit": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0]["pid"] if results else None


async def fetch_plantbook_thresholds(species: str) -> dict[str, int | None] | None:
    """
    Fetch threshold data from OpenPlantbook for a given species.

    First attempts an exact-species search. If that yields no match, retries
    with just the genus (first word of the binomial) and uses the best match's
    thresholds as a genus-level approximation.

    Args:
        species: Scientific binomial (e.g. "Monstera deliciosa").

    Returns:
        Mapped threshold dict with an additional "match_type" key:
          - "exact"  — exact species match found
          - "genus"  — genus-level fallback used (approximation)
        Returns None if lookup fails entirely (auth failure, no match at any
        level, or missing credentials).
    """
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
            token = await _get_plantbook_token(client)
            if token is None:
                return None

            # Try exact species search first
            pid = await _search_plantbook_pid(client, token, species)
            match_type = "exact"

            # Genus-level fallback: use first word of binomial
            if pid is None:
                genus = species.split()[0] if species.strip() else ""
                if genus and genus != species:
                    log.info(
                        "event=plantbook_exact_miss_trying_genus species=%r genus=%r",
                        species, genus,
                    )
                    pid = await _search_plantbook_pid(client, token, genus)
                    match_type = "genus"

            if pid is None:
                log.info("event=plantbook_no_match species=%r", species)
                return None

            # pid is URL-encoded by httpx automatically when passed in the path.
            # OpenPlantbook 301-redirects the non-trailing-slash form; httpx
            # doesn't follow redirects by default, so raise_for_status() would
            # throw on the 301 itself. Use the canonical trailing-slash URL.
            url = f"https://open.plantbook.io/api/v1/plant/detail/{pid}/"
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            result = map_plantbook_to_thresholds(resp.json())
            result["match_type"] = match_type
            return result
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            log.warning("event=plantbook_lookup_failed species=%r error=%r", species, exc)
            return None
