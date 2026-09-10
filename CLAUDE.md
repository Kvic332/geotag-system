# Geo-tagging system

## Project overview

Real-time device geo-tagging system with geofencing for Android.
Tracks live device positions and fires enter/exit events when devices cross geofence boundaries.

### Core capabilities
- Real-time position tracking (sub-2ms reads via Redis)
- Geofence zone management (create, update, delete polygon/circle zones)
- Enter / exit / dwell event notifications via SNS or webhook
- React dashboard — live map, device list, geofence editor, event log
- Android SDK (Kotlin) — plug-and-play library for client apps

---

## Stack

| Layer | Local dev (free) | Cloud (production) |
|---|---|---|
| Position store (latest) | Redis 7 (Docker) | AWS ElastiCache |
| Position history + geofences | PostgreSQL 16 + PostGIS 3.4 (Docker) | Supabase (free tier) |
| Compute | Python Lambda via AWS SAM CLI | AWS Lambda |
| API | SAM local (port 3000) | AWS API Gateway |
| Geofencing engine | AWS Location Service (mocked locally) | AWS Location Service |
| Events | SNS (mocked via LocalStack or logged) | AWS SNS |
| Dashboard | React + Vite (port 5173) | Vercel / S3 + CloudFront |
| Android SDK | Kotlin library | Published to Maven / JitPack |
| Auth | Cognito JWT (mocked locally) | AWS Cognito |

---

## Project structure

```
geotag-system/
├── CLAUDE.md                        ← you are here
├── infra/
│   ├── docker-compose.yml           ← postgres+postgis + redis containers
│   ├── template.yaml                ← AWS SAM template (all Lambdas + API GW)
│   └── migrations/
│       ├── 001_init.sql             ← positions, geofences, device_events tables
│       └── 002_indexes.sql          ← spatial indexes
├── backend/
│   ├── shared/                      ← shared utils (db.py, redis_client.py, auth.py)
│   ├── position_tracker/
│   │   ├── handler.py               ← POST /positions
│   │   └── requirements.txt
│   ├── geofence_manager/
│   │   ├── handler.py               ← GET/POST/PUT/DELETE /geofences
│   │   └── requirements.txt
│   └── event_processor/
│       ├── handler.py               ← triggered by AWS Location Service
│       └── requirements.txt
├── dashboard/
│   ├── src/
│   │   ├── components/
│   │   │   ├── LiveMap.jsx          ← Mapbox GL / Leaflet map with device dots
│   │   │   ├── DeviceList.jsx       ← sidebar list of active devices
│   │   │   ├── GeofenceEditor.jsx   ← draw polygon on map → save zone
│   │   │   └── EventLog.jsx         ← real-time enter/exit event feed
│   │   ├── api/                     ← axios wrappers for backend routes
│   │   └── App.jsx
│   ├── .env.local                   ← VITE_API_URL=http://localhost:3000
│   └── package.json
└── android-sdk/
    ├── geotag-sdk/
    │   └── src/main/java/com/geotag/sdk/
    │       ├── GeoTagClient.kt      ← main entry point
    │       ├── LocationManager.kt   ← FusedLocationProviderClient wrapper
    │       ├── GeofenceClient.kt    ← register / deregister zones
    │       ├── EventCallback.kt     ← onEnter / onExit / onDwell interfaces
    │       └── BatchQueue.kt        ← buffer pings, flush every N seconds
    └── build.gradle
```

---

## Environment variables

### Backend Lambdas (`.env` or SAM `env.json`)

```bash
# Data
DATABASE_URL=postgresql://postgres:dev@localhost:5432/geotag
REDIS_HOST=localhost
REDIS_PORT=6379

# AWS
AWS_REGION=eu-west-1
AWS_LOCATION_COLLECTION=geotag-geofences     # AWS Location Service collection name

# Auth
COGNITO_USER_POOL_ID=eu-west-1_xxxxxxx
COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx

# SNS
SNS_TOPIC_ARN=arn:aws:sns:eu-west-1:123456789:geotag-events

# Env flag
ENVIRONMENT=local   # or: production
```

### React dashboard (`dashboard/.env.local`)

```bash
VITE_API_URL=http://localhost:3000
VITE_MAPBOX_TOKEN=pk.xxxxxxxxxxxxxxxxxxxxxxxx   # get free token at mapbox.com
```

### Production overrides (Supabase + ElastiCache)

```bash
DATABASE_URL=postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres
REDIS_HOST=[cluster-id].xxxxxx.cfg.euw1.cache.amazonaws.com
ENVIRONMENT=production
```

Swap these two vars — all Lambda code is identical across environments.

---

## Local dev setup

### Prerequisites

```bash
# Docker Desktop (or Docker Engine on Linux)
docker --version        # 24+
docker compose version  # 2.x

# AWS SAM CLI
sam --version           # 1.100+

# Node.js (for React dashboard)
node --version          # 18+

# Python
python --version        # 3.11+
```

### Start the data tier

```bash
cd infra
docker compose up -d
# Postgres+PostGIS on :5432, Redis on :6379
```

### Apply migrations

```bash
docker exec -i geotag-postgres psql -U postgres -d geotag < infra/migrations/001_init.sql
docker exec -i geotag-postgres psql -U postgres -d geotag < infra/migrations/002_indexes.sql
```

### Run Lambdas locally

```bash
cd infra
sam build
sam local start-api --env-vars env.local.json --port 3000
# API available at http://localhost:3000
```

### Start React dashboard

```bash
cd dashboard
npm install
npm run dev
# Dashboard at http://localhost:5173
```

---

## Database schema (PostGIS)

### Key tables

```sql
-- Latest device position (also cached in Redis)
CREATE TABLE positions (
    device_id   TEXT        NOT NULL,
    location    GEOGRAPHY(POINT, 4326) NOT NULL,
    accuracy    FLOAT,
    speed       FLOAT,
    bearing     FLOAT,
    battery     INT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (device_id, recorded_at)
);

-- Geofence zones
CREATE TABLE geofences (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    boundary    GEOGRAPHY(POLYGON, 4326) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Enter/exit/dwell events
CREATE TABLE device_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   TEXT        NOT NULL,
    geofence_id UUID        REFERENCES geofences(id),
    event_type  TEXT        NOT NULL CHECK (event_type IN ('enter','exit','dwell')),
    location    GEOGRAPHY(POINT, 4326),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## API routes

| Method | Route | Lambda | Description |
|---|---|---|---|
| POST | /positions | position_tracker | Ingest device position ping |
| GET | /positions/{device_id} | position_tracker | Get latest position for device |
| GET | /positions/{device_id}/history | position_tracker | Position history (time range) |
| GET | /geofences | geofence_manager | List all geofences |
| POST | /geofences | geofence_manager | Create a new geofence zone |
| PUT | /geofences/{id} | geofence_manager | Update a geofence zone |
| DELETE | /geofences/{id} | geofence_manager | Delete a geofence zone |
| GET | /events | event_processor | Query geofence events |

### Sample position ping payload

```json
{
  "device_id": "device_abc123",
  "lat": 6.5244,
  "lng": 3.3792,
  "accuracy": 12.5,
  "speed": 0.0,
  "bearing": 180.0,
  "battery": 82,
  "timestamp": 1725868800
}
```

---

## Redis data model

```
device:{device_id}:position   → HASH  (lat, lng, accuracy, speed, ts, battery)
  TTL: 3600s (1 hour inactivity eviction)

devices:active                → SET   (all device_ids seen in last hour)
  TTL: 3600s

geofence:{id}:members         → SET   (device_ids currently inside zone)
device:{device_id}:zones      → SET   (geofence IDs device is currently inside)
```

---

## Android SDK usage (for client apps)

```kotlin
// Initialise once (e.g. in Application.onCreate)
val client = GeoTagClient.Builder(context)
    .apiUrl("https://your-api-gateway-url.amazonaws.com/prod")
    .apiKey("your-cognito-jwt")
    .batchIntervalSeconds(10)
    .build()

// Start tracking
client.startTracking()

// Listen for geofence events
client.setEventCallback(object : GeofenceEventCallback {
    override fun onEnter(deviceId: String, geofenceId: String) { }
    override fun onExit(deviceId: String, geofenceId: String) { }
    override fun onDwell(deviceId: String, geofenceId: String, dwellMs: Long) { }
})

// Stop tracking (e.g. in onDestroy)
client.stopTracking()
```

---

## Build order

Work through these in sequence — each step depends on the previous:

1. `infra/docker-compose.yml` → spin up Postgres+PostGIS and Redis locally
2. `infra/migrations/` → apply DB schema and spatial indexes
3. `backend/shared/` → db.py (psycopg2 pool) and redis_client.py (connection singleton)
4. `backend/position_tracker/handler.py` → core ping endpoint; test with `sam local invoke`
5. `backend/geofence_manager/handler.py` → CRUD zones; wire to AWS Location Service
6. `backend/event_processor/handler.py` → handle enter/exit trigger from Location Service
7. `infra/template.yaml` → SAM template wiring all three Lambdas to API Gateway
8. `dashboard/` → React app with Leaflet map, device list, geofence draw tool, event log
9. `android-sdk/` → Kotlin library wrapping FusedLocation + batch queue + event callbacks
10. Deploy → swap DATABASE_URL to Supabase, REDIS_HOST to ElastiCache, push Lambdas

---

## Key decisions log

| Decision | Choice | Reason |
|---|---|---|
| History DB | PostgreSQL + PostGIS | Native geo queries, free locally, Supabase free tier in cloud |
| Latest position | Redis HASH | Sub-2ms reads, TTL-based auto-eviction |
| Geofencing engine | AWS Location Service | Handles polygon geometry math, no custom code |
| Mobile platform | Android (Kotlin) | Initial scope; iOS can be added later |
| Dashboard map | Leaflet (open source) or Mapbox GL | Leaflet = free; Mapbox = better UX, free 50k map loads/month |
| Lambda runtime | Python 3.11 | Consistent with Renmoney Risk Engine stack |

---

## Coding standards

- Python: type hints on all function signatures; no bare `except`; structured logging via `aws_lambda_powertools`
- React: functional components + hooks only; no class components
- Kotlin: coroutines for async; no blocking calls on main thread
- All secrets via environment variables — never hardcoded
- Every Lambda must handle Redis failure gracefully (log + continue; Postgres is source of truth)

---

## Useful commands

```bash
# Check PostGIS is working
docker exec -it geotag-postgres psql -U postgres -d geotag -c "SELECT PostGIS_Version();"

# Test position ping locally
curl -X POST http://localhost:3000/positions \
  -H "Content-Type: application/json" \
  -d '{"device_id":"test_001","lat":6.5244,"lng":3.3792,"accuracy":10,"battery":90}'

# Check Redis
docker exec -it geotag-redis redis-cli HGETALL "device:test_001:position"

# Tail SAM logs
sam logs -n PositionTracker --tail

# Build and deploy to AWS (when ready)
sam build && sam deploy --guided
```
