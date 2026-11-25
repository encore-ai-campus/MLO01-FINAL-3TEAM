import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import exporter


class CollectorReadTests(unittest.TestCase):
    def test_reports_observed_metadata_without_exporting_payloads(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            status = root / "status"
            status.mkdir()
            (status / "gmail.json").write_text(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(), "state": "ready", "account": "private-mailbox",
            }))
            (status / "discord.json").write_text('{"state":"ready"}')
            with closing(sqlite3.connect(root / "capture.sqlite3")) as db:
                db.executescript("""
                    CREATE TABLE capture_outbox(channel text, published_at text, envelope text);
                    INSERT INTO capture_outbox VALUES ('gmail', NULL, 'private-payload');
                    CREATE TABLE collector_checkpoint(company_id text, channel text, connection_id text,
                        scope_id text, position text, phase text);
                    INSERT INTO collector_checkpoint VALUES
                        ('example', 'gmail', 'private-mailbox', '', '42', 'active'),
                        ('example', 'discord', 'private-guild', 'private-room', '1299555050899079218', 'active');
                """)
            rows = []
            with patch.object(exporter, "ROOT", root):
                exporter.capture_metrics(rows)
            actual = "\n".join(rows)
            self.assertIn('voc_collector_ready{channel="gmail"} 1', actual)
            self.assertNotIn('voc_collector_ready{channel="discord"}', actual)
            self.assertIn('voc_capture_pending{channel="gmail"} 1', actual)
            self.assertIn('position="1299555050899079218"', actual)
            self.assertNotIn("private-", actual)


if __name__ == "__main__":
    unittest.main()
