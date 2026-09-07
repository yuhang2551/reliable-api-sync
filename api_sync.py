"""Paginated API synchronization with bounded retries and a durable outbox.

Python 3.10+, standard library. No marketplace login, scraping, or model access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RETRYABLE = {429, 500, 502, 503, 504}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url: str) -> str:
    p = urlsplit(url)
    if p.username or p.password or p.query or p.fragment or not p.hostname:
        raise ValueError("Use a credential-free URL without query or fragment")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("HTTPS required except for explicit loopback demos")
    return url.rstrip("/")


def reject_constant(value):
    raise ValueError("Non-finite JSON numbers are not supported")


class JsonClient:
    def __init__(self, token=None, attempts=3, timeout=5, sleep=time.sleep, opener=None):
        if not 1 <= attempts <= 5 or not 0 < timeout <= 60:
            raise ValueError("Attempts must be 1-5; timeout must be in (0, 60]")
        self.token, self.attempts, self.timeout, self.sleep = token, attempts, timeout, sleep
        self.opener = opener or build_opener(NoRedirect())
        self.trace = []

    def request(self, url, body=None, idempotency_key=None):
        method = "GET" if body is None else "POST"
        headers = {"Accept": "application/json", "User-Agent": "ReliableApiSync/1.0"}
        if self.token: headers["Authorization"] = "Bearer " + self.token
        if body is not None: headers["Content-Type"] = "application/json"
        if idempotency_key: headers["Idempotency-Key"] = idempotency_key
        payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        for attempt in range(1, self.attempts + 1):
            delay = min(.1 * 2 ** (attempt - 1), 2)
            try:
                with self.opener.open(Request(url, data=payload, headers=headers, method=method), timeout=self.timeout) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ValueError("Response exceeds 2 MiB")
                    result = json.loads(raw, parse_constant=reject_constant)
                    self.trace.append({"method": method, "attempt": attempt, "status": response.status})
                    return result
            except HTTPError as exc:
                status = exc.code
                self.trace.append({"method": method, "attempt": attempt, "status": status})
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                exc.close()
                if status not in RETRYABLE or attempt == self.attempts:
                    raise RuntimeError(f"HTTP {status}; request stopped") from None
                try:
                    seconds = float(retry_after)
                    if math.isfinite(seconds): delay = min(max(seconds, 0), 30)
                except ValueError: pass
            except (URLError, TimeoutError) as exc:
                self.trace.append({"method": method, "attempt": attempt, "status": "transport_error"})
                if attempt == self.attempts:
                    raise RuntimeError("Transport failed after bounded attempts") from None
            self.sleep(delay)
        raise AssertionError("Unreachable")


def validate_product(item):
    if not isinstance(item, dict) or set(item) != {"id", "revision", "price", "currency"}:
        raise ValueError("Product schema mismatch")
    if not isinstance(item["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"]):
        raise ValueError("Invalid product ID")
    if type(item["revision"]) is not int or item["revision"] < 1:
        raise ValueError("Revision must be a positive integer")
    if not isinstance(item["price"], str) or not re.fullmatch(r"\d{1,10}(\.\d{1,2})?", item["price"]):
        raise ValueError("Price must be a nonnegative decimal string with at most two decimal places")
    if item["currency"] not in {"USD", "GBP", "CNY", "AED"}:
        raise ValueError("Currency is not in the configured demo allowlist")
    return {**item, "price": format(Decimal(item["price"]), ".2f")}


def fetch_products(client, base_url, max_pages=100):
    base = validate_url(base_url)
    if not 1 <= max_pages <= 1000:
        raise ValueError("max_pages must be 1-1000")
    cursor, seen_cursors, products = None, set(), {}
    for page in range(max_pages):
        query = "" if cursor is None else "?" + urlencode({"cursor": cursor})
        reply = client.request(base + "/products" + query)
        if not isinstance(reply, dict) or set(reply) != {"items", "next_cursor"} or not isinstance(reply["items"], list):
            raise ValueError("Pagination response schema mismatch")
        for raw in reply["items"]:
            item = validate_product(raw)
            if item["id"] in products and products[item["id"]] != item:
                raise ValueError("Conflicting product across pages")
            products[item["id"]] = item
            if len(products) > 10000: raise ValueError("Product limit exceeded")
        cursor = reply["next_cursor"]
        if cursor is None: return list(products.values())
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512 or cursor in seen_cursors:
            raise ValueError("Invalid or repeated pagination cursor")
        seen_cursors.add(cursor)
    raise ValueError("Page limit reached before pagination completed")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
        """)
        self.db.commit()
    def close(self): self.db.close()
    def sync(self, products):
        checked = [validate_product(p) for p in products]
        if len({p["id"] for p in checked}) != len(checked): raise ValueError("Duplicate product IDs")
        counts = {"new": 0, "updated": 0, "unchanged": 0, "stale": 0, "events_created": 0}
        with self.db:
            for p in checked:
                old = self.db.execute("SELECT revision,price,currency FROM products WHERE id=?", (p["id"],)).fetchone()
                if old:
                    if p["revision"] < old[0]:
                        counts["stale"] += 1; continue
                    if p["revision"] == old[0]:
                        if (p["price"], p["currency"]) != old[1:]: raise ValueError("Revision reused with conflicting data")
                        counts["unchanged"] += 1; continue
                    counts["updated"] += 1
                    if (p["price"], p["currency"]) != old[1:]:
                        event_id = hashlib.sha256((p["id"] + ":" + str(p["revision"])).encode()).hexdigest()
                        event = {"event_id": event_id, "product_id": p["id"], "revision": p["revision"],
                                 "previous": {"price": old[1], "currency": old[2]},
                                 "current": {"price": p["price"], "currency": p["currency"]}}
                        self.db.execute("INSERT INTO outbox(event_id,payload) VALUES(?,?)", (event_id, json.dumps(event, sort_keys=True)))
                        counts["events_created"] += 1
                else: counts["new"] += 1
                self.db.execute("INSERT INTO products VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,price=excluded.price,currency=excluded.currency",
                                (p["id"], p["revision"], p["price"], p["currency"]))
        return counts
    def pending(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM outbox WHERE delivered=0 ORDER BY event_id")]
    def deliver(self, client, destination):
        destination = validate_url(destination)
        count = 0
        for event in self.pending():
            reply = client.request(destination, body=event, idempotency_key=event["event_id"])
            if not isinstance(reply, dict) or reply.get("event_id") != event["event_id"] or reply.get("accepted") is not True:
                raise ValueError("Receiver did not acknowledge the matching event ID")
            with self.db:
                self.db.execute("UPDATE outbox SET delivered=1 WHERE event_id=?", (event["event_id"],))
            count += 1
        return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--db", type=Path, default=Path("state.sqlite"))
    parser.add_argument("--token-env", help="Name of an existing environment variable; token is never logged")
    parser.add_argument("--send-to", help="Explicitly enable delivery to this authorized receiver URL")
    args = parser.parse_args()
    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not token: parser.error("The named token environment variable is empty")
    store = None
    try:
        validate_url(args.base_url)
        if args.send_to: validate_url(args.send_to)
        client = JsonClient(token=token)
        products = fetch_products(client, args.base_url)
        store = Store(args.db)
        result = store.sync(products)
        # Never forward the source API token to a different receiver.
        result["delivered"] = store.deliver(JsonClient(), args.send_to) if args.send_to else 0
        result["pending"] = len(store.pending())
        print(json.dumps(result))
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"API sync stopped: {exc}\n")
    finally:
        if store: store.close()


if __name__ == "__main__":
    main()
