# This expression is shared by ticket reads and operations holding the ticket lock.
# Both queries expose the source ticket as t.
CLOSE_AT_SQL = """
    CASE WHEN t.status = '답변완료' THEN (
        SELECT max(m.sent_at) + interval '72 hours'
        FROM ticket_message m
        WHERE m.ticket_uuid = t.ticket_uuid AND m.message_type = 'reply'
          AND m.delivery_status = 'sent'
    ) END
"""
STATUS_SQL = f"CASE WHEN ({CLOSE_AT_SQL}) <= now() THEN '문의 종료' ELSE t.status END"
