CREATE OR REPLACE FUNCTION vault_finalize_import_batch(
    p_job_pk bigint,
    p_worker_id text,
    p_batch_size integer,
    p_lease_seconds integer
)
RETURNS TABLE (
    processed bigint,
    committed bigint,
    skipped bigint,
    failed bigint,
    accounted_through bigint
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_job vault_importjob%ROWTYPE;
    v_now timestamptz;
    v_finished timestamptz;
    v_old_checkpoint bigint;
    v_new_checkpoint bigint;
    v_picked bigint := 0;
    v_updated bigint := 0;
    v_committed bigint := 0;
    v_skipped bigint := 0;
    v_failed bigint := 0;
    v_previous_terminal bigint := 0;
    v_previous_committed bigint := 0;
    v_previous_skipped bigint := 0;
    v_previous_failed bigint := 0;
    v_attempted_delta bigint := 0;
    v_committed_delta bigint := 0;
    v_skipped_delta bigint := 0;
    v_failed_delta bigint := 0;
    v_rel record;
BEGIN
    IF p_batch_size IS NULL OR p_batch_size < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Import finalization batch size must be positive.';
    END IF;

    PERFORM set_config('synchronous_commit', 'on', true);

    SELECT *
      INTO v_job
      FROM vault_importjob
     WHERE id = p_job_pk
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002',
            MESSAGE = 'Import job does not exist.';
    END IF;

    v_now := clock_timestamp();
    IF v_job.worker_id IS DISTINCT FROM p_worker_id
       OR v_job.lease_expires_at IS NULL
       OR v_job.lease_expires_at <= v_now
       OR v_job.status NOT IN ('processing', 'paused', 'cancelled') THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'Import worker no longer owns this job.';
    END IF;

    v_old_checkpoint := COALESCE(
        NULLIF(v_job.last_checkpoint ->> 'accounted_through', '')::bigint,
        0
    );

    CREATE TEMP TABLE IF NOT EXISTS vault_import_finalize_work (
        import_record_id bigint PRIMARY KEY,
        source_position bigint NOT NULL,
        previous_status text NOT NULL,
        url text NOT NULL,
        account_type text NOT NULL,
        ident text NOT NULL,
        secret text NOT NULL,
        ip text,
        url_id bigint,
        email_id bigint,
        user_id bigint,
        password_id bigint,
        ip_id bigint,
        fingerprint text,
        first_position bigint
    ) ON COMMIT DROP;
    TRUNCATE TABLE pg_temp.vault_import_finalize_work;

    CREATE TEMP TABLE IF NOT EXISTS vault_import_finalize_new_accounts (
        fingerprint text PRIMARY KEY,
        account_id bigint NOT NULL
    ) ON COMMIT DROP;
    TRUNCATE TABLE pg_temp.vault_import_finalize_new_accounts;

    CREATE TEMP TABLE IF NOT EXISTS vault_import_finalize_unique (
        url text NOT NULL,
        account_type text NOT NULL,
        ident text NOT NULL,
        secret text NOT NULL,
        ip text,
        url_id bigint,
        email_id bigint,
        user_id bigint,
        password_id bigint,
        ip_id bigint,
        fingerprint text,
        first_position bigint
    ) ON COMMIT DROP;
    TRUNCATE TABLE pg_temp.vault_import_finalize_unique;

    -- Lock and select only narrow ledger keys here. The staged payload is
    -- fetched after selection so PostgreSQL does not spool credential values
    -- into the materialized CTE's tuplestore for large batches.
    WITH picked AS MATERIALIZED (
        SELECT
            s.import_record_id,
            r.source_position,
            r.status AS previous_status
        FROM vault_import_stage_record AS s
        JOIN vault_importrecord AS r
          ON r.id = s.import_record_id
        WHERE r.import_job_id = p_job_pk
          AND (
                r.status IN ('pending', 'processing')
                OR (r.status = 'failed' AND r.retryable)
          )
        ORDER BY r.source_position
        LIMIT p_batch_size
        FOR UPDATE OF s, r
    )
    INSERT INTO pg_temp.vault_import_finalize_work (
        import_record_id,
        source_position,
        previous_status,
        url,
        account_type,
        ident,
        secret,
        ip
    )
    SELECT
        picked.import_record_id,
        picked.source_position,
        picked.previous_status,
        s.url,
        s.account_type,
        s.ident,
        s.secret,
        s.ip
    FROM picked
    JOIN vault_import_stage_record AS s
      ON s.import_record_id = picked.import_record_id;

    -- The CTE's ORDER BY determines which source positions fit within the
    -- batch limit. The insert order into this private temp table is irrelevant:
    -- later stages compute first_position and checkpoint continuity explicitly.
    GET DIAGNOSTICS v_picked = ROW_COUNT;

    IF v_picked = 0 THEN
        -- A terminal ledger outcome must never retain a credential-bearing
        -- payload. This also repairs leftovers from an interrupted upgrade.
        DELETE FROM vault_import_stage_record AS s
        USING vault_importrecord AS r
        WHERE r.id = s.import_record_id
          AND r.import_job_id = p_job_pk
          AND r.status IN ('committed', 'skipped', 'failed');

        RETURN QUERY
        SELECT 0::bigint, 0::bigint, 0::bigint, 0::bigint,
               v_old_checkpoint;
        RETURN;
    END IF;

    ANALYZE pg_temp.vault_import_finalize_work;

    -- Canonical values have already passed parse_line() and the application's
    -- existing canonicalizers. Unique constraints remain authoritative when
    -- multiple jobs concurrently stage the same reference value.
    INSERT INTO vault_url (name, created_at, updated_at)
    SELECT DISTINCT url, v_now, v_now
      FROM pg_temp.vault_import_finalize_work
     ORDER BY url
    ON CONFLICT (name) DO NOTHING;

    INSERT INTO vault_pass (name, created_at, updated_at)
    SELECT DISTINCT secret, v_now, v_now
      FROM pg_temp.vault_import_finalize_work
     ORDER BY secret
    ON CONFLICT (name) DO NOTHING;

    INSERT INTO vault_email (name, created_at, updated_at)
    SELECT DISTINCT ident, v_now, v_now
      FROM pg_temp.vault_import_finalize_work
     WHERE account_type = 'email'
     ORDER BY ident
    ON CONFLICT (name) DO NOTHING;

    INSERT INTO vault_user (name, created_at, updated_at)
    SELECT DISTINCT ident, v_now, v_now
      FROM pg_temp.vault_import_finalize_work
     WHERE account_type = 'user'
     ORDER BY ident
    ON CONFLICT (name) DO NOTHING;

    INSERT INTO vault_ip (name, created_at, updated_at)
    SELECT DISTINCT ip, v_now, v_now
      FROM pg_temp.vault_import_finalize_work
     WHERE ip IS NOT NULL
     ORDER BY ip
    ON CONFLICT (name) DO NOTHING;

    -- The old importer explicitly refreshed reference tables whose statistics
    -- still said "empty". Retain that protection against catastrophic nested
    -- loop plans on a restored or recently purged database.
    FOR v_rel IN
        SELECT n.nspname, c.relname
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE c.oid = ANY (ARRAY[
                   to_regclass('vault_email')::oid,
                   to_regclass('vault_ip')::oid,
                   to_regclass('vault_pass')::oid,
                   to_regclass('vault_url')::oid,
                   to_regclass('vault_user')::oid
               ])
           AND c.reltuples <= 0
           AND pg_relation_size(c.oid) >= 131072
         ORDER BY c.relname
    LOOP
        EXECUTE format('ANALYZE %I.%I', v_rel.nspname, v_rel.relname);
    END LOOP;

    -- Resolve references once per distinct credential tuple instead of once
    -- per source line. Repeated source lines still get individual ledger
    -- outcomes below, but duplicate pairs no longer multiply reference-index
    -- probes and fingerprint calculations.
    WITH unique_payloads AS MATERIALIZED (
        SELECT
            url,
            account_type,
            ident,
            secret,
            ip,
            min(source_position) AS first_source_position
        FROM pg_temp.vault_import_finalize_work
        GROUP BY url, account_type, ident, secret, ip
    ), resolved AS MATERIALIZED (
        SELECT
            w.url,
            w.account_type,
            w.ident,
            w.secret,
            w.ip,
            w.first_source_position,
            u.id AS url_id,
            e.id AS email_id,
            us.id AS user_id,
            p.id AS password_id,
            i.id AS ip_id,
            CASE
                WHEN u.id IS NOT NULL
                 AND p.id IS NOT NULL
                 AND (
                       (w.account_type = 'email' AND e.id IS NOT NULL)
                       OR (w.account_type = 'user' AND us.id IS NOT NULL)
                 )
                THEN encode(
                    sha256(convert_to(
                        coalesce(u.id::text, '') || '|' || w.account_type || '|' ||
                        coalesce(e.id::text, '') || '|' ||
                        coalesce(us.id::text, '') || '|' ||
                        coalesce(p.id::text, '') || '|' ||
                        coalesce(i.id::text, ''),
                        'UTF8'
                    )),
                    'hex'
                )
                ELSE NULL
            END AS fingerprint
        FROM unique_payloads AS w
        LEFT JOIN vault_url AS u ON u.name = w.url
        LEFT JOIN vault_pass AS p ON p.name = w.secret
        LEFT JOIN vault_email AS e
          ON w.account_type = 'email' AND e.name = w.ident
        LEFT JOIN vault_user AS us
          ON w.account_type = 'user' AND us.name = w.ident
        LEFT JOIN vault_ip AS i ON i.name = w.ip
    ), resolved_with_first AS (
        SELECT
            r.*,
            CASE WHEN r.fingerprint IS NOT NULL
                 THEN min(r.first_source_position)
                      OVER (PARTITION BY r.fingerprint)
                 ELSE NULL
            END AS first_position
        FROM resolved AS r
    )
    INSERT INTO pg_temp.vault_import_finalize_unique (
        url, account_type, ident, secret, ip,
        url_id, email_id, user_id, password_id, ip_id,
        fingerprint, first_position
    )
    SELECT
        url, account_type, ident, secret, ip,
        url_id, email_id, user_id, password_id, ip_id,
        fingerprint, first_position
    FROM resolved_with_first;

    ANALYZE pg_temp.vault_import_finalize_unique;

    UPDATE pg_temp.vault_import_finalize_work AS w
       SET url_id = u.url_id,
           email_id = u.email_id,
           user_id = u.user_id,
           password_id = u.password_id,
           ip_id = u.ip_id,
           fingerprint = u.fingerprint,
           first_position = u.first_position
      FROM pg_temp.vault_import_finalize_unique AS u
     WHERE w.url = u.url
       AND w.account_type = u.account_type
       AND w.ident = u.ident
       AND w.secret = u.secret
       AND w.ip IS NOT DISTINCT FROM u.ip;

    -- Only one candidate per fingerprint reaches the Account insert. Global
    -- uniqueness resolves collisions with existing vault rows and concurrent
    -- jobs; RETURNING identifies the new records for first-source-line ledger
    -- attribution below.
    WITH candidates AS MATERIALIZED (
        SELECT DISTINCT ON (fingerprint)
            url_id,
            account_type,
            email_id,
            user_id,
            password_id,
            ip_id,
            fingerprint,
            first_position
        FROM pg_temp.vault_import_finalize_unique
        WHERE fingerprint IS NOT NULL
        ORDER BY fingerprint, first_position
    ), inserted AS (
        INSERT INTO vault_account (
            url_id,
            account_type,
            email_id,
            user_id,
            password_id,
            ip_id,
            fingerprint,
            created_at,
            updated_at
        )
        SELECT
            url_id,
            account_type,
            email_id,
            user_id,
            password_id,
            ip_id,
            fingerprint,
            v_now,
            v_now
        FROM candidates
        ORDER BY fingerprint
        ON CONFLICT (fingerprint) DO NOTHING
        RETURNING fingerprint, id
    )
    INSERT INTO pg_temp.vault_import_finalize_new_accounts (
        fingerprint, account_id
    )
    SELECT fingerprint, id FROM inserted;

    -- Work rows were picked for this job while locking their ledger records.
    -- Keep the job predicate explicit so the composite (job, status) index
    -- bounds this update when other jobs have pending or retryable rows. This
    -- single-use projection is intentionally foldable to avoid materializing a
    -- large per-row outcome spool before the UPDATE.
    WITH outcomes AS (
        SELECT
            w.import_record_id,
            w.source_position,
            w.first_position,
            w.fingerprint,
            a.id AS account_id,
            n.account_id AS newly_inserted_account_id,
            CASE
                WHEN w.fingerprint IS NULL THEN 'failed'
                WHEN n.account_id IS NOT NULL
                 AND w.source_position = w.first_position THEN 'committed'
                ELSE 'skipped'
            END AS outcome_status
        FROM pg_temp.vault_import_finalize_work AS w
        LEFT JOIN vault_account AS a
          ON a.fingerprint = w.fingerprint
        LEFT JOIN pg_temp.vault_import_finalize_new_accounts AS n
          ON n.fingerprint = w.fingerprint
    )
    UPDATE vault_importrecord AS r
       SET status = o.outcome_status,
           account_id = o.account_id,
           account_fingerprint = coalesce(o.fingerprint, ''),
           error_message = '',
           error_category = CASE o.outcome_status
               WHEN 'skipped' THEN 'duplicate_fingerprint'
               WHEN 'failed' THEN 'missing_reference'
               ELSE ''
           END,
           retryable = false,
           processed_at = v_now
      FROM outcomes AS o
     WHERE r.id = o.import_record_id
       AND r.import_job_id = p_job_pk
       AND (
             r.status IN ('pending', 'processing')
             OR (r.status = 'failed' AND r.retryable)
       );

    GET DIAGNOSTICS v_updated = ROW_COUNT;

    SELECT
        count(*) FILTER (WHERE r.status = 'committed'),
        count(*) FILTER (WHERE r.status = 'skipped'),
        count(*) FILTER (WHERE r.status = 'failed'),
        count(*) FILTER (
            WHERE w.previous_status IN ('committed', 'skipped', 'failed')
        ),
        count(*) FILTER (WHERE w.previous_status = 'committed'),
        count(*) FILTER (WHERE w.previous_status = 'skipped'),
        count(*) FILTER (WHERE w.previous_status = 'failed')
      INTO
        v_committed,
        v_skipped,
        v_failed,
        v_previous_terminal,
        v_previous_committed,
        v_previous_skipped,
        v_previous_failed
      FROM pg_temp.vault_import_finalize_work AS w
      JOIN vault_importrecord AS r ON r.id = w.import_record_id;

    v_attempted_delta := v_updated - v_previous_terminal;
    v_committed_delta := v_committed - v_previous_committed;
    v_skipped_delta := v_skipped - v_previous_skipped;
    v_failed_delta := v_failed - v_previous_failed;

    DELETE FROM vault_import_stage_record AS s
    USING pg_temp.vault_import_finalize_work AS w
    WHERE s.import_record_id = w.import_record_id;

    -- Advance only the contiguous terminal prefix. The recursive step stops
    -- at the first missing or nonterminal source position, so later outcomes
    -- never leap over a checkpoint gap.
    WITH RECURSIVE contiguous AS (
        SELECT r.source_position, r.status
        FROM vault_importrecord AS r
        WHERE r.import_job_id = p_job_pk
          AND r.source_position = v_old_checkpoint + 1
        UNION ALL
        SELECT r.source_position, r.status
        FROM contiguous AS c
        JOIN vault_importrecord AS r
          ON r.import_job_id = p_job_pk
         AND r.source_position = c.source_position + 1
        WHERE c.status IN ('committed', 'skipped', 'failed')
    )
    SELECT coalesce(
        max(source_position) FILTER (
            WHERE status IN ('committed', 'skipped', 'failed')
        ),
        v_old_checkpoint
    )
      INTO v_new_checkpoint
      FROM contiguous;

    -- The set-based batch may run longer than the lease (especially while
    -- search indexes are suspended). Renew from completion time, not the
    -- transaction's start time, before releasing the job-row lock.
    v_finished := clock_timestamp();

    UPDATE vault_importjob
       SET total_attempted = greatest(0, total_attempted + v_attempted_delta),
           total_committed = greatest(0, total_committed + v_committed_delta),
           total_skipped = greatest(0, total_skipped + v_skipped_delta),
           total_failed = greatest(0, total_failed + v_failed_delta),
           last_checkpoint =
               (CASE
                   WHEN jsonb_typeof(last_checkpoint) = 'object'
                   THEN last_checkpoint
                   ELSE '{}'::jsonb
                END)
               || jsonb_build_object(
                   'accounted_through', v_new_checkpoint,
                   'updated_at', v_finished
               ),
           stage = 'finalizing',
           last_activity_at = v_finished,
           lease_expires_at = v_finished + make_interval(secs => p_lease_seconds)
     WHERE id = p_job_pk;

    RETURN QUERY
    SELECT v_updated,
           v_committed,
           v_skipped,
           v_failed,
           v_new_checkpoint;
END;
$$;
