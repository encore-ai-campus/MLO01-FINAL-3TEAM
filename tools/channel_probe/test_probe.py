"""Offline provider-shape tests. These never connect to Discord or Gmail."""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.channel_probe import providers
from tools.channel_probe.artifacts import read_json, save_inbound, verify, write_json
from tools.channel_probe.normalize import ProbeError, SAMPLE_TEXT, is_discord_test, mail_text

RUN = "VOC-unit"
CHANNEL = "2002"


def part(text, mime="text/plain"):
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")}}


def gmail():
    return {
        "id": "mail-1", "threadId": "conversation-1", "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1704067200000", "snippet": "short preview",
        "payload": {"mimeType": "multipart/mixed", "headers": [
            {"name": "subject", "value": f"[{RUN}]"}, {"name": "From", "value": "Test <sender@example.invalid>"}],
            "parts": [{"mimeType": "multipart/alternative", "parts": [
                part("<p>HTML alternative</p>", "text/html"), part(SAMPLE_TEXT)]},
                {**part("private attachment"), "filename": "attachment.txt"}]},
    }


def gateway():
    return {"op": 0, "t": "MESSAGE_CREATE", "s": 10, "d": {
        "id": "1001", "channel_id": CHANNEL, "guild_id": "3003", "type": 0,
        "author": {"id": "4004", "bot": False}, "content": f"[{RUN}] {SAMPLE_TEXT}",
        "timestamp": "2024-01-01T00:00:00+00:00",
    }}


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def discord(self, raw=None, thread=False):
        return save_inbound(self.base, RUN, "discord", raw or gateway(), "3003",
                            channel_id=CHANNEL, is_thread=thread)

    def mail(self, raw=None):
        return save_inbound(self.base, RUN, "gmail", raw or gmail(), "test@example.invalid")

    def test_offline_pair(self):
        a, b = self.discord(), self.mail()
        self.assertEqual(read_json(a / "raw.json"), gateway())
        self.assertEqual(read_json(b / "raw.json"), gmail())
        requests = verify(self.base, RUN, SAMPLE_TEXT)
        self.assertEqual(set(requests["gmail"]), {"schema_version","ticket_id","body_version","analysis_text"})
        self.assertNotEqual(requests["gmail"]["ticket_id"], requests["discord"]["ticket_id"])
        self.assertEqual(set(read_json(a / "standard.json")), set(read_json(b / "standard.json")))
        mail = read_json(b / "standard.json")
        self.assertEqual(mail["source_conversation_id"], "conversation-1")
        self.assertEqual(mail["occurred_at"], "2024-01-01T00:00:00+00:00")
        self.assertEqual(mail["sender_ref"], "sender@example.invalid")

    def test_gateway_filter(self):
        for field in ({"channel_id":"999"}, {"author":{"id":"bot","bot":True}},
                      {"webhook_id":"55"}, {"content":f"prefix [{RUN}] {SAMPLE_TEXT}"}):
            raw = gateway()
            raw["d"].update(field)
            self.assertFalse(is_discord_test(raw,RUN,CHANNEL))
        self.assertFalse(is_discord_test({"t":"MESSAGE_UPDATE","d":gateway()["d"]},RUN,CHANNEL))

    def test_thread_and_unresolved_reply(self):
        raw = gateway()
        raw["d"]["type"] = 19
        with self.assertRaises(ProbeError):
            self.discord(raw)
        self.assertEqual(len(list((self.base / "runs" / RUN).glob("review-discord-*/raw.json"))), 1)
        folder = self.discord(raw, True)
        self.assertEqual(read_json(folder / "standard.json")["source_conversation_id"], CHANNEL)

    def test_html_only_and_equal_subject(self):
        raw = gmail()
        raw["payload"]["headers"][0]["value"] += " " + SAMPLE_TEXT
        raw["payload"]["parts"] = [part(f"<head>title</head><p>{SAMPLE_TEXT}</p><style>other</style>", "text/html")]
        output = self.mail(raw)
        self.assertEqual(read_json(output / "classifier-input.json")["analysis_text"], SAMPLE_TEXT)

    def test_empty_or_unusable_body_keeps_raw_for_review(self):
        for bad_body in ({"data": "@@@"}, {"size":0}, {"attachmentId":"external-text"}):
            raw = gmail()
            raw["payload"]["parts"] = [{"mimeType":"text/plain","body":bad_body}]
            with self.assertRaises(ProbeError):
                self.mail(raw)
        self.assertFalse((self.base / "runs" / RUN / "gmail").exists())
        self.assertEqual(len(list((self.base / "runs" / RUN).glob("review-gmail-*/raw.json"))), 3)
        self.assertEqual(mail_text(part("안녕"))[1], "안녕")

    def test_repeated_capture_preserves_first_and_conflicts_retain_original(self):
        folder = self.mail()
        original = read_json(folder / "capture.json")
        self.mail()
        self.assertEqual(read_json(folder / "capture.json"), original)
        raw = gmail()
        raw["id"] = "another-mail"
        with self.assertRaisesRegex(ProbeError, "다른 메시지"):
            self.mail(raw)
        self.assertEqual(read_json(folder / "raw.json")["id"], "mail-1")

    def test_webhook_alone_and_damaged_output_cannot_pass(self):
        self.mail()
        test_dir = self.base / "runs" / RUN / "discord-webhook"
        write_json(test_dir / "raw.json", gateway()["d"])
        with self.assertRaisesRegex(ProbeError, "discord 수신 기록"):
            verify(self.base, RUN)
        folder = self.discord()
        data = read_json(folder / "classifier-input.json")
        data["body_version"] = True
        write_json(folder / "classifier-input.json", data)
        with self.assertRaises(ProbeError):
            verify(self.base, RUN)

    def test_gmail_authorization_timeout_is_retryable(self):
        from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

        write_json(self.base / "credentials.json", {"installed": {}})
        with patch.object(InstalledAppFlow, "from_client_secrets_file") as flow_factory:
            flow_factory.return_value.run_local_server.side_effect = WSGITimeoutError()
            with self.assertRaisesRegex(ProbeError, "같은 RUN"):
                providers.gmail_credentials(self.base)
        self.assertFalse((self.base / "token.json").exists())

    def test_only_exact_gmail_subject_gets_full_body(self):
        matching = gmail()
        other = {"payload":{"headers":[{"name":"Subject","value":"other "+RUN}]}}
        plan = [
            {"messages":[{"id":"other"}],"nextPageToken":"next"}, other,
            {"messages":[{"id":"mail-1"}]}, matching, matching,
        ]
        with patch.object(providers, "gmail_get", side_effect=plan) as calls:
            self.assertEqual(providers.find_gmail(object(), RUN), matching)
            self.assertEqual([c.args[1] for c in calls.call_args_list if c.kwargs.get("format")=="full"], ["messages/mail-1"])
            self.assertEqual(calls.call_args_list[2].kwargs["pageToken"], "next")

    def test_webhook_wait_and_no_duplicate_post(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps({"id":"1","channel_id":CHANNEL,"content":f"[{RUN}] {SAMPLE_TEXT}"}).encode()
        with patch.object(providers,"secret",return_value="https://discord.com/api/webhooks/100/example-placeholder?thread_id=12"), \
             patch.object(providers,"HTTPSConnection") as connection:
            client = connection.return_value
            client.getresponse.return_value = response
            folder = providers.post_webhook(self.base, RUN)
            self.assertIn("wait=true", client.request.call_args.args[1])
            self.assertIn("thread_id=12", client.request.call_args.args[1])
            self.assertEqual(read_json(folder / "request.json")["allowed_mentions"], {"parse":[]})
            self.assertFalse((folder / "classifier-input.json").exists())
            providers.post_webhook(self.base, RUN)
            client.request.assert_called_once()

    def test_gateway_listener_with_real_sdk_types_and_simulated_frames(self):
        import discord
        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def event(self, fn):
                setattr(self,fn.__name__,fn)
                return fn
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get_channel(self, channel_id):
                channel = Mock(spec=discord.TextChannel)
                channel.permissions_for.return_value.view_channel = True
                return channel
            async def start(self, token):
                await self.on_ready()
                await self.on_socket_raw_receive(json.dumps({"t":"READY","op":0,"d":{}}))
                await self.on_socket_raw_receive(json.dumps(gateway()))
                await asyncio.sleep(60)
            async def close(self):
                pass
        with patch.object(discord, "Client", FakeClient), patch("builtins.print"):
            raw, is_thread = asyncio.run(providers.receive_discord(RUN,CHANNEL,"unit-only",2))
        self.assertEqual(raw,gateway())
        self.assertFalse(is_thread)


if __name__ == "__main__":
    unittest.main()
