import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_PROFILE_SCHEDULER"] = "1"

import web  # noqa: E402
from src import log_capture  # noqa: E402


def _blank_credentials() -> dict:
    """An empty-but-complete credential blob for `create_profile`."""
    return {
        "simkl": {
            "client_id": "",
            "client_secret": "",
            "access_token": "",
            "selected_statuses": {"shows": [], "movies": [], "anime": []},
        },
        "anilist": {"username": "", "selected_statuses": []},
        "trakt": {
            "client_id": "",
            "client_secret": "",
            "access_token": "",
            "refresh_token": "",
            "selected_lists": [],
        },
        "mdblist": {"api_key": "", "selected_lists": []},
        "pmdb": {"api_key": ""},
    }


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_site_access_password = web.SITE_ACCESS_PASSWORD
        self.original_admin_password = web.ADMIN_PASSWORD
        web._profile_store = web.ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        web._session_store = web.ServerSessionStore(ttl_seconds=3600)
        web._profile_log_store = web.ProfileLogStore()
        web._login_limiter = web.LoginAttemptLimiter(max_attempts=5, window_seconds=60)
        web._access_store = web.ServerSessionStore(ttl_seconds=3600)
        web._access_limiter = web.LoginAttemptLimiter(max_attempts=5, window_seconds=60)
        web.SITE_ACCESS_PASSWORD = ""
        web.ADMIN_PASSWORD = ""
        self.client = web.app.test_client()

    def tearDown(self) -> None:
        web.SITE_ACCESS_PASSWORD = self.original_site_access_password
        web.ADMIN_PASSWORD = self.original_admin_password
        self.tmpdir.cleanup()

    def test_index_contains_new_source_sections(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Sync Your Lists.<br>Keep Them Fresh.", html)
        self.assertIn("keep watchlists, collections, and history in sync", html)
        self.assertIn(">Trakt</div>", html)
        self.assertIn(">MDBList</div>", html)
        self.assertIn("SIMKL Lists", html)
        self.assertIn("AniList Lists", html)
        self.assertIn("MDBList Lists", html)
        self.assertIn("Search public MDBList lists", html)
        self.assertIn("dot-mdblist", html)
        self.assertIn("Use the redirect URL if asked.", html)
        self.assertIn('id="simkl-redirect-url"', html)
        self.assertIn('id="trakt-redirect-url"', html)
        self.assertIn('id="anilist-access-token"', html)
        self.assertIn('id="anilist-client-id"', html)
        self.assertIn('id="anilist-client-secret"', html)

    def test_index_offers_persistent_counterlock_theme(self) -> None:
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="snav-appearance"', html)
        self.assertIn('<option value="counterlock">Counterlock</option>', html)
        self.assertIn("localStorage.setItem(THEME_STYLE_KEY, selected)", html)
        self.assertIn("localStorage.setItem(THEME_MODE_KEY, selected)", html)
        self.assertIn('html[data-theme="counterlock"]', html)
        self.assertIn('data-mode-toggle', html)
        # AniList's redirect URL is a fixed AniList endpoint, not this site's
        # address, and it is what makes the connect flow work at all.
        self.assertIn('value="https://anilist.co/api/v2/oauth/pin"', html)
        self.assertIn("Connect AniList", html)
        self.assertIn('id="anilist-auth-code"', html)
        self.assertIn("https://github.com/Febsho/SyncMeta-for-PublicMetaDB", html)
        self.assertIn("List Visibility", html)
        self.assertIn('id="opt-simkl-visibility"', html)
        self.assertIn('id="opt-anilist-visibility"', html)
        self.assertIn('id="opt-trakt-personal-visibility"', html)
        self.assertIn('id="opt-trakt-public-visibility"', html)
        self.assertIn('id="opt-mdblist-visibility"', html)
        self.assertIn("Recommended: private", html)
        self.assertIn("Applies to liked Trakt lists and Discover/public Trakt lists.", html)
        self.assertIn("Delete User Records", html)
        self.assertIn("Danger Zone", html)
        self.assertIn("Stored securely for this profile. Leave blank to keep it.", html)
        self.assertIn("Watchlist, watch history, collections, custom lists, resume progress, and device auth.", html)
        self.assertIn("Native MDBList watchlist/history writes are not exposed by this app", html)
        self.assertNotIn("data-nav=\"mapping\"", html)
        self.assertIn("Activity Sync", html)
        self.assertIn("Sync watch history automatically in the background", html)
        self.assertIn("Auto-Sync Every (Seconds)", html)
        self.assertIn("Minimum 21600 s (6 h). Default 43200 s (12 h).", html)
        self.assertIn("Minimum 86400 s (24 h). Only affects automatic history sync.", html)
        self.assertIn("Only import SIMKL anime watch history", html)
        self.assertIn('id="opt-activity-resume-source"', html)
        self.assertIn('id="opt-auto-resume-sync"', html)
        self.assertIn('id="opt-resume-interval-seconds"', html)
        self.assertIn("Minimum 86400 s (24 h). Only affects automatic resume sync.", html)
        self.assertIn('id="activity-cards"', html)
        self.assertIn("Sync Watch History", html)
        self.assertIn("Clear PMDB History", html)
        self.assertIn("Sync Resume Progress", html)
        self.assertIn('id="connection-readiness" role="status"', html)
        self.assertIn('id="dashboard-readiness" role="status"', html)
        self.assertIn("Test all", html)
        self.assertIn("/api/profile/connections/check", html)
        self.assertIn("Import watched history into PublicMetaDB. The selected source is the only write direction for this rule.", html)
        self.assertIn("Import continue-watching progress. When background sync is on, this refreshes automatically on its own schedule.", html)
        self.assertIn("Queued", html)
        self.assertIn("position", html)
        self.assertIn("RESULTS_PAGE_SIZE = 25", html)
        self.assertIn("results-prev-page", html)
        self.assertIn('id="btn-stop"', html)
        self.assertIn("SyncMeta</div>", html)
        self.assertNotIn("cookie_notice_ack", html)
        self.assertIn("Choose which movie, show, and anime statuses should sync from SIMKL.", html)
        self.assertIn("Choose which anime lists should sync into PublicMetaDB.", html)
        self.assertIn("Delete PublicMetaDB lists when disabled in SyncMeta", html)
        self.assertIn("SIMKL Plan to Watch -&gt; PublicMetaDB Watchlist", html)
        self.assertIn("Trakt Watchlist -&gt; PublicMetaDB Watchlist", html)
        self.assertIn("AniList Planning -&gt; PublicMetaDB Watchlist", html)
        self.assertNotIn('href="/impressum"', html)
        self.assertNotIn('href="/datenschutz"', html)
        self.assertNotIn('href="/terms"', html)
        self.assertNotIn('href="/cookie-settings"', html)
        self.assertNotIn("Sync Series", html)
        self.assertNotIn("Sync Movies", html)
        self.assertNotIn("Sync Anime", html)

    def test_healthz(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ok"], True)

    def test_connection_check_requires_session(self) -> None:
        response = self.client.post("/api/profile/connections/check", json={})
        self.assertEqual(response.status_code, 401)

    @patch("web.check_connections")
    def test_connection_check_persists_only_saved_credential_results(self, check_connections_mock) -> None:
        credentials = _blank_credentials()
        credentials["pmdb"]["api_key"] = "saved-key"
        credentials["simkl"]["client_id"] = "client"
        credentials["simkl"]["access_token"] = "token"
        profile = web._profile_store.create_profile("secret", credentials, {
            "auto_sync": False,
            "media_types": ["shows", "movies", "anime"],
        })
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        def fake_checks(creds, providers, **_kwargs):
            status = "error" if creds["pmdb"]["api_key"] == "draft-key" else "healthy"
            return [{
                "provider": provider,
                "status": status if provider == "pmdb" else "healthy",
                "code": "auth_invalid" if status == "error" and provider == "pmdb" else "ok",
                "message": "checked",
                "checked_at": "2026-08-03T12:00:00+00:00",
                "capabilities": {"readable": True, "writable": provider != "mdblist"},
                "identity": "",
                "recovery_action": "none",
            } for provider in providers]

        check_connections_mock.side_effect = fake_checks
        saved = self.client.post("/api/profile/connections/check", json={"providers": ["pmdb", "simkl"]})
        self.assertEqual(saved.status_code, 200)
        self.assertFalse(saved.get_json()["readiness"]["ready"])
        self.assertIn("Create and enable at least one sync pair.", saved.get_json()["readiness"]["blockers"])
        stored = web._profile_store.get_private_profile_by_id(profile["profile_id"])["connection_health"]
        self.assertEqual(stored["pmdb"]["status"], "healthy")

        draft = self.client.post("/api/profile/connections/check", json={
            "providers": ["pmdb"],
            "credentials": {"pmdb": {"api_key": "draft-key"}},
        })
        self.assertEqual(draft.get_json()["checks"][0]["status"], "error")
        self.assertNotIn("draft-key", draft.get_data(as_text=True))
        self.assertNotIn("saved-key", draft.get_data(as_text=True))
        stored = web._profile_store.get_private_profile_by_id(profile["profile_id"])["connection_health"]
        self.assertEqual(stored["pmdb"]["status"], "healthy")

    @patch("web.check_connections")
    def test_connection_check_uses_fresh_cached_result(self, check_connections_mock) -> None:
        credentials = _blank_credentials()
        credentials["pmdb"]["api_key"] = "saved-key"
        profile = web._profile_store.create_profile("secret", credentials, {"auto_sync": False, "media_types": ["movies"]})
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        web._profile_store.record_connection_health(profile["profile_id"], [{
            "provider": "pmdb", "status": "healthy", "code": "ok", "message": "cached",
            "checked_at": web.datetime.now(web.timezone.utc).isoformat(),
            "capabilities": {"readable": True, "writable": True}, "identity": "", "recovery_action": "none",
        }])

        response = self.client.post("/api/profile/connections/check", json={"providers": ["pmdb"], "force": False})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["checks"][0]["message"], "cached")
        check_connections_mock.assert_not_called()

    def test_connection_readiness_reports_missing_and_bad_pairs(self) -> None:
        profile = {
            "credentials": _blank_credentials(),
            "options": {"auto_sync": False, "media_types": ["movies"]},
        }
        empty = web._connection_readiness(web._config_from_profile(profile), [])
        self.assertFalse(empty["ready"])
        self.assertEqual(empty["blockers"], ["Create and enable at least one sync pair."])

        profile["credentials"]["pmdb"]["api_key"] = "pm-key"
        profile["credentials"]["simkl"]["client_id"] = "simkl-client"
        profile["credentials"]["simkl"]["access_token"] = "simkl-token"
        # AniList with a username but no access token reads fine and cannot be
        # written to, which is what makes it an invalid pair target.
        profile["credentials"]["anilist"]["username"] = "someone"
        profile["options"]["sync_pairs"] = [{
            "pair_id": "bad-target", "name": "Bad target", "source": "simkl", "target": "anilist",
            "categories": ["watchlist"], "enabled": True,
        }]
        health = [
            {"provider": "pmdb", "status": "healthy", "capabilities": {"readable": True, "writable": True}},
            {"provider": "simkl", "status": "healthy", "capabilities": {"readable": True, "writable": True}},
            {"provider": "anilist", "status": "healthy", "capabilities": {"readable": True, "writable": False}},
        ]
        blocked = web._connection_readiness(web._config_from_profile(profile), health)
        self.assertFalse(blocked["ready"])
        self.assertTrue(any("Bad target" in item for item in blocked["blockers"]))

    def test_parse_tmdb_id_accepts_slug_or_url(self) -> None:
        self.assertEqual(web._parse_tmdb_id("1600672"), 1600672)
        self.assertEqual(web._parse_tmdb_id("1600672-30"), 1600672)
        self.assertEqual(
            web._parse_tmdb_id("https://www.themoviedb.org/movie/1600672-30"),
            1600672,
        )

    def test_contribute_manual_resolution_mapping_uses_available_external_ids(self) -> None:
        calls = []

        class _PMDB:
            def create_id_mapping(self, tmdb_id, media_type, id_type, id_value):
                calls.append((tmdb_id, media_type, id_type, id_value))
                return True

        web._contribute_manual_resolution_mapping(
            _PMDB(),
            {
                "media_type": "movie",
                "imdb_id": "tt123",
                "mal_id": "55",
                "anilist_id": "66",
                "root_mal_id": "77",
            },
            1600672,
        )

        self.assertIn((1600672, "movie", "imdb", "tt123"), calls)
        self.assertIn((1600672, "movie", "mal", "55"), calls)
        self.assertIn((1600672, "movie", "anilist", "66"), calls)
        self.assertIn((1600672, "movie", "mal", "77"), calls)

    def test_legal_pages_render(self) -> None:
        impressum = self.client.get("/impressum")
        privacy = self.client.get("/datenschutz")
        terms = self.client.get("/terms")
        cookies = self.client.get("/cookie-settings")

        self.assertEqual(impressum.status_code, 404)
        self.assertEqual(privacy.status_code, 404)
        self.assertEqual(terms.status_code, 404)
        self.assertEqual(cookies.status_code, 404)

    def test_site_access_password_gate(self) -> None:
        web.SITE_ACCESS_PASSWORD = "letmein"

        blocked = self.client.get("/")
        self.assertEqual(blocked.status_code, 401)
        self.assertIn("Private Access", blocked.get_data(as_text=True))

        api_blocked = self.client.post("/api/profile/status", json={})
        self.assertEqual(api_blocked.status_code, 401)
        self.assertEqual(api_blocked.get_json()["error"], "Site password required")

        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 43200,
            "media_types": ["shows", "movies", "anime"],
        })

        reset_response = self.client.post("/api/profile/password/reset", json={
            "profile_id": profile["profile_id"],
            "current_password": "secret",
            "new_password": "new-secret",
        })
        self.assertEqual(reset_response.status_code, 200)

        wrong = self.client.post("/access", data={"password": "wrong"})
        self.assertEqual(wrong.status_code, 200)
        self.assertIn("Wrong site password.", wrong.get_data(as_text=True))

        unlocked = self.client.post("/access", data={"password": "letmein"}, follow_redirects=True)
        self.assertEqual(unlocked.status_code, 200)
        self.assertIn("Sync Your Lists.<br>Keep Them Fresh.", unlocked.get_data(as_text=True))

    @patch("web.SimklClient.request_pin")
    def test_simkl_pin_start(self, mock_request_pin) -> None:
        mock_request_pin.return_value = {
            "user_code": "ABCD",
            "verification_url": "https://simkl.com/pin/",
            "interval": 5,
            "expires_in": 900,
            "result": "OK",
        }

        response = self.client.post("/api/simkl/pin/start", json={"client_id": "client"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["user_code"], "ABCD")
        self.assertEqual(data["verification_url"], "https://simkl.com/pin/")

    @patch("web.SimklClient.check_pin")
    def test_simkl_pin_check_approved(self, mock_check_pin) -> None:
        mock_check_pin.return_value = {
            "result": "OK",
            "access_token": "token-123",
        }

        response = self.client.post("/api/simkl/pin/check", json={
            "client_id": "client",
            "user_code": "ABCD",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["access_token"], "token-123")

    @patch("web.TraktClient.request_device_code")
    def test_trakt_device_start(self, mock_request_device_code) -> None:
        mock_request_device_code.return_value = {
            "device_code": "device",
            "user_code": "TRAKT",
            "verification_url": "https://trakt.tv/activate",
            "interval": 5,
            "expires_in": 600,
        }

        response = self.client.post("/api/trakt/device/start", json={
            "client_id": "client",
            "client_secret": "secret",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["device_code"], "device")
        self.assertEqual(data["user_code"], "TRAKT")

    @patch("web.TraktClient.poll_device_token")
    def test_trakt_device_check_approved(self, mock_poll_device_token) -> None:
        mock_poll_device_token.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
        }

        response = self.client.post("/api/trakt/device/check", json={
            "client_id": "client",
            "client_secret": "secret",
            "device_code": "device",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["access_token"], "access")
        self.assertEqual(data["refresh_token"], "refresh")
        self.assertFalse(data["saved"])

    @patch("web.TraktClient.poll_device_token")
    def test_trakt_device_check_approved_persists_tokens_for_signed_in_profile(self, mock_poll_device_token) -> None:
        mock_poll_device_token.return_value = {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_in": 7200,
            "created_at": 1893456000,
        }
        profile = web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "client", "client_secret": "secret", "access_token": "old", "refresh_token": "old-refresh"},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb"},
        }, {"auto_sync": False, "media_types": ["shows"]})
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/trakt/device/check", json={"device_code": "device"})
        data = response.get_json()
        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["saved"])
        self.assertEqual(private_profile["credentials"]["trakt"]["access_token"], "access-new")
        self.assertEqual(private_profile["credentials"]["trakt"]["refresh_token"], "refresh-new")
        self.assertTrue(private_profile["credentials"]["trakt"]["access_token_expires_at"])

    @patch("web.TraktClient.poll_device_token")
    def test_trakt_device_check_pending_is_not_reported_as_error(self, mock_poll_device_token) -> None:
        mock_poll_device_token.return_value = {
            "error": "authorization_pending",
            "error_description": "User has not finished authorizing yet.",
        }

        response = self.client.post("/api/trakt/device/check", json={
            "client_id": "client",
            "client_secret": "secret",
            "device_code": "device",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "pending")
        self.assertTrue(data["message"])

    @patch("web.TraktClient.get_personal_lists_metadata")
    @patch("web.TraktClient.get_liked_lists_metadata")
    def test_trakt_catalogs_personal_and_liked_lists(self, mock_get_liked_lists_metadata, mock_get_personal_lists_metadata) -> None:
        mock_get_personal_lists_metadata.return_value = [{
            "name": "My Anime Picks",
            "user": "demo",
            "slug": "my-anime-picks",
            "source": "personal",
            "item_count": 5,
        }]
        mock_get_liked_lists_metadata.return_value = [{
            "name": "Anime",
            "user": "demo",
            "slug": "anime",
            "source": "liked",
            "likes": 42,
            "item_count": 10,
        }]

        response = self.client.post("/api/trakt/catalogs", json={
            "client_id": "client",
            "access_token": "token",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["source"], "personal")
        self.assertEqual(data["items"][1]["slug"], "anime")

    @patch("web.TraktClient.search_lists")
    def test_trakt_catalogs_search(self, mock_search_lists) -> None:
        mock_search_lists.return_value = [{
            "name": "Top Rated",
            "user": "demo",
            "slug": "top-rated",
            "source": "discover",
        }]

        response = self.client.post("/api/trakt/catalogs", json={
            "client_id": "client",
            "access_token": "token",
            "query": "top rated",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["query"], "top rated")
        self.assertEqual(data["items"][0]["source"], "discover")

    @patch("web.MdbListClient.get_user_lists")
    def test_mdblist_lists(self, mock_get_user_lists) -> None:
        mock_get_user_lists.return_value = [{
            "id": 7,
            "name": "Favorites",
            "mediatype": "movie",
        }]

        response = self.client.post("/api/mdblist/lists", json={
            "api_key": "mdb-key",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], 7)

    @patch("web.MdbListClient.search_public_lists")
    def test_mdblist_lists_search(self, mock_search_public_lists) -> None:
        mock_search_public_lists.return_value = [{
            "id": 11,
            "name": "Popular Movies",
            "mediatype": "movie",
            "user_name": "demo",
        }]

        response = self.client.post("/api/mdblist/lists", json={
            "api_key": "mdb-key",
            "query": "popular",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["query"], "popular")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], 11)

    def test_index_contains_auto_map_action_for_unresolved_items(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Auto Map", html)

    def test_login_uses_session_and_masks_saved_secrets(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "super-secret",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-secret",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        login_data = login_response.get_json()

        self.assertEqual(login_response.status_code, 200)
        self.assertIn("Set-Cookie", login_response.headers)
        self.assertTrue(login_data["profile"]["credentials"]["simkl"]["client_secret_saved"])
        self.assertTrue(login_data["profile"]["credentials"]["pmdb"]["api_key_saved"])
        self.assertNotIn("api_key", login_data["profile"]["credentials"]["pmdb"])

        status_response = self.client.post("/api/profile/status", json={"include_credentials": True})
        status_data = status_response.get_json()

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_data["profile"]["profile_id"], profile["profile_id"])
        self.assertTrue(status_data["profile"]["credentials"]["simkl"]["access_token_saved"])
        self.assertNotIn("access_token", status_data["profile"]["credentials"]["simkl"])

    def test_signed_profile_session_survives_session_store_recreation(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-secret"},
        }, {"auto_sync": False, "media_types": ["shows"]})

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        web._session_store = web.ServerSessionStore(ttl_seconds=3600)
        status_response = self.client.post("/api/profile/status", json={"include_credentials": True})
        status_data = status_response.get_json()

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_data["profile"]["profile_id"], profile["profile_id"])
        self.assertTrue(status_data["profile"]["credentials"]["pmdb"]["api_key_saved"])

    def test_profile_logs_are_scoped_to_signed_in_profile(self) -> None:
        first = web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-secret"},
        }, {"auto_sync": False, "media_types": ["shows"]})
        second = web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-secret"},
        }, {"auto_sync": False, "media_types": ["shows"]})

        self.client.post("/api/profile/login", json={"profile_id": first["profile_id"], "password": "secret"})
        web.logger.info("first-only log", extra={"profile_id": first["profile_id"]})
        web.logger.info("second-only log", extra={"profile_id": second["profile_id"]})

        logs_response = self.client.post("/api/profile/logs", json={"after": 0})
        messages = [entry["message"] for entry in logs_response.get_json()["entries"]]

        self.assertEqual(logs_response.status_code, 200)
        self.assertIn("first-only log", messages)
        self.assertNotIn("second-only log", messages)

        clear_response = self.client.post("/api/profile/logs/clear", json={})
        logs_after_clear = self.client.post("/api/profile/logs", json={"after": 0})

        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(logs_after_clear.get_json()["entries"], [])

    def _make_bare_profile(self) -> dict:
        return web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-secret"},
        }, {"auto_sync": False, "media_types": ["shows"]})

    def test_global_logs_endpoint_requires_a_session(self) -> None:
        # Unauthenticated, this returned every profile's buffered sync logs.
        response = self.client.get("/api/logs")
        self.assertEqual(response.status_code, 401)

    def test_global_logs_endpoint_ignores_a_client_supplied_profile(self) -> None:
        first = self._make_bare_profile()
        second = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": first["profile_id"], "password": "secret"})

        log_capture.set_profile_context(first["profile_id"])
        web.logger.info("first-only capture line")
        log_capture.set_profile_context(second["profile_id"])
        web.logger.info("second-only capture line")
        log_capture.set_profile_context(None)

        # Asking for the other profile's logs must not honour the request.
        response = self.client.get("/api/logs?profile=" + second["profile_id"])
        messages = [entry["message"] for entry in response.get_json()["entries"]]

        self.assertEqual(response.status_code, 200)
        self.assertIn("first-only capture line", messages)
        self.assertNotIn("second-only capture line", messages)

    def test_global_logs_clear_requires_a_session(self) -> None:
        response = self.client.post("/api/logs/clear")
        self.assertEqual(response.status_code, 401)

    def test_global_logs_clear_only_affects_the_caller(self) -> None:
        first = self._make_bare_profile()
        second = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": first["profile_id"], "password": "secret"})

        log_capture.set_profile_context(first["profile_id"])
        web.logger.info("caller line")
        log_capture.set_profile_context(second["profile_id"])
        web.logger.info("bystander line")
        log_capture.set_profile_context(None)

        seq_before = log_capture.snapshot(limit=500)[-1]["seq"]
        clear_response = self.client.post("/api/logs/clear")
        self.assertEqual(clear_response.status_code, 200)

        remaining = [entry["message"] for entry in log_capture.snapshot(limit=500)]
        self.assertNotIn("caller line", remaining)
        self.assertIn("bystander line", remaining, "another profile's logs must survive")

        # The shared sequence counter must keep advancing, or every other
        # client's cursor would sit above all future entries.
        log_capture.set_profile_context(second["profile_id"])
        web.logger.info("line after clear")
        log_capture.set_profile_context(None)
        seq_after = log_capture.snapshot(limit=500)[-1]["seq"]
        self.assertGreater(seq_after, seq_before)

    def test_pairs_require_a_session(self) -> None:
        self.assertEqual(self.client.post("/api/profile/pairs", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/profile/pairs/save", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/profile/pairs/run", json={}).status_code, 401)

    def test_pairs_reports_provider_capabilities(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        data = self.client.post("/api/profile/pairs", json={}).get_json()

        self.assertEqual(data["pairs"], [])
        by_key = {p["key"]: p for p in data["providers"]}
        # Library is always present: it is local, needs no credential, and is
        # the hub the remote providers are meant to feed.
        self.assertEqual(set(by_key), {"library", "trakt", "simkl", "anilist", "mdblist", "pmdb"})
        # This profile only has a PMDB key, so PMDB is configured and the rest
        # are reported as unconfigured rather than omitted.
        self.assertTrue(by_key["pmdb"]["configured"])
        self.assertFalse(by_key["trakt"]["configured"])
        self.assertIn("watchlist", by_key["pmdb"]["writes"])
        self.assertEqual([c["key"] for c in data["categories"]], ["watchlist", "history", "collection"])
        self.assertEqual(data["removal_modes"][0]["key"], "additive")

    def test_saving_a_pair_round_trips(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        save = self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "name": "Trakt to SIMKL",
            "source": "trakt",
            "target": "simkl",
            "categories": ["watchlist"],
        }]})
        self.assertEqual(save.status_code, 200)

        listed = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["source"], "trakt")
        self.assertEqual(listed[0]["removal_mode"], "additive", "must default to the safe mode")
        self.assertTrue(listed[0]["pair_id"])
        # Trakt is not configured on this profile, so the pair explains itself.
        self.assertIn("not configured", listed[0]["problem"])

    def test_saving_a_pair_that_uses_the_library_round_trips(self) -> None:
        """The Library is a provider like any other, at either end.

        It was missing from the save endpoint's own copy of the provider table,
        so every pair pointed at the local library came back as "unknown
        provider" even though the editor offered it.
        """
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        save = self.client.post("/api/profile/pairs/save", json={"pairs": [
            {
                "name": "SIMKL into the library",
                "source": "simkl",
                "target": "library",
                "categories": ["watchlist", "collection"],
            },
            {
                "name": "Library out to Trakt",
                "source": "library",
                "target": "trakt",
                "categories": ["history"],
            },
        ]})
        self.assertEqual(save.status_code, 200, save.get_json())

        listed = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]
        self.assertEqual(
            [(pair["source"], pair["target"]) for pair in listed],
            [("simkl", "library"), ("library", "trakt")],
        )

    def test_every_provider_in_the_listing_can_be_saved(self) -> None:
        """No provider may be offered by the editor but rejected by the save.

        Both sides now read `providers.ADAPTER_TYPES`; this pins them together
        so a new provider cannot be half-enrolled again.
        """
        from src.providers import ADAPTER_TYPES, PROVIDER_ORDER

        self.assertEqual(set(ADAPTER_TYPES), set(PROVIDER_ORDER))
        for key, adapter_class in ADAPTER_TYPES.items():
            self.assertEqual(adapter_class.key, key)
            self.assertTrue(adapter_class.label)

    def test_a_rejected_pair_reports_which_one_failed(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/profile/pairs/save", json={"pairs": [
            {"source": "simkl", "target": "library", "categories": ["watchlist"]},
            {"source": "trakt", "target": "trakt", "categories": ["watchlist"]},
        ]})

        self.assertEqual(response.status_code, 400)
        # Zero-based, so the editor can index straight into its own card list.
        self.assertEqual(response.get_json()["pair_index"], 1)

    def test_saving_simkl_pmdb_collection_pair_is_supported(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "name": "SIMKL ↔ PublicMetaDB",
            "source": "simkl",
            "target": "pmdb",
            "mode": "two_way",
            "categories": ["collection"],
            "source_lists": ["status:completed:movies"],
        }]})

        self.assertEqual(response.status_code, 200)
        saved = response.get_json()["profile"]["options"]["sync_pairs"][0]
        self.assertEqual(saved["categories"], ["collection"])
        self.assertEqual(saved["mode"], "two_way")

    def test_saving_an_invalid_pair_is_rejected_with_a_reason(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt", "target": "trakt", "categories": ["watchlist"],
        }]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("must differ", response.get_json()["error"])

    def test_saving_pairs_rejects_a_non_list(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        response = self.client.post("/api/profile/pairs/save", json={"pairs": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_running_an_unknown_pair_is_a_404(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        response = self.client.post("/api/profile/pairs/run", json={"pair_id": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_running_with_no_pairs_configured_is_rejected(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        response = self.client.post("/api/profile/pairs/run", json={})
        self.assertEqual(response.status_code, 400)

    def test_running_a_pair_reports_why_it_could_not_run(self) -> None:
        # An unconfigured provider must surface as a per-pair error, not a crash.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt", "target": "pmdb", "categories": ["watchlist"],
        }]})

        response = self.client.post("/api/profile/pairs/run", json={"dry_run": True})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data["results"]), 1)
        self.assertIn("not configured", data["results"][0]["errors"][0])

    def test_pair_managed_keys_persist_across_runs(self) -> None:
        profile = self._make_bare_profile()
        web._profile_store.update_pair_managed_keys(
            profile["profile_id"], {"p1": {"watchlist": ["movie:tmdb:1"]}},
        )
        stored = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertEqual(
            stored["activity_state"]["pair_managed_keys"], {"p1": {"watchlist": ["movie:tmdb:1"]}},
        )
        # A second pair's keys must merge, not replace.
        web._profile_store.update_pair_managed_keys(
            profile["profile_id"], {"p2": {"watchlist": ["movie:tmdb:2"]}},
        )
        stored = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertEqual(set(stored["activity_state"]["pair_managed_keys"]), {"p1", "p2"})

    def test_status_does_not_ship_managed_keys(self) -> None:
        # Ownership bookkeeping is server-side only and grows with one key per
        # item per pair per category, so it must never ride the 2s status poll.
        # The cursors do stay — the clear-cursors endpoint reports them back.
        profile = self._make_bare_profile()
        web._profile_store.update_pair_managed_keys(
            profile["profile_id"], {"p1": {"watchlist": [f"movie:tmdb:{i}" for i in range(50)]}},
        )
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/profile/status", json={})
        activity_state = response.get_json()["profile"]["activity_state"]

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pair_managed_keys", activity_state)
        self.assertNotIn("pmdb_watchlist_managed_keys", activity_state)
        self.assertIn("simkl_history_cursor", activity_state)
        # The private profile still has them — this trims the response only.
        stored = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertEqual(len(stored["activity_state"]["pair_managed_keys"]["p1"]["watchlist"]), 50)

    def _make_anilist_profile(self, access_token: str) -> dict:
        return web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "someone", "access_token": access_token, "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-secret"},
        }, {"auto_sync": False, "media_types": ["anime"]})

    def test_anilist_is_a_source_but_not_a_target_without_a_token(self) -> None:
        # The existing-user shape: a username and no token. AniList must stay
        # readable rather than being dropped or forcing a re-login.
        profile = self._make_anilist_profile("")
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        providers = {p["key"]: p for p in self.client.post("/api/profile/pairs", json={}).get_json()["providers"]}

        self.assertTrue(providers["anilist"]["configured"])
        self.assertEqual(providers["anilist"]["reads"], ["watchlist", "collection"])
        self.assertEqual(providers["anilist"]["writes"], [])
        self.assertIn("access token", providers["anilist"]["write_blocked_reason"])

    def test_adding_an_anilist_token_makes_it_a_target(self) -> None:
        profile = self._make_anilist_profile("anilist-token")
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        providers = {p["key"]: p for p in self.client.post("/api/profile/pairs", json={}).get_json()["providers"]}

        self.assertEqual(providers["anilist"]["writes"], ["watchlist", "collection"])
        self.assertEqual(providers["anilist"]["write_blocked_reason"], "")

    def test_a_blank_anilist_token_keeps_the_saved_one(self) -> None:
        # "Leave blank to keep it" must not silently revoke write access.
        profile = self._make_anilist_profile("anilist-token")
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        merged = web.merge_credentials(
            web._profile_store.get_private_profile_by_id(profile["profile_id"])["credentials"],
            web.normalize_credentials({
                "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
                "anilist": {"username": "someone", "access_token": "", "selected_statuses": []},
                "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
                "mdblist": {"api_key": "", "selected_lists": []},
                "pmdb": {"api_key": "pmdb-secret"},
            }),
        )

        self.assertEqual(merged["anilist"]["access_token"], "anilist-token")

    def test_mdblist_is_offered_as_a_full_read_write_provider(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "mdb-key", "selected_lists": [{"id": 10, "name": "Picks", "mediatype": "movie"}]},
            "pmdb": {"api_key": "pmdb-secret"},
        }, {"auto_sync": False, "media_types": ["movies"]})
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        providers = {p["key"]: p for p in self.client.post("/api/profile/pairs", json={}).get_json()["providers"]}

        self.assertTrue(providers["mdblist"]["configured"])
        self.assertTrue(providers["mdblist"]["reads"], "MDBList must be usable as a source")
        # MDBList's sync API is readable and writable, so it is a valid target.
        self.assertEqual(
            sorted(providers["mdblist"]["writes"]), ["collection", "history", "watchlist"],
        )
        self.assertEqual(providers["mdblist"]["write_blocked_reason"], "")
        self.assertTrue(providers["mdblist"]["has_target_lists"])
        # ...but only its curated lists are named destinations, never history.
        self.assertNotIn("history", providers["mdblist"]["target_list_categories"])

    def test_capabilities_do_not_enumerate_lists(self) -> None:
        # Enumerating named lists calls every provider's API. Doing it here made
        # opening the sync view wait on live network round trips, so the
        # capability response only advertises *whether* a provider has lists.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        providers = self.client.post("/api/profile/pairs", json={}).get_json()["providers"]

        for provider in providers:
            self.assertNotIn("lists", provider)
        by_key = {p["key"]: p for p in providers}
        self.assertTrue(by_key["pmdb"]["has_lists"])
        self.assertFalse(by_key["simkl"]["has_lists"], "SIMKL categories are fixed statuses")
        self.assertFalse(by_key["anilist"]["has_lists"])

    def test_pair_lists_endpoint_requires_a_session(self) -> None:
        self.assertEqual(self.client.post("/api/profile/pairs/lists", json={}).status_code, 401)

    def test_pair_lists_endpoint_needs_a_provider(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        response = self.client.post("/api/profile/pairs/lists", json={})
        self.assertEqual(response.status_code, 400)

    def test_pair_lists_for_an_unconfigured_provider_is_empty_not_an_error(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        response = self.client.post("/api/profile/pairs/lists", json={"provider": "trakt"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["lists"], [])

    @patch("web.MdbListClient.get_user_lists")
    def test_pair_lists_include_live_mdblist_account_lists(self, mock_get_user_lists) -> None:
        mock_get_user_lists.return_value = [{"id": 27, "name": "My Anime"}]
        profile = web._profile_store.create_profile("secret", {
            **_blank_credentials(),
            "mdblist": {"api_key": "mdb-key", "selected_lists": []},
        }, {"auto_sync": False, "media_types": ["shows"]})
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/profile/pairs/lists", json={"provider": "mdblist"})

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            {"key": "list:27", "label": "My Anime", "category": "watchlist", "kind": "list"},
            response.get_json()["lists"],
        )

    def test_pair_source_lists_round_trip(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt", "target": "simkl", "categories": ["watchlist"],
            "source_lists": ["watchlist", "list:me/faves"],
        }]})
        listed = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]

        self.assertEqual(listed[0]["source_lists"], ["watchlist", "list:me/faves"])

    def test_anilist_auth_start_needs_a_client_id(self) -> None:
        response = self.client.post("/api/anilist/auth/start", json={})
        self.assertEqual(response.status_code, 400)

    def test_anilist_auth_start_returns_the_authorize_url(self) -> None:
        response = self.client.post("/api/anilist/auth/start", json={"client_id": "12345"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("client_id=12345", data["authorize_url"])
        self.assertIn("response_type=code", data["authorize_url"])
        # The pin redirect is what lets AniList hand the code back to the user.
        self.assertEqual(data["redirect_uri"], "https://anilist.co/api/v2/oauth/pin")
        self.assertIn("oauth%2Fpin", data["authorize_url"])

    def test_anilist_auth_check_validates_its_inputs(self) -> None:
        missing_secret = self.client.post("/api/anilist/auth/check", json={"client_id": "1", "code": "abc"})
        self.assertEqual(missing_secret.status_code, 400)
        missing_code = self.client.post("/api/anilist/auth/check", json={"client_id": "1", "client_secret": "s"})
        self.assertEqual(missing_code.status_code, 400)

    @patch("web.anilist_exchange_code_for_token")
    def test_anilist_auth_check_stores_the_token_on_the_profile(self, exchange) -> None:
        exchange.return_value = "fresh-token"
        profile = self._make_anilist_profile("")
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/anilist/auth/check", json={
            "client_id": "cid", "client_secret": "csecret", "code": "the-code",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "approved")
        self.assertTrue(data["saved"], "the code is single-use, so it must persist immediately")
        exchange.assert_called_once_with("cid", "csecret", "the-code")

        stored = web._profile_store.get_private_profile_by_id(profile["profile_id"])["credentials"]["anilist"]
        self.assertEqual(stored["access_token"], "fresh-token")
        self.assertEqual(stored["client_id"], "cid")
        self.assertEqual(stored["client_secret"], "csecret")

        # And AniList immediately becomes a usable sync target.
        providers = {p["key"]: p for p in self.client.post("/api/profile/pairs", json={}).get_json()["providers"]}
        self.assertEqual(providers["anilist"]["writes"], ["watchlist", "collection"])

    @patch("web.anilist_exchange_code_for_token")
    def test_anilist_auth_check_reuses_the_saved_secret(self, exchange) -> None:
        # A saved secret is masked in the form, so the client must be able to
        # finish connecting without the user retyping it.
        exchange.return_value = "tok"
        profile = self._make_anilist_profile("")
        web._profile_store.update_anilist_auth(profile["profile_id"], "stored-id", "stored-secret", "")
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/anilist/auth/check", json={"code": "the-code"})

        self.assertEqual(response.status_code, 200)
        exchange.assert_called_once_with("stored-id", "stored-secret", "the-code")

    @patch("web.anilist_exchange_code_for_token")
    def test_anilist_auth_check_surfaces_a_rejected_code(self, exchange) -> None:
        exchange.side_effect = ValueError("AniList rejected the authorization code: invalid_grant.")
        profile = self._make_anilist_profile("")
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/anilist/auth/check", json={
            "client_id": "cid", "client_secret": "csecret", "code": "bad",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("rejected the authorization code", response.get_json()["error"])
        stored = web._profile_store.get_private_profile_by_id(profile["profile_id"])["credentials"]["anilist"]
        self.assertEqual(stored["access_token"], "", "a failed exchange must not store anything")

    def test_capabilities_report_target_list_support(self) -> None:
        # Writing into a named list is a real capability. SIMKL and AniList have
        # no writable custom lists, so the destination picker must not appear.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        by_key = {p["key"]: p for p in self.client.post("/api/profile/pairs", json={}).get_json()["providers"]}

        self.assertTrue(by_key["pmdb"]["has_target_lists"])
        self.assertFalse(by_key["simkl"]["has_target_lists"])
        self.assertFalse(by_key["anilist"]["has_target_lists"])
        self.assertFalse(by_key["mdblist"]["has_target_lists"])
        # Every provider answers the same shape, configured or not, so the UI
        # never has to special-case the unconfigured branch.
        for provider in by_key.values():
            self.assertIn("has_list_search", provider)

    def test_saving_settings_keeps_configured_sync_pairs(self) -> None:
        # The settings form does not carry sync_pairs, and it used to wipe them.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        saved = self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt",
            "target": "pmdb",
            "categories": ["watchlist"],
            "source_lists": ["list:someone/top-100"],
        }]})
        self.assertEqual(saved.status_code, 200)

        self.client.post("/api/profile/save", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
            "credentials": {},
            "options": {"interval_seconds": 3600},
        })

        pairs = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["source_lists"], ["list:someone/top-100"])

    def test_a_self_explanatory_auth_error_gets_no_guessed_hint(self) -> None:
        # The 403 message names the Connections screen, and a bare "connection"
        # substring test turned that into "check your network connection" —
        # contradicting the real cause right next to it.
        from src.trakt_client import TraktAuthenticationError

        exc = TraktAuthenticationError(
            "Trakt returned 403 Forbidden — the Trakt Client ID is invalid. "
            "Verify the Client ID/Secret in Connections and reconnect Trakt."
        )

        self.assertIsNone(web._derive_provider_hint("Trakt", exc))

    def test_a_real_transport_failure_still_gets_the_network_hint(self) -> None:
        import requests

        hint = web._derive_provider_hint(
            "Trakt",
            requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='api.trakt.tv', port=443): Max retries exceeded "
                "(Caused by NewConnectionError('Failed to establish a new connection'))"
            ),
        )

        self.assertIn("Could not reach the Trakt API", hint or "")

    def test_prose_mentioning_connections_is_not_a_network_hint(self) -> None:
        hint = web._derive_provider_hint(
            "Trakt", RuntimeError("Re-authorize this app under Connections."),
        )

        self.assertIsNone(hint)

    def _fake_pair_adapters(self, contents):
        """Three in-memory adapters counting how often each is read."""
        from src.providers import CATEGORY_WATCHLIST, ProviderAdapter

        class Fake(ProviderAdapter):
            def __init__(self, key, items=None):
                self.key = key
                self.label = key.title()
                self.reads = (CATEGORY_WATCHLIST,)
                self.writes = (CATEGORY_WATCHLIST,)
                self._items = list(items or [])
                self.fetches = 0

            def fetch(self, category, source_lists=None):
                self.fetches += 1
                return list(self._items)

            def add(self, category, items, target_list=""):
                self._items.extend(items)
                return {"added": len(items)}

            def remove(self, category, items, target_list=""):
                return {"deleted": 0}

        return {key: Fake(key, items) for key, items in contents.items()}

    def test_running_all_pairs_reads_a_shared_source_once(self) -> None:
        # Fanning one service out to several targets is the normal setup, and it
        # used to cost one full read of the source per pair.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        self.client.post("/api/profile/pairs/save", json={"pairs": [
            {"name": "T-S", "source": "trakt", "target": "simkl", "categories": ["watchlist"]},
            {"name": "T-P", "source": "trakt", "target": "pmdb", "categories": ["watchlist"]},
        ]})

        movie = {"title": "M", "media_type": "movie", "tmdb_id": "1", "ids": {"tmdb": "1"}}
        adapters = self._fake_pair_adapters({"trakt": [movie], "simkl": [], "pmdb": []})

        with patch.object(web, "_build_provider_adapters", return_value=adapters):
            response = self.client.post("/api/profile/pairs/run", json={})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(adapters["trakt"].fetches, 1, "the shared source was read once per pair")
        self.assertEqual(body["cached_reads"], 1)
        self.assertEqual(body["provider_reads"], 3)
        self.assertEqual([r["added"] for r in body["results"]], [1, 1])

    def test_pair_run_records_last_result_for_the_dashboard(self) -> None:
        # The dashboard renders pair status straight off /status, so the run has
        # to leave both the definition and its outcome on the public profile.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        self.client.post("/api/profile/pairs/save", json={"pairs": [
            {"name": "T-S", "source": "trakt", "target": "simkl", "categories": ["watchlist"]},
        ]})

        movie = {"title": "M", "media_type": "movie", "tmdb_id": "1", "ids": {"tmdb": "1"}}
        adapters = self._fake_pair_adapters({"trakt": [movie], "simkl": []})
        with patch.object(web, "_build_provider_adapters", return_value=adapters):
            self.client.post("/api/profile/pairs/run", json={})

        status = self.client.post("/api/profile/status", json={}).get_json()["profile"]
        stored_pairs = status["options"]["sync_pairs"]
        self.assertEqual(len(stored_pairs), 1)
        pair_id = stored_pairs[0]["pair_id"]
        self.assertIn(pair_id, status["last_pair_results"])
        self.assertEqual(status["last_pair_results"][pair_id]["added"], 1)

        # The editor gets the same outcome attached to each pair.
        pairs = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]
        self.assertEqual(pairs[0]["last_result"]["added"], 1)

    def test_background_pair_run_populates_latest_results_and_sync_history(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        saved = self.client.post("/api/profile/pairs/save", json={"pairs": [
            {"name": "T-S", "source": "trakt", "target": "simkl", "categories": ["watchlist"]},
        ]}).get_json()
        pair_id = saved["profile"]["options"]["sync_pairs"][0]["pair_id"]
        modes = {
            "lists": False, "history": False, "resume": False,
            "pairs": True, "pair_ids": [pair_id],
        }
        claimed = web._profile_store.claim_profile_for_sync_by_id(profile["profile_id"], modes)
        movie = {"title": "M", "media_type": "movie", "tmdb_id": "1", "ids": {"tmdb": "1"}}
        adapters = self._fake_pair_adapters({"trakt": [movie], "simkl": []})

        with patch.object(web, "_build_provider_adapters", return_value=adapters):
            web._run_profile_sync(claimed, False, modes)

        status = self.client.post("/api/profile/status", json={}).get_json()["profile"]
        self.assertEqual(status["last_results"][0]["display_name"], "T-S")
        self.assertEqual(status["last_results"][0]["items_added"], 1)
        self.assertEqual(status["history"][0]["results"][0]["items_added"], 1)
        self.assertEqual(status["history"][0]["status"], "completed")
        self.assertTrue(status["last_sync"])

    def test_pair_result_errors_are_redacted_before_returning_or_storing(self) -> None:
        raw = {
            "pair_id": "a-b", "name": "A-B", "source": "a", "target": "b",
            "categories": [{
                "category": "watchlist",
                "errors": ["405 for https://api.mdblist.com/watchlist?apikey=secret-value"],
            }],
            "errors": [],
        }

        sanitized = web._sanitize_pair_result(raw)

        message = sanitized["categories"][0]["errors"][0]
        self.assertNotIn("secret-value", message)
        self.assertIn("apikey=[redacted]", message)

    def test_a_dry_run_does_not_overwrite_the_stored_pair_result(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        self.client.post("/api/profile/pairs/save", json={"pairs": [
            {"name": "T-S", "source": "trakt", "target": "simkl", "categories": ["watchlist"]},
        ]})
        movie = {"title": "M", "media_type": "movie", "tmdb_id": "1", "ids": {"tmdb": "1"}}

        adapters = self._fake_pair_adapters({"trakt": [movie], "simkl": []})
        with patch.object(web, "_build_provider_adapters", return_value=adapters):
            self.client.post("/api/profile/pairs/run", json={})
            self.client.post("/api/profile/pairs/run", json={"dry_run": True})

        status = self.client.post("/api/profile/status", json={}).get_json()["profile"]
        stored = list(status["last_pair_results"].values())[0]
        self.assertFalse(stored["dry_run"], "a dry run replaced the real run's recorded outcome")

    def test_two_way_pair_round_trips_and_is_offered_to_the_editor(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        saved = self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt", "target": "pmdb", "categories": ["watchlist"],
            "mode": "two_way", "removal_mode": "managed",
        }]})
        self.assertEqual(saved.status_code, 200)

        body = self.client.post("/api/profile/pairs", json={}).get_json()
        self.assertEqual(body["pairs"][0]["mode"], "two_way")
        self.assertEqual([m["key"] for m in body["pair_modes"]], ["one_way", "two_way"])
        # The editor narrows its removal choices from this rather than offering
        # one the backend would silently rewrite.
        self.assertEqual(body["two_way_removal_modes"], ["additive", "managed"])
        self.assertNotIn("mirror", body["two_way_removal_modes"])

    def test_saving_a_two_way_mirror_pair_stores_the_downgrade(self) -> None:
        # Mirror has no bidirectional meaning; the pair must not come back
        # claiming a mode it will not actually run in.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt", "target": "pmdb", "categories": ["watchlist"],
            "mode": "two_way", "removal_mode": "mirror",
        }]})

        pairs = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]
        self.assertEqual(pairs[0]["mode"], "two_way")
        self.assertEqual(pairs[0]["removal_mode"], "managed")

    def test_an_existing_pair_defaults_to_one_way(self) -> None:
        # Pairs saved before two-way existed carry no mode field.
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt", "target": "pmdb", "categories": ["watchlist"],
        }]})

        pairs = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]
        self.assertEqual(pairs[0]["mode"], "one_way")
        self.assertEqual(pairs[0]["removal_mode"], "additive")

    def test_running_a_two_way_pair_writes_both_services(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})
        self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "name": "T-S", "source": "trakt", "target": "simkl",
            "categories": ["watchlist"], "mode": "two_way",
        }]})

        a = {"title": "A", "media_type": "movie", "tmdb_id": "1", "ids": {"tmdb": "1"}}
        b = {"title": "B", "media_type": "movie", "tmdb_id": "2", "ids": {"tmdb": "2"}}
        adapters = self._fake_pair_adapters({"trakt": [a], "simkl": [b]})
        with patch.object(web, "_build_provider_adapters", return_value=adapters):
            response = self.client.post("/api/profile/pairs/run", json={})

        self.assertEqual(response.status_code, 200)
        result = response.get_json()["results"][0]
        self.assertEqual(result["mode"], "two_way")
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["categories"][0]["added_back"], 1)

    def test_pair_lists_target_role_returns_writable_lists_only(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/profile/pairs/lists", json={"provider": "simkl", "role": "target"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["lists"], [])

    def test_pair_target_list_round_trips(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        self.client.post("/api/profile/pairs/save", json={"pairs": [{
            "source": "trakt", "target": "pmdb", "categories": ["watchlist"],
            "target_list": "list:42",
        }]})
        listed = self.client.post("/api/profile/pairs", json={}).get_json()["pairs"]

        self.assertEqual(listed[0]["target_list"], "list:42")

    def test_pair_lists_search_returns_a_results_field(self) -> None:
        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        response = self.client.post("/api/profile/pairs/lists", json={"provider": "trakt", "search": "top"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.get_json())

    def test_status_can_recover_readonly_profile_from_uuid_without_session(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-secret"},
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        response = self.client.post("/api/profile/status", json={
            "profile_id": profile["profile_id"],
            "include_credentials": False,
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["profile"]["profile_id"], profile["profile_id"])
        self.assertNotIn("credentials", data["profile"])
        self.assertIn("queue_status", data["profile"])
        self.assertEqual(data["profile"]["queue_status"]["max_concurrent"], web.MAX_CONCURRENT_SYNCS)

    def test_delete_profile_endpoint_removes_signed_in_profile(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-secret",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        delete_response = self.client.post("/api/profile/delete", json={"confirm_text": "DELETE"})
        delete_data = delete_response.get_json()

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_data["status"], "deleted")

        with self.assertRaises(KeyError):
            web._profile_store.get_private_profile_by_id(profile["profile_id"])

    def test_reset_profile_password_updates_password_for_signed_in_profile(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 43200,
            "media_types": ["shows", "movies", "anime"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        reset_response = self.client.post("/api/profile/password/reset", json={
            "current_password": "secret",
            "new_password": "new-secret",
        })
        reset_data = reset_response.get_json()

        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_data["profile"]["profile_id"], profile["profile_id"])

        relogin_old = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(relogin_old.status_code, 401)

        relogin_new = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "new-secret",
        })
        self.assertEqual(relogin_new.status_code, 200)

    def test_reset_profile_password_requires_the_current_password(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 43200,
            "media_types": ["shows", "movies", "anime"],
        })

        # The profile UUID is a handle, not a secret: it is displayed in the UI
        # and accepted unauthenticated by /api/profile/status. Knowing it alone
        # must not let a stranger change the password, take a session, and lock
        # the owner out.
        takeover = self.client.post("/api/profile/password/reset", json={
            "profile_id": profile["profile_id"],
            "new_password": "attacker-secret",
        })
        self.assertEqual(takeover.status_code, 400)
        self.assertEqual(takeover.get_json()["error"], "Current profile password is required")

        wrong_password = self.client.post("/api/profile/password/reset", json={
            "profile_id": profile["profile_id"],
            "current_password": "not-the-password",
            "new_password": "attacker-secret",
        })
        self.assertEqual(wrong_password.status_code, 401)
        self.assertNotIn("profile", wrong_password.get_json())

        # The owner's password still works and the attacker's does not.
        self.assertEqual(self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "attacker-secret",
        }).status_code, 401)
        self.assertEqual(self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        }).status_code, 200)

        # With the current password it succeeds, from a signed-out client too.
        signed_out = web.app.test_client()
        changed = signed_out.post("/api/profile/password/reset", json={
            "profile_id": profile["profile_id"],
            "current_password": "secret",
            "new_password": "new-secret",
        })
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.get_json()["profile"]["profile_id"], profile["profile_id"])
        self.assertEqual(self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "new-secret",
        }).status_code, 200)

    def test_reset_profile_password_requires_uuid_when_not_signed_in(self) -> None:
        reset_response = self.client.post("/api/profile/password/reset", json={
            "new_password": "new-secret",
        })
        reset_data = reset_response.get_json()

        self.assertEqual(reset_response.status_code, 400)
        self.assertEqual(reset_data["error"], "Profile UUID is required")

    def test_scheduler_waits_before_claiming_anything_on_startup(self) -> None:
        # The scheduler is started by the first HTTP request, and every profile
        # whose schedule lapsed while the server was down is due immediately.
        # Without a grace period that request triggers a stampede of syncs and
        # then has to compete with it, so the site hangs for exactly the person
        # who woke it up.
        claimed: list[int] = []

        class _Store:
            def claim_due_profiles(self, limit=None):
                claimed.append(limit)
                return []

        scheduler = web.ProfileScheduler(_Store(), poll_seconds=60)
        original_grace = web.SCHEDULER_STARTUP_GRACE_SECONDS
        try:
            web.SCHEDULER_STARTUP_GRACE_SECONDS = 30
            scheduler.start()
            time.sleep(0.4)
            self.assertEqual(claimed, [], "claimed profiles during the startup grace period")

            # With no grace it polls straight away, and asks for a bounded batch
            # rather than every due profile at once.
            web.SCHEDULER_STARTUP_GRACE_SECONDS = 0
            immediate = web.ProfileScheduler(_Store(), poll_seconds=60)
            immediate.start()
            time.sleep(0.4)
            self.assertEqual(claimed, [web.SCHEDULER_CLAIM_BATCH])
            immediate._stop.set()
        finally:
            web.SCHEDULER_STARTUP_GRACE_SECONDS = original_grace
            scheduler._stop.set()

    def test_profile_save_rate_limits_password_guesses(self) -> None:
        # Saving with a UUID + password and no session signs in and hands back a
        # session cookie, so it must be throttled like /api/profile/login.
        profile = web._profile_store.create_profile("secret", _blank_credentials(), {
            "auto_sync": True,
            "interval_seconds": 43200,
            "media_types": ["shows", "movies", "anime"],
        })
        web._login_limiter = web.LoginAttemptLimiter(max_attempts=3, window_seconds=60)

        attacker = web.app.test_client()
        credentials = _blank_credentials()
        credentials["pmdb"]["api_key"] = "pm-key"
        credentials["anilist"] = {"username": "someone", "selected_statuses": ["CURRENT"]}
        payload = {
            "profile_id": profile["profile_id"],
            "credentials": credentials,
            "options": {"auto_sync": True, "interval_seconds": 43200, "media_types": ["shows"]},
        }
        statuses = [
            attacker.post("/api/profile/save", json={**payload, "password": f"guess-{i}"}).status_code
            for i in range(5)
        ]

        self.assertEqual(statuses[:3], [401, 401, 401])
        self.assertEqual(statuses[3:], [429, 429])

        # The real password is refused too while the lock holds — the throttle is
        # on the client, not on the guess.
        locked_out = attacker.post("/api/profile/save", json={**payload, "password": "secret"})
        self.assertEqual(locked_out.status_code, 429)

    def test_password_change_signs_out_sessions_opened_before_it(self) -> None:
        profile = web._profile_store.create_profile("secret", _blank_credentials(), {
            "auto_sync": True,
            "interval_seconds": 43200,
            "media_types": ["shows", "movies", "anime"],
        })

        # An attacker who got in before the password change keeps a cookie.
        stolen = web.app.test_client()
        self.assertEqual(stolen.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        }).status_code, 200)
        self.assertEqual(stolen.post("/api/profile/status", json={}).status_code, 200)

        owner = web.app.test_client()
        self.assertEqual(owner.post("/api/profile/password/reset", json={
            "profile_id": profile["profile_id"],
            "current_password": "secret",
            "new_password": "new-secret",
        }).status_code, 200)

        self.assertEqual(stolen.post("/api/profile/status", json={}).status_code, 401)
        self.assertEqual(owner.post("/api/profile/status", json={}).status_code, 200)

    def test_stop_sync_endpoint_marks_profile_as_stopping(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        web._profile_store.claim_profile_for_sync_by_id(profile["profile_id"])

        stop_response = self.client.post("/api/profile/sync/stop", json={})
        stop_data = stop_response.get_json()

        self.assertEqual(stop_response.status_code, 200)
        self.assertEqual(stop_data["status"], "stopping")
        self.assertTrue(stop_data["profile"]["sync_cancel_requested"])
        self.assertEqual(stop_data["profile"]["sync_status"], "Stopping...")
        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertTrue(private_profile["sync_cancel_requested"])

    @patch("web.PublicMetaDBClient.clear_watched_history")
    def test_clear_watch_history_endpoint_clears_pmdb_history(self, mock_clear_watched_history) -> None:
        mock_clear_watched_history.return_value = 7
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows", "movies", "anime"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        private_profile["activity_state"] = {
            "simkl_history_cursor": "2026-04-01T12:00:00Z",
            "trakt_history_cursor": "2026-04-02T12:00:00Z",
        }
        web._profile_store._profiles[profile["profile_id"]] = private_profile

        clear_response = self.client.post("/api/profile/activity/history/clear", json={})
        clear_data = clear_response.get_json()

        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(clear_data["status"], "cleared")
        self.assertEqual(clear_data["deleted_count"], 7)
        self.assertEqual(clear_data["profile"]["activity_state"]["simkl_history_cursor"], "")
        self.assertEqual(clear_data["profile"]["activity_state"]["trakt_history_cursor"], "")
        mock_clear_watched_history.assert_called_once()

    def test_activity_sync_endpoint_starts_history_only_run(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "trakt-client",
                "client_secret": "trakt-secret",
                "access_token": "trakt-token",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
            "trakt_sync_watched_history": True,
            "trakt_watched_history_interval_seconds": 43200,
            "trakt_sync_resume_progress": False,
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        with patch.object(web._sync_runner, "enqueue") as mock_enqueue:
            response = self.client.post("/api/profile/activity/sync", json={"mode": "history"})
            data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "started")
        self.assertEqual(data["mode"], "history")
        enqueue_args = mock_enqueue.call_args.args
        self.assertEqual(enqueue_args[2], {"lists": False, "history": True, "resume": False, "full_history": True})

    def test_profile_sync_endpoint_enqueues_list_job(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows", "movies", "anime"],
            "sync_pairs": [{
                "pair_id": "simkl-pmdb", "source": "simkl", "target": "pmdb",
                "categories": ["collection"], "enabled": True,
            }],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        with patch.object(web._sync_runner, "enqueue") as mock_enqueue, patch.object(web._sync_runner, "queue_size", return_value=3):
            response = self.client.post("/api/profile/sync", json={"dry_run": False})
            data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "started")
        self.assertEqual(data["dry_run"], False)
        self.assertEqual(data["queued_jobs"], 3)
        enqueue_args = mock_enqueue.call_args.args
        self.assertEqual(enqueue_args[1], False)
        self.assertEqual(enqueue_args[2], {
            "lists": False, "history": False, "resume": False,
            "pairs": True, "pair_ids": ["simkl-pmdb"],
        })

    def test_sync_run_endpoints_return_summaries_and_details(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-key"},
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        claimed = web._profile_store.claim_profile_for_sync(profile["profile_id"], "secret")
        run_id = claimed["sync_job_id"]
        web._profile_store.record_sync_success(
            profile["profile_id"],
            [{
                "list_name": "Watching - Series",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
                "row_key": "simkl|watching-series|status_list",
                "row_type": "status_list",
                "has_details": True,
                "run_id": run_id,
                "error_count": 1,
            }],
            detailed_results=[{
                "list_name": "Watching - Series",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
                "row_key": "simkl|watching-series|status_list",
                "row_type": "status_list",
                "errors": ["Bearer secret-token failed"],
                "error_count": 1,
                "sample_failed_titles": ["Demo Show"],
            }],
        )

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        runs_response = self.client.post("/api/profile/sync/runs", json={"page": 1, "page_size": 10})
        runs_data = runs_response.get_json()
        self.assertEqual(runs_response.status_code, 200)
        self.assertEqual(runs_data["items"][0]["run_id"], run_id)

        detail_response = self.client.post("/api/profile/sync/run-details", json={"run_id": run_id})
        detail_data = detail_response.get_json()
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_data["run"]["run_id"], run_id)
        self.assertEqual(detail_data["run"]["rows"][0]["errors"], ["Bearer=[redacted] failed"])

    def test_admin_stats_include_anime_mapping_cache_metadata(self) -> None:
        stats = web._admin_stats()

        self.assertIn("fribb_last_checked_at", stats["cache"])
        self.assertIn("fribb_cache_updated_at", stats["cache"])
        self.assertIn("fribb_source_url", stats["cache"])
        self.assertIn("xml_last_checked_at", stats["cache"])
        self.assertIn("xml_cache_updated_at", stats["cache"])
        self.assertIn("xml_source_url", stats["cache"])
        self.assertIn("mapping_refresh_interval_seconds", stats["cache"])

    def test_admin_dashboard_renders_anime_cache_metadata(self) -> None:
        web.ADMIN_PASSWORD = "secret"

        login = self.client.post("/admin/login", data={"password": "secret"})
        response = self.client.get("/admin")
        html = response.get_data(as_text=True)

        self.assertEqual(login.status_code, 302)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fribb checked", html)
        self.assertIn("XML checked", html)
        self.assertIn("Refresh interval", html)

    def test_admin_dashboard_offers_theme_and_instance_purge(self) -> None:
        web.ADMIN_PASSWORD = "secret"
        self.client.post("/admin/login", data={"password": "secret"})

        html = self.client.get("/admin").get_data(as_text=True)

        self.assertIn('id="admin-theme-select"', html)
        self.assertIn('data-mode-toggle', html)
        self.assertIn("Instance Danger Zone", html)
        self.assertIn("Remove all linked profiles", html)
        self.assertIn('id="delete-all-profiles-confirm"', html)

    def test_delete_all_profiles_requires_admin_and_confirmation(self) -> None:
        web.ADMIN_PASSWORD = "secret"
        profile = web._profile_store.create_profile("pw", _blank_credentials(), {})

        unauthorized = self.client.post(
            "/admin/api/profiles/delete-all",
            json={"confirm_text": "DELETE ALL PROFILES"},
        )
        self.assertEqual(unauthorized.status_code, 401)

        self.client.post("/admin/login", data={"password": "secret"})
        rejected = self.client.post(
            "/admin/api/profiles/delete-all", json={"confirm_text": "DELETE"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIsNotNone(
            web._profile_store.get_private_profile_by_id(profile["profile_id"])
        )

    def test_admin_can_delete_all_profiles_and_linked_runtime_data(self) -> None:
        from src.library_store import LibraryStore

        first = web._profile_store.create_profile("pw", _blank_credentials(), {})
        second = web._profile_store.create_profile("pw", _blank_credentials(), {})
        first_id = first["profile_id"]
        second_id = second["profile_id"]
        token = web._session_store.create(first_id)
        web._mdblist_pkce_store.put(first_id, "verifier", "http://localhost/callback")

        library_path = Path(self.tmpdir.name) / "library" / f"{first_id}.json"
        library = LibraryStore(library_path)
        library.save()
        web._library_stores[first_id] = library
        self.addCleanup(web._library_stores.pop, first_id, None)

        self._admin_login()
        response = self.client.post(
            "/admin/api/profiles/delete-all",
            json={"confirm_text": "DELETE ALL PROFILES"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["deleted"], 2)
        self.assertEqual(response.get_json()["remaining"], 0)
        self.assertEqual(web._profile_store._profiles, {})
        self.assertFalse(library_path.exists())
        self.assertIsNone(web._session_store.get_profile_id(token))
        self.assertIsNone(web._mdblist_pkce_store.take(first_id))
        self.assertNotIn(first_id, web._library_stores)
        self.assertNotIn(second_id, web._profile_store._profiles)

    def test_delete_all_profiles_is_all_or_nothing_during_a_sync(self) -> None:
        first = web._profile_store.create_profile("pw", _blank_credentials(), {})
        second = web._profile_store.create_profile("pw", _blank_credentials(), {})
        web._profile_store._profiles[first["profile_id"]]["sync_running"] = True
        self._admin_login()

        response = self.client.post(
            "/admin/api/profiles/delete-all",
            json={"confirm_text": "DELETE ALL PROFILES"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(web._profile_store._profiles), 2)
        self.assertIn(first["profile_id"], web._profile_store._profiles)
        self.assertIn(second["profile_id"], web._profile_store._profiles)

    # ── admin settings ────────────────────────────────────────────────────
    def _admin_login(self) -> None:
        web.ADMIN_PASSWORD = "secret"
        self.client.post("/admin/login", data={"password": "secret"})

    def _use_temp_settings(self) -> None:
        from src.env_settings import SettingsStore
        self._original_settings_store = web._settings_store
        self._original_env_before = web._ENV_BEFORE_OVERRIDES
        web._settings_store = SettingsStore(Path(self.tmpdir.name) / "settings.json")
        web._ENV_BEFORE_OVERRIDES = {}
        self.addCleanup(setattr, web, "_settings_store", self._original_settings_store)
        self.addCleanup(setattr, web, "_ENV_BEFORE_OVERRIDES", self._original_env_before)

    def test_settings_endpoints_require_admin(self) -> None:
        web.ADMIN_PASSWORD = "secret"
        self.assertEqual(self.client.get("/admin/api/settings").status_code, 401)
        self.assertEqual(self.client.post("/admin/api/settings", json={"values": {}}).status_code, 401)
        self.assertEqual(self.client.post("/admin/api/settings/reset", json={"key": "x"}).status_code, 401)

    def test_settings_are_saved_and_reported_as_overrides(self) -> None:
        self._use_temp_settings()
        self._admin_login()

        saved = self.client.post("/admin/api/settings", json={
            "values": {"SYNCMETA_TRAKT_READ_TIMEOUT": "45"},
        })
        data = saved.get_json()

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(data["changed"], ["SYNCMETA_TRAKT_READ_TIMEOUT"])
        # It is read into a module constant at import, so it cannot move now.
        self.assertEqual(data["needs_restart"], ["SYNCMETA_TRAKT_READ_TIMEOUT"])
        self.assertEqual(os.environ["SYNCMETA_TRAKT_READ_TIMEOUT"], "45")
        self.addCleanup(os.environ.pop, "SYNCMETA_TRAKT_READ_TIMEOUT", None)

        row = next(r for r in self.client.get("/admin/api/settings").get_json()["settings"]
                   if r["key"] == "SYNCMETA_TRAKT_READ_TIMEOUT")
        self.assertEqual(row["source"], "override")
        self.assertEqual(row["value"], "45")

    def test_an_invalid_value_is_rejected_with_a_usable_message(self) -> None:
        self._use_temp_settings()
        self._admin_login()

        resp = self.client.post("/admin/api/settings", json={
            "values": {"SYNCMETA_MAX_CONCURRENT_SYNCS": "500"},
        })

        self.assertEqual(resp.status_code, 400)
        self.assertIn("maximum is 8", resp.get_json()["error"])
        self.assertEqual(web._settings_store.overrides(), {})

    def test_an_unlisted_variable_cannot_be_written(self) -> None:
        self._use_temp_settings()
        self._admin_login()

        resp = self.client.post("/admin/api/settings", json={
            "values": {"SYNCMETA_MASTER_KEY": "attacker-supplied"},
        })

        self.assertEqual(resp.status_code, 400)
        self.assertNotEqual(os.environ.get("SYNCMETA_MASTER_KEY"), "attacker-supplied")

    def test_a_live_setting_takes_effect_without_a_restart(self) -> None:
        self._use_temp_settings()
        self._admin_login()

        resp = self.client.post("/admin/api/settings", json={
            "values": {"SITE_ACCESS_PASSWORD": "gate"},
        })

        self.assertEqual(resp.get_json()["applied_live"], ["SITE_ACCESS_PASSWORD"])
        self.assertEqual(web.SITE_ACCESS_PASSWORD, "gate")
        self.addCleanup(os.environ.pop, "SITE_ACCESS_PASSWORD", None)

    def test_the_admin_password_cannot_be_reset_into_nothing(self) -> None:
        self._use_temp_settings()
        self._admin_login()
        self.client.post("/admin/api/settings", json={"values": {"ADMIN_PASSWORD": "newpass"}})
        self.addCleanup(os.environ.pop, "ADMIN_PASSWORD", None)

        resp = self.client.post("/admin/api/settings/reset", json={"key": "ADMIN_PASSWORD"})

        # There is no environment value behind it, so a reset would leave the
        # panel with no password at all — which disables it permanently.
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(web.ADMIN_PASSWORD, "newpass")

    def test_resetting_falls_back_to_the_environment_value(self) -> None:
        self._use_temp_settings()
        web._ENV_BEFORE_OVERRIDES = {"SITE_ACCESS_PASSWORD": "from-compose"}
        self._admin_login()
        self.client.post("/admin/api/settings", json={"values": {"SITE_ACCESS_PASSWORD": "from-panel"}})
        self.assertEqual(web.SITE_ACCESS_PASSWORD, "from-panel")

        resp = self.client.post("/admin/api/settings/reset", json={"key": "SITE_ACCESS_PASSWORD"})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["removed"])
        self.assertEqual(web.SITE_ACCESS_PASSWORD, "from-compose")
        self.addCleanup(os.environ.pop, "SITE_ACCESS_PASSWORD", None)

    def test_saved_secrets_are_not_returned_to_the_browser(self) -> None:
        self._use_temp_settings()
        self._admin_login()
        self.client.post("/admin/api/settings", json={"values": {"SITE_ACCESS_PASSWORD": "topsecret"}})
        self.addCleanup(os.environ.pop, "SITE_ACCESS_PASSWORD", None)

        body = self.client.get("/admin/api/settings").get_data(as_text=True)
        page = self.client.get("/admin").get_data(as_text=True)

        self.assertNotIn("topsecret", body)
        self.assertNotIn("topsecret", page)

    def test_admin_dashboard_renders_the_settings_form(self) -> None:
        self._use_temp_settings()
        self._admin_login()

        html = self.client.get("/admin").get_data(as_text=True)

        self.assertIn("SYNCMETA_MAX_CONCURRENT_SYNCS", html)
        self.assertIn("Network timeouts", html)
        # Locked keys are shown so they are visibly not-editable, not hidden.
        self.assertIn("SYNCMETA_MASTER_KEY", html)


    # ── local library ─────────────────────────────────────────────────────
    def _library_profile(self):
        creds = _blank_credentials()
        creds["pmdb"]["api_key"] = "pk"
        creds["anilist"] = {"username": "someone", "access_token": "", "selected_statuses": ["CURRENT"]}
        profile = web._profile_store.create_profile("pw", creds, {})
        profile_id = profile["profile_id"]
        self.client.post("/api/profile/login", json={"profile_id": profile_id, "password": "pw"})
        store = web._library_store_for(profile_id)
        store.add("watchlist", [
            {"media_type": "tv", "tmdb_id": "1429", "title": "Attack on Titan", "season": 1, "anilist_id": 16498},
            {"media_type": "tv", "tmdb_id": "1429", "title": "AoT S2", "season": 2, "anilist_id": 25777},
            {"media_type": "movie", "tmdb_id": "128", "title": "Mononoke", "mal_id": 164},
            {"media_type": "movie", "tmdb_id": "27205", "title": "Inception"},
        ], source="simkl")
        store.mark_watched([
            {"media_type": "tv", "tmdb_id": "1429", "title": "Attack on Titan", "season": 1, "episode": 1},
            {"media_type": "tv", "tmdb_id": "1429", "title": "Attack on Titan", "season": 2, "episode": 1},
        ], source="trakt")
        self.addCleanup(web._library_stores.pop, profile_id, None)
        return profile_id, store

    def test_library_entries_require_a_session(self) -> None:
        self.assertEqual(self.client.post("/api/profile/library/entries", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/profile/library/entry", json={}).status_code, 401)

    def test_two_seasons_of_one_anime_are_one_library_row(self) -> None:
        self._library_profile()

        data = self.client.post("/api/profile/library/entries", json={}).get_json()

        self.assertEqual(data["total"], 3)
        titles = sorted(row["title"] for row in data["entries"])
        self.assertEqual(titles, ["Attack on Titan", "Inception", "Mononoke"])

    def test_the_kind_filter_separates_anime_films_from_films(self) -> None:
        self._library_profile()

        anime_films = self.client.post(
            "/api/profile/library/entries", json={"kinds": ["anime_movie"]},
        ).get_json()
        films = self.client.post(
            "/api/profile/library/entries", json={"kinds": ["movie"]},
        ).get_json()

        self.assertEqual([row["title"] for row in anime_films["entries"]], ["Mononoke"])
        self.assertEqual([row["title"] for row in films["entries"]], ["Inception"])

    def test_the_section_filter_selects_watched_titles(self) -> None:
        self._library_profile()

        data = self.client.post(
            "/api/profile/library/entries", json={"section": "history"},
        ).get_json()

        self.assertEqual([row["title"] for row in data["entries"]], ["Attack on Titan"])

    def test_search_matches_titles(self) -> None:
        self._library_profile()

        data = self.client.post(
            "/api/profile/library/entries", json={"query": "incep"},
        ).get_json()

        self.assertEqual([row["title"] for row in data["entries"]], ["Inception"])

    def test_the_detail_view_lists_seasons_and_watched_episodes(self) -> None:
        self._library_profile()

        data = self.client.post(
            "/api/profile/library/entry", json={"key": "tmdb:tv:1429"},
        ).get_json()

        self.assertEqual([season["season"] for season in data["seasons"]], [1, 2])
        self.assertEqual(data["watched_total"], 2)
        watched = [
            episode for season in data["seasons"]
            for episode in season["episodes"] if episode["watched"]
        ]
        self.assertEqual(len(watched), 2)

    def test_the_detail_view_works_without_a_tmdb_key(self) -> None:
        # Episode numbers and watched state are local; only names and stills
        # need TMDB, so the view degrades rather than failing.
        self._library_profile()

        data = self.client.post(
            "/api/profile/library/entry", json={"key": "tmdb:tv:1429"},
        ).get_json()

        self.assertFalse(data["tmdb_configured"])
        self.assertEqual(data["seasons"][0]["episodes"][0]["name"], "Episode 1")

    def test_an_unknown_key_is_a_404_not_an_empty_page(self) -> None:
        self._library_profile()

        response = self.client.post("/api/profile/library/entry", json={"key": "tmdb:tv:999999"})

        self.assertEqual(response.status_code, 404)

    def test_library_is_offered_as_a_pair_provider(self) -> None:
        profile_id, _store = self._library_profile()

        data = self.client.post("/api/profile/pairs", json={}).get_json()

        library = next(p for p in data["providers"] if p["key"] == "library")
        self.assertTrue(library["configured"])
        self.assertEqual(
            set(library["writes"]), {"watchlist", "history", "collection"},
        )

    def test_status_summary_omits_verbose_error_arrays(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-key"},
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        claimed = web._profile_store.claim_profile_for_sync(profile["profile_id"], "secret")
        web._profile_store.record_sync_success(
            profile["profile_id"],
            [{
                "list_name": "Watching - Series",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
                "row_key": "simkl|watching-series|status_list",
                "row_type": "status_list",
                "has_details": True,
                "run_id": claimed["sync_job_id"],
                "error_count": 1,
            }],
            detailed_results=[{
                "list_name": "Watching - Series",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
                "row_key": "simkl|watching-series|status_list",
                "row_type": "status_list",
                "errors": ["Authorization: Bearer super-secret"],
                "error_count": 1,
            }],
        )

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        response = self.client.post("/api/profile/status", json={})
        data = response.get_json()
        row = data["profile"]["last_results"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(row["row_key"], "simkl|watching-series|status_list")
        self.assertTrue(row["has_details"])
        self.assertNotIn("errors", row)

    @patch("web.PublicMetaDBClient.add_item_to_list")
    @patch("web._resolve_unresolved_item_automatically")
    def test_unresolved_auto_resolve_endpoint_maps_and_adds_item(self, mock_auto_resolve, mock_add_item_to_list) -> None:
        mock_auto_resolve.return_value = 4567
        mock_add_item_to_list.return_value = {"success": True}

        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["anime"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        private_profile["managed_lists"] = [{
            "list_name": "Watching - Anime",
            "list_id": "pmdb-list-1",
            "display_name": "Watching - Anime",
            "source_name": "AniList",
            "selection": {"source": "anilist", "status": "CURRENT"},
        }]
        private_profile["last_results"] = [{
            "list_name": "Watching - Anime",
            "display_name": "Watching - Anime",
            "source_name": "AniList",
            "items_resolved": 0,
            "items_added": 0,
            "items_skipped_unresolved": 1,
        }]
        private_profile["unresolved_items"] = [{
            "cache_key": "anime-1",
            "title": "Example Anime",
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "12345",
            "list_name": "Watching - Anime",
        }]
        web._profile_store._profiles[profile["profile_id"]] = private_profile

        response = self.client.post("/api/profile/unresolved/auto-resolve", json={"cache_key": "anime-1"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "resolved")
        self.assertEqual(data["tmdb_id"], 4567)
        self.assertTrue(data["pmdb_added"])
        self.assertEqual(data["items"], [])
        self.assertEqual(data["profile"]["last_results"][0]["items_skipped_unresolved"], 0)
        self.assertEqual(data["profile"]["last_results"][0]["items_added"], 1)
        mock_add_item_to_list.assert_called_once_with("pmdb-list-1", 4567, "tv")

    @patch("src.fribb_client.lookup_by_anilist")
    @patch("web.PublicMetaDBClient.lookup_by_external_id_detailed")
    def test_auto_resolve_does_not_reuse_a_rejected_candidate(
        self, mock_lookup_detailed, mock_fribb_anilist,
    ) -> None:
        # candidate_tmdb_id is the ID the matcher *declined* — an unverified or
        # blocklisted community mapping, recorded only as a hint for the user.
        # Returning it as the automatic answer re-applied the exact mapping the
        # safety guards rejected.
        mock_fribb_anilist.return_value = None
        mock_lookup_detailed.return_value = {"tmdb_id": None, "status": "miss"}

        profile = self._make_bare_profile()
        self.client.post("/api/profile/login", json={"profile_id": profile["profile_id"], "password": "secret"})

        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        private_profile["unresolved_items"] = [{
            "cache_key": "anime-blocked",
            "title": "Example Anime",
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "12345",
            "anime_resolve_mode": "list_identity",
            # 277700 is on the known-bad blocklist in matcher.py.
            "candidate_tmdb_id": 277700,
            "unresolved_reason": "unconfirmed_mapping",
        }]
        web._profile_store._profiles[profile["profile_id"]] = private_profile

        response = self.client.post("/api/profile/unresolved/auto-resolve", json={"cache_key": "anime-blocked"})
        data = response.get_json()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(data["status"], "unresolved")
        self.assertIn("No automatic mapping", data["message"])

    @patch("web.PublicMetaDBClient.delete_list")
    def test_delete_managed_list_endpoint_removes_mdblist_selection(self, mock_delete_list) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "mdbl-key",
                "selected_lists": [{
                    "id": 7,
                    "name": "Favorites",
                    "mediatype": "movie",
                }],
            },
            "pmdb": {
                "api_key": "pmdb-secret",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows", "movies"],
        })
        web._profile_store.record_sync_success(profile["profile_id"], [{
            "list_name": "Favorites",
            "display_name": "Favorites",
            "source_name": "MDBList",
        }], managed_lists=[{
            "list_name": "Favorites",
            "list_id": "pmdb-list-1",
            "display_name": "Favorites",
            "source_name": "MDBList",
            "selection": {
                "source": "mdblist",
                "id": 7,
                "mediatype": "movie",
            },
        }])

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        delete_response = self.client.post("/api/profile/list/delete", json={"list_name": "Favorites"})
        delete_data = delete_response.get_json()

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_data["profile"]["last_results"], [])
        self.assertEqual(delete_data["profile"]["credentials"]["mdblist"]["selected_lists"], [])
        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertEqual(private_profile["managed_lists"], [])
        self.assertEqual(private_profile["credentials"]["mdblist"]["selected_lists"], [])
        mock_delete_list.assert_called_once_with("pmdb-list-1")

    @patch("web.PublicMetaDBClient.delete_list")
    def test_delete_managed_list_endpoint_removes_trakt_selection_by_list_name(self, mock_delete_list) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "trakt-client",
                "client_secret": "trakt-secret",
                "access_token": "trakt-token",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [{
                    "name": "Recommended Movies",
                    "user": "trakt",
                    "slug": "recommended-movies",
                    "source": "default",
                    "catalog_key": "recommended-movies",
                }],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-secret",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows", "movies"],
        })
        web._profile_store.record_sync_success(profile["profile_id"], [{
            "list_name": "Recommended Movies",
            "display_name": "Recommended Movies",
            "source_name": "Trakt",
        }], managed_lists=[{
            "list_name": "Recommended Movies",
            "list_id": "pmdb-list-2",
            "display_name": "Recommended Movies",
            "source_name": "Trakt",
            "selection": {},
        }])

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        delete_response = self.client.post("/api/profile/list/delete", json={"list_name": "Recommended Movies"})
        delete_data = delete_response.get_json()

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_data["profile"]["credentials"]["trakt"]["selected_lists"], [])
        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertEqual(private_profile["credentials"]["trakt"]["selected_lists"], [])
        mock_delete_list.assert_called_once_with("pmdb-list-2")

    def _make_library_profile(self, tmdb_key: str = "tmdb-key") -> dict:
        return web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": "pmdb-secret"},
            "tmdb": {"api_key": tmdb_key},
        }, {"auto_sync": False, "media_types": ["shows"]})

    def _login(self, profile: dict) -> None:
        response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(response.status_code, 200)

    def test_library_endpoints_require_a_session(self) -> None:
        for path in ("/api/profile/library/overview", "/api/profile/library/items", "/api/profile/library/history"):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 401, path)

    def test_library_overview_lists_pmdb_lists_and_tmdb_state(self) -> None:
        self._login(self._make_library_profile(tmdb_key=""))
        with patch.object(web.PublicMetaDBClient, "get_lists", return_value=[
            {"id": "l2", "name": "Anime", "type": "custom", "item_count": 3},
            {"id": "l1", "name": "Watchlist", "type": "watchlist", "item_count": 7},
            {"name": "No id — skipped"},
        ]):
            response = self.client.post("/api/profile/library/overview", json={})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data["tmdb_configured"])
        # The watchlist sorts first, entries without an id are dropped.
        self.assertEqual([entry["id"] for entry in data["lists"]], ["l1", "l2"])

    def test_library_overview_attaches_source_service(self) -> None:
        profile = self._make_library_profile(tmdb_key="")
        web._profile_store.record_sync_success(profile["profile_id"], [], managed_lists=[
            {"list_name": "Watching - Anime", "list_id": "l2", "display_name": "Watching - Anime", "source_name": "SIMKL", "selection": {}},
            {"list_name": "Action", "list_id": "", "display_name": "Action", "source_name": "Trakt", "selection": {}},
        ])
        self._login(profile)
        with patch.object(web.PublicMetaDBClient, "get_lists", return_value=[
            {"id": "l2", "name": "Watching - Anime", "type": "custom"},
            {"id": "l9", "name": "Action", "type": "custom"},
            {"id": "l5", "name": "Manually made", "type": "custom"},
        ]):
            response = self.client.post("/api/profile/library/overview", json={})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        by_id = {entry["id"]: entry["source"] for entry in data["lists"]}
        self.assertEqual(by_id["l2"], "SIMKL")   # joined on list id
        self.assertEqual(by_id["l9"], "Trakt")   # joined on list name
        self.assertEqual(by_id["l5"], "")        # not SyncMeta-managed

    def test_library_items_enrich_with_tmdb_details(self) -> None:
        self._login(self._make_library_profile())
        with patch.object(web.PublicMetaDBClient, "get_list_items", return_value=[
            {"id": "i1", "tmdb_id": 100, "media_type": "tv"},
            {"id": "i2", "tmdb_id": 200, "media_type": "movie"},
        ]), patch.object(web.TmdbClient, "get_details_batch", return_value={
            ("tv", 100): {"title": "Frieren", "year": "2023", "poster_url": "https://img/f.jpg"},
        }):
            response = self.client.post("/api/profile/library/items", json={"list_id": "l1"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["tmdb_configured"])
        self.assertEqual(data["items"][0]["title"], "Frieren")
        self.assertEqual(data["items"][0]["poster_url"], "https://img/f.jpg")
        # The unmatched item keeps its bare shape rather than being dropped.
        self.assertEqual(data["items"][1]["tmdb_id"], 200)
        self.assertNotIn("poster_url", data["items"][1])

    def test_list_preview_resolves_the_list_by_managed_name(self) -> None:
        # The dashboard rows only carry a list name; the id lives in
        # managed_lists, which is deliberately not shipped to the client.
        profile = self._make_library_profile()
        web._profile_store.record_sync_success(profile["profile_id"], [], managed_lists=[
            {"list_name": "Completed - Anime", "list_id": "l7", "display_name": "Completed - Anime",
             "source_name": "SIMKL", "selection": {}},
        ])
        self._login(profile)
        with patch.object(web.PublicMetaDBClient, "get_list_items", return_value=[
            {"id": "i1", "tmdb_id": 100, "media_type": "tv"},
        ]) as get_items, patch.object(web.TmdbClient, "get_details_batch", return_value={
            ("tv", 100): {"title": "Frieren", "year": "2023", "poster_url": "https://img/f.jpg"},
        }):
            response = self.client.post(
                "/api/profile/library/list-preview", json={"list_name": "Completed - Anime"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        get_items.assert_called_once_with("l7")
        self.assertFalse(data["missing"])
        self.assertEqual(data["items"][0]["poster_url"], "https://img/f.jpg")
        self.assertEqual(data["total"], 1)

    def test_list_preview_falls_back_to_a_name_lookup(self) -> None:
        self._login(self._make_library_profile())
        with patch.object(web.PublicMetaDBClient, "find_list_by_name", return_value={"id": "l9"}), \
             patch.object(web.PublicMetaDBClient, "get_list_items", return_value=[]) as get_items:
            response = self.client.post(
                "/api/profile/library/list-preview", json={"list_name": "Made by hand"})

        self.assertEqual(response.status_code, 200)
        get_items.assert_called_once_with("l9")

    def test_list_preview_reports_a_missing_list_rather_than_failing(self) -> None:
        self._login(self._make_library_profile())
        with patch.object(web.PublicMetaDBClient, "find_list_by_name", return_value=None):
            response = self.client.post(
                "/api/profile/library/list-preview", json={"list_name": "Never synced"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["missing"])
        self.assertEqual(data["items"], [])

    def test_list_preview_caps_the_posters_but_reports_the_real_total(self) -> None:
        profile = self._make_library_profile()
        web._profile_store.record_sync_success(profile["profile_id"], [], managed_lists=[
            {"list_name": "Big", "list_id": "l1", "display_name": "Big", "source_name": "Trakt", "selection": {}},
        ])
        self._login(profile)
        many = [{"id": f"i{n}", "tmdb_id": n, "media_type": "movie"} for n in range(1, 61)]
        with patch.object(web.PublicMetaDBClient, "get_list_items", return_value=many), \
             patch.object(web.TmdbClient, "get_details_batch") as batch:
            batch.return_value = {}
            response = self.client.post("/api/profile/library/list-preview", json={"list_name": "Big"})
            # Only the shown slice is sent to TMDB, not all 60.
            self.assertEqual(len(batch.call_args[0][0]), web.LIST_PREVIEW_LIMIT)
        data = response.get_json()

        self.assertEqual(data["total"], 60)
        self.assertEqual(data["shown"], web.LIST_PREVIEW_LIMIT)
        self.assertEqual(len(data["items"]), web.LIST_PREVIEW_LIMIT)

    def test_list_preview_requires_a_name(self) -> None:
        self._login(self._make_library_profile())
        response = self.client.post("/api/profile/library/list-preview", json={})
        self.assertEqual(response.status_code, 400)

    def test_library_items_require_a_list_id(self) -> None:
        self._login(self._make_library_profile())
        response = self.client.post("/api/profile/library/items", json={})
        self.assertEqual(response.status_code, 400)

    def test_library_items_report_a_bad_tmdb_key_without_failing(self) -> None:
        self._login(self._make_library_profile())
        with patch.object(web.PublicMetaDBClient, "get_list_items", return_value=[
            {"id": "i1", "tmdb_id": 100, "media_type": "tv"},
        ]), patch.object(web.TmdbClient, "get_details_batch", side_effect=web.TmdbError("TMDB rejected the API key", status_code=401)):
            response = self.client.post("/api/profile/library/items", json={"list_id": "l1"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("rejected", data["tmdb_error"])
        self.assertEqual(data["items"][0]["tmdb_id"], 100)

    def test_library_provider_overview_and_items_use_the_shared_adapter(self) -> None:
        from src.providers import CATEGORY_WATCHLIST, ProviderAdapter

        class FakeTrakt(ProviderAdapter):
            key = "trakt"
            label = "Trakt"
            reads = (CATEGORY_WATCHLIST,)

            def list_sources(self):
                return [{
                    "key": "list:me/favorites", "label": "Favorites",
                    "category": CATEGORY_WATCHLIST, "kind": "list",
                }]

            def fetch(self, category, source_lists=None):
                self.last_fetch = (category, source_lists)
                return [{
                    "title": "The Matrix", "year": 1999, "media_type": "movie",
                    "tmdb_id": "603", "ids": {"tmdb": "603"},
                }]

        self._login(self._make_library_profile(tmdb_key=""))
        adapter = FakeTrakt()
        with patch.object(web, "_build_provider_adapters", return_value={"trakt": adapter}):
            overview = self.client.post(
                "/api/profile/library/provider/overview", json={"provider": "trakt"},
            )
            items = self.client.post(
                "/api/profile/library/provider/items",
                json={"provider": "trakt", "source_key": "list:me/favorites"},
            )

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.get_json()["lists"][0]["name"], "Favorites")
        self.assertEqual(items.status_code, 200)
        self.assertEqual(items.get_json()["items"][0]["tmdb_id"], "603")
        self.assertEqual(adapter.last_fetch, (CATEGORY_WATCHLIST, ["list:me/favorites"]))

    def test_library_provider_requires_a_configured_connection(self) -> None:
        self._login(self._make_library_profile(tmdb_key=""))

        response = self.client.post(
            "/api/profile/library/provider/overview", json={"provider": "trakt"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Connect Trakt", response.get_json()["error"])

    def test_library_history_groups_plays_per_title(self) -> None:
        self._login(self._make_library_profile(tmdb_key=""))
        with patch.object(web.PublicMetaDBClient, "get_watched_history", return_value=[
            {"id": "w1", "tmdb_id": 1, "media_type": "tv", "season": 1, "episode": 1, "watched_at": "2026-01-01T10:00:00Z"},
            {"id": "w2", "tmdb_id": 1, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-01-02T10:00:00Z"},
            {"id": "w3", "tmdb_id": 1, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-01-03T10:00:00Z"},
            {"id": "w4", "tmdb_id": 2, "media_type": "movie", "watched_at": "2026-03-01T10:00:00Z"},
        ]):
            response = self.client.post("/api/profile/library/history", json={})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        # One row per title, not one per play, newest activity first.
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["total_plays"], 4)
        self.assertEqual(data["items"][0]["tmdb_id"], 2)
        show = data["items"][1]
        self.assertEqual(show["play_count"], 3)
        self.assertEqual(show["episodes_watched"], 2)
        self.assertEqual(show["last_watched_at"], "2026-01-03T10:00:00Z")

    def test_library_history_title_returns_episode_breakdown(self) -> None:
        self._login(self._make_library_profile())
        with patch.object(web.PublicMetaDBClient, "get_watched_history", return_value=[
            {"id": "w1", "tmdb_id": 1, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-01-02T10:00:00Z"},
            {"id": "w2", "tmdb_id": 1, "media_type": "tv", "season": 1, "episode": 1, "watched_at": "2026-01-01T10:00:00Z"},
            {"id": "w3", "tmdb_id": 1, "media_type": "tv", "season": 1, "episode": 1, "watched_at": "2026-01-05T10:00:00Z"},
            {"id": "w4", "tmdb_id": 9, "media_type": "movie", "watched_at": "2026-03-01T10:00:00Z"},
        ]), patch.object(web.TmdbClient, "get_details", return_value={
            "title": "FROM", "year": "2022", "poster_url": "https://img/from.jpg",
            "tmdb_id": 1, "media_type": "tv", "overview": "",
        }), patch.object(web.TmdbClient, "get_season_episodes", return_value={
            1: {"name": "Long Day's Journey Into Night", "still_url": "https://img/e1.jpg", "air_date": "2022-02-20"},
            2: {"name": "The Way Things Are Now", "still_url": "https://img/e2.jpg", "air_date": "2022-02-20"},
        }):
            response = self.client.post("/api/profile/library/history/title", json={"tmdb_id": 1, "media_type": "tv"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["title"], "FROM")
        # Repeat plays collapse into one row per episode, ordered by S/E.
        self.assertEqual(len(data["episodes"]), 2)
        first = data["episodes"][0]
        self.assertEqual((first["season"], first["episode"]), (1, 1))
        self.assertEqual(first["plays"], 2)
        self.assertEqual(first["watched_at"], "2026-01-05T10:00:00Z")
        self.assertEqual(first["name"], "Long Day's Journey Into Night")
        self.assertEqual(first["still_url"], "https://img/e1.jpg")

    def test_library_history_title_movie_returns_plays(self) -> None:
        self._login(self._make_library_profile(tmdb_key=""))
        with patch.object(web.PublicMetaDBClient, "get_watched_history", return_value=[
            {"id": "w1", "tmdb_id": 9, "media_type": "movie", "watched_at": "2026-01-01T10:00:00Z"},
            {"id": "w2", "tmdb_id": 9, "media_type": "movie", "watched_at": "2026-02-01T10:00:00Z"},
        ]):
            response = self.client.post("/api/profile/library/history/title", json={"tmdb_id": 9, "media_type": "movie"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["plays"], ["2026-02-01T10:00:00Z", "2026-01-01T10:00:00Z"])
        self.assertEqual(data["episodes"], [])

    def test_library_history_title_requires_tmdb_id(self) -> None:
        self._login(self._make_library_profile(tmdb_key=""))
        response = self.client.post("/api/profile/library/history/title", json={"media_type": "tv"})
        self.assertEqual(response.status_code, 400)

    def test_library_requires_a_pmdb_key(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {"client_id": "", "client_secret": "", "access_token": "", "selected_statuses": {"shows": [], "movies": [], "anime": []}},
            "anilist": {"username": "", "access_token": "", "selected_statuses": []},
            "trakt": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "", "sync_watchlist": False, "sync_liked_lists": False, "selected_lists": []},
            "mdblist": {"api_key": "", "selected_lists": []},
            "pmdb": {"api_key": ""},
        }, {"auto_sync": False, "media_types": ["shows"]})
        self._login(profile)
        response = self.client.post("/api/profile/library/overview", json={})
        self.assertEqual(response.status_code, 409)

    def test_index_contains_library_and_live_activity_ui(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-nav="library"', html)
        self.assertIn('id="view-library"', html)
        self.assertIn('id="library-chips"', html)
        self.assertIn('id="library-tmdb-notice"', html)
        for provider in ("pmdb", "simkl", "trakt", "anilist", "mdblist"):
            self.assertIn(f'data-lib-mode="{provider}"', html)
        self.assertIn('id="tmdb-api-key"', html)
        self.assertIn('id="dot-tmdb"', html)
        self.assertIn('id="live-activity-panel"', html)
        self.assertIn("Live Sync Activity", html)

    def test_index_retires_legacy_pipeline_in_favour_of_pairs(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        # Compatibility inputs remain in the DOM for old profile payloads, but
        # the PMDB-first pipeline is no longer a user-facing settings surface.
        self.assertIn('id="sync-settings" class="hidden" style="display:none!important"', html)
        for key in ("simkl", "anilist", "trakt", "mdblist", "schedule", "activity"):
            self.assertIn(f'id="pipe-{key}"', html)
        self.assertIn("Sync All Pairs", html)
        self.assertLess(html.index(">Sync Setup<"), html.index('id="sync-settings"'),
                        "the destination-first setup must remain the primary sync UI")
        self.assertIn('id="multi-sync-target"', html)
        self.assertIn('id="multi-sync-sources"', html)
        self.assertIn('id="btn-multi-sync-add"', html)
        self.assertIn("Send several services to one place", html)
        self.assertNotIn('id="stab-lists"', html)
        self.assertNotIn('id="stab-behavior"', html)
        self.assertNotIn('id="snav-lists"', html)
        self.assertNotIn('id="snav-behavior"', html)
        # The moved inputs must exist exactly once — duplicated ids would make
        # the save logic read whichever copy came first.
        for element_id in ('id="opt-auto-sync"', 'id="opt-simkl-visibility"', 'id="simkl-status-groups"',
                           'id="trakt-catalog-list"', 'id="mdblist-list"', 'id="opt-activity-history-source"'):
            self.assertEqual(html.count(element_id), 1, element_id)
        self.assertIn('id="pipelines-panel"', html)
        self.assertIn('id="pipelines-panel" style="display:none!important"', html)
        self.assertLess(html.index('id="pairs-dash-panel"'), html.index('id="pipelines-panel"'),
                        "the dashboard must lead with pairs too")


if __name__ == "__main__":
    unittest.main()
