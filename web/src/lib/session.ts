/**
 * AES-256-GCM cookie crypto + cookie helpers.
 *
 * Two cookies are managed here, both encrypted with the same key derived
 * from SESSION_COOKIE_KEY (a 64-char hex string = 32 random bytes):
 *
 *   mcp_session   — long-lived (max-age = refresh_token lifetime).
 *                   Payload: { access_token, refresh_token, expires_at,
 *                              client_id }.
 *   mcp_oauth_state — short-lived (max-age = 10min). Holds the in-flight
 *                   PKCE verifier + CSRF state + return-to URL between
 *                   /oauth/start and /oauth/callback. Deleted on callback
 *                   completion (success or failure).
 *
 * Two cookies (not one) because the lifecycles diverge: the OAuth state is
 * meaningless once the callback has fired, but the session is the asset we
 * want to keep alive across page navigations and browser restarts.
 *
 * Crypto choice: AES-256-GCM via Node's `crypto`. Authenticated, no
 * padding oracle pitfalls, runs in stock Node 22 without any npm
 * dependency. Format on the wire: base64url("iv (12B) | ciphertext | tag
 * (16B)"). Reading old cookies after a key rotation is impossible by
 * design — they fail to decrypt and we treat them as "no cookie."
 */
import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
} from 'node:crypto';

const ALGO = 'aes-256-gcm';
const IV_BYTES = 12;
const TAG_BYTES = 16;
const KEY_BYTES = 32;

export interface McpSession {
  access_token: string;
  refresh_token: string;
  expires_at: number; // unix seconds
  client_id: string;  // DCR-issued, needed for refresh + revoke
}

export interface McpOAuthState {
  verifier: string;    // PKCE code_verifier (~64 chars)
  state: string;       // CSRF token (~22 chars)
  return_to: string;   // where to bounce after callback
  client_id: string;   // DCR-issued client_id for THIS in-flight flow
}

/* ---- key handling --------------------------------------------------- */

let _cachedKey: Buffer | null = null;

function getKey(): Buffer {
  if (_cachedKey) return _cachedKey;
  const hex = process.env.SESSION_COOKIE_KEY;
  if (!hex) {
    throw new Error(
      'SESSION_COOKIE_KEY env var is required. Generate with: ' +
      'openssl rand -hex 32',
    );
  }
  const key = Buffer.from(hex, 'hex');
  if (key.length !== KEY_BYTES) {
    throw new Error(
      `SESSION_COOKIE_KEY must be ${KEY_BYTES * 2} hex chars ` +
      `(got ${hex.length}). Generate: openssl rand -hex 32`,
    );
  }
  _cachedKey = key;
  return key;
}

/* ---- encrypt / decrypt --------------------------------------------- */

function encrypt(plaintext: string): string {
  const iv = randomBytes(IV_BYTES);
  const cipher = createCipheriv(ALGO, getKey(), iv);
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, encrypted, tag]).toString('base64url');
}

function decrypt(token: string): string | null {
  try {
    const buf = Buffer.from(token, 'base64url');
    if (buf.length < IV_BYTES + TAG_BYTES + 1) return null;
    const iv = buf.subarray(0, IV_BYTES);
    const tag = buf.subarray(buf.length - TAG_BYTES);
    const ciphertext = buf.subarray(IV_BYTES, buf.length - TAG_BYTES);
    const decipher = createDecipheriv(ALGO, getKey(), iv);
    decipher.setAuthTag(tag);
    const plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
    return plaintext.toString('utf8');
  } catch {
    // Bad key, tampered cookie, or key rotation — treat as "no cookie."
    return null;
  }
}

/* ---- cookie I/O ----------------------------------------------------- */

const COOKIE_SESSION = 'mcp_session';
const COOKIE_OAUTH_STATE = 'mcp_oauth_state';

const COOKIE_BASE = 'Path=/; HttpOnly; Secure; SameSite=Lax';

function parseCookies(header: string | null): Record<string, string> {
  if (!header) return {};
  const out: Record<string, string> = {};
  for (const piece of header.split(/;\s*/)) {
    const eq = piece.indexOf('=');
    if (eq < 0) continue;
    const k = piece.slice(0, eq).trim();
    const v = piece.slice(eq + 1).trim();
    if (k) out[k] = decodeURIComponent(v);
  }
  return out;
}

export function readSession(request: Request): McpSession | null {
  const cookies = parseCookies(request.headers.get('cookie'));
  const raw = cookies[COOKIE_SESSION];
  if (!raw) return null;
  const plain = decrypt(raw);
  if (!plain) return null;
  try {
    return JSON.parse(plain) as McpSession;
  } catch {
    return null;
  }
}

export function readOAuthState(request: Request): McpOAuthState | null {
  const cookies = parseCookies(request.headers.get('cookie'));
  const raw = cookies[COOKIE_OAUTH_STATE];
  if (!raw) return null;
  const plain = decrypt(raw);
  if (!plain) return null;
  try {
    return JSON.parse(plain) as McpOAuthState;
  } catch {
    return null;
  }
}

/**
 * Build a Set-Cookie header value for `mcp_session`. max-age is derived
 * from session.expires_at + a 30-day refresh window (we want the cookie
 * to outlive the access token so refresh works on return visits).
 */
export function serializeSession(session: McpSession): string {
  const encrypted = encrypt(JSON.stringify(session));
  const maxAge = 30 * 24 * 3600; // 30 days
  return `${COOKIE_SESSION}=${encrypted}; Max-Age=${maxAge}; ${COOKIE_BASE}`;
}

/** Build a Set-Cookie header value for `mcp_oauth_state` (10min window). */
export function serializeOAuthState(state: McpOAuthState): string {
  const encrypted = encrypt(JSON.stringify(state));
  return `${COOKIE_OAUTH_STATE}=${encrypted}; Max-Age=600; ${COOKIE_BASE}`;
}

export function clearSessionCookie(): string {
  return `${COOKIE_SESSION}=; Max-Age=0; ${COOKIE_BASE}`;
}

export function clearOAuthStateCookie(): string {
  return `${COOKIE_OAUTH_STATE}=; Max-Age=0; ${COOKIE_BASE}`;
}

/**
 * Extract the `sub` claim from a JWT without verifying it.
 *
 * Safe because: the token was already validated by mcp.blender.bet when
 * issued, AND we only use the sub for read-only DB lookups (no privilege
 * is granted based on what we read here). For privileged operations the
 * token round-trips through mcp.blender.bet which re-validates.
 *
 * Returns null on any decode failure — caller should redirect to
 * /oauth/start to mint a fresh session.
 */
export function decodeJwtSub(jwt: string): string | null {
  const parts = jwt.split('.');
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(
      Buffer.from(parts[1], 'base64url').toString('utf8'),
    );
    const sub = payload?.sub;
    return typeof sub === 'string' && sub.length > 0 ? sub : null;
  } catch {
    return null;
  }
}
