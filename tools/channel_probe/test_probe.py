"""Offline provider-shape tests. These never connect to Discord or Gmail."""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.channel_probe import providers
from tools.channel_probe.artifacts import read_json, regenerate, save_inbound, verify, write_json
from tools.channel_probe.normalize import ProbeError, SAMPLE_TEXT, is_discord_test, mail_text, transform

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
        chat, mail = (read_json(d / "standard.json") for d in (a, b))
        columns = {"schema_version", "company_id", "channel", "connection_id", "ticket_id", "body_version",
                   "received_at", "raw_ref", "source_message_id", "source_conversation_id",
                   "sender_ref", "title", "body_text", "occurred_at"}
        for record in (chat, mail):
            self.assertEqual(set(record), columns)
            self.assertEqual((record["schema_version"], record["body_version"]), (2, 1))
            self.assertIsNone(record["title"])
            self.assertRegex(record["received_at"], r"T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
            self.assertEqual(record["occurred_at"], "2024-01-01T00:00:00.000000Z")
        self.assertEqual(chat["raw_ref"], "96fe0f3d-e352-5208-bf29-2d4da24405f1")
        self.assertEqual(mail["raw_ref"], "ec1b3d87-7864-5738-b8a3-683ed7046767")
        self.assertEqual(chat["ticket_id"], "VOC-f8658e5646ca580dbf55f4f6653e6e72")
        self.assertEqual(mail["ticket_id"], "VOC-2552f1560f7451e19b8d5725777ffd27")
        self.assertEqual(mail["source_conversation_id"], "conversation-1")
        self.assertEqual(mail["sender_ref"], "sender@example.invalid")

    def test_offsets_missing_fields_and_opaque_ids(self):
        raw = gateway()
        raw["d"].update(id="00001001", timestamp="2024-01-01T09:00:00.005+09:00", author={})
        capture = {"channel_id": CHANNEL, "is_thread": False, "connection_id": "3003", "run": RUN,
                   "received_at": "2024-01-01T09:00:01.123456+09:00"}
        standard, _ = transform("discord", raw, capture)
        self.assertEqual(standard["source_message_id"], "00001001")
        self.assertEqual(standard["occurred_at"], "2024-01-01T00:00:00.005000Z")
        self.assertEqual(standard["received_at"], "2024-01-01T00:00:01.123456Z")
        self.assertIsNone(standard["sender_ref"])
        raw["d"].pop("timestamp")
        self.assertIsNone(transform("discord", raw, capture)[0]["occurred_at"])
        with self.assertRaisesRegex(ProbeError, "received_at"):
            transform("discord", raw, {**capture, "received_at": "2024-01-01T09:00:00"})
        raw["d"]["id"] = 1001
        with self.assertRaises(ProbeError):
            transform("discord", raw, capture)

    def test_real_subjects_opaque_mail_ids_and_optional_source_time(self):
        raw = gmail()
        raw.update(id="00aBf", internalDate="invalid")
        headers = raw["payload"]["headers"]
        headers[0]["value"] = "  [결제] 카드 승인 오류  "
        headers.append({"name": "Date", "value": "Tue, 2 Jan 2024 09:00:00 +0900"})
        capture = {"company_id": "tenant-a", "connection_id": "mailbox", "received_at": "2024-01-02T03:00:00Z"}
        value, _ = transform("gmail", raw, capture)
        self.assertEqual(value["title"], "[결제] 카드 승인 오류")
        self.assertEqual(value["source_message_id"], "00aBf")
        self.assertEqual(value["occurred_at"], "2024-01-02T00:00:00.000000Z")
        self.assertNotEqual(value["raw_ref"], transform("gmail", raw, {**capture, "company_id": "tenant-b"})[0]["raw_ref"])
        headers.clear()
        missing, _ = transform("gmail", raw, capture)
        self.assertIsNone(missing["title"])
        self.assertIsNone(missing["occurred_at"])
        self.assertIsNone(missing["sender_ref"])

    def test_offline_replay_only_replaces_standard_files(self):
        folders = self.discord(), self.mail()
        standards = {d / "standard.json": (d / "standard.json").read_bytes() for d in folders}
        originals = {f: f.read_bytes() for d in folders for f in d.iterdir() if f.name != "standard.json"}
        for path in standards:
            write_json(path, {"schema_version": 1})
        for _ in range(2):
            regenerate(self.base, RUN)
            self.assertEqual({p: p.read_bytes() for p in standards}, standards)
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        verify(self.base, RUN, SAMPLE_TEXT)

        # A broken second capture must not replace the first output.
        write_json(folders[0] / "standard.json", {"schema_version": 1})
        write_json(folders[1] / "capture.json", {"run": "another-run"})
        with self.assertRaises(ProbeError):
            regenerate(self.base, RUN)
        self.assertEqual(read_json(folders[0] / "standard.json"), {"schema_version": 1})

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
                channel.name = "문의-테스트"
                channel.jump_url = "https://discord.com/channels/3003/2002"
                channel.permissions_for.return_value.view_channel = True
                return channel
            async def start(self, token):
                await self.on_ready()
                await self.on_socket_raw_receive(json.dumps({"t":"READY","op":0,"d":{}}))
                for changes in (
                    {"channel_id": "999", "content": "다른 채널 본문"},
                    {"webhook_id": "55"},
                    {"content": "표식 없는 문의 본문"},
                    {"content": "두 번째 표식 없는 문의"},
                    {"content": ""},
                ):
                    frame = gateway()
                    frame["d"].update(changes)
                    await self.on_socket_raw_receive(json.dumps(frame))
                await self.on_socket_raw_receive(json.dumps(gateway()))
                await asyncio.sleep(60)
            async def close(self):
                pass
        with patch.object(discord, "Client", FakeClient), patch("builtins.print") as output:
            raw, is_thread = asyncio.run(providers.receive_discord(RUN,CHANNEL,"unit-only",2))
        self.assertEqual(raw,gateway())
        self.assertFalse(is_thread)
        lines = [call.args[0] for call in output.call_args_list]
        self.assertIn("https://discord.com/channels/3003/2002", lines[0])
        self.assertEqual(len(lines), 4)  # READY plus one hint per skip reason.
        self.assertIn("봇이나 웹훅", lines[1])
        self.assertIn("표식이 일치하지 않습니다", lines[2])
        self.assertIn("텍스트가 비어 있습니다", lines[3])
        self.assertNotIn("문의 본문", "\n".join(lines))
        self.assertNotIn("다른 채널 본문", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
