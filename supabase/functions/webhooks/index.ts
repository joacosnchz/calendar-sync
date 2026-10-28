// /functions/v1/webhooks/* → Microsoft Graph change and lifecycle notifications.
import { createWebhooksApp } from "../_shared/api/webhooks.ts";
import { getServices } from "../_shared/services.ts";

Deno.serve(createWebhooksApp(await getServices()).fetch);
