"""Offline regressions for durable handoff, provider recovery and collection scope."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from collector import pipeline, sources
from collector.events import COMPANY, archive_path, atomic_json, make_event
from collector.spool import CaptureStore, Checkpoint, Scope


class CollectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = CaptureStore(self.root)
        self.scope = Scope(COMPANY, "discord", "3003", "11")
        self.gmail = Scope(COMPANY, "gmail", "demo-mailbox", "Label_test")

    def message(self, ident):
        return {"id": str(ident), "channel_id": "11", "type": 0,
                "author": {"id": "2002"}, "content": "일반 문의"}

    def discord_event(self, ident):
        raw = {"op": 0, "t": "MESSAGE_CREATE", "d": self.message(ident)}
        return sources.discord_event(raw["d"], raw, "discord_gateway", self.scope, False)

    def test_concurrent_receipt_and_publish_ack_crash(self):
        event = self.discord_event(1001)
        with ThreadPoolExecutor(max_workers=4) as pool:
            saved = list(pool.map(lambda _: self.store.stage(event), range(8)))
        self.assertEqual(saved.count(True), 1)
        self.assertEqual(self.store.pending(), event)
        with patch.object(pipeline, "send") as send:
            with patch.object(self.store, "published", side_effect=RuntimeError("interrupted")):
                with self.assertRaises(RuntimeError):
                    pipeline.publish_next(self.store, self.root, None)
            reopened = CaptureStore(self.root)
            self.assertTrue(pipeline.publish_next(reopened, self.root, None))
            self.assertEqual(send.call_args_list[0], send.call_args_list[1])
            self.assertFalse(pipeline.publish_next(reopened, self.root, None))
            self.assertFalse(reopened.stage(event))
        self.assertEqual(json.loads(archive_path(self.root, event).read_bytes()), event["raw"])

    def test_discord_body_conflict_before_and_after_publish(self):
        first = self.discord_event(77)
        history = self.message(77)
        same = sources.discord_event(history, history, "discord_history", self.scope, False)
        edited = dict(history, content="바뀐 본문")
        conflict = sources.discord_event(edited, edited, "discord_history", self.scope, False)
        self.store.stage(first)
        self.assertFalse(self.store.stage(same))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM capture_issue").fetchone()[0], 0)
        self.assertFalse(self.store.stage(conflict))
        self.assertEqual(self.store.pending(), first)
        with patch.object(pipeline, "send"):
            self.assertTrue(pipeline.publish_next(self.store, self.root, None))
        self.assertFalse(self.store.stage(same))
        self.assertFalse(self.store.stage(conflict))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT reason,reference FROM capture_issue").fetchall(),
                             [("source_body_changed", first["event_id"])])
        self.assertEqual(json.loads(archive_path(self.root, first).read_bytes()), first["raw"])

    def test_legacy_pending_with_archive_and_bronze_reconciliation(self):
        event = self.discord_event(17)
        atomic_json(self.root / "pending" / (event["event_id"] + ".json"), event)
        atomic_json(archive_path(self.root, event), event["raw"])
        migrated = CaptureStore(self.root)
        self.assertEqual(migrated.pending(), event)
        row = dict(event, received_at=datetime.now(timezone.utc))
        migrated.confirm_bronze([row])
        self.assertIsNone(CaptureStore(self.root).pending())

    def test_discord_history_page_and_gateway_share_identity(self):
        legacy = self.root / "cursors" / f"discord-{COMPANY}-11.json"
        atomic_json(legacy, "100")
        state = sources.init_discord_checkpoint(self.store, self.root, self.scope, "999")
        self.assertEqual(state, Checkpoint("100"))
        live = self.discord_event(110)
        self.store.stage(live)
        self.assertEqual(self.store.checkpoint(self.scope), state)
        excluded = dict(self.message(112), webhook_id="77")
        page = [excluded, self.message(110), self.message(105)]
        self.assertTrue(sources.save_discord_history(self.store, self.scope, state, page, False))
        self.assertFalse(sources.save_discord_history(self.store, self.scope, state, [self.message(106)], False))
        self.assertEqual(sources.init_discord_checkpoint(self.store, self.root, self.scope, "999"), Checkpoint("112"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM capture_outbox").fetchone()[0], 2)
        self.assertEqual(self.store.pending(), live)

    def test_process_death_rolls_back_half_discord_page(self):
        self.store.initialize(self.scope, Checkpoint("100"))
        program = """
import os, sys
from pathlib import Path
from collector.events import COMPANY, make_event
from collector.spool import CaptureStore, Checkpoint, Scope
store = CaptureStore(Path(sys.argv[1]))
def interrupted():
    yield make_event("discord", "3003", "101", "discord_history", {})
    os._exit(81)
store.save_page(Scope(COMPANY, "discord", "3003", "11"), Checkpoint("100"),
                interrupted(), advance=Checkpoint("102"))
"""
        proc = subprocess.run([sys.executable, "-c", program, str(self.root)], check=False)
        self.assertEqual(proc.returncode, 81)
        self.assertEqual(self.store.checkpoint(self.scope), Checkpoint("100"))
        self.assertIsNone(self.store.pending())
        sources.save_discord_history(self.store, self.scope, Checkpoint("100"),
                                     [self.message(102), self.message(101)], False)
        self.assertEqual(self.store.checkpoint(self.scope), Checkpoint("102"))

    def test_plain_reply_and_attachment_are_eligible(self):
        message = self.message(101)
        captured = sources.discord_event(message, message, "discord_history", self.scope, False, "문의")
        self.assertEqual(captured["context"]["channel_name"], "문의")
        msg = self.message(1001)
        self.assertTrue(sources.human_message(msg))
        self.assertTrue(sources.human_message({**msg, "type": 19}))
        self.assertTrue(sources.human_message({**msg, "content": "", "attachments": [{"id": "42"}]}))
        self.assertFalse(sources.human_message({**msg, "author": {"id":"2002", "bot":True}}))
        self.assertFalse(sources.human_message({**msg, "webhook_id": "77"}))
        self.assertFalse(sources.human_message({**msg, "type": 7}))

    def poll(self, session):
        return sources.poll_gmail(session, self.gmail.connection, self.gmail.source, self.store)

    @staticmethod
    def response(data, status=200):
        result = Mock(status_code=status)
        result.json.return_value = data
        return result

    def test_gmail_bootstrap_privacy_and_added_label_on_existing_mail(self):
        label = self.gmail.source
        reads = []
        def fetch(url, *, params, timeout):
            ident = url.rsplit("/", 1)[-1]
            if ident == "history":
                self.assertEqual(params["startHistoryId"], "100")
                self.assertEqual(params["labelId"], label)
                change = {"messagesAdded": [{"message": {"id":"new"}}],
                          "labelsAdded": [{"message":{"id":"new"}, "labelIds":[label]},
                                          {"message":{"id":"old"}, "labelIds":[label]},
                                          {"message":{"id":"unrelated"}, "labelIds":["OTHER"]}]}
                return self.response({"history":[change],"historyId":"130"})
            fmt = params["format"]
            reads.append((ident, fmt))
            eligible = ident != "removed" and not (ident == "removed_later" and fmt == "full")
            return self.response({"id":ident, "labelIds":[label] if eligible else []})
        session = Mock()
        session.get.side_effect = fetch
        pages = [{"historyId":"100"},
                 {"messages":[{"id":"removed"},{"id":"removed_later"}], "nextPageToken":"p2"},
                 {"messages":[{"id":"new"}]}]
        with patch.object(sources,"gmail_get",side_effect=pages) as listing:
            self.assertEqual(self.poll(session), "ready")
        self.assertEqual(listing.call_args_list[-1].kwargs["pageToken"], "p2")
        self.assertEqual(listing.call_args_list[-1].kwargs["labelIds"], label)
        self.assertEqual([i for i,f in reads if f=="full"], ["removed_later","new","old"])
        self.assertEqual(self.store.checkpoint(self.gmail), Checkpoint("130"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM capture_issue").fetchone()[0], 2)

    def test_gmail_interrupted_bootstrap_resumes_from_original_baseline(self):
        session = Mock()
        session.get.return_value = self.response({"historyId":"105"})
        with patch.object(sources,"gmail_get",side_effect=[{"historyId":"100"}, TimeoutError()]):
            with self.assertRaises(TimeoutError):
                self.poll(session)
        self.assertEqual(self.store.checkpoint(self.gmail), Checkpoint("100", "bootstrap"))
        with patch.object(sources,"gmail_get",return_value={"messages":[]}) as listing:
            self.assertEqual(self.poll(session), "ready")
            self.assertEqual(listing.call_count, 1)
        self.assertEqual(session.get.call_args.kwargs["params"]["startHistoryId"], "100")

    def test_gmail_history_replay_only_advances_after_final_page(self):
        self.store.initialize(self.gmail, Checkpoint("100"))
        label = self.gmail.source
        session = Mock()
        first = {"history":[{"messagesAdded":[{"message":{"id":"first"}}]}],
                 "nextPageToken":"next","historyId":"140"}
        last = {"history":[{"labelsAdded":[{"message":{"id":"second"},"labelIds":[label]}]}],
                "historyId":"140"}
        full_reads = []
        history_reads = []
        failure = [True]
        def fetch(url, *, params, timeout):
            ident = url.rsplit("/",1)[-1]
            if ident == "history":
                history_reads.append((params["startHistoryId"],params.get("pageToken")))
                if params.get("pageToken") and failure[0]:
                    raise TimeoutError()
                return self.response(last if params.get("pageToken") else first)
            if params["format"]=="full":
                full_reads.append(ident)
            return self.response({"id":ident,"labelIds":[label]})
        session.get.side_effect = fetch
        with self.assertRaises(TimeoutError):
            self.poll(session)
        self.assertEqual(self.store.checkpoint(self.gmail), Checkpoint("100"))
        failure[0] = False
        self.assertEqual(self.poll(session), "ready")
        self.assertEqual(history_reads, [("100",None),("100","next")]*2)
        self.assertEqual(full_reads, ["first","second"])
        self.assertEqual(self.store.checkpoint(self.gmail), Checkpoint("140"))

    def test_gmail_expired_history_and_unavailable_message(self):
        self.store.initialize(self.gmail, Checkpoint("1"))
        session = Mock()
        session.get.side_effect = [self.response({},404),self.response({},404),
                                   self.response({"historyId":"502"})]
        with patch.object(sources,"gmail_get",side_effect=[
                {"historyId":"500"},{"messages":[{"id":"deleted"}]}]):
            self.assertEqual(self.poll(session), "resync_pending")
            self.assertEqual(self.store.checkpoint(self.gmail), Checkpoint("500","bootstrap"))
            self.assertEqual(self.poll(session), "ready")
        self.assertEqual(self.store.checkpoint(self.gmail), Checkpoint("502"))
        with self.store.connect() as db:
            self.assertEqual(set(db.execute("SELECT reason,reference FROM capture_issue")),
                             {("history_expired","1"),("message_unavailable","deleted")})


if __name__ == "__main__":
    unittest.main()
