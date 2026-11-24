"""Offline regressions for privacy boundaries and repeat delivery. Run inside the worker image."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from collector import sources
from collector.events import already_received, archive_path, atomic_json, make_event, pending_path, stage


class CollectionTests(unittest.TestCase):
    def test_concurrent_receipt_and_archive_survive_replay(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            raw = {"op": 0, "t": "MESSAGE_CREATE", "d": {"id": "1001", "content": "일반 문의"}}
            event = make_event("discord", "3003", "1001", "discord_gateway", raw)
            with ThreadPoolExecutor(max_workers=4) as pool:
                saved = list(pool.map(lambda _: stage(root, event), range(8)))
            self.assertEqual(saved.count(True), 1)
            pending = pending_path(root, event)
            self.assertEqual(json.loads(pending.read_bytes())["raw"], raw)
            path = archive_path(root, event)
            atomic_json(path, raw)
            pending.unlink()
            self.assertTrue(already_received(root, event))
            self.assertFalse(stage(root, event))
            self.assertEqual(path.name, "1001.json")
            self.assertEqual(json.loads(path.read_bytes()), raw)

    def test_plain_reply_and_attachment_are_eligible(self):
        msg = {"author": {"id": "2002"}, "type": 0, "content": "일반 문의"}
        self.assertTrue(sources.human_message(msg))
        self.assertTrue(sources.human_message({**msg, "type": 19}))
        self.assertTrue(sources.human_message({**msg, "content": "", "attachments": [{"id": "42"}]}))
        self.assertFalse(sources.human_message({**msg, "author": {"id":"2002", "bot":True}}))
        self.assertFalse(sources.human_message({**msg, "webhook_id": "77"}))
        self.assertFalse(sources.human_message({**msg, "type": 7}))

    def test_gmail_label_pagination_recheck_and_dedup(self):
        label = "Label_test"
        session = Mock()

        def fetch(url, *, params, timeout):
            name = url.rsplit("/", 1)[-1]
            responses = {"eligible": [label], "removed": [], "removed_later": [label]}
            labels = responses[name]
            if name == "removed_later" and params["format"] == "full":
                labels = []
            result = Mock(status_code=200)
            result.json.return_value = {"id": name, "labelIds": labels}
            return result

        session.get.side_effect = fetch
        pages = [{"messages":[{"id":"removed"},{"id":"removed_later"}],"nextPageToken":"next"},
                 {"messages":[{"id":"eligible"}]}]
        with tempfile.TemporaryDirectory() as work, patch.object(sources,"ROOT",Path(work)), patch("builtins.print"):
            with patch.object(sources, "gmail_get", side_effect=pages) as listing:
                sources.poll_gmail(session,"demo-mailbox",label)
                for call in listing.call_args_list:
                    self.assertEqual(call.kwargs["labelIds"],label)
                self.assertEqual(listing.call_args_list[-1].kwargs["pageToken"],"next")
            fetched = [c.args[0].rsplit("/",1)[-1] for c in session.get.call_args_list if c.kwargs["params"]["format"] == "full"]
            self.assertEqual(fetched,["removed_later","eligible"])
            stored = [json.loads(p.read_bytes()) for p in (Path(work)/"pending").glob("*.json")]
            self.assertEqual([e["source_message_id"] for e in stored], ["eligible"])
            session.reset_mock()
            with patch.object(sources,"gmail_get",return_value=pages[1]):
                sources.poll_gmail(session,"demo-mailbox",label)
            session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
