-- Synthetic tenant, domain and channel for this isolated database.
INSERT INTO public.domain(domain_uuid, name)
VALUES ('11111111-1111-4111-8111-111111111111','로컬 시험 도메인') ON CONFLICT DO NOTHING;
INSERT INTO public.company(company_uuid, email_domain, name, domain_uuid)
VALUES ('22222222-2222-4222-8222-222222222222','canary.example.test','로컬 시연 회사',
        '11111111-1111-4111-8111-111111111111') ON CONFLICT DO NOTHING;
INSERT INTO public.channel(channel_uuid, company_uuid, name, type)
VALUES ('33333333-3333-4333-8333-333333333333','22222222-2222-4222-8222-222222222222',
        '시험 Gmail','gmail') ON CONFLICT DO NOTHING;
INSERT INTO voc.ticket_source_connection(company_id, channel, connection_id, source_channel_id, company_uuid, channel_uuid)
VALUES ('canary-demo','gmail','demo-inbox','','22222222-2222-4222-8222-222222222222',
        '33333333-3333-4333-8333-333333333333') ON CONFLICT DO NOTHING;
INSERT INTO public.category(category_uuid, domain_uuid, name)
VALUES ('44444444-4444-4444-8444-444444444444','11111111-1111-4111-8111-111111111111','배송') ON CONFLICT DO NOTHING;
INSERT INTO voc.classification_category_map(domain_uuid, label, category_uuid)
VALUES ('11111111-1111-4111-8111-111111111111','delivery','44444444-4444-4444-8444-444444444444') ON CONFLICT DO NOTHING;
