from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from config.database import read_snapshot

ELIGIBLE = """
    SELECT candidate.*
    FROM public.ticket t
    JOIN voc.ticket_answer_completion a USING(ticket_uuid)
    CROSS JOIN LATERAL voc.eligible_ticket_retraining_candidates(a) candidate
    WHERE t.company_uuid=%s AND NOT EXISTS (
        SELECT FROM voc.retraining_candidate existing
        WHERE existing.ticket_uuid=candidate.ticket_uuid AND existing.kind=candidate.kind
    )
    ORDER BY candidate.completed_at,candidate.ticket_uuid,candidate.kind
    LIMIT %s
"""


class Command(BaseCommand):
    help = "Preview a company's historical candidates, or store them with --apply."

    def add_arguments(self, parser):
        parser.add_argument("company_uuid", type=UUID)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000")
        params = (options["company_uuid"], limit)
        if options["apply"]:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("INSERT INTO voc.retraining_candidate " + ELIGIBLE + " ON CONFLICT DO NOTHING", params)
                self.stdout.write(f"inserted={cursor.rowcount}")
        else:
            with read_snapshot() as cursor:
                cursor.execute("SELECT count(*) FROM (" + ELIGIBLE + ") eligible", params)
                self.stdout.write(f"eligible={cursor.fetchone()[0]}")
