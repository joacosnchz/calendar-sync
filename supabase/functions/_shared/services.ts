// Shared runtime dependencies for every function.

import { loadSettings, type Settings } from "./config.ts";
import { createSql, type Sql } from "./db.ts";
import { Cipher } from "./fernet.ts";
import { GraphClient } from "./graph/client.ts";
import { TokenService } from "./graph/tokens.ts";

export interface Services {
  settings: Settings;
  sql: Sql;
  cipher: Cipher;
  tokens: TokenService;
  graph: GraphClient;
  /** Keep `task` running after the response is sent (EdgeRuntime.waitUntil). Work that
   * used to be queued for a worker runs this way: webhook processing, initial syncs,
   * unlinks. */
  background: (task: Promise<unknown>) => void;
}

declare const EdgeRuntime: { waitUntil(promise: Promise<unknown>): void } | undefined;

function edgeBackground(task: Promise<unknown>): void {
  const settled = task.catch((err) => console.error("background task failed", err));
  if (typeof EdgeRuntime !== "undefined" && EdgeRuntime?.waitUntil) EdgeRuntime.waitUntil(settled);
}

export async function createServices(
  settings: Settings,
  opts: { sql?: Sql; background?: Services["background"] } = {},
): Promise<Services> {
  const sql = opts.sql ?? createSql(settings.databaseUrl);
  const cipher = await Cipher.create(settings.encryptionKeys);
  const tokens = new TokenService(settings, sql, cipher);
  return {
    settings,
    sql,
    cipher,
    tokens,
    graph: new GraphClient(settings, sql, tokens),
    background: opts.background ?? edgeBackground,
  };
}

let instance: Promise<Services> | undefined;

/** The services for this function instance, created on first use. */
export function getServices(): Promise<Services> {
  instance ??= createServices(loadSettings());
  instance.catch(() => (instance = undefined));
  return instance;
}
