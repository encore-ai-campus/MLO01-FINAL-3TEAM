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
        self.assertEqual(set(requests["gmail"]), {"event_id", "preprocess_version", "body_version", "model_text"})
        self.assertNotEqual(requests["gmail"]["event_id"], requests["discord"]["event_id"])
        chat, mail = (read_json(d / "standard.json") for d in (a, b))
        columns = {"schema_version", "event_id", "company_id", "channel", "connection_id", "body_version",
                   "received_at", "source_message_id", "source_conversation_id", "source_channel_id",
                   "source_channel_name", "reply_to_message_id", "message_kind", "sender_ref",
                   "sender_display_name", "title", "body_text", "occurred_at", "source_metadata"}
        for record in (chat, mail):
            self.assertEqual(set(record), columns)
            self.assertEqual((record["schema_version"], record["body_version"]), (4, 1))
            self.assertEqual(record["message_kind"], "unknown")
            self.assertEqual(record["body_text"], requests[record["channel"]]["model_text"])
            self.assertRegex(record["received_at"], r"T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
            self.assertEqual(record["occurred_at"], "2024-01-01T00:00:00.000000Z")
        self.assertEqual(chat["event_id"], "96fe0f3d-e352-5208-bf29-2d4da24405f1")
        self.assertEqual(mail["event_id"], "ec1b3d87-7864-5738-b8a3-683ed7046767")
        self.assertIsNone(chat["title"])
        self.assertEqual(mail["title"], f"[{RUN}]")
        self.assertEqual(mail["source_metadata"], {"label_ids": ["INBOX", "UNREAD"], "to_addresses": []})
        self.assertEqual(mail["sender_display_name"], "Test")
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
        with self.assertRaisesRegex(ProbeError, "internalDate"):
            transform("gmail", raw, capture)
        raw["internalDate"] = "1704153600000"
        value, request = transform("gmail", raw, capture)
        self.assertEqual(value["title"], "[결제] 카드 승인 오류")
        self.assertEqual(request["model_text"], SAMPLE_TEXT)
        self.assertEqual(value["source_message_id"], "00aBf")
        self.assertEqual(value["occurred_at"], "2024-01-02T00:00:00.000000Z")
        self.assertNotEqual(value["event_id"], transform("gmail", raw, {**capture, "company_id": "tenant-b"})[0]["event_id"])
        raw.pop("internalDate")
        headers[:] = [h for h in headers if h["name"] == "Date"]
        missing, _ = transform("gmail", raw, capture)
        self.assertIsNone(missing["title"])
        self.assertIsNone(missing["occurred_at"])
        self.assertIsNone(missing["sender_ref"])

    def test_offline_replay_upgrades_both_derived_files(self):
        folders = self.discord(), self.mail()
        standards = {f: f.read_bytes() for d in folders for f in (d / "standard.json", d / "classifier-input.json")}
        originals = {f: f.read_bytes() for d in folders for f in (d / "raw.json", d / "capture.json")}
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
        raw["d"]["message_reference"] = {"message_id": "1000"}
        folder = self.discord(raw)
        reply = read_json(folder / "standard.json")
        self.assertEqual(reply["reply_to_message_id"], "1000")
        self.assertIsNone(reply["source_conversation_id"])
        capture = {**read_json(folder / "capture.json"), "is_thread": True, "channel_name": "문의"}
        thread, _ = transform("discord", raw, capture)
        self.assertEqual((thread["source_conversation_id"], thread["source_channel_name"]), (CHANNEL, "문의"))

    def test_discord_parent_validation_and_channel_defaults(self):
        for reference in ({}, {"message_id": "1001"}, {"type": 1, "message_id": "1000"}):
            raw = gateway()
            raw["d"].update(type=19, message_reference=reference)
            with self.assertRaisesRegex(ProbeError, "부모"):
                self.discord(raw)
        root = self.base / "runs" / RUN
        self.assertEqual(len(list(root.glob("review-discord-*/raw.json"))), 3)
        self.assertFalse((root / "discord").exists())

        raw = gateway()
        raw["d"].update(member={"nick": "  "}, author={"id": "", "global_name": "이름", "username": "계정"},
                        content=f"  [{RUN}] 첫 줄\r\n둘째 줄  ", message_reference={"type": 1, "message_id": "1000"})
        folder = self.discord(raw)
        standard = read_json(folder / "standard.json")
        self.assertEqual(standard["source_conversation_id"], "1001")
        self.assertIsNone(standard["reply_to_message_id"])
        self.assertIsNone(standard["sender_ref"])
        self.assertEqual(standard["sender_display_name"], "이름")
        self.assertEqual(standard["body_text"], f"[{RUN}] 첫 줄\r\n둘째 줄")
        self.assertEqual(standard["body_text"], read_json(folder / "classifier-input.json")["model_text"])

    def test_gmail_encoded_and_repeated_headers(self):
        raw = gmail()
        headers = raw["payload"]["headers"]
        headers[:] = [
            {"name": "Subject", "value": "=?UTF-8?Q?=5BVOC-unit=5D_=E2=9C=93?="},
            {"name": "From", "value": "=?UTF-8?B?7ZmN6ri464+Z?= <Sender@example.test>"},
            {"name": "To", "value": 'Team: "Doe, Jo" <jo@example.test>, other@example.test;'},
            {"name": "To", "value": "<desk+voc@example.test>"},
            {"name": "Auto-Submitted", "value": "no"},
            {"name": "Auto-Submitted", "value": "auto-replied; owner=desk"},
        ]
        folder = self.mail(raw)
        standard = read_json(folder / "standard.json")
        self.assertEqual((standard["sender_ref"], standard["sender_display_name"]), ("Sender@example.test", "홍길동"))
        self.assertEqual(standard["title"], f"[{RUN}] ✓")
        self.assertEqual(standard["source_metadata"]["to_addresses"], ["jo@example.test", "other@example.test", "desk+voc@example.test"])
        self.assertEqual(standard["message_kind"], "other")

    def test_html_only_and_equal_subject(self):
        raw = gmail()
        raw["payload"]["headers"][0]["value"] += " " + SAMPLE_TEXT
        raw["payload"]["parts"] = [part(f"<head>title</head><p>{SAMPLE_TEXT}</p><style>other</style>", "text/html")]
        output = self.mail(raw)
        self.assertEqual(read_json(output / "classifier-input.json")["model_text"], SAMPLE_TEXT)
        html = ("<div title='<style>'>line  1<style>.private {color:red}</style></div>"
                "<!-- <style> --><table><tr><td>A</td><td>B</td></tr></table><script>hidden()</script>")
        self.assertEqual(mail_text(part(html, "text/html"))[1], "line 1\nA\nB")

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
        matching["payload"]["headers"][0]["value"] = "=?UTF-8?Q?=5BVOC-unit=5D?="
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
            raw, is_thread, channel_name = asyncio.run(providers.receive_discord(RUN,CHANNEL,"unit-only",2))
        self.assertEqual(raw,gateway())
        self.assertFalse(is_thread)
        self.assertEqual(channel_name, "문의-테스트")
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
