// Runs sync work (formerly arq jobs) and centralises what happens when it fails:
// throttling and transient errors are retried a few times in place and otherwise left for
// the sweep, a dead delegated grant pauses the source, anything else is recorded for the
// operator console. Tasks never throw, so they are safe to hand to `background`.

import { GraphError, GraphForbidden, GraphThrottled } from "../graph/client.ts";
import { TokenError } from "../graph/tokens.ts";
import { TransportError } from "../http.ts";
import type { Services } from "../services.ts";
import { sleep } from "../timeutil.ts";
import { markReauthRequired } from "./lifecycle.ts";

// Attempts made in place before leaving a transient failure to the sweep.
const ATTEMPTS = 3;
const BACKOFF_SECONDS = [2, 5];
// Throttling is waited out in place while each Retry-After is at most this long and all of
// them add up to at most THROTTLE_BUDGET_SECONDS; longer throttles are left to the sweep.
const MAX_RETRY_AFTER_SECONDS = 30;
const THROTTLE_BUDGET_SECONDS = 60;

export interface TaskOptions {
  sourceId?: string | null;
  userId?: string | null;
  /** Make sure the work is picked up again later (by the sweep) after a throttled or
   * transient failure. */
  onTransient?: () => Promise<void>;
}

const jitter = () => Math.random() * 1000;

export async function recordError(
  services: Services,
  kind: string,
  err: unknown,
  { sourceId = null, userId = null }: { sourceId?: string | null; userId?: string | null } = {},
): Promise<void> {
  const message = ((err as Error)?.message || (err as Error)?.constructor?.name || String(err)).slice(0, 4000);
  const { sql } = services;
  let tenantPk: string | null = null;
  if (sourceId) {
    const [source] = await sql<{ tenant_pk: string; user_id: string }[]>`
      update linked_sources set last_error = ${message.slice(0, 2000)}, last_error_at = now()
      where id = ${sourceId}
      returning tenant_pk, user_id
    `;
    if (source) {
      tenantPk = source.tenant_pk;
      userId = source.user_id;
    } else {
      sourceId = null;
    }
  }
  await sql`
    insert into sync_errors (user_id, tenant_pk, source_id, kind, message)
    values (${userId}, ${tenantPk}, ${sourceId}, ${kind.slice(0, 64)}, ${message})
  `;
}

function reauthReason(err: TokenError): string {
  const detail = err.message.split(/\r?\n/)[0];
  const code = err.code && !detail.includes(err.code) ? ` (${err.code})` : "";
  return `Sign in again to reconnect this calendar. Microsoft said: ${detail}${code}`;
}

export async function runTask(
  services: Services,
  name: string,
  fn: () => Promise<string>,
  opts: TaskOptions = {},
): Promise<string> {
  const sourceId = opts.sourceId ?? null;
  let attempt = 0;
  let throttledFor = 0;
  for (;;) {
    try {
      const result = await fn();
      console.info(`task ${name}${sourceId ? ` source=${sourceId}` : ""}: ${result}`);
      return result;
    } catch (err) {
      const retryIn = async (seconds: number): Promise<boolean> => {
        if (++attempt >= ATTEMPTS) return false;
        await sleep(seconds * 1000 + jitter());
        return true;
      };
      const giveUp = async (outcome: string, record = true): Promise<string> => {
        console.warn(`task ${name}${sourceId ? ` source=${sourceId}` : ""}: ${outcome}: ${(err as Error).message}`);
        await opts.onTransient?.();
        if (record) await recordError(services, name, err, { sourceId, userId: opts.userId });
        return outcome;
      };

      if (err instanceof GraphThrottled) {
        if (err.retryAfter <= MAX_RETRY_AFTER_SECONDS && throttledFor + err.retryAfter <= THROTTLE_BUDGET_SECONDS) {
          throttledFor += err.retryAfter;
          await sleep(err.retryAfter * 1000 + jitter());
          continue;
        }
        // Not an error: the sweep runs it once the tenant's back-off has passed.
        return await giveUp("throttled", false);
      }
      if (err instanceof TokenError) {
        const reauthSource = err.sourceId ?? sourceId;
        if (err.reauthRequired && reauthSource) {
          // Only the user can fix this (sign in again): pause the source, keep its events.
          await markReauthRequired(services, reauthSource, reauthReason(err));
          return "reauth-required";
        }
        if (err.transient) {
          if (await retryIn(BACKOFF_SECONDS[attempt] ?? 5)) continue;
          return await giveUp("retry-later");
        }
        await recordError(services, `${name}:token`, err, { sourceId, userId: opts.userId });
        console.error(`task ${name} failed: ${err.message}`);
        return "failed";
      }
      if (err instanceof GraphError && err.authFailure && err.sourceId) {
        // 401 even after refreshing the delegated token, or 403 on the user's own mailbox:
        // the grant no longer covers it. Only signing in again fixes that.
        await markReauthRequired(
          services,
          err.sourceId,
          `Microsoft Graph refused access (${err.status}): ${err.graphMessage}`,
        );
        return "reauth-required";
      }
      if (err instanceof TransportError || (err instanceof GraphError && err.transient)) {
        if (await retryIn(BACKOFF_SECONDS[attempt] ?? 5)) continue;
        return await giveUp("retry-later");
      }
      const kind = err instanceof GraphForbidden ? `${name}:forbidden` : name;
      console.error(`task ${name} failed`, err);
      await recordError(services, kind, err, { sourceId, userId: opts.userId });
      return "failed";
    }
  }
}
