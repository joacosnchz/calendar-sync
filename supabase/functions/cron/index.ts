// /functions/v1/cron/sweep → the periodic sweep, called by pg_cron.
import { createCronApp } from "../_shared/api/cron.ts";
import { getServices } from "../_shared/services.ts";

Deno.serve(createCronApp(await getServices()).fetch);
