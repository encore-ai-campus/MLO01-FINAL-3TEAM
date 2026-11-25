"""Deliver committed category decisions to the assignment service, in the same database."""
import time
from uuid import UUID

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection, transaction

from apps.assignment.services import apply_category_result


def assign_next(company, version):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("""
            SELECT j.request_id,j.event_id,j.preprocess_version,j.body_version,p.value::uuid,p.request_id
            FROM voc.classification_job j
            LEFT JOIN voc.classification_projection p ON p.request_id=j.request_id AND p.status='succeeded'
            WHERE j.company_uuid=%s AND j.model_version=%s AND j.analysis_kind='category'
              AND j.state='succeeded' AND j.applied AND j.assignment_checked_at IS NULL
              AND EXISTS (SELECT FROM visible_ticket t WHERE t.company_uuid=j.company_uuid AND t.source_event_id=j.event_id)
            ORDER BY j.finished_at,j.request_id LIMIT 1 FOR UPDATE OF j SKIP LOCKED
        """, (company, version))
        row = cursor.fetchone()
        if row is None:
            return False
        request_id, event, preprocess, body, category, current_projection = row
        if current_projection:
            apply_category_result(company_uuid=company, event_id=event, preprocess_version=preprocess,
                                  body_version=body, category_uuid=category)
        cursor.execute("UPDATE voc.classification_job SET assignment_checked_at=clock_timestamp() WHERE request_id=%s",
                       (request_id,))
        return True


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, type=UUID)
        parser.add_argument("--model-version", required=True)
        parser.add_argument("--once", action="store_true", help="Drain available decisions and exit")

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            if not assign_next(options["company"], options["model_version"]):
                if options["once"]:
                    return
                time.sleep(0.5)
