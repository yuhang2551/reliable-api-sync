import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from api_sync import JsonClient, Store, fetch_products, validate_product, validate_url
from demo import run_demo


def product(**changes):
    return {**{"id": "DEMO-01", "revision": 1, "price": "10.00", "currency": "USD"}, **changes}


class FakeClient:
    def __init__(self, replies): self.replies = iter(replies)
    def request(self, *args, **kwargs): return next(self.replies)


class ApiSyncTests(unittest.TestCase):
    def setUp(self): self.store = Store(":memory:")
    def tearDown(self): self.store.close()
    def test_baseline_does_not_send_events(self):
        self.assertEqual(self.store.sync([product()])["new"], 1); self.assertEqual(self.store.pending(), [])
    def test_change_creates_one_event(self):
        self.store.sync([product()]); self.store.sync([product(revision=2, price="9")])
        self.assertEqual(len(self.store.pending()), 1)
    def test_replay_is_idempotent(self):
        self.store.sync([product()]); self.store.sync([product(revision=2, price="9")])
        self.assertEqual(self.store.sync([product(revision=2, price="9")])["events_created"], 0)
    def test_stale_revision(self):
        self.store.sync([product(revision=2)])
        self.assertEqual(self.store.sync([product()])["stale"], 1)
    def test_reused_revision_conflict(self):
        self.store.sync([product()])
        with self.assertRaises(ValueError): self.store.sync([product(price="11")])
    def test_transaction_rolls_back(self):
        self.store.sync([product(), product(id="DEMO-02")])
        with self.assertRaises(ValueError): self.store.sync([product(revision=2, price="9"), product(id="DEMO-02", price="13")])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.db.execute("SELECT revision FROM products WHERE id='DEMO-01'").fetchone()[0], 1)
    def test_no_price_change_no_event(self):
        self.store.sync([product()]); self.store.sync([product(revision=2)])
        self.assertEqual(self.store.pending(), [])
    def test_bad_schema(self):
        with self.assertRaises(ValueError): validate_product({"id": "x"})
    def test_bad_decimal(self):
        for price in ["NaN", "-1", "1.234", "1e3", 3.5]:
            with self.assertRaises(ValueError): validate_product(product(price=price))
    def test_bad_revision(self):
        for rev in [True, 0, "1"]:
            with self.assertRaises(ValueError): validate_product(product(revision=rev))
    def test_duplicate_products_rejected(self):
        with self.assertRaises(ValueError): self.store.sync([product(), product()])
    def test_url_boundaries(self):
        for url in ["http://example.test", "https://user:secret@example.test", "https://example.test?token=x"]:
            with self.assertRaises(ValueError): validate_url(url)
        self.assertEqual(validate_url("https://example.test/"), "https://example.test")
    def test_pagination(self):
        c = FakeClient([{"items": [product()], "next_cursor": "two"}, {"items": [product(id="DEMO-02")], "next_cursor": None}])
        self.assertEqual(len(fetch_products(c, "https://example.test")), 2)
    def test_cursor_loop(self):
        c = FakeClient([{"items": [], "next_cursor": "same"}] * 2)
        with self.assertRaises(ValueError): fetch_products(c, "https://example.test")
    def test_page_limit(self):
        with self.assertRaises(ValueError): fetch_products(FakeClient([{"items": [], "next_cursor": "more"}]), "https://example.test", 1)
    def test_cross_page_conflict(self):
        c = FakeClient([{"items": [product()], "next_cursor": "two"}, {"items": [product(price="11")], "next_cursor": None}])
        with self.assertRaises(ValueError): fetch_products(c, "https://example.test")
    def test_bad_ack_keeps_pending(self):
        self.store.sync([product()]); self.store.sync([product(revision=2, price="9")])
        with self.assertRaises(ValueError): self.store.deliver(FakeClient([{"accepted": True, "event_id": "wrong"}]), "https://example.test/events")
        self.assertEqual(len(self.store.pending()), 1)
    def test_401_not_retried(self):
        class Opener:
            calls = 0
            def open(self, *a, **k):
                self.calls += 1; raise HTTPError("https://example.test", 401, "no", {}, io.BytesIO(b""))
        opener = Opener()
        with self.assertRaises(RuntimeError): JsonClient(opener=opener, sleep=lambda _: None).request("https://example.test")
        self.assertEqual(opener.calls, 1)
    def test_retry_exhaustion(self):
        class Opener:
            calls = 0
            def open(self, *a, **k):
                self.calls += 1; raise HTTPError("https://example.test", 503, "no", {"Retry-After":"NaN"}, io.BytesIO(b""))
        opener = Opener()
        with self.assertRaises(RuntimeError): JsonClient(opener=opener, sleep=lambda _: None).request("https://example.test")
        self.assertEqual(opener.calls, 3)
    def test_loopback_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_demo(Path(d) / "integration")
            self.assertEqual((r["unique_receiver_events"], r["receiver_post_attempts"], r["delivery_replay"]), (3, 4, 0))
            self.assertIn(429, [t["status"] for t in r["http_trace"]])
            self.assertIn(503, [t["status"] for t in r["http_trace"]])


if __name__ == "__main__": unittest.main()
