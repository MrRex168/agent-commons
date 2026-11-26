-- Run only on the receiver through a private, read-only operator connection.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '2s';
SELECT json_build_object(
    'retained', (SELECT count(*) FROM a2a_tasks),
    'duplicate_groups', (
        SELECT count(*) FROM (
            SELECT 1 FROM a2a_tasks
            GROUP BY caller_root, recipient_agent_id, message_id HAVING count(*) > 1
        ) AS duplicate_tasks
    )
);
COMMIT;
