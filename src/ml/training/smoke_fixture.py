"""Insert synthetic Bronze and Silver rows into the isolated verification database."""

import json
from uuid import uuid4

from .datasets import database


def receive(cn, company, channel, sender, title, body, *, received_at=None):
    event = uuid4()
    room = "101" if channel == "discord" else None
    time = received_at or cn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    cn.execute("""
        INSERT INTO voc.bronze_message VALUES (%s,%s,%s,'smoke',%s,'synthetic',%s,'{}','{}')
    """, (event, str(company), channel, str(event), time))
    cn.execute("""
        INSERT INTO voc.silver_message(schema_version,event_id,source_company_id,source_channel_type,source_connection_id,
            source_message_id,source_conversation_id,source_channel_id,message_kind,sender_ref,title,
            body_text,body_version,received_at,source_metadata_json)
        VALUES (4,%s,%s,%s,'smoke',%s,%s,%s,'customer_inquiry',%s,%s,%s,1,%s,%s::jsonb)
    """, (event, str(company), channel, str(event), str(event), room, sender, title, body, time,
          '{}' if room else '{"label_ids":[],"to_addresses":[]}'))
    return cn.execute("SELECT * FROM voc.feature_snapshot WHERE silver_event_id=%s", (event,)).fetchone()


def review_and_send(cn, snapshot, reviewer, category, sentiment, urgent):
    with cn.transaction():
        ticket, feature = snapshot["ticket_id"], snapshot["feature_snapshot_id"]
        cn.execute("""
            UPDATE public.ticket SET sentiment=%s,category_id=%s WHERE ticket_id=%s
        """, (sentiment, category, ticket))
        cn.execute("""
            INSERT INTO voc.counselor_review(feature_snapshot_id,reviewer_id,sentiment,category_id,is_urgent,evidence_ref)
            VALUES (%s,%s,%s,%s,%s,'synthetic review')
        """, (feature, reviewer, sentiment, category, urgent))
        reply = cn.execute("""
            INSERT INTO public.ticket_message(ticket_id,staff_user_id,body,sender_type,message_type,
                delivery_status,created_at,reviewed_feature_id)
            VALUES (%s,%s,'시연 답변','staff','reply','sending',clock_timestamp(),%s)
            RETURNING ticket_message_id
        """, (ticket, reviewer, feature)).fetchone()["ticket_message_id"]
        cn.execute("""
            UPDATE public.ticket_message SET delivery_status='sent',sent_at=clock_timestamp()
            WHERE ticket_message_id=%s
        """, (reply,))
        cn.execute("""
            INSERT INTO public.ticket_event(ticket_id,event_type_id,actor_id,from_status,to_status,created_at)
            SELECT %s,event_type_id,%s,'미처리','답변완료',clock_timestamp()
            FROM public.event_type WHERE code='ticket_reply_completed'
        """, (ticket, reviewer))
        cn.execute("UPDATE public.ticket SET status='답변완료' WHERE ticket_id=%s", (ticket,))
    return cn.execute("SELECT * FROM voc.feature_snapshot_label WHERE completion_message_id=%s", (reply,)).fetchall()


def seed(cn):
    company, domain, reviewer, delivery, other = (uuid4() for _ in range(5))
    cn.execute("INSERT INTO public.domain VALUES (%s,'시연 커머스')", (domain,))
    cn.execute("INSERT INTO public.company(company_id,email_domain,domain_id) VALUES (%s,%s,%s)",
               (company, f"{company}.example.test", domain))
    cn.execute("""
        INSERT INTO public.staff_user(staff_user_id,company_id,email,name,password_hash)
        VALUES (%s,%s,%s,'시연 상담사','!')
    """, (reviewer, company, f"reviewer@{company}.example.test"))
    for category, name in ((delivery, "배송 지연"), (other, "교환")):
        cn.execute("INSERT INTO public.category VALUES (%s,%s,%s)", (category, domain, name))
    for channel, room in (("gmail", ""), ("discord", "101")):
        channel_id = cn.execute("""
            INSERT INTO public.channel(company_id,name,type) VALUES (%s,%s,%s) RETURNING channel_id
        """, (company, channel, channel)).fetchone()["channel_id"]
        cn.execute("INSERT INTO voc.ticket_source_connection VALUES (%s,%s,'smoke',%s,%s,%s)",
                   (str(company), channel, room, company, channel_id))

    receive(cn, company, "gmail", "client@example.test", "이전 문의", "미해결 이력용 문의")
    first = receive(cn, company, "gmail", "client@example.test", "택배 지연", "지난주부터 택배가 오지 않습니다.")
    assert first["unresolved_count"] == 1 and first["avg_first_reply_7d_ms"] is None
    assert len(review_and_send(cn, first, reviewer, delivery, "부정", True)) == 2
    second = receive(cn, company, "gmail", "client@example.test", "교환 절차", "다른 색상으로 교환할 수 있을까요?")
    assert second["unresolved_count"] == 1 and second["avg_first_reply_7d_ms"] > 0
    assert len(review_and_send(cn, second, reviewer, other, "중립", False)) == 2
    eval_sample = receive(cn, company, "discord", "1234567890", None, "배송 시간 변경을 안내받아 감사합니다.")
    assert eval_sample["unresolved_count"] == 0 and eval_sample["avg_first_reply_7d_ms"] is None
    gold = review_and_send(cn, eval_sample, reviewer, delivery, "긍정", False)
    assert len(gold) == 2
    return {
        "company_uuid": str(company), "train_feature_uuid": str(second["feature_snapshot_id"]),
        "domains": {str(domain): {"task": "commerce", "categories": {
            str(delivery): "delivery_delay", str(other): "exchange",
        }}},
        "eval_reviews": [
            {"gold_label_uuid": str(label["feature_snapshot_label_id"]), "reviewer_uuid": str(reviewer),
             "evidence_ref": "synthetic held-out review"} for label in gold
        ],
        "cutoff": cn.execute("SELECT clock_timestamp() AS now").fetchone()["now"].isoformat(),
    }


if __name__ == "__main__":
    with database() as conn:
        print(json.dumps(seed(conn)))
