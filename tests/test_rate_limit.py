import unittest
from unittest.mock import Mock

import requests

from src.rate_limit import (
    DEFAULT_RATE_LIMIT_WAIT_SECONDS,
    MAX_RATE_LIMIT_WAIT_SECONDS,
    RateLimiter,
    retry_after_seconds,
    retry_on_rate_limit,
)


def _http_error(status: int, headers: dict | None = None) -> requests.HTTPError:
    response = Mock(spec=requests.Response)
    response.status_code = status
    response.headers = headers or {}
    return requests.HTTPError(f"{status}", response=response)


class RetryAfterTests(unittest.TestCase):
    def test_standard_header_is_used(self) -> None:
        self.assertEqual(retry_after_seconds(_http_error(429, {"Retry-After": "7"}).response), 7.0)

    def test_reset_after_header_is_used_when_retry_after_is_absent(self) -> None:
        response = _http_error(429, {"X-RateLimit-Reset-After": "3.5"}).response
        self.assertEqual(retry_after_seconds(response), 3.5)

    def test_absolute_reset_epoch_becomes_a_delay(self) -> None:
        import time
        response = _http_error(429, {"X-RateLimit-Reset": str(time.time() + 5)}).response
        self.assertTrue(3 <= retry_after_seconds(response) <= 6)

    def test_a_reset_in_the_past_does_not_produce_a_negative_wait(self) -> None:
        import time
        response = _http_error(429, {"X-RateLimit-Reset": str(time.time() - 500)}).response
        self.assertEqual(retry_after_seconds(response), DEFAULT_RATE_LIMIT_WAIT_SECONDS)

    def test_an_absurd_wait_is_clamped(self) -> None:
        # A provider asking us to wait an hour must not stall a sync that long.
        response = _http_error(429, {"Retry-After": "99999"}).response
        self.assertEqual(retry_after_seconds(response), MAX_RATE_LIMIT_WAIT_SECONDS)

    def test_garbage_headers_fall_back_to_the_default(self) -> None:
        self.assertEqual(
            retry_after_seconds(_http_error(429, {"Retry-After": "soon"}).response),
            DEFAULT_RATE_LIMIT_WAIT_SECONDS,
        )
        self.assertEqual(retry_after_seconds(None), DEFAULT_RATE_LIMIT_WAIT_SECONDS)


class RetryOnRateLimitTests(unittest.TestCase):
    def test_a_429_is_retried_until_it_succeeds(self) -> None:
        slept: list[float] = []
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(429, {"Retry-After": "1"})
            return "ok"

        result = retry_on_rate_limit(send, provider="Test", sleep=slept.append)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(slept), 2)

    def test_a_500_is_never_retried(self) -> None:
        # The whole point: a write that 5xx'd may already have been applied, and
        # /sync/history is not idempotent, so a retry would duplicate the play.
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            raise _http_error(500)

        with self.assertRaises(requests.HTTPError):
            retry_on_rate_limit(send, provider="Test", sleep=lambda _s: None)
        self.assertEqual(calls["n"], 1, "a 5xx write must not be retried")

    def test_a_timeout_is_never_retried(self) -> None:
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            raise requests.Timeout("read timed out")

        with self.assertRaises(requests.Timeout):
            retry_on_rate_limit(send, provider="Test", sleep=lambda _s: None)
        self.assertEqual(calls["n"], 1)

    def test_it_gives_up_and_raises_the_last_429(self) -> None:
        def send():
            raise _http_error(429, {"Retry-After": "1"})

        with self.assertRaises(requests.HTTPError) as caught:
            retry_on_rate_limit(send, provider="Test", attempts=2, sleep=lambda _s: None)
        self.assertEqual(caught.exception.response.status_code, 429)

    def test_cancellation_is_honoured_between_attempts(self) -> None:
        class Stop(Exception):
            pass

        seen = {"n": 0}

        def check():
            seen["n"] += 1
            if seen["n"] > 1:
                raise Stop()

        def send():
            raise _http_error(429, {"Retry-After": "1"})

        with self.assertRaises(Stop):
            retry_on_rate_limit(
                send, provider="Test", sleep=lambda _s: None, check_cancelled=check,
            )


class RateLimiterTests(unittest.TestCase):
    def test_requests_under_the_ceiling_do_not_wait(self) -> None:
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.wait()
        self.assertEqual(len(limiter._timestamps), 3)

    def test_the_window_slides(self) -> None:
        limiter = RateLimiter(max_requests=2, window_seconds=0.05)
        limiter.wait()
        limiter.wait()
        # Third would block until the window rolls; it should return promptly.
        limiter.wait()
        self.assertLessEqual(len(limiter._timestamps), 2 + 1)


class ClientPolicyTests(unittest.TestCase):
    """Reads may be retried automatically; writes may not."""

    def _adapter_methods(self, client):
        return client._session.get_adapter("https://example.test").max_retries.allowed_methods

    def test_no_client_auto_retries_a_write(self) -> None:
        from src.config import MdbListConfig, SimklConfig, TraktConfig
        from src.mdblist_client import MdbListClient
        from src.simkl_client import SimklClient
        from src.trakt_client import TraktClient

        for client in (
            TraktClient(TraktConfig(client_id="x")),
            SimklClient(SimklConfig(client_id="x")),
            MdbListClient(MdbListConfig(api_key="k")),
        ):
            methods = self._adapter_methods(client)
            self.assertIn("GET", methods)
            self.assertNotIn("POST", methods, f"{type(client).__name__} auto-retries writes")

    def test_every_client_paces_itself(self) -> None:
        from src.config import (
            MdbListConfig, PublicMetaDBConfig, SimklConfig, TraktConfig,
        )
        from src.mdblist_client import MdbListClient
        from src.publicmetadb_client import PublicMetaDBClient
        from src.simkl_client import SimklClient
        from src.trakt_client import TraktClient

        for client in (
            TraktClient(TraktConfig(client_id="x")),
            SimklClient(SimklConfig(client_id="x")),
            MdbListClient(MdbListConfig(api_key="k")),
            PublicMetaDBClient(PublicMetaDBConfig(api_key="k")),
        ):
            self.assertIsInstance(client._limiter, RateLimiter)

    def test_read_timeouts_are_generous_enough_for_a_large_account(self) -> None:
        # 6s was the old value and is what the reported timeout storms hit.
        from src.mdblist_client import REQUEST_TIMEOUT as MDBLIST_TIMEOUT
        from src.simkl_client import REQUEST_TIMEOUT as SIMKL_TIMEOUT
        from src.trakt_client import REQUEST_TIMEOUT as TRAKT_TIMEOUT

        for timeout in (TRAKT_TIMEOUT, SIMKL_TIMEOUT, MDBLIST_TIMEOUT):
            self.assertGreaterEqual(timeout[1], 12)


if __name__ == "__main__":
    unittest.main()
