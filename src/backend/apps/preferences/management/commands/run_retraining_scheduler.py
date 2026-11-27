"""Check saved retraining policies and reconcile previously submitted Argo workflows."""
import logging
import time
from uuid import UUID

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections, connection

from apps.preferences.retraining import process_company


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check each company once and exit")
        parser.add_argument("--company", type=UUID, help="Limit checks to this company")

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("""WITH companies AS (
                          SELECT p.company_id FROM voc.retraining_policy p
                          WHERE p.is_enabled OR EXISTS (SELECT FROM voc.retraining_dispatch d
                                WHERE d.company_id=p.company_id AND d.state IN ('running','pending'))
                          UNION
                          SELECT company_id FROM voc.training_stop_request WHERE state='requested'
                        )
                        SELECT company_id FROM companies
                        WHERE %s::uuid IS NULL OR company_id=%s ORDER BY company_id""",
                        (options["company"], options["company"]))
                    companies = [row[0] for row in cursor.fetchall()]
                for company in companies:
                    try:
                        result = process_company(company)
                        if options["once"]:
                            self.stdout.write(f"{company} {result}")
                    except DatabaseError:
                        if options["once"]:
                            raise
                        logger.exception("회사 예약 학습 확인에 실패했습니다. company=%s",company)
            except DatabaseError:
                if options["once"]:
                    raise
                logger.exception("예약 정책 DB에 다시 연결합니다.")
            if options["once"]:
                return
            time.sleep(30)
