// /functions/v1/api/* → /api/* (user and operator API). Proxied by the web app.
import { createApiApp } from "../_shared/api/app.ts";
import { getServices } from "../_shared/services.ts";

Deno.serve(createApiApp(await getServices()).fetch);
