import type { Sql } from "./db.ts";

export const SYSTEM = "system";

export interface Actor {
  id: string;
  email: string;
}

/** Add an audit record (pass a transaction to commit it with the caller's changes). */
export async function audit(
  sql: Sql,
  action: string,
  targetType: string,
  targetId: string | null,
  { actor = SYSTEM, ...details }: { actor?: Actor | string; [key: string]: unknown } = {},
): Promise<void> {
  const actorName = typeof actor === "string" ? actor : actor.email;
  const actorId = typeof actor === "string" ? null : actor.id;
  await sql`
    insert into audit_log (actor, actor_user_id, action, target_type, target_id, details)
    values (${actorName}, ${actorId}, ${action}, ${targetType}, ${targetId}, ${sql.json(details as never)})
  `;
}
