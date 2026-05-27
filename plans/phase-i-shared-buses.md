# Phase I — shared buses + multi-user collaboration + LLM session visibility

## Context

End of Phase H, the bus is **per-Authentik-user**: `bus_manager.get_bus(user_id)`
is the entire isolation model. Every dispatch tool implicitly operates on
"my bus." Every `ClientInfo` lives in exactly one bus.

This works for single-user-with-multiple-Blenders (the user-rpm case
right now — open two Blenders, both register on the same bus, dispatch
tools auto-pick or ambiguity-error). It does NOT work for:

1. **Cross-user collaboration** — two humans want one human's Blender
   driven by the other human's Claude Code session. Today they have
   separate buses; no path to share.

2. **LLM-session visibility** — when multiple Claude sessions are
   connected to the same MCP server, none of them know about each
   other. The Phase H role gate blocks LLM clients from
   `bus_register_client`, so they never become visible.

3. **Team workflows** — "the marketing team's Blender" or "the
   prototyping bus" as shared shop resources, with multiple humans
   coming and going.

**Intended outcome:** users can create named buses, invite each other,
join by invitation; every dispatch tool accepts an optional `bus_id`
(defaulting to the caller's "personal" bus); LLM clients can opt-in to
be visible on a bus so cross-session coordination becomes possible.

## Approach

### Data model (new)

```
Bus
├── bus_id     UUID
├── name       str
├── description str
├── owner_user_id  str  (Authentik hashed_user_id)
├── is_personal  bool   (one personal bus per user, auto-created on first request)
├── created_at  datetime
└── revoked_at  datetime | None  (soft delete)

BusMembership
├── bus_id   UUID  → Bus.bus_id
├── user_id  str   (Authentik hashed_user_id)
├── role     enum  (owner | member | guest)
├── joined_at  datetime
├── revoked_at datetime | None
└── (primary key: bus_id + user_id; unique constraint)

BusInvitation
├── invitation_id  UUID
├── bus_id  UUID   → Bus.bus_id
├── invited_by  str  (user_id of inviter, must be owner or member)
├── code  str  (random short token — what gets shared out-of-band)
├── invitee_user_id  str | None  (if targeting a specific user)
├── role  enum  (member | guest — what role the joiner gets)
├── expires_at  datetime  (default: 24h)
├── consumed_at  datetime | None
└── consumed_by_user_id  str | None
```

**ClientInfo additions** (existing dataclass in `message_bus.py`):
```python
@dataclass
class ClientInfo:
    uuid: str
    client_type: str           # "blender" | "llm" | other
    label: Optional[str]       # NEW — human-readable hostname/scene/session
    bus_id: str                # NEW — which bus this client is registered on
    is_persistent: bool = False
    capabilities: list[str] = field(default_factory=list)
    group_id: Optional[str] = None
    owner_user_id: str         # NEW — the user that registered this client
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    session: Any = None
```

### Persistence — Postgres + SQLAlchemy + Alembic (DECIDED)

New service in `docker-compose.yml`: `blender-mcp-postgres` (UNIQUE
name per the CLAUDE.md DNS-collision rule — generic `postgres` on the
shared caddy fabric collides with other stacks). Postgres 16-alpine,
on the prod stack's `internal` network only (NOT the shared `caddy`
network — DB should never be reverse-proxied).

Why Postgres from day 1: skips a SQLite→Postgres migration later if
we ever need HA / multi-instance. Cost is one extra container in the
stack (~80 MB resident).

Schema migrations via Alembic. New module layout:
```
src/blender_mcp/storage/
├── __init__.py        # AsyncSession factory + engine
├── models.py          # SQLAlchemy 2.x async-style models (DeclarativeBase)
├── bus_repo.py        # async repository ops (create_bus, add_member, ...)
└── migrations/
    ├── env.py
    ├── script.py.mako
    └── versions/
        └── 20260527_0001_initial_bus_schema.py
```

Env vars (added to .env.example):
```
DATABASE_URL=postgresql+asyncpg://blender_mcp:CHANGEME@blender-mcp-postgres:5432/blender_mcp
POSTGRES_PASSWORD=CHANGEME    # consumed by the postgres container's init
```

The async adapter wraps the existing synchronous `bus_manager` API so
the call sites in `dispatch_component.py` + `bus_tools.py` don't all
need to be rewritten. `bus_manager` becomes a thin in-memory write-
through cache over `bus_repo`.

### Invitation flow — Code-based (DECIDED)

Bus owner runs `bus_invite_user(role)`, server returns a short token
(`BMI-7K2XYZABCD`). Owner shares it out-of-band (chat, email).
Recipient runs `bus_join(code)`. No need to know who the other person
is in Authentik terms.

Code format: `BMI-{base32(random_8_bytes).upper()[:10]}` — 10 chars,
~50 bits of entropy, unambiguous human-typeable. Expires 24h.
Single-use (atomic CONSUME → JOIN in one transaction).

Future: a username-based flow (`bus_invite_user(username='alice')`)
could be added once we have a directory-listing UI to make username
discovery cheap. Out of scope for Phase I.

### Personal-bus auto-provisioning

Today the server creates a bus for a user on first `bus_manager.get_bus(uid)`
call. After Phase I, that becomes a `is_personal=true` Bus with the
user as owner. Auto-created on first authenticated request that touches
the bus subsystem. The user can never delete their personal bus, and
they're always a member of it (can't `bus_leave` their personal bus).

### API surface

**New tools** (all in `bus_tools.py`):

| Tool | Role gate | Returns |
|---|---|---|
| `bus_list_buses` | shared (any auth) | All buses I'm a member of, with role + label |
| `bus_create_bus(name, description)` | shared | New bus_id; caller is owner |
| `bus_invite_user(bus_id, role)` | owner/member of bus | Invitation code |
| `bus_join(code)` | shared | bus_id joined |
| `bus_leave(bus_id)` | member (not owner of non-personal) | ok / cannot_leave_personal |
| `bus_revoke_member(bus_id, user_id)` | owner | ok |
| `bus_set_default(bus_id)` | member | The bus used when `bus_id` omitted from dispatch tools |

**Modified tools** (all dispatch tools in `dispatch_component.py`):
```python
async def get_scene_info(
    self,
    target_uuid: Optional[str] = None,
    bus_id: Optional[str] = None,        # NEW
    _timeout: float = DEFAULT_TIMEOUT_S,
    ctx: Context = None,
) -> str:
    return await self._call(ctx, "get_scene_info", {}, target_uuid, bus_id, _timeout)
```

`_call` resolves `bus_id` from prefs.default_bus or the user's
personal bus when omitted. `_pick_blender_target` widens to search
within the specified bus (not the caller's personal bus). Membership
check happens at `_call` entry: 403-equivalent if user isn't a
member of `bus_id`.

**Modified `bus_register_client`** (in `bus_tools.py`):
```python
async def register_client(
    self,
    client_uuid: str,
    client_type: str,
    bus_id: Optional[str] = None,        # NEW — defaults to user's default
    label: Optional[str] = None,         # NEW
    is_persistent: bool = False,
    capabilities: Optional[list[str]] = None,
    group_id: Optional[str] = None,
    ctx: Context = None,
) -> str:
    ...
```

### LLM session visibility (bundled)

Today: only the addon registers on the bus. LLM clients are invisible
to each other.

After Phase I: LLM clients can opt-in via `bus_register_session(...)`
— a new tool with role gate `llm-client` (mirror of the `addon` gate
on `bus_register_client`). Registers a ClientInfo with
`client_type="llm"`. Now `bus_list_available_clients` returns both
Blender clients AND LLM sessions in a single list.

Why opt-in (not auto-register on connect)? Because most MCP sessions
don't NEED visibility — they just want to dispatch. Auto-registering
would crowd the discovery list with ephemeral clients. The opt-in
makes "I want to be visible" an explicit decision.

Future possibility (deferred): `bus_send_message` from LLM client to
LLM client for cross-session signaling ("I'm starting a long render,
don't dispatch to that Blender"). Out of scope for Phase I.

### Addon-side changes

- `addon/preferences.py`: new field `default_bus_id` (initially empty
  = use personal bus); new field `label` (defaults to
  `f"Blender {bpy.app.version_string} on {socket.gethostname()}"`).
- `addon/client/bus_client.py`: pass `bus_id=prefs.default_bus_id`
  and `label=prefs.label` to the `bus_register_client` call.
- `addon/ui/panel.py`: new "Bus" dropdown above the Connect button
  — populated from `bus_list_buses()` on Login. Lets the user pick
  which bus to register on per-session.
- `addon/ui/operators.py`: `BLENDERMCP_OT_CreateBus`,
  `BLENDERMCP_OT_JoinBus` (prompts for code), `BLENDERMCP_OT_LeaveBus`
  operators wired to the sidebar.

### web/ UI for bus management

- `web/src/pages/buses.astro` (SSR, gated): list buses I'm a member
  of; per-bus inline "Invite" button (returns the code with a "Copy"
  affordance); "Leave" button.
- `web/src/pages/buses/[bus_id].astro` (SSR, gated): bus detail —
  members list with role badges, "Invite member" form, "Revoke" per
  member (owners only).
- Reuses the same forward_auth setup as `/login-complete`.

## Phases (each ships independently, no flag-flipping needed)

| # | Phase | Outcome | Verification |
|---|---|---|---|
| I1 | ClientInfo.label + addon sends it | `bus_list_available_clients` returns labels like "Blender 5.1 on rpm-bullet" | Gate: register two fake clients with different labels; list shows both with distinguishable names |
| I2 | Storage layer | SQLAlchemy models + alembic migration 0001; in-memory bus_manager becomes a write-through cache over the DB | Gate: stop+start server, observe membership persists |
| I3 | Bus + BusMembership tools + personal-bus auto-provisioning | `bus_list_buses`, `bus_create_bus`, `bus_set_default` work. Every user has a personal bus | Gate: two users each see only their own personal bus |
| I4 | Invitations | `bus_invite_user`, `bus_join`, `bus_leave`, `bus_revoke_member` work | Gate: user A creates shared bus, invites B, B accepts, B sees the bus in `bus_list_buses` |
| I5 | bus_id threaded through dispatch tools | Every `blender_*` tool accepts optional `bus_id`; auto-resolves to default bus | Gate: A registers a Blender on shared bus; B can dispatch to it |
| I6 | LLM session visibility | `bus_register_session(label, bus_id)` with `llm-client` role gate; `bus_list_available_clients` returns both | Gate: two `claude -p` sessions register on the same bus, each sees the other |
| I7 | Addon UI: bus picker | New sidebar dropdown + create/join/leave operators | Real-Blender check: two Blenders, two buses, route each to its bus |
| I8 | web/ UI: /buses + /buses/[id] | Astro SSR pages, forward_auth-gated, full CRUD for memberships | Manual click-through: invite via web, accept via addon, dispatch works |

## Critical files

| File | Action |
|---|---|
| `src/blender_mcp/storage/__init__.py` | NEW (I2) — async DB adapter |
| `src/blender_mcp/storage/models.py` | NEW (I2) — Bus, BusMembership, BusInvitation SQLAlchemy models |
| `src/blender_mcp/storage/migrations/` | NEW (I2) — alembic env + 0001 |
| `src/blender_mcp/message_bus.py` | EDIT (I1, I3) — ClientInfo.label/bus_id/owner_user_id; bus_manager becomes DB-backed |
| `src/blender_mcp/bus_tools.py` | EDIT (I3, I4, I6) — new bus_* tools |
| `src/blender_mcp/dispatch_component.py` | EDIT (I5) — bus_id param on every `blender_*` tool |
| `src/blender_mcp/client_role.py` | EDIT (I6) — `bus_register_session` allowed for `llm-client` role |
| `addon/preferences.py` | EDIT (I7) — default_bus_id + label fields |
| `addon/client/bus_client.py` | EDIT (I7) — pass label + bus_id to register_client |
| `addon/ui/panel.py` | EDIT (I7) — bus picker dropdown |
| `addon/ui/operators.py` | EDIT (I7) — Create/Join/Leave bus operators |
| `web/src/pages/buses.astro` | NEW (I8) |
| `web/src/pages/buses/[bus_id].astro` | NEW (I8) |
| `scripts/gate_i_shared_buses.py` | NEW (I3, I4, I5) — multi-user dispatch round-trip |
| `docker-compose.yml` | EDIT (I2) — mount SQLite db file as volume |

## Reuse

- `message_bus.Bus` class becomes the runtime cache for a DB-backed
  Bus row; the existing `register/unregister/route` methods keep
  their signatures. The change is internal to `bus_manager`.
- `examples/fake_blender_peer.py` already accepts `bus_register_client`
  with arbitrary client_type — minor extension to send `label` +
  `bus_id`.
- Authentik's hashed_user_id (already in JWT `sub` claim) is the
  membership identity. No need to introduce a separate user store.
- The Phase H role registry maps `client_id → role`. For I6, add
  `bus_register_session` to the set of tools allowed for
  `role=llm-client`.

## Verification

### Per-phase

```bash
uv run ruff check src/blender_mcp/

# I2 — storage round-trip
uv run python -m blender_mcp.storage.smoke  # create + read + restart-and-read

# I3-I5 — multi-user dispatch
uv run python scripts/gate_i_shared_buses.py
# (Spawns server + two fake peers under different user_ids; verifies
#  A can create shared bus, invite B, B joins, B dispatches to A's
#  Blender, A sees B's LLM session.)

# I7 — addon real test
# Edit > Preferences > Add-ons > BlenderMCP → Bus dropdown shows
# Personal + Shared; switching buses re-registers the client; the
# webclient at https://blender.bet/buses shows both with correct
# member counts.
```

### End-to-end (after I8)

1. User A: opens Blender, addon connects to their personal bus
2. User A: visits `blender.bet/buses`, clicks "Create bus" → "Team Demo"
3. User A: clicks "Invite member", copies the code (`BMI-XXX...`)
4. User A: sends code to User B via Slack
5. User B: visits `blender.bet/buses`, clicks "Join with code", pastes
6. User B: now sees "Team Demo" in their bus list
7. User B: in Blender, addon panel → Bus dropdown → Team Demo → Connect
8. User B: now has a Blender registered on Team Demo
9. User A: from Claude Code, `blender_get_scene_info` with
   `bus_id=<team-demo>` → returns User B's scene
10. User A's Claude session shows up in User B's `bus_list_available_clients`
    output (after User A opts in via `bus_register_session`)

## Risk register

| # | Risk | Mitigation |
|---|---|---|
| 1 | SQLite write contention under load | Single writer at a time is fine at our scale (≤100 dispatch/s). If we ever push past that, swap to Postgres without API changes. |
| 2 | Invitation code leakage (shared in wrong channel) | Codes expire 24h + single-use. Owner can `bus_revoke_invitation`. Short blast radius. |
| 3 | Cross-user privilege escalation via shared bus | Membership check in `_call` is the single gate. Bus owner can revoke any member. Every state-changing op logs `(user_id, bus_id, action)`. |
| 4 | LLM-session opt-in floods discovery list | Mitigate via TTL: ephemeral LLM sessions auto-unregister after N min idle. (`Phase I6+` polish.) |
| 5 | Personal-bus deletion edge case | API rejects `bus_revoke_member` on personal bus, `bus_leave` on personal bus. Storage layer's `is_personal=True` is a hard constraint. |
| 6 | Migration of existing in-memory state | Phase I2's first run treats existing bus_manager state as transient; users will need to reconnect addons. Document in commit + tag the change as breaking. |
| 7 | Multi-instance server (e.g., HA) split-brain | OUT OF SCOPE for I. Single-instance only. Document this constraint. If/when we need HA, swap SQLite → Postgres + add advisory locking. |

## Decisions (locked in)

1. ✅ **Storage**: Postgres 16 + SQLAlchemy 2.x async + Alembic
   migrations. New container `blender-mcp-postgres` on the prod
   stack's internal network.
2. ✅ **Invitation flow**: Code-based (`BMI-XXX...`). Username-based
   invitations deferred.
3. ✅ **Phase scope**: All 8 phases in one branch (`feat/shared-buses`).
   Open as draft PR after I2 lands (storage foundation) so the rest
   of the work is reviewable in flight.
4. **Personal-bus migration**: clean break. The first I2 deploy
   wipes the in-memory bus_manager state; users must reconnect
   addons (single re-login). Documented in the merge commit message.

## Estimated effort

8-15 hours of focused work, almost certainly across multiple
sessions. Suggested per-phase pacing:

| Phase | Estimate |
|---|---|
| I1 ClientInfo.label | 30 min |
| I2 Postgres + storage + migration | 2-3 hr |
| I3 Bus CRUD tools + personal-bus auto-provisioning | 1-2 hr |
| I4 Invitations (code-based) | 1 hr |
| I5 bus_id threaded through dispatch | 1-2 hr |
| I6 LLM session visibility | 1 hr |
| I7 Addon UI (bus picker + ops) | 1-2 hr |
| I8 Web UI (/buses, /buses/[id]) | 2-3 hr |
