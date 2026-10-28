-- Local stack only (`supabase start` / `supabase db reset`): where pg_cron reaches the
-- `cron` Edge Function from inside the Supabase network, and the shared secret it sends.
-- Must match CRON_SECRET in supabase/functions/.env. Hosted projects set their own values
-- (see README.md, "Deploying to a hosted Supabase project").
select vault.create_secret('http://kong:8000/functions/v1', 'calsync_functions_url');
select vault.create_secret('local-dev-cron-secret', 'calsync_cron_secret');
