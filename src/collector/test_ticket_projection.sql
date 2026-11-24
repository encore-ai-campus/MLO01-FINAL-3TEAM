-- Run on a disposable database after collector/schema.sql, collector/ticket_schema.sql and collector/monitoring.sql.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL plpgsql.check_asserts=on;
CREATE FUNCTION pg_temp.test_mail(company text, mailbox text, source text, kind text, sender text) RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE event uuid:=gen_random_uuid();
BEGIN
    INSERT INTO voc.bronze_messages VALUES(event,company,'gmail',mailbox,source,'gmail_api',
        '2026-01-02T03:04:05Z','{}','{}');
    INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,source_message_id,
        source_conversation_id,message_kind,sender_ref,sender_display_name,title,body_text,body_version,
        occurred_at,received_at,source_metadata)
    VALUES(4,event,company,'gmail',mailbox,source,'shared-thread',kind,sender,'시험 고객',NULL,
        E'\n  배송 문의!\n다음 줄',1,NULL,'2026-01-02T03:04:05Z','{"label_ids":[],"to_addresses":[]}');
    RETURN event;
END $$;

DO $$
DECLARE
    company_one uuid; company_two uuid; domain_id uuid; channel_one uuid; channel_two uuid;
    registered_mailbox constant text := 'helpdesk@example.invalid';
    customer_mail constant text := 'client@example.invalid';
    e1 uuid; e2 uuid; e3 uuid; e_other uuid; missing uuid; original public.ticket%ROWTYPE;
    discord_event uuid:=gen_random_uuid(); discord_ticket uuid; discord_channel uuid;
BEGIN
    INSERT INTO public.domain(name) VALUES('테스트 도메인') RETURNING domain_uuid INTO domain_id;
    INSERT INTO public.company(name,email_domain,domain_uuid) VALUES('테스트 회사 1','qa1.invalid',domain_id)
        RETURNING company_uuid INTO company_one;
    INSERT INTO public.company(name,email_domain,domain_uuid) VALUES('테스트 회사 2','qa2.invalid',domain_id)
        RETURNING company_uuid INTO company_two;
    INSERT INTO public.channel(company_uuid,name,type) VALUES(company_one,'테스트 메일함','gmail') RETURNING channel_uuid INTO channel_one;
    INSERT INTO public.channel(company_uuid,name,type) VALUES(company_two,'테스트 메일함','gmail') RETURNING channel_uuid INTO channel_two;

    e1 := pg_temp.test_mail('fixture-a',registered_mailbox,'original','unknown',customer_mail);
    ASSERT NOT EXISTS(SELECT FROM public.ticket WHERE source_event_id=e1);
    UPDATE voc.silver_messages SET message_kind='customer_inquiry' WHERE event_id=e1;
    ASSERT (SELECT reason='unregistered_source' FROM voc.ticket_projection_issue WHERE event_id=e1);

    INSERT INTO voc.ticket_source_connection VALUES('fixture-a','gmail',registered_mailbox,'',company_one,channel_one);
    SELECT * INTO STRICT original FROM public.ticket WHERE source_event_id=e1;
    ASSERT NOT EXISTS(SELECT FROM public.visible_ticket WHERE source_event_id=e1);
    ASSERT original.company_uuid=company_one AND original.ticket_number='00000001';
    ASSERT original.status='미처리' AND original.title='배송 문의' AND original.assignee_uuid IS NULL;
    ASSERT original.sentiment_status='pending' AND original.category_status='pending';
    ASSERT original.created_at=original.updated_at AND original.created_at='2026-01-02T03:04:05Z';
    ASSERT NOT EXISTS(SELECT FROM voc.ticket_projection_issue WHERE event_id=e1);
    ASSERT (SELECT body=E'\n  배송 문의!\n다음 줄' AND created_at=original.created_at
            FROM public.ticket_message WHERE source_event_id=e1);
    ASSERT (SELECT count(*)=3 FROM voc.ticket_analysis_outbox WHERE event_id=e1);

    -- Replays and role corrections preserve independent staff workflow and the first number.
    UPDATE public.ticket SET status='보류' WHERE ticket_uuid=original.ticket_uuid;
    PERFORM voc.project_ticket(e1);
    UPDATE voc.silver_messages SET message_kind='unknown' WHERE event_id=e1;
    ASSERT NOT EXISTS(SELECT FROM public.visible_ticket WHERE source_event_id=e1);
    ASSERT NOT EXISTS(SELECT FROM voc.ticket_analysis_outbox WHERE event_id=e1);
    UPDATE voc.silver_messages SET message_kind='customer_inquiry' WHERE event_id=e1;
    ASSERT (SELECT ticket_uuid=original.ticket_uuid AND ticket_number='00000001' AND status='보류'
            FROM public.ticket WHERE source_event_id=e1);
    ASSERT NOT EXISTS(SELECT FROM public.visible_ticket WHERE source_event_id=e1);
    ASSERT (SELECT count(*)=1 FROM public.ticket_message WHERE source_event_id=e1);
    ASSERT (SELECT count(*)=3 FROM voc.ticket_analysis_outbox WHERE event_id=e1);

    UPDATE voc.silver_messages SET body_version=2, body_text='승인된 본문 정정' WHERE event_id=e1;
    ASSERT (SELECT body_version=2 AND body='승인된 본문 정정' FROM public.ticket_message WHERE source_event_id=e1);
    ASSERT (SELECT array_agg(model_text ORDER BY body_version)=ARRAY[E'\n  배송 문의!\n다음 줄','승인된 본문 정정']
            FROM voc.model_text_history WHERE event_id=e1);
    ASSERT (SELECT count(*)=6 FROM voc.ticket_analysis_outbox WHERE event_id=e1);

    e2 := pg_temp.test_mail('fixture-a',registered_mailbox,'second','customer_inquiry',upper(customer_mail));
    ASSERT (SELECT customer_uuid=original.customer_uuid AND ticket_number='00000002' FROM public.ticket WHERE source_event_id=e2);
    e_other := pg_temp.test_mail('fixture-a',registered_mailbox,'staff','staff_reply',customer_mail);
    ASSERT NOT EXISTS(SELECT FROM public.ticket WHERE source_event_id=e_other);
    missing := pg_temp.test_mail('fixture-a',registered_mailbox,'no-sender','customer_inquiry',NULL);
    ASSERT (SELECT reason='missing_sender' FROM voc.ticket_projection_issue WHERE event_id=missing);
    ASSERT NOT EXISTS(SELECT FROM public.ticket WHERE source_event_id=missing);

    INSERT INTO voc.ticket_source_connection VALUES('fixture-b','gmail',registered_mailbox,'',company_two,channel_two);
    e3 := pg_temp.test_mail('fixture-b',registered_mailbox,'other-tenant','customer_inquiry',customer_mail);
    ASSERT (SELECT customer_uuid<>original.customer_uuid AND company_uuid=company_two AND ticket_number='00000001'
            FROM public.ticket WHERE source_event_id=e3);
    INSERT INTO public.channel(company_uuid,name,type) VALUES(company_one,'테스트 디스코드','discord')
        RETURNING channel_uuid INTO discord_channel;
    INSERT INTO voc.ticket_source_connection VALUES('fixture-a','discord','server','room',company_one,discord_channel);
    INSERT INTO voc.bronze_messages VALUES(discord_event,'fixture-a','discord','server','message','discord_history',
        now(),'{}','{}');
    INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,source_message_id,
        source_channel_id,source_conversation_id,message_kind,sender_ref,body_text,body_version,received_at,source_metadata)
    VALUES(4,discord_event,'fixture-a','discord','server','message','room','message','customer_inquiry',
           'user','배송 문의',1,now(),'{}');
    SELECT ticket_uuid INTO STRICT discord_ticket FROM public.ticket WHERE source_event_id=discord_event;
    INSERT INTO public.ticket_duplicate_candidate VALUES(discord_ticket,original.ticket_uuid) ON CONFLICT DO NOTHING;
    INSERT INTO public.ticket_duplicate_candidate VALUES(original.ticket_uuid,discord_ticket) ON CONFLICT DO NOTHING;
    ASSERT (SELECT count(*)=1 FROM public.ticket_duplicate_candidate
            WHERE ticket_uuid=least(original.ticket_uuid,discord_ticket) AND candidate_ticket_uuid=greatest(original.ticket_uuid,discord_ticket));
    BEGIN
        INSERT INTO public.ticket_duplicate_candidate SELECT original.ticket_uuid,ticket_uuid FROM public.ticket WHERE source_event_id=e2;
        RAISE EXCEPTION 'same-channel candidate accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO public.ticket_duplicate_candidate SELECT discord_ticket,ticket_uuid FROM public.ticket WHERE source_event_id=e3;
        RAISE EXCEPTION 'cross-company candidate accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        UPDATE public.ticket SET channel_uuid=channel_two WHERE ticket_uuid=original.ticket_uuid;
        RAISE EXCEPTION 'cross-company channel assignment accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    ASSERT length(voc.ticket_display_title(repeat('긴',70),'본문'))=50;
    ASSERT length(voc.ticket_display_title(NULL,repeat('긴',70)))=40;
    ASSERT voc.ticket_display_title(NULL,'!!!')='문의';
END $$;
ROLLBACK;
