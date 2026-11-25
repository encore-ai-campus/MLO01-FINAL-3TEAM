import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from config.database import fetch_rows, read_snapshot
from config.http import DeskJSONEncoder


class Command(BaseCommand):
    help = "Read one company's retraining candidates as JSON lines."

    def add_arguments(self, parser):
        parser.add_argument("company_uuid", type=UUID)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000")
        with read_snapshot() as cursor:
            cursor.execute("""
                SELECT c.channel,c.company_uuid,c.domain_uuid,c.ticket_uuid,c.chat_completion_uuid,
                       a.delivery_uuid,ch.conversation_uuid,c.completed_by,c.completed_at,
                       c.input_uuid,c.input_version,c.preprocess_version,ch.input_sha256,
                       c.kind,c.value,c.origin,c.label_staff_uuid,c.evidence_at,
                       coalesce(c.ticket_attempt_uuid,c.chat_attempt_uuid) AS prediction_attempt_uuid,
                       c.predicted_value,c.model_name,c.model_version,c.rollout
                FROM voc.retraining_candidate c
                LEFT JOIN voc.ticket_answer_completion a USING(ticket_uuid)
                LEFT JOIN voc.chat_training_completion ch ON c.chat_completion_uuid=ch.completion_uuid
                WHERE c.company_uuid=%s
                ORDER BY c.completed_at DESC,c.ticket_uuid,c.chat_completion_uuid,c.kind
                LIMIT %s
            """, (options["company_uuid"], limit))
            for candidate in fetch_rows(cursor):
                self.stdout.write(json.dumps(candidate, cls=DeskJSONEncoder, ensure_ascii=False))
