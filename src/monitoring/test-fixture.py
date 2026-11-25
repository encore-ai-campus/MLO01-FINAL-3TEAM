"""Serve synthetic model and reply APIs, and stage test inquiries for the real collector."""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


def fixture_id(name):
    return uuid5(NAMESPACE_URL, "https://voc-test.invalid/" + name)


DOMAIN_ID = fixture_id("domain")
COMPANY_ID = fixture_id("company")
STAFF_ID = fixture_id("staff/agent")
CATEGORY_ID = fixture_id("category/delivery")
CHANNELS = {
    "gmail": ("support@voc-test.invalid", ""),
    "discord": ("990000000000000001", "990000000000000002"),
}
SCENARIOS = (
    ("KEEP", "gmail", "stable"),
    ("CHANGE", "discord", "canary"),
    ("RETURN", "gmail", "stable"),
    ("FAIL", "discord", "canary"),
)
FAILURE_MARKER = "CATEGORYFAIL"


class FixtureHandler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.respond(HTTPStatus.OK, {"status": "ready"})
        else:
            self.respond(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})

    def do_POST(self):
        route = self.path.removeprefix("/")
        if route not in ("stable", "canary", "embedding", "reply"):
            self.respond(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 1024 * 1024:
                raise ValueError
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError
            if route == "reply":
                result = self.confirm_reply(body)
            elif route == "embedding":
                result = {"embedding": [0.25, 0.5, 0.75], "model_name": "voc-test-embedding",
                          "model_version": "test-v1", "model_ms": 1}
            else:
                result = {"sentiment": "중립", "model_name": "voc-test-classifier",
                          "model_version": f"test-{route}-v1", "rollout": route, "model_ms": 1}
                if not any(FAILURE_MARKER in str(body.get(field, "")) for field in ("title", "text")):
                    result["category_id"] = str(CATEGORY_ID)
        except (TypeError, ValueError):
            self.respond(HTTPStatus.BAD_REQUEST, {"error": "invalid fixture request"})
            return
        self.respond(HTTPStatus.OK, result)

    def confirm_reply(self, body):
        channel = body.get("channel")
        if channel not in CHANNELS or body.get("connection_id") != CHANNELS[channel][0]:
            raise ValueError("only synthetic sources receive simulated reply confirmation")
        reply_id = UUID(self.headers.get("Idempotency-Key", ""))
        return {"delivery_confirmed": True, "provider_delivery_id": f"voc-test-{reply_id}"}


def seed_reference_data(cn, company, password_hash):
    with cn.transaction():
        cn.execute("INSERT INTO public.domain(domain_uuid,name) VALUES (%s,'VoC 테스트') ON CONFLICT DO NOTHING",
                   (DOMAIN_ID,))
        cn.execute("""
            INSERT INTO public.company(company_uuid,email_domain,name,domain_uuid)
            VALUES (%s,'voc-test.invalid','VoC 테스트',%s) ON CONFLICT DO NOTHING
        """, (COMPANY_ID, DOMAIN_ID))
        cn.execute("""
            INSERT INTO public.staff_user(staff_user_uuid,company_uuid,email,password_hash,name)
            VALUES (%s,%s,'agent@voc-test.invalid',%s,'테스트 상담사')
            ON CONFLICT (staff_user_uuid) DO UPDATE SET password_hash=excluded.password_hash
        """, (STAFF_ID, COMPANY_ID, password_hash))
        cn.execute("""
            INSERT INTO public.staff_permission(staff_user_uuid,permission_uuid)
            SELECT %s,permission_uuid FROM public.permission WHERE code IN ('ticket.read','ticket.handle')
            ON CONFLICT DO NOTHING
        """, (STAFF_ID,))
        cn.execute("""
            INSERT INTO public.category(category_uuid,domain_uuid,name)
            VALUES (%s,%s,'배송 문의'),(%s,%s,'결제 문의') ON CONFLICT DO NOTHING
        """, (CATEGORY_ID, DOMAIN_ID, fixture_id("category/payment"), DOMAIN_ID))
        for channel, (connection, source) in CHANNELS.items():
            channel_id = fixture_id(f"channel/{channel}")
            cn.execute("""
                INSERT INTO public.channel(channel_uuid,company_uuid,name,type) VALUES (%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (channel_id, COMPANY_ID, channel, channel))
            cn.execute("""
                INSERT INTO voc.ticket_source_connection(company_id,channel,connection_id,source_channel_id,
                                                         company_uuid,channel_uuid)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (company, channel, connection, source, COMPANY_ID, channel_id))


def inquiry(channel, scenario, title, occurred):
    from collector.events import make_event

    connection, source = CHANNELS[channel]
    message_id = uuid4().hex
    body = title + "\n배송 주소를 변경하려면 어떻게 해야 하나요?"
    if scenario == "FAIL":
        body += "\n" + FAILURE_MARKER
    if channel == "gmail":
        raw = {
            "id": message_id,
            "threadId": message_id,
            "labelIds": ["VOC", "INBOX"],
            "internalDate": str(int(occurred.timestamp() * 1000)),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Test Customer <customer@voc-test.invalid>"},
                    {"name": "To", "value": connection},
                    {"name": "Subject", "value": title},
                ],
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            },
        }
        return make_event(channel, connection, message_id, "gmail_api", raw, label="VOC")
    # Exercise Flink's offset conversion using a provider timestamp for the same UTC instant.
    provider_time = occurred.astimezone(timezone(timedelta(hours=9))) if scenario == "CHANGE" else occurred
    message = {
        "id": message_id,
        "guild_id": connection,
        "channel_id": source,
        "type": 0,
        "content": body,
        "author": {"id": "990000000000000003", "username": "voc-test-customer"},
        "timestamp": provider_time.isoformat(),
    }
    return make_event(channel, connection, message_id, "discord_gateway",
                      {"op": 0, "t": "MESSAGE_CREATE", "d": message},
                      channel_id=source, is_thread=False, channel_name="voc-test")


def confirm_inquiries(cn, company, event_ids):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        confirmed = cn.execute("""
            WITH batch AS (
                SELECT o.event_id FROM voc.standardize_outbox o
                JOIN voc.bronze_messages b USING(event_id)
                WHERE b.company_id=%s AND o.target_schema_version=4 AND o.event_id=ANY(%s)
            )
            UPDATE voc.standardize_outbox o SET verified_message_kind='customer_inquiry'
            WHERE o.target_schema_version=4 AND o.event_id IN (SELECT event_id FROM batch)
              AND (SELECT count(*) FROM batch)=%s
        """, (company, event_ids, len(event_ids))).rowcount
        if confirmed == len(event_ids):
            return
        time.sleep(0.5)
    raise SystemExit("Timed out waiting for the staged inquiries in the Bronze outbox.")


def seed():
    from collector.events import COMPANY, ROOT
    from collector.inference import endpoint, initial_route
    from collector.pipeline import db
    from collector.spool import CaptureStore

    password_hash = sys.stdin.readline().strip()
    if not password_hash.startswith("$argon2id$"):
        raise SystemExit("Seed requires an Argon2id password hash on stdin.")
    batch = uuid4().hex[:16]
    occurred = datetime.now(timezone.utc)
    canary_percent = int(os.environ.get("VOC_CANARY_PERCENT", "0"))
    split = (endpoint("classification", "canary") and not endpoint("classification", "router")
             and 0 < canary_percent < 100)
    with db() as cn:
        cn.execute("SET TIME ZONE 'UTC'")
        seed_reference_data(cn, COMPANY, password_hash)
        store = CaptureStore(ROOT)
        tickets = []
        for scenario, channel, preferred_route in SCENARIOS:
            title = f"VOC TEST {batch} {scenario}"
            event = inquiry(channel, scenario, title, occurred)
            while split and initial_route(event["event_id"]) != preferred_route:
                event = inquiry(channel, scenario, title, occurred)
            if not store.stage(event):
                raise SystemExit(f"Test inquiry already staged: {event['event_id']}")
            tickets.append({"title": title, "channel": channel, "event_id": event["event_id"],
                            "initial_route": initial_route(event["event_id"])})
        print(json.dumps({"batch": batch, "created_at": occurred.isoformat(), "tickets": tickets}), flush=True)
        confirm_inquiries(cn, COMPANY, [UUID(ticket["event_id"]) for ticket in tickets])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "seed"))
    if parser.parse_args().mode == "seed":
        seed()
    else:
        with ThreadingHTTPServer(("0.0.0.0", 8080), FixtureHandler) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
