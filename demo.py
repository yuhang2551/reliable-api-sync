"""Run a real loopback HTTP integration test; never contacts an external service."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from api_sync import JsonClient, Store, fetch_products


def run_demo(output):
    output.mkdir(parents=True, exist_ok=False)
    state = {"revision": 1, "rate_limit_sent": False, "ack_lost": False, "post_attempts": 0, "received": {}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, code, payload):
            raw = json.dumps(payload).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if code in (429, 503): self.send_header("Retry-After", "0")
            self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            if urlsplit(self.path).path != "/products": return self.reply(404, {})
            if not state["rate_limit_sent"]:
                state["rate_limit_sent"] = True; return self.reply(429, {"error": "fixture rate limit"})
            offset = int(parse_qs(urlsplit(self.path).query).get("cursor", ["0"])[0])
            products = [{"id": f"DEMO-{i:02d}", "revision": state["revision"] if i <= 3 else 1,
                         "price": str(100 + i - (5 if i <= 3 and state["revision"] == 2 else 0)), "currency": "USD"} for i in range(1, 13)]
            return self.reply(200, {"items": products[offset:offset+5], "next_cursor": str(offset+5) if offset+5 < len(products) else None})
        def do_POST(self):
            if self.path != "/events": return self.reply(404, {})
            state["post_attempts"] += 1
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            key = self.headers.get("Idempotency-Key")
            if key != body.get("event_id"): return self.reply(400, {})
            state["received"].setdefault(key, body)
            if not state["ack_lost"]:
                state["ack_lost"] = True
                return self.reply(503, {"error": "fixture: processing happened but acknowledgement failed"})
            self.reply(200, {"accepted": True, "event_id": key})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    client = JsonClient()
    store = None
    try:
        store = Store(output / "state.sqlite")
        baseline = store.sync(fetch_products(client, base)); store.close(); store = None
        state["revision"] = 2
        store = Store(output / "state.sqlite")
        changed = store.sync(fetch_products(client, base))
        replay = store.sync(fetch_products(client, base))
        delivered = store.deliver(client, base + "/events")
        store.close(); store = Store(output / "state.sqlite")
        delivery_replay = store.deliver(client, base + "/events")
        result = {"environment": "loopback HTTP fixture, synthetic products", "baseline": baseline,
                  "changed": changed, "replay": replay, "delivered": delivered,
                  "delivery_replay": delivery_replay, "pending_after_restart": len(store.pending()),
                  "receiver_post_attempts": state["post_attempts"], "unique_receiver_events": len(state["received"]),
                  "http_trace": client.trace, "events": list(state["received"].values()),
                  "limit": "Receiver must durably deduplicate Idempotency-Key in production. This demo receiver is in-memory."}
        assert baseline["new"] == 12 and changed["events_created"] == 3
        assert replay["events_created"] == delivery_replay == result["pending_after_restart"] == 0
        assert delivered == result["unique_receiver_events"] == 3 and state["post_attempts"] == 4
        (output / "evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    finally:
        if store: store.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("--output", type=Path, default=Path("demo-output"))
    r = run_demo(p.parse_args().output)
    print(json.dumps({k: r[k] for k in ("delivered", "delivery_replay", "unique_receiver_events", "receiver_post_attempts")}))
