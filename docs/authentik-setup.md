# Authentik OAuth setup

How to provision a new BlenderMCP OAuth application in your Authentik
instance. Required when running with `AUTH_BACKEND=authentik` (the
default + production path).

Two paths: **API** (faster, scriptable) or **Admin UI** (more familiar).

## What you'll end up with

- An **OAuth2/OpenID Provider** in Authentik named "BlenderMCP OAuth"
- An **Application** named "BlenderMCP", slug `blender-mcp`
- A per-app OIDC discovery URL at
  `https://<your-authentik>/application/o/blender-mcp/.well-known/openid-configuration`
- A `client_id` + `client_secret` to put in your blender-mcp server's `.env`

## Path A — API (recommended for scripted setup)

You need an Authentik admin API token. The easiest way to get one when you
have shell access to the Authentik container:

```bash
# Generate a one-shot admin token via the ak shell
docker exec authentik-server ak shell -c "$(cat <<'PYEOF'
from authentik.core.models import User, Token, TokenIntents
user = User.objects.filter(username='akadmin').first()
existing = Token.objects.filter(user=user, identifier='blender-mcp-setup').first()
if existing: existing.delete()
tok = Token.objects.create(
    identifier='blender-mcp-setup',
    user=user,
    intent=TokenIntents.INTENT_API,
    description='Phase G OAuth setup for blender-mcp',
    expiring=False,
)
print(f'TOKEN={tok.key}')
PYEOF
)" | grep ^TOKEN
```

Then set:

```bash
export AK_URL="https://auth.your-authentik.example.com"
export AK_TOKEN="<the token from above>"
```

### 1. Look up reference PKs

```bash
# Authorization flow (default explicit-consent)
AUTH_FLOW_PK=$(curl -fsS \
  "${AK_URL}/api/v3/flows/instances/?slug=default-provider-authorization-explicit-consent" \
  -H "Authorization: Bearer ${AK_TOKEN}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['results'][0]['pk'])")

# Invalidation flow (for logout)
INVAL_FLOW_PK=$(curl -fsS \
  "${AK_URL}/api/v3/flows/instances/?slug=default-provider-invalidation-flow" \
  -H "Authorization: Bearer ${AK_TOKEN}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['results'][0]['pk'])")

# Signing key (any existing cert; default is the self-signed one)
SIGNING_KEY=$(curl -fsS \
  "${AK_URL}/api/v3/crypto/certificatekeypairs/?has_key=true" \
  -H "Authorization: Bearer ${AK_TOKEN}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['results'][0]['pk'])")

# Scope property mappings (openid, profile, email, offline_access)
SCOPE_PKS=$(curl -fsS \
  "${AK_URL}/api/v3/propertymappings/provider/scope/" \
  -H "Authorization: Bearer ${AK_TOKEN}" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
wanted = {'openid', 'profile', 'email', 'offline_access'}
pks = [r['pk'] for r in d['results'] if r['scope_name'] in wanted]
print(json.dumps(pks))
")
```

### 2. Create the OAuth2 Provider

```bash
PROVIDER_RESP=$(curl -fsS -X POST "${AK_URL}/api/v3/providers/oauth2/" \
  -H "Authorization: Bearer ${AK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json, os
print(json.dumps({
    'name': 'BlenderMCP OAuth',
    'authorization_flow': os.environ['AUTH_FLOW_PK'],
    'invalidation_flow': os.environ['INVAL_FLOW_PK'],
    'client_type': 'confidential',
    'access_code_validity': 'minutes=10',
    'access_token_validity': 'hours=8',
    'refresh_token_validity': 'days=7',
    'include_claims_in_id_token': True,
    'issuer_mode': 'per_provider',
    'sub_mode': 'hashed_user_id',
    'signing_key': os.environ['SIGNING_KEY'],
    'redirect_uris': [
        {'url': 'https://your-mcp-server.example.com/mcp/auth/callback',
         'matching_mode': 'strict'},
        {'url': 'http://localhost:8000/mcp/auth/callback',
         'matching_mode': 'strict'},
    ],
    'property_mappings': json.loads(os.environ['SCOPE_PKS']),
}))
")")

# Extract creds
echo "$PROVIDER_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'PROVIDER_PK={d[\"pk\"]}')
print(f'AUTHENTIK_CLIENT_ID={d[\"client_id\"]}')
print(f'AUTHENTIK_CLIENT_SECRET={d[\"client_secret\"]}')
"
```

### 3. Create the Application

```bash
PROVIDER_PK=<from previous step>
curl -fsS -X POST "${AK_URL}/api/v3/core/applications/" \
  -H "Authorization: Bearer ${AK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"BlenderMCP\",
    \"slug\": \"blender-mcp\",
    \"provider\": ${PROVIDER_PK},
    \"meta_launch_url\": \"https://your-mcp-server.example.com/\"
  }"
```

### 4. Verify the OIDC discovery endpoint

```bash
curl -fsS "${AK_URL}/application/o/blender-mcp/.well-known/openid-configuration" \
  | python3 -m json.tool | head -10
```

Should return JSON with `issuer`, `authorization_endpoint`, `token_endpoint`,
`jwks_uri`, etc.

### 5. Wire into your blender-mcp `.env`

```
AUTH_BACKEND=authentik
AUTHENTIK_CONFIG_URL=https://auth.your-authentik.example.com/application/o/blender-mcp/.well-known/openid-configuration
AUTHENTIK_CLIENT_ID=<from step 2>
AUTHENTIK_CLIENT_SECRET=<from step 2>
PUBLIC_BASE_URL=https://your-mcp-server.example.com
```

`make rebuild && docker compose up -d` and you're live.

## Path B — Admin UI

In Authentik's admin UI (`https://<authentik>/if/admin/`):

1. **Applications → Providers → Create**
   - Type: **OAuth2/OpenID Provider**
   - Name: `BlenderMCP OAuth`
   - Authorization flow: `default-provider-authorization-explicit-consent`
   - Client type: **Confidential**
   - Client ID + Client Secret: auto-generated; **copy them**
   - Redirect URIs:
     - `https://<your-mcp-server>/mcp/auth/callback`
     - `http://localhost:8000/mcp/auth/callback`
   - Signing Key: any existing cert (or the default self-signed)
   - Subject mode: `Based on the User's hashed ID`
   - Include claims in id_token: ✓
2. **Applications → Applications → Create**
   - Name: `BlenderMCP`
   - Slug: `blender-mcp`
   - Provider: (the one you just created)
   - Launch URL: `https://<your-mcp-server>/`
3. Same step 4 + 5 as Path A.

## Why these specific settings

- **`sub_mode: hashed_user_id`** — gives stable, privacy-preserving user
  identity that survives username changes. The hashed ID becomes the bus
  isolation key in BlenderMCP (each user gets their own bus).
- **`include_claims_in_id_token: True`** — claims (sub, preferred_username,
  email) come back in the ID token, so we can resolve user identity without
  an extra userinfo round-trip per request.
- **`access_token_validity: hours=8`** — comfortable for a workday without
  forcing mid-edit re-auth. Refresh tokens auto-rotate, so the user logs
  in once per Authentik refresh window (7 days).
- **`refresh_token_validity: days=7`** — bounded re-auth cadence. Long
  enough for a normal work week; short enough that a leaked refresh token
  doesn't have indefinite reach.
- **Two redirect URIs** — production (`https://mcp...`) and local dev
  (`http://localhost:8000/...`). Authentik requires strict redirect-URI
  matching for security; supplying both means the same Authentik app can
  serve both deployments without per-environment provisioning.
- **`client_type: confidential`** — server has a client_secret. The
  BlenderMCP server holds this; clients (Blender addon, Claude Code, etc.)
  don't see it — they use Dynamic Client Registration with our FastMCP
  OAuthProxy, which translates to the confidential upstream credentials.

## Removing the app

If you ever need to nuke + recreate:

```bash
curl -fsS -X DELETE "${AK_URL}/api/v3/core/applications/<slug>/" \
  -H "Authorization: Bearer ${AK_TOKEN}"

curl -fsS -X DELETE "${AK_URL}/api/v3/providers/oauth2/${PROVIDER_PK}/" \
  -H "Authorization: Bearer ${AK_TOKEN}"
```

Delete the application FIRST; provider deletion may fail if an application
still references it.
