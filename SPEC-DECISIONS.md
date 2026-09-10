# Spec decisions — resolutions to gaps in CLAUDE.md

CLAUDE.md is the spec. Where it is silent, contradictory, or unbuildable as written,
this file is the binding resolution. **This file wins over CLAUDE.md on these six points.**

Every deviation from CLAUDE.md is listed here and nowhere else. Do not invent further
deviations — if you hit another gap, add it to this file under the same heading style and
call it out in your final report.

---

## D1 — Geofencing engine: PostGIS is the real engine, Location Service is a swap-in

CLAUDE.md names AWS Location Service as the engine ("no custom code") but "mocked locally",
and never specifies the mock. Since enter/exit detection is the product, the local path must
be a genuine implementation.

**Resolution.** Define `backend/shared/geofence_engine.py` with an abstract base:

```python
class GeofenceEngine(ABC):
    @abstractmethod
    def zones_containing(self, tenant_id: str, lat: float, lng: float) -> list[str]: ...
```

- `PostGISGeofenceEngine` — real implementation, `ST_Covers(boundary, point)`, tenant-scoped.
  Used when `ENVIRONMENT=local`. This is the default and the tested path.
- `LocationServiceGeofenceEngine` — calls AWS Location Service `BatchEvaluateGeofences`.
  Used when `ENVIRONMENT=production` **and** `AWS_LOCATION_COLLECTION` is set. It is
  acceptable for this to be thin and untested against real AWS; mark it clearly as unverified.
- `get_engine()` factory reads `ENVIRONMENT`. Handlers never import a concrete engine.

Enter/exit diffing (comparing the previous zone set to the current one) lives in
`event_processor`, NOT in the engine. The engine only answers "which zones contain this point".

## D2 — Circle zones: accept circles, store polygons

CLAUDE.md promises "polygon/circle zones" but the table is `GEOGRAPHY(POLYGON, 4326)`.

**Resolution.** Keep the column as POLYGON. Add to `geofences`:

```sql
shape_type  TEXT NOT NULL DEFAULT 'polygon' CHECK (shape_type IN ('polygon','circle')),
center      GEOGRAPHY(POINT, 4326),   -- non-null iff shape_type='circle'
radius_m    DOUBLE PRECISION,         -- non-null iff shape_type='circle'
```

`POST /geofences` accepts either `{"type":"polygon","coordinates":[[lng,lat],...]}` or
`{"type":"circle","center":[lng,lat],"radius_m":250}`. A circle is converted on write with
`ST_Buffer(center::geography, radius_m)` (64 segments) and stored in `boundary`. Circles
round-trip on read as circles using `center`/`radius_m`; containment always uses `boundary`,
so the engine has exactly one code path. Add a CHECK constraint enforcing the iff above.

## D3 — Dwell events: per-zone threshold, computed on subsequent pings

CLAUDE.md lists `dwell` as an event type but specifies no threshold and no trigger.

**Resolution.** Add `dwell_threshold_seconds INT` to `geofences`, nullable; NULL disables
dwell for that zone. On enter, `event_processor` records the entry time in Redis:

```
geofence:{id}:entered:{device_id}  -> STRING unix_ts, TTL 86400
```

On every later ping where the device is still inside the zone, if
`now - entered_at >= dwell_threshold_seconds` and no dwell event has yet been emitted for
this occupancy, emit exactly one `dwell` event carrying `dwell_ms`. Mark it emitted with

```
geofence:{id}:dwelled:{device_id}  -> STRING "1", TTL 86400
```

Both keys are deleted on exit. Dwell fires **at most once per continuous occupancy**.

## D4 — Batch ingest: POST /positions takes one ping or many

The Android SDK has a `BatchQueue` that flushes every N seconds, but the route table has no
batch endpoint and the sample payload is a single object.

**Resolution.** No new route. `POST /positions` accepts either the documented single-ping
object, or `{"pings": [ <ping>, ... ]}` with a hard cap of 500 pings per request. The
response is `{"accepted": N, "rejected": [{"index": i, "error": "..."}]}` — a malformed ping
inside a batch must not fail the whole batch. Single-ping requests keep the documented
response shape. Pings within a batch are processed in `timestamp` order regardless of array
order, so geofence transitions are evaluated correctly.

## D5 — devices:active must be a ZSET, not a TTL'd SET

CLAUDE.md specifies `devices:active -> SET, TTL 3600s`. A TTL on a Redis SET expires the
whole key at once, dropping every device simultaneously — it cannot express "devices seen in
the last hour".

**Resolution.** `devices:active` is a **ZSET** scored by unix timestamp of last ping.
On each write: `ZADD devices:active <ts> <device_id>` then
`ZREMRANGEBYSCORE devices:active -inf <ts-3600>`. Active devices are read with
`ZRANGEBYSCORE devices:active <now-3600> +inf`. All other Redis keys are exactly as CLAUDE.md
specifies. Document this deviation in the README's Redis section.

## D6 — Tenancy: every row and every query is tenant-scoped

CLAUDE.md has no tenant/org column anywhere and Cognito is "mocked locally" with no user
model. As written, any authenticated caller can read any device's location. For a location
tracking product this is the highest-severity gap in the spec.

**Resolution.**

- Add `tenant_id TEXT NOT NULL` to `positions`, `geofences`, and `device_events`.
- `positions` PRIMARY KEY becomes `(tenant_id, device_id, recorded_at)`.
- Every index in `002_indexes.sql` leads with `tenant_id`.
- `backend/shared/auth.py` exposes `require_auth(event) -> AuthContext(tenant_id, subject)`.
  It raises `AuthError` (→ 401) on a missing/invalid token.
  - `ENVIRONMENT=local`: verify an HS256 JWT signed with `DEV_JWT_SECRET`. Provide
    `backend/scripts/mint_dev_token.py` to mint one for curl/dashboard use.
  - `ENVIRONMENT=production`: verify an RS256 Cognito JWT against the pool JWKS
    (`COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`), cache JWKS in module scope.
  - Both read tenant from the `custom:tenant_id` claim. A token without it is a 401.
- **Every** SQL statement and every Redis key is scoped by tenant. Redis keys become
  `t:{tenant_id}:device:{device_id}:position` etc. — same structure as CLAUDE.md, prefixed.
- A handler that reaches the DB without an `AuthContext` is a bug. There must be a test
  proving tenant A cannot read tenant B's positions, geofences, or events.

## D7 — dwell_ms belongs on every event object, not just GET /events

Caught by the Android SDK agent: API-CONTRACT.md originally put `dwell_ms` only on the
objects returned by `GET /events`, but the SDK's `onDwell` callback is fed from the `events`
array embedded in the `POST /positions` response (single and batch forms) — that is the
real-time path, `GET /events` is the query/history path. Without `dwell_ms` there, the SDK
cannot report dwell duration to the app in real time.

**Resolution.** `dwell_ms` is now part of the one event-object shape used everywhere an event
appears: `POST /positions` (single and batch) and `GET /events`. Non-null only when
`event_type == "dwell"`. API-CONTRACT.md has been updated accordingly — if you built against
the version without this field, add it now.

## D8 — POST /positions needs events synchronously: shared CodeUri, diffing lives in event_processor.diffing

D1 says enter/exit diffing "lives in event_processor, NOT in the engine," but API-CONTRACT's
POST /positions response carries an `events` array in the *same* response — there is no local
AWS Location Service to trigger `event_processor` asynchronously and fill that array later.

**Resolution.** The diffing + dwell logic (D1, D3) lives in
`backend/event_processor/diffing.py` as plain importable functions
(`process_ping_events`, `current_zone_ids`, `zone_device_count`) — not only behind
`event_processor/handler.py`'s Lambda entrypoint. `position_tracker/handler.py` calls
`process_ping_events` directly and synchronously on every ping to build the response's
`events` array; `geofence_manager/handler.py` calls `zone_device_count` for `device_count`.
`event_processor/handler.py` itself now only serves `GET /events` — CLAUDE.md's "triggered by
AWS Location Service" description has no local equivalent (see that file's docstring).

For this cross-package import to be valid in a real deploy (not just locally, where
everything is on one `sys.path`), `infra/template.yaml` points **every** function's `CodeUri`
at the whole `backend/` directory rather than its own subfolder, so `position_tracker` and
`geofence_manager`'s deployment packages actually contain `event_processor/` (and `shared/`).
Consequently `sam build` installs from `backend/requirements.txt` (the CodeUri root) rather
than the per-function `requirements.txt` files, which are kept identical to it for local `pip
install` convenience and to match CLAUDE.md's project-structure listing. This has not been
exercised with a real `sam build`/`sam local` in this environment (no SAM CLI installed here,
D1's toolchain constraints) — flagged for validation before a real deploy.

## D9 — Code-review fixes: geofence-delete DoS, dwell-during-Redis-outage, dwell-TTL cap, N+1 in GET /geofences

A code review after the initial build (see the review's findings) found several real bugs.
Fixed here rather than left as a follow-up, because each was either a live correctness bug or
cheap to close:

- **Geofence delete left a permanent per-device outage.** `delete_geofence` cleared only the
  zone's own Redis membership set, never each affected device's `device_zones` set. That
  device's next ping computed an `exit` for a `geofence_id` that no longer existed, which
  violated the `device_events` FK, threw unhandled, and — because the Redis cleanup that would
  have self-healed the state never ran (the DB insert failed first) — repeated on every
  subsequent ping forever. Fixed in two layers: `delete_geofence`
  (`backend/geofence_manager/handler.py`) now proactively clears every affected device's
  `device_zones`/`entered_at`/`dwelled` keys at delete time, and `process_ping_events`
  (`backend/event_processor/diffing.py`) now runs the Redis cleanup for an exit *before*
  attempting the DB insert, and skips the insert/callback entirely (with a warning log) when
  the geofence no longer exists — so even a stale reference self-heals on the very next ping
  instead of failing forever.
- **Dwell re-fired on every ping during a Redis outage.** The "already dwelled" guard had no
  Postgres fallback (unlike the `entered_at` check next to it), so a degraded Redis made every
  ping re-emit a `dwell` event. Fixed with `_dwell_already_fired`, a Postgres fallback that
  checks `device_events` for a `dwell` row since the current occupancy began.
- **Dwell silently never fired for `dwell_threshold_seconds` > 24h.** The Redis TTL on
  `entered_at`/`dwelled` was a fixed `DWELL_STATE_TTL_SECONDS` (24h) regardless of the zone's
  own threshold, so a long-threshold zone's tracking key expired mid-occupancy and dwell for
  that occupancy never fired again. Fixed with `_dwell_state_ttl(threshold)`, which scales the
  TTL to `threshold + 24h` grace, and a new `MAX_DWELL_THRESHOLD_SECONDS` (30 days) cap in
  `parse_dwell_threshold` so the TTL this produces stays bounded.
- **`GET /geofences` did N sequential per-zone Redis/Postgres lookups**, polled by the
  dashboard every 20s. Fixed with `zone_device_counts` (plural) — one Redis pipeline for all
  zones in the response, with one grouped Postgres query (not N) as the degraded-Redis
  fallback.
- **`POST /positions` batch path (D4) re-queried geofence metadata once per ping.** Fixed with
  an optional `meta_cache` threaded through `process_ping_events` from the batch loop in
  `position_tracker/handler.py`, reused across all pings in one request. This is a **partial**
  fix — it removes the geofence-metadata query from the per-ping round-trip count, but the
  position INSERT, the Redis pipeline, and the containment query are still done once per ping
  rather than batched across the request. A full fix (multi-row INSERT via `execute_values`,
  one `UNNEST`-based containment query for the whole batch) is real work with real correctness
  risk around the timestamp-ordering requirement in D4, and was deliberately not attempted
  under review-fix time pressure — flagged here as a follow-up, not silently left undone.
- **Backend rejected `accuracy < 0`, `speed < 0`, `bearing` outside [0,360] without
  API-CONTRACT.md saying so.** Documented in API-CONTRACT.md's `POST /positions` section
  instead of removing the checks (they reject genuinely nonsensical values).
- **`redis_op`'s except clause caught a bare `ValueError`**, which would misclassify an
  unrelated application bug as a Redis outage. Narrowed to `UnicodeDecodeError` (a `ValueError`
  subclass that covers real response-decode corruption) so a future callback's own logic error
  no longer gets silently filed as "Redis is down."
- **Android SDK's `EventCallback.kt` doc comment and README "Known contract gaps" section
  described `dwell_ms` as missing from the batch response** — true when written, false since
  D7 landed. Updated both to say so.

**Deliberately NOT fixed here** (real findings, but a rushed fix carried more risk than the
bug itself, right before a commit):
- **Tenant scoping (D6) is enforced by convention, not by a structural mechanism.** Every
  query today correctly includes `tenant_id`, but `shared/db.py`'s `query_all`/`query_one`/
  `execute` take raw SQL with no tenant-aware wrapper — nothing would catch the next query
  someone adds if they forget the clause. `shared/cache_keys.py` already solves the equivalent
  problem for Redis (tenant_id as a mandatory first argument through a shared prefix
  helper); the Postgres layer should eventually get the same treatment. Left as a backlog item
  rather than retrofitted across every handler under time pressure.
- **D8's shared-`CodeUri`-across-all-Lambdas workaround** for the `event_processor.diffing`
  cross-import is a packaging-model workaround, not a proper fix (a Lambda Layer, or relocating
  `diffing.py` into `shared/`, would be the general fix). It is self-documented as unverified
  against real SAM tooling. Left alone rather than restructured blind, since it has never been
  exercised against `sam build` here and a change to it can't be verified on this machine either.

---

## Environment / toolchain constraints (not spec gaps, but binding)

- **No AWS SAM CLI on this machine.** Handlers stay pure Lambda-shaped
  (`handler(event, context)` taking API-Gateway proxy events). Add
  `backend/local_server.py` — a FastAPI/uvicorn adapter that builds API-Gateway-proxy events
  and dispatches to the same handler functions on port 3000. **Zero handler logic may be
  duplicated in the shim.** `infra/template.yaml` is still written per spec for real deploys.
- **Python 3.14 locally, Lambda runtime `python3.11`** in template.yaml. Verify
  `psycopg2-binary` actually installs on 3.14 before committing to it; if it does not, use
  `psycopg[binary]` (psycopg 3) with `psycopg_pool` and record that deviation here. Do not
  leave a requirements file that cannot install.
- **No JDK / Gradle / Android SDK.** The Android SDK module is written to spec but cannot be
  compiled or verified here; it must be labelled as such in its own README.
- `AWS_REGION=eu-west-1` per spec.

## Coding standards (from CLAUDE.md, restated because they are testable)

- Type hints on every Python function signature. No bare `except`. Structured logging via
  `aws_lambda_powertools`.
- React: function components + hooks only.
- Kotlin: coroutines for async, nothing blocking on the main thread.
- Secrets only from env vars — nothing hardcoded, no secrets committed.
- **Every Lambda must survive Redis being down**: log and continue, serve from Postgres.
  Postgres is the source of truth. There must be a test that proves this by pointing the
  client at a dead Redis.
