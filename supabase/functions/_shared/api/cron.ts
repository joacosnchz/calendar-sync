// The `cron` function: pg_cron POSTs /cron/sweep every minute with the shared secret.

import { Hono } from "../deps.ts";
import { timingSafeEqual } from "../security.ts";
import type { Services } from "../services.ts";
import { sweep } from "../sync/sweeps.ts";
import { type AppEnv, HttpError, installErrorHandler } from "./common.ts";

export function createCronApp(services: Services): Hono<AppEnv> {
  const app = new Hono<AppEnv>();
  installErrorHandler(app);

  app.post("/cron/sweep", async (c) => {
    const secret = services.settings.cronSecret;
    if (!secret) throw new HttpError(503, "CRON_SECRET is not configured");
    if (!timingSafeEqual(c.req.header("x-cron-secret") ?? "", secret)) throw new HttpError(401, "Bad cron secret");

    // ?wait=1 runs the sweep before answering (for operators and tests). pg_cron doesn't
    // wait: the sweep continues after the 202.
    if (c.req.query("wait") === "1") return c.json(await sweep(services));
    services.background(
      sweep(services).then((summary) => console.info(`sweep ${JSON.stringify(summary)}`)),
    );
    return c.json({ status: "started" }, 202);
  });

  return app;
}
