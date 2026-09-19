import os
import unittest
from unittest.mock import patch

from src.oauth_credentials import get_oauth_app_credentials, hosted_oauth_status


class OAuthCredentialResolutionTests(unittest.TestCase):
    def test_hosted_credentials_win_without_mutating_profile_tokens(self):
        profile = {"trakt": {"client_id": "profile-id", "client_secret": "profile-secret", "access_token": "user-token"}}
        with patch.dict(os.environ, {"TRAKT_CLIENT_ID": "hosted-id", "TRAKT_CLIENT_SECRET": "hosted-secret"}, clear=False):
            resolved = get_oauth_app_credentials("trakt", profile)
        self.assertEqual({"client_id": "hosted-id", "client_secret": "hosted-secret", "hosted": True}, resolved)
        self.assertEqual("user-token", profile["trakt"]["access_token"])
        self.assertEqual("profile-secret", profile["trakt"]["client_secret"])

    def test_profile_credentials_remain_the_fallback(self):
        with patch.dict(os.environ, {"ANILIST_CLIENT_ID": "", "ANILIST_CLIENT_SECRET": ""}, clear=False):
            resolved = get_oauth_app_credentials("anilist", {"anilist": {"client_id": "id", "client_secret": "secret"}})
        self.assertEqual({"client_id": "id", "client_secret": "secret", "hosted": False}, resolved)

    def test_status_only_exposes_booleans(self):
        with patch.dict(os.environ, {"MDBLIST_CLIENT_ID": "id", "MDBLIST_CLIENT_SECRET": "secret"}, clear=False):
            status = hosted_oauth_status()
        self.assertIs(status["mdblist"], True)
        self.assertNotIn("secret", status)
