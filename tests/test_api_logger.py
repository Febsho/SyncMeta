import unittest

from src import api_logger


class ApiLoggerTests(unittest.TestCase):
    def test_request_diagnostics_redact_secret_query_values(self) -> None:
        api_logger.record(
            "mdblist", "GET",
            "https://api.mdblist.com/watchlist/items?limit=100&apikey=secret&offset=0",
            405, 12.0,
        )

        url = api_logger.snapshot(1)[0]["url"]

        self.assertNotIn("secret", url)
        self.assertIn("apikey=%5Bredacted%5D", url)
        self.assertIn("offset=0", url)


if __name__ == "__main__":
    unittest.main()
