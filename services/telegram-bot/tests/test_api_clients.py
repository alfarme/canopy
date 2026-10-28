"""
Tests for API client parsing logic — pure functions, no network calls.
"""
import httpx
import pytest
from app.api_clients import (
    parse_plantnet_results,
    map_plantbook_to_thresholds,
    has_usable_thresholds,
    fetch_plantbook_thresholds,
    fetch_reference_image,
    identify_plant,
    DEFAULT_THRESHOLDS,
)


class TestIdentifyPlantRequestShape:
    """Regression test for a real crash found in live testing: httpx raised
    'Attempted to send a sync request with an AsyncClient instance.' The
    cause was passing `data` as a list of tuples for the repeated `organs`
    multipart field — httpx's encode_request() treats a non-Mapping `data`
    as raw content and skips multipart encoding entirely (silently, no
    validation error), producing a sync stream that then fails at send time.
    `data` must be a dict; a list value gives one field per list item.
    """

    @pytest.mark.asyncio
    async def test_single_image_builds_valid_multipart_request(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["content_type"] = request.headers.get("content-type", "")
            return httpx.Response(200, json={"results": []})

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        results = await identify_plant([("photo.jpg", b"fake-image-bytes")])

        assert results == []
        assert "multipart/form-data" in captured["content_type"]

    @pytest.mark.asyncio
    async def test_multiple_images_does_not_raise(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"results": []})

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        images = [(f"p{i}.jpg", b"bytes") for i in range(3)]
        results = await identify_plant(images)
        assert results == []


class TestPlantbookAuthFailsClosed:
    """OpenPlantbook needs OAuth2 client_credentials (both ID and secret).

    Without real credentials configured, the token exchange must never be
    attempted with empty strings — it should fail closed (return None) so the
    caller falls back to DEFAULT_THRESHOLDS, not raise.
    """

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_none_no_network(self, monkeypatch):
        monkeypatch.delenv("OPENPLANTBOOK_CLIENT_ID", raising=False)
        monkeypatch.delenv("OPENPLANTBOOK_CLIENT_SECRET", raising=False)
        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is None


class TestHasUsableThresholds:
    """Guards the re-seed path from overwriting good values with all-None."""

    def test_none_is_not_usable(self):
        assert has_usable_thresholds(None) is False

    def test_all_none_dict_is_not_usable(self):
        assert has_usable_thresholds({k: None for k in DEFAULT_THRESHOLDS}) is False

    def test_partial_dict_is_usable(self):
        d = {k: None for k in DEFAULT_THRESHOLDS}
        d["min_soil_moist"] = 15
        assert has_usable_thresholds(d) is True

    def test_full_dict_is_usable(self):
        assert has_usable_thresholds(DEFAULT_THRESHOLDS) is True

    def test_empty_dict_is_not_usable(self):
        assert has_usable_thresholds({}) is False


class TestParsePlantnetResults:
    def test_typical_response(self):
        data = {
            "results": [
                {
                    "score": 0.952,
                    "species": {
                        "scientificNameWithoutAuthor": "Monstera deliciosa",
                        "commonNames": ["Swiss cheese plant", "Ceriman"],
                    },
                },
                {
                    "score": 0.031,
                    "species": {
                        "scientificNameWithoutAuthor": "Philodendron bipinnatifidum",
                        "commonNames": ["Tree philodendron"],
                    },
                },
                {
                    "score": 0.008,
                    "species": {
                        "scientificNameWithoutAuthor": "Epipremnum aureum",
                        "commonNames": ["Golden pothos", "Devil's ivy"],
                    },
                },
            ]
        }
        results = parse_plantnet_results(data)
        assert len(results) == 3
        assert results[0]["scientific_name"] == "Monstera deliciosa"
        assert results[0]["common_names"] == ["Swiss cheese plant", "Ceriman"]
        assert results[0]["score"] == 0.952
        assert results[1]["scientific_name"] == "Philodendron bipinnatifidum"
        assert results[2]["score"] == 0.008

    def test_empty_results(self):
        data = {"results": []}
        results = parse_plantnet_results(data)
        assert results == []

    def test_missing_results_key(self):
        data = {}
        results = parse_plantnet_results(data)
        assert results == []

    def test_missing_common_names(self):
        data = {
            "results": [
                {
                    "score": 0.8,
                    "species": {
                        "scientificNameWithoutAuthor": "Rare plantus",
                    },
                },
            ]
        }
        results = parse_plantnet_results(data)
        assert results[0]["common_names"] == []

    def test_missing_species_data(self):
        data = {
            "results": [
                {"score": 0.5},
            ]
        }
        results = parse_plantnet_results(data)
        assert results[0]["scientific_name"] == "Unknown"
        assert results[0]["common_names"] == []


class TestMapPlantbookToThresholds:
    def test_full_mapping(self):
        data = {
            "min_soil_moist": 15,
            "max_soil_moist": 60,
            "min_soil_ec": 150,
            "max_soil_ec": 2500,
            "min_light_lux": 500,
            "max_light_lux": 2500,
            "min_temp": 15,
            "max_temp": 30,
            # Extra fields that should be ignored
            "pid": "monstera deliciosa",
            "display_pid": "Monstera deliciosa",
            "max_light_mmol": 3000,
        }
        result = map_plantbook_to_thresholds(data)
        assert result == {
            "min_soil_moist": 15,
            "max_soil_moist": 60,
            "min_soil_ec": 150,
            "max_soil_ec": 2500,
            "min_light_lux": 500,
            "max_light_lux": 2500,
            "min_temp": 15,
            "max_temp": 30,
        }

    def test_partial_data(self):
        data = {
            "min_soil_moist": 20,
            "max_soil_moist": 50,
            # Missing EC, light, temp
        }
        result = map_plantbook_to_thresholds(data)
        assert result["min_soil_moist"] == 20
        assert result["max_soil_moist"] == 50
        assert result["min_soil_ec"] is None
        assert result["max_soil_ec"] is None
        assert result["min_light_lux"] is None
        assert result["max_light_lux"] is None
        assert result["min_temp"] is None
        assert result["max_temp"] is None

    def test_empty_data(self):
        result = map_plantbook_to_thresholds({})
        assert all(v is None for v in result.values())


class TestDefaultThresholds:
    def test_all_keys_present(self):
        expected_keys = {
            "min_soil_moist", "max_soil_moist",
            "min_soil_ec", "max_soil_ec",
            "min_light_lux", "max_light_lux",
            "min_temp", "max_temp",
        }
        assert set(DEFAULT_THRESHOLDS.keys()) == expected_keys

    def test_min_less_than_max(self):
        assert DEFAULT_THRESHOLDS["min_soil_moist"] < DEFAULT_THRESHOLDS["max_soil_moist"]
        assert DEFAULT_THRESHOLDS["min_soil_ec"] < DEFAULT_THRESHOLDS["max_soil_ec"]
        assert DEFAULT_THRESHOLDS["min_light_lux"] < DEFAULT_THRESHOLDS["max_light_lux"]
        assert DEFAULT_THRESHOLDS["min_temp"] < DEFAULT_THRESHOLDS["max_temp"]


# ------------------------------------------------------------------ Part A regression tests
# External-API failure modes: timeout, 5xx, malformed JSON for both PlantNet and OpenPlantbook.


class TestIdentifyPlantFailureModes:
    """Verify identify_plant() failure modes propagate as exceptions
    that the caller's broad `except Exception` catches cleanly."""

    @pytest.mark.asyncio
    async def test_plantnet_timeout_raises(self, monkeypatch):
        """PlantNet timeout → httpx.TimeoutException propagates (caught by caller)."""

        def handler(request):
            raise httpx.TimeoutException("connect timeout")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        with pytest.raises(httpx.TimeoutException):
            await identify_plant([("photo.jpg", b"fake")])

    @pytest.mark.asyncio
    async def test_plantnet_5xx_raises(self, monkeypatch):
        """PlantNet 500 → httpx.HTTPStatusError propagates (caught by caller)."""

        def handler(request):
            return httpx.Response(500, text="Internal Server Error")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        with pytest.raises(httpx.HTTPStatusError):
            await identify_plant([("photo.jpg", b"fake")])

    @pytest.mark.asyncio
    async def test_plantnet_malformed_json_raises(self, monkeypatch):
        """PlantNet 200 with non-JSON body → ValueError propagates (caught by caller)."""

        def handler(request):
            return httpx.Response(200, content=b"not json at all")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        # json.JSONDecodeError is a subclass of ValueError
        with pytest.raises(ValueError):
            await identify_plant([("photo.jpg", b"fake")])


class TestPlantbookFailureModes:
    """Verify fetch_plantbook_thresholds() degrades to None for all failure modes."""

    @pytest.fixture(autouse=True)
    def _set_credentials(self, monkeypatch):
        """Provide fake credentials so _get_plantbook_token() attempts the exchange."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "fake-id")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "fake-secret")
        # Clear cached token so each test starts fresh
        from app.api_clients import _token_cache
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0.0

    @pytest.mark.asyncio
    async def test_token_endpoint_timeout_returns_none(self, monkeypatch):
        """Token endpoint timeout → returns None (falls back to DEFAULT_THRESHOLDS)."""

        def handler(request):
            raise httpx.TimeoutException("connect timeout")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is None

    @pytest.mark.asyncio
    async def test_token_endpoint_5xx_returns_none(self, monkeypatch):
        """Token endpoint 500 → returns None."""

        def handler(request):
            return httpx.Response(500, text="Internal Server Error")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is None

    @pytest.mark.asyncio
    async def test_token_endpoint_malformed_json_returns_none(self, monkeypatch):
        """Token endpoint returns 200 with non-JSON body → returns None."""

        def handler(request):
            # Token endpoint returns garbage
            if "token" in str(request.url):
                return httpx.Response(200, content=b"not json")
            return httpx.Response(200, json={})

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_endpoint_timeout_returns_none(self, monkeypatch):
        """Detail endpoint timeout (after successful token) → returns None."""

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                return httpx.Response(200, json={"results": [{"pid": "monstera_deliciosa"}]})
            # Detail endpoint times out
            raise httpx.TimeoutException("read timeout")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_endpoint_5xx_returns_none(self, monkeypatch):
        """Detail endpoint 500 (after successful token) → returns None."""

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                return httpx.Response(200, json={"results": [{"pid": "monstera_deliciosa"}]})
            return httpx.Response(500, text="Server Error")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_endpoint_malformed_json_returns_none(self, monkeypatch):
        """Detail endpoint 200 with non-JSON body (after successful token) → returns None."""

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                return httpx.Response(200, json={"results": [{"pid": "monstera_deliciosa"}]})
            # Detail endpoint returns garbage
            return httpx.Response(200, content=b"<html>not json</html>")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_url_has_trailing_slash(self, monkeypatch):
        """Regression: OpenPlantbook 301-redirects the no-trailing-slash detail
        URL, and httpx doesn't follow redirects by default, so the old code
        threw HTTPStatusError on the 301 itself and silently returned None
        for every real lookup. Assert the request URL is already canonical
        (trailing slash) so no redirect is needed."""
        captured = {}

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                return httpx.Response(200, json={"results": [{"pid": "monstera_deliciosa"}]})
            captured["path"] = request.url.path
            return httpx.Response(200, json={"min_soil_moist": 15})

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert captured["path"].endswith("/")
        assert result["min_soil_moist"] == 15

    @pytest.mark.asyncio
    async def test_pid_resolved_via_search_not_guessed(self, monkeypatch):
        """Regression: a real live bug. PlantNet returned "Rhaphidophora
        tetrasperma"; guessing the pid as species.lower() hit
        .../rhaphidophora%20tetrasperma/ which 404s — that's not a real
        OpenPlantbook pid slug. The search endpoint must be used to resolve
        the actual pid before calling detail."""
        captured = {}

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                captured["search_alias"] = request.url.params.get("alias")
                return httpx.Response(200, json={
                    "results": [{"pid": "rhaphidophora_tetrasperma"}]
                })
            captured["detail_path"] = request.url.path
            return httpx.Response(200, json={"min_soil_moist": 20})

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Rhaphidophora tetrasperma")
        assert captured["search_alias"] == "Rhaphidophora tetrasperma"
        assert captured["detail_path"] == "/api/v1/plant/detail/rhaphidophora_tetrasperma/"
        assert result["min_soil_moist"] == 20

    @pytest.mark.asyncio
    async def test_search_no_match_returns_none(self, monkeypatch):
        """Search endpoint returning zero results → None, no detail call attempted."""

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                return httpx.Response(200, json={"results": []})
            pytest.fail("detail endpoint should not be called when search has no match")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Some Unknown Species")
        assert result is None


class TestFetchReferenceImage:
    """PlantNet's identify response has no images field in our plan (verified
    against a real live response: only gbif/powo/score/species keys) — we
    substitute a Wikipedia thumbnail lookup by scientific name."""

    @pytest.mark.asyncio
    async def test_returns_thumbnail_url(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={
                "query": {"pages": {"123": {
                    "thumbnail": {"source": "https://upload.wikimedia.org/x.jpg"}
                }}}
            })

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_reference_image("Monstera deliciosa")
        assert result == "https://upload.wikimedia.org/x.jpg"

    @pytest.mark.asyncio
    async def test_no_thumbnail_returns_none(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"query": {"pages": {"123": {}}}})

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_reference_image("Some Unknown Species")
        assert result is None

    @pytest.mark.asyncio
    async def test_request_failure_returns_none(self, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("connect timeout")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_reference_image("Monstera deliciosa")
        assert result is None


class TestPTBWarningSupressed:
    """Verify the ConversationHandler construction does not emit the
    per_message=False PTBUserWarning."""

    def test_no_ptb_per_message_warning(self):
        """build_identify_handler() should not emit PTBUserWarning about per_message."""
        import warnings
        from unittest.mock import MagicMock, AsyncMock
        from app.identify import build_identify_handler

        conn = MagicMock()
        admin_check = lambda uid: True

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            handler = build_identify_handler(conn, admin_check)

        ptb_warnings = [
            w for w in caught
            if "per_message" in str(w.message)
        ]
        assert ptb_warnings == [], f"Unexpected PTBUserWarning: {ptb_warnings}"


class TestGenusFallback:
    """Genus-level fallback: when exact species search misses, retry with genus."""

    @pytest.fixture(autouse=True)
    def _set_credentials(self, monkeypatch):
        """Provide fake credentials so _get_plantbook_token() attempts the exchange."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "fake-id")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "fake-secret")
        from app.api_clients import _token_cache
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0.0

    @pytest.mark.asyncio
    async def test_exact_match_returns_exact_match_type(self, monkeypatch):
        """When exact species search finds a pid, match_type is 'exact'."""

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                # Exact species search succeeds
                return httpx.Response(200, json={
                    "results": [{"pid": "monstera_deliciosa"}]
                })
            # Detail endpoint
            return httpx.Response(200, json={
                "min_soil_moist": 15,
                "max_soil_moist": 65,
                "min_temp": 12,
                "max_temp": 32,
            })

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera deliciosa")
        assert result is not None
        assert result["match_type"] == "exact"
        assert result["min_soil_moist"] == 15
        assert result["max_soil_moist"] == 65

    @pytest.mark.asyncio
    async def test_genus_fallback_triggers_when_exact_fails(self, monkeypatch):
        """When exact species search returns no results, genus fallback is tried
        and result is flagged with match_type='genus'."""
        captured_searches = []

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                alias = request.url.params.get("alias")
                captured_searches.append(alias)
                # First search (exact species) fails, second (genus) succeeds
                if alias == "Rhaphidophora unknown-species":
                    return httpx.Response(200, json={"results": []})
                elif alias == "Rhaphidophora":
                    return httpx.Response(200, json={
                        "results": [{"pid": "rhaphidophora_tetrasperma"}]
                    })
                return httpx.Response(200, json={"results": []})
            # Detail endpoint
            return httpx.Response(200, json={
                "min_soil_moist": 20,
                "max_soil_moist": 60,
                "min_temp": 15,
                "max_temp": 30,
            })

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Rhaphidophora unknown-species")
        assert result is not None
        assert result["match_type"] == "genus"
        assert result["min_soil_moist"] == 20
        assert result["min_temp"] == 15
        # Verify both searches were attempted in order
        assert captured_searches == ["Rhaphidophora unknown-species", "Rhaphidophora"]

    @pytest.mark.asyncio
    async def test_full_fallback_when_genus_also_fails(self, monkeypatch):
        """When both exact and genus searches return no results, returns None
        (caller falls back to DEFAULT_THRESHOLDS)."""

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                # Both exact and genus search fail
                return httpx.Response(200, json={"results": []})
            pytest.fail("detail endpoint should not be called when all searches fail")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Unknownus mysterius")
        assert result is None

    @pytest.mark.asyncio
    async def test_single_word_species_no_genus_retry(self, monkeypatch):
        """A single-word species name should not trigger genus retry (genus == species)."""
        search_count = []

        def handler(request):
            if "token" in str(request.url):
                return httpx.Response(200, json={
                    "access_token": "fake-token",
                    "expires_in": 3600,
                })
            if "search" in str(request.url):
                search_count.append(1)
                return httpx.Response(200, json={"results": []})
            pytest.fail("detail endpoint should not be called")

        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)

        result = await fetch_plantbook_thresholds("Monstera")
        assert result is None
        # Only one search attempt (no genus retry since genus == species)
        assert len(search_count) == 1

    @pytest.mark.asyncio
    async def test_has_usable_thresholds_with_match_type(self):
        """has_usable_thresholds correctly ignores match_type metadata key."""
        # All-None thresholds with match_type should still be not usable
        all_none = {k: None for k in DEFAULT_THRESHOLDS}
        all_none["match_type"] = "genus"
        assert has_usable_thresholds(all_none) is False

        # Partial thresholds with match_type should be usable
        partial = {k: None for k in DEFAULT_THRESHOLDS}
        partial["min_soil_moist"] = 15
        partial["match_type"] = "exact"
        assert has_usable_thresholds(partial) is True
