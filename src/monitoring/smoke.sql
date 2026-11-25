BEGIN READ ONLY;
SET LOCAL ROLE voc_monitor_reader;
DO $$
BEGIN
    ASSERT has_table_privilege(current_user, 'voc_monitor.diagnostics', 'SELECT');
    ASSERT NOT EXISTS (
        SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE (n.nspname,c.relname) IN (('voc','bronze_messages'),('voc','silver_messages'),
                                       ('public','ticket'),('public','ticket_message'))
          AND has_table_privilege(current_user,c.oid,'SELECT')
    );
    ASSERT NOT has_schema_privilege(current_user, 'voc_monitor', 'CREATE');
    ASSERT NOT EXISTS (SELECT FROM voc_monitor.inference WHERE applied AND
                       (ended_at IS NULL OR error IS NOT NULL));
END $$;
SELECT * FROM voc_monitor.inventory;
ROLLBACK;
