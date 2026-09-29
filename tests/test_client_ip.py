"""Regression: visitors behind a proxy must not share one throttle bucket.

_get_client_ip keyed on the connecting peer. Behind a reverse proxy every
request arrives from the same private address, so on Render -- where the Docker
image passes --proxy-headers but the native Python runtime does not -- all
visitors collapsed into a single bucket and the site behaved as though it were
restricted to one IP.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402

# NOTE: 198.51.100.0/24 (TEST-NET-2) and 203.0.113.0/24 (TEST-NET-3) are
# reported as is_private=True by Python's ipaddress module, so they cannot
# stand in for genuinely public clients here. These are real global addresses.


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, peer=None, xff=None):
        self.client = FakeClient(peer) if peer else None
        self.headers = {}
        if xff:
            self.headers["x-forwarded-for"] = xff


class ClientIpTests(unittest.TestCase):
    def test_direct_public_peer_wins(self):
        self.assertEqual(
            main._get_client_ip(FakeRequest(peer="93.184.216.34")),
            "93.184.216.34",
        )

    def test_private_peer_falls_back_to_header(self):
        """The Render native-runtime case: peer is the router, header is real."""
        req = FakeRequest(peer="10.0.0.1", xff="93.184.216.34, 10.0.0.1")
        self.assertEqual(main._get_client_ip(req), "93.184.216.34")

    def test_two_different_clients_get_different_ips(self):
        a = FakeRequest(peer="10.0.0.1", xff="93.184.216.34, 10.0.0.1")
        b = FakeRequest(peer="10.0.0.1", xff="8.8.4.4, 10.0.0.1")
        self.assertNotEqual(main._get_client_ip(a), main._get_client_ip(b))

    def test_spoofed_prefix_is_discarded(self):
        """A caller prepending a fake IP must not be believed over the real one.

        Proxies append, so the rightmost public hop is the edge's observation
        and anything prepended by the caller is discarded.
        """
        req = FakeRequest(peer="10.0.0.1", xff="9.9.9.9, 93.184.216.34, 10.0.0.1")
        self.assertEqual(main._get_client_ip(req), "93.184.216.34")

    def test_loopback_peer_uses_header(self):
        req = FakeRequest(peer="127.0.0.1", xff="8.8.4.4")
        self.assertEqual(main._get_client_ip(req), "8.8.4.4")

    def test_no_header_falls_back_to_peer(self):
        self.assertEqual(
            main._get_client_ip(FakeRequest(peer="10.0.0.1")), "10.0.0.1"
        )

    def test_missing_client_and_header(self):
        self.assertEqual(main._get_client_ip(FakeRequest()), "unknown")

    def test_uvicorn_rewritten_peer_is_authoritative(self):
        """When --proxy-headers already resolved the peer, do not second-guess it."""
        req = FakeRequest(peer="93.184.216.34", xff="1.1.1.1")
        self.assertEqual(main._get_client_ip(req), "93.184.216.34")

    def test_trusted_proxy_hops_overrides(self):
        saved = main.TRUSTED_PROXY_HOPS
        try:
            main.TRUSTED_PROXY_HOPS = 1
            req = FakeRequest(peer="10.0.0.1", xff="93.184.216.34, 8.8.4.4")
            self.assertEqual(main._get_client_ip(req), "8.8.4.4")
        finally:
            main.TRUSTED_PROXY_HOPS = saved

    def test_internal_addr_helper(self):
        for internal in ("10.0.0.1", "192.168.1.1", "127.0.0.1", "169.254.1.1"):
            self.assertTrue(main._is_internal_addr(internal), internal)
        for public in ("93.184.216.34", "8.8.8.8", "2606:4700::1111"):
            self.assertFalse(main._is_internal_addr(public), public)
        self.assertFalse(main._is_internal_addr("testclient"))


class ThrottleIsolationTests(unittest.TestCase):
    def test_distinct_clients_are_not_throttled_together(self):
        table: dict[str, float] = {}
        a = main._get_client_ip(FakeRequest(peer="10.0.0.1", xff="93.184.216.34, 10.0.0.1"))
        b = main._get_client_ip(FakeRequest(peer="10.0.0.1", xff="8.8.4.4, 10.0.0.1"))
        self.assertNotEqual(a, b)
        main._throttle(a, table, delay_seconds=5)
        # B must not be rejected because A just used the server.
        try:
            main._throttle(b, table, delay_seconds=5)
        except HTTPException as exc:  # pragma: no cover - failure path
            self.fail("distinct client was throttled: %s" % exc)
        # A must still be rejected on an immediate repeat.
        with self.assertRaises(HTTPException):
            main._throttle(a, table, delay_seconds=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
