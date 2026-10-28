// /functions/v1/auth/* → /auth/* (sign-in, linking, reconnecting). Proxied by the web app;
// Microsoft redirects to {PUBLIC_WEB_URL}/auth/callback.
import { createApiApp } from "../_shared/api/app.ts";
import { getServices } from "../_shared/services.ts";

Deno.serve(createApiApp(await getServices()).fetch);
