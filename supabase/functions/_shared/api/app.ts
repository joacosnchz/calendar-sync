// The web app's backend: /api/* (user + operator API) and /auth/* (OAuth flows). Served by
// the `api` and `auth` functions, which the Next.js app proxies same-origin so cookies are
// first-party.

import { Hono } from "../deps.ts";
import type { Services } from "../services.ts";
import { adminRoutes } from "./admin.ts";
import { authRoutes } from "./auth.ts";
import { type AppEnv, installErrorHandler } from "./common.ts";
import { meRoutes } from "./me.ts";

export function createApiApp(services: Services): Hono<AppEnv> {
  const app = new Hono<AppEnv>();
  installErrorHandler(app);
  app.use(async (c, next) => {
    c.set("services", services);
    await next();
  });

  app.get("/api/healthz", (c) => c.json({ status: "ok" }));
  app.get("/api/readyz", async (c) => {
    try {
      await services.sql`select 1`;
    } catch (err) {
      return c.json({ status: "unavailable", error: (err as Error).message }, 503);
    }
    return c.json({ status: "ok" });
  });

  app.route("/api/admin", adminRoutes());
  app.route("/api", meRoutes());
  app.route("/auth", authRoutes());
  return app;
}
