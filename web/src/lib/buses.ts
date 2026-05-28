/**
 * Read-only bus queries for the blender.bet SSR pages.
 *
 * We share the same Postgres instance as the MCP server's
 * blender_mcp.storage module (same DATABASE_URL pointed at the same
 * ``blender-mcp-postgres`` container). This keeps the web pages
 * trivial to ship: no second OAuth flow, no service tokens. For
 * mutations (create/join/leave) the pages link OUT to the addon UI
 * — those are infrequent + safer when paired with the real auth
 * flow + the in-memory client/job state the server already manages.
 *
 * Schema is owned by the MCP server's Alembic migrations
 * (src/blender_mcp/storage/migrations/). This module just reads.
 */

import pg from 'pg';

const { Pool } = pg;

let _pool: pg.Pool | null = null;

function pool(): pg.Pool {
  if (_pool === null) {
    const url = process.env.DATABASE_URL;
    if (!url) {
      throw new Error('DATABASE_URL not set — web/ needs Postgres for /buses');
    }
    // Strip the sqlalchemy ``+asyncpg`` / ``+psycopg`` driver suffix
    // node-postgres doesn't know about; everything after the scheme
    // is standard libpq URL.
    const cleaned = url
      .replace(/^postgresql\+asyncpg:\/\//, 'postgresql://')
      .replace(/^postgresql\+psycopg:\/\//, 'postgresql://');
    _pool = new Pool({
      connectionString: cleaned,
      max: 4,
      idleTimeoutMillis: 30_000,
    });
  }
  return _pool;
}

export interface BusListItem {
  bus_id: string;
  name: string;
  description: string | null;
  role: 'owner' | 'member' | 'guest';
  is_personal: boolean;
  owner_user_id: string;
  is_owned_by_me: boolean;
  created_at: string;
}

export interface MemberRow {
  user_id: string;
  role: 'owner' | 'member' | 'guest';
  joined_at: string;
  is_owner: boolean;
}

/**
 * Buses the given user is an active member of, ordered personal-first
 * then chronologically.
 */
export async function listBusesForUser(userId: string): Promise<BusListItem[]> {
  const result = await pool().query(
    `
    SELECT b.bus_id, b.name, b.description, m.role,
           b.is_personal, b.owner_user_id, b.created_at
    FROM bus_membership m
    JOIN bus b ON b.bus_id = m.bus_id
    WHERE m.user_id = $1
      AND m.revoked_at IS NULL
      AND b.revoked_at IS NULL
    ORDER BY b.is_personal DESC, b.created_at ASC
    `,
    [userId],
  );
  return result.rows.map((r: any) => ({
    bus_id: r.bus_id,
    name: r.name,
    description: r.description,
    role: r.role,
    is_personal: r.is_personal,
    owner_user_id: r.owner_user_id,
    is_owned_by_me: r.owner_user_id === userId,
    created_at: new Date(r.created_at).toISOString(),
  }));
}

/**
 * Get a single bus + verify the user is a member. Returns null if the
 * bus doesn't exist OR if the user isn't a member (treat as 404 in the
 * page — don't leak existence).
 */
export async function getBusForUser(
  busId: string,
  userId: string,
): Promise<BusListItem | null> {
  const result = await pool().query(
    `
    SELECT b.bus_id, b.name, b.description, m.role,
           b.is_personal, b.owner_user_id, b.created_at
    FROM bus b
    JOIN bus_membership m ON m.bus_id = b.bus_id AND m.user_id = $2
    WHERE b.bus_id = $1
      AND b.revoked_at IS NULL
      AND m.revoked_at IS NULL
    `,
    [busId, userId],
  );
  if (result.rowCount === 0) return null;
  const r = result.rows[0];
  return {
    bus_id: r.bus_id,
    name: r.name,
    description: r.description,
    role: r.role,
    is_personal: r.is_personal,
    owner_user_id: r.owner_user_id,
    is_owned_by_me: r.owner_user_id === userId,
    created_at: new Date(r.created_at).toISOString(),
  };
}

/**
 * Members of a bus, with role + joined_at. Anyone in the bus can list
 * other members.
 */
export async function listMembers(busId: string): Promise<MemberRow[]> {
  const result = await pool().query(
    `
    SELECT m.user_id, m.role, m.joined_at, b.owner_user_id
    FROM bus_membership m
    JOIN bus b ON b.bus_id = m.bus_id
    WHERE m.bus_id = $1
      AND m.revoked_at IS NULL
    ORDER BY (m.user_id = b.owner_user_id) DESC, m.joined_at ASC
    `,
    [busId],
  );
  return result.rows.map((r: any) => ({
    user_id: r.user_id,
    role: r.role,
    joined_at: new Date(r.joined_at).toISOString(),
    is_owner: r.user_id === r.owner_user_id,
  }));
}
