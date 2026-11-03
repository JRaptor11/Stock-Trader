import unittest

from utils.http_utils import configure_default_request_timeout


class _FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return kwargs


class _FakeClient:
    def __init__(self):
        self._session = _FakeSession()


class DefaultRequestTimeoutTests(unittest.TestCase):
    def test_applies_default_and_preserves_explicit_timeout(self):
        client = _FakeClient()
        self.assertTrue(
            configure_default_request_timeout(client, (5.0, 20.0))
        )

        default_result = client._session.request("GET", "https://example.test")
        explicit_result = client._session.request(
            "GET",
            "https://example.test",
            timeout=3.0,
        )

        self.assertEqual((5.0, 20.0), default_result["timeout"])
        self.assertEqual(3.0, explicit_result["timeout"])

    def test_reconfiguration_is_idempotent(self):
        client = _FakeClient()
        configure_default_request_timeout(client, (5.0, 20.0))
        configure_default_request_timeout(client, (2.0, 8.0))

        result = client._session.request("GET", "https://example.test")

        self.assertEqual((2.0, 8.0), result["timeout"])
        self.assertEqual(1, len(client._session.calls))

    def test_missing_requests_session_is_rejected(self):
        self.assertFalse(configure_default_request_timeout(object(), 10.0))


if __name__ == "__main__":
    unittest.main()
