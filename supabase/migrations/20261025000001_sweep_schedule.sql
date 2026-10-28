-- Periodic sweep (FR-6, FR-7): pg_cron calls the `cron` Edge Function every minute.
-- When nothing is due that is a handful of indexed queries. The sweep renews
-- subscriptions before they expire, recreates missing ones, runs the safety-net delta and
-- any resync that couldn't run at the time, and finishes unlinks and account deletions
-- that were interrupted.
--
-- The function's URL and shared secret come from Vault, so they are set per environment:
--   select vault.create_secret('https://<project-ref>.supabase.co/functions/v1', 'calsync_functions_url');
--   select vault.create_secret('<same value as the CRON_SECRET function secret>', 'calsync_cron_secret');
-- supabase/seed.sql sets both for the local stack.

create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

create schema if not exists calsync_private;
revoke all on schema calsync_private from public, anon, authenticated;

create function calsync_private.run_sweep() returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
    functions_url text;
    cron_secret text;
begin
    select decrypted_secret into functions_url
        from vault.decrypted_secrets where name = 'calsync_functions_url';
    select decrypted_secret into cron_secret
        from vault.decrypted_secrets where name = 'calsync_cron_secret';
    if functions_url is null or cron_secret is null then
        raise warning 'calsync sweep skipped: set the calsync_functions_url and calsync_cron_secret vault secrets';
        return null;
    end if;
    return net.http_post(
        url := rtrim(functions_url, '/') || '/cron/sweep',
        headers := jsonb_build_object('content-type', 'application/json', 'x-cron-secret', cron_secret),
        body := '{}'::jsonb,
        timeout_milliseconds := 10000
    );
end;
$$;
revoke execute on function calsync_private.run_sweep() from public, anon, authenticated;

select cron.schedule('calsync-sweep', '* * * * *', 'select calsync_private.run_sweep()');
