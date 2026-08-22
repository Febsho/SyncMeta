import unittest
from unittest.mock import Mock, patch

import requests

from src.connection_health import check_connections, check_provider
from src.profile_store import normalize_credentials


def _credentials():
    return normalize_credentials({
        "pmdb": {"api_key": "pm-key"},
        "tmdb": {"api_key": "tmdb-key"},
        "simkl": {"client_id": "simkl-client", "access_token": "simkl-token"},
        "trakt": {"client_id": "trakt-client", "client_secret": "secret", "access_token": "trakt-token",
                  "refresh_token": "refresh", "username": "viewer"},
        "anilist": {"username": "AnimeFan", "access_token": "anilist-token"},
        "mdblist": {"api_key": "mdb-key"},
    })


class ConnectionHealthTests(unittest.TestCase):
    @patch("src.connection_health.MdbListClient.get_user_lists", return_value=[])
    @patch("src.connection_health.TraktClient.get_last_activities", return_value={})
    @patch("src.connection_health.SimklClient.get_activities", return_value={})
    @patch("src.connection_health.TmdbClient.validate_key", return_value=True)
    @patch("src.connection_health.PublicMetaDBClient.get_lists", return_value=[])
    def test_read_only_probes_report_expected_capabilities(self, *_mocks):
        for provider, writable in (("pmdb", True), ("tmdb", False), ("simkl", True),
                                   ("trakt", True), ("mdblist", False)):
            row = check_provider(provider, _credentials())
            self.assertEqual(row["status"], "healthy")
            self.assertTrue(row["capabilities"]["readable"])
            self.assertEqual(row["capabilities"]["writable"], writable)

    @patch("src.connection_health.MdbListClient.get_user_lists", return_value=[])
    def test_mdblist_oauth_only_is_configured_and_writable(self, _lists):
        """A profile that connected through OAuth has no api key at all, and
        used to be reported as 'not configured' — which is what made a working
        MDBList login look broken."""
        credentials = normalize_credentials({
            "mdblist": {"client_id": "cid", "access_token": "mdb-token",
                        "refresh_token": "mdb-refresh"},
        })

        row = check_provider("mdblist", credentials)

        self.assertEqual(row["status"], "healthy")
        self.assertTrue(row["capabilities"]["readable"])
        # OAuth is the mode that carries the write scope.
        self.assertTrue(row["capabilities"]["writable"])

    def test_mdblist_with_neither_credential_is_unconfigured(self) -> None:
        credentials = normalize_credentials({"mdblist": {}})

        row = check_provider("mdblist", credentials)

        self.assertEqual(row["status"], "unconfigured")

    @patch("src.connection_health.requests.post")
    def test_anilist_public_and_authenticated_capabilities(self, post):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.side_effect = [
            {"data": {"User": {"name": "AnimeFan"}}},
            {"data": {"Viewer": {"name": "AnimeFan"}}},
        ]
        post.return_value = response
        public = _credentials()
        public["anilist"]["access_token"] = ""

        read_only = check_provider("anilist", public)
        authenticated = check_provider("anilist", _credentials())

        self.assertEqual(read_only["status"], "healthy")
        self.assertFalse(read_only["capabilities"]["writable"])
        self.assertTrue(authenticated["capabilities"]["writable"])
        self.assertEqual(authenticated["identity"], "AnimeFan")

    @patch("src.connection_health.PublicMetaDBClient.get_lists")
    def test_auth_and_rate_limit_errors_are_normalized(self, get_lists):
        response = Mock(status_code=401, headers={})
        get_lists.side_effect = requests.HTTPError(response=response)
        auth = check_provider("pmdb", _credentials())
        self.assertEqual(auth["code"], "auth_invalid")
        self.assertEqual(auth["recovery_action"], "edit_credentials")

        response.status_code = 429
        response.headers = {"Retry-After": "30"}
        limited = check_provider("pmdb", _credentials())
        self.assertEqual(limited["status"], "degraded")
        self.assertEqual(limited["retry_after"], 30)

    @patch("src.connection_health.check_provider")
    def test_one_provider_failure_does_not_drop_other_results(self, probe):
        def side_effect(provider, *_args, **_kwargs):
            if provider == "simkl":
                raise RuntimeError("boom")
            return {"provider": provider, "status": "healthy", "capabilities": {"readable": True, "writable": False}}
        probe.side_effect = side_effect

        rows = check_connections(_credentials(), ["simkl", "pmdb"])

        self.assertEqual([row["provider"] for row in rows], ["simkl", "pmdb"])
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[1]["status"], "healthy")

    @patch("src.connection_health.TraktClient.get_last_activities", autospec=True)
    def test_trakt_refresh_callback_is_forwarded(self, activities):
        callback = Mock()

        def refresh(client):
            client._token_refreshed_callback("new-access", "new-refresh", "expiry")
            return {}

        activities.side_effect = refresh
        row = check_provider("trakt", _credentials(), trakt_token_callback=callback)

        self.assertEqual(row["status"], "healthy")
        callback.assert_called_once_with("new-access", "new-refresh", "expiry")


if __name__ == "__main__":
    unittest.main()
