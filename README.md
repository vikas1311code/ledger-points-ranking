# Ledger — Points & Ranking Service

A small backend service (Python/Flask + SQLite) that records point-earning
transactions for users and exposes a fair, multi-factor leaderboard, plus a
single-page frontend to exercise it live.

## 1. How to run it

### Backend

```bash
cd backend
python3 -m venv venv && source venv/bin/activate   # optional
pip install -r requirements.txt
python app.py
```

This starts the API on `http://localhost:5000` and creates `backend/app.db`
(SQLite file) on first run — no external database needed.

Health check: `GET http://localhost:5000/health`

### Frontend

`frontend/index.html` is a single static file with no build step.

- **Locally:** just open the file in a browser, or serve it:
  `python3 -m http.server 8080` from inside `frontend/`, then visit
  `http://localhost:8080`.
- **Deployed:** the file can be dropped onto any static host (GitHub Pages,
  Netlify, Vercel static, S3, etc.) since it has no server dependency. The
  page has an "API URL" field at the top — point it at wherever the backend
  is deployed (e.g. Render/Railway/Fly.io URL) and click **Save URL**. CORS
  is already enabled on the backend (`flask-cors`) for this reason.

> **Assumption documented:** since this is a take-home assignment, the
> backend is meant to be run by the reviewer locally or on a small free-tier
> host. The frontend is a static file with zero build tooling so it can be
> deployed to literally any static file host in seconds and pointed at
> whichever backend URL the reviewer chooses.

---

## 2. The APIs

### `POST /transaction`

Records one transaction (points earned, spent, or adjusted) for a user.

**Body:**
```json
{
  "user_id": "alice",
  "type": "earn",
  "points": 50,
  "idempotency_key": "client-generated-unique-string"
}
```

- `user_id` — required, 1–64 chars, letters/digits/`_`/`-` only.
- `type` — one of `earn`, `bonus`, `purchase`, `adjustment`. Default `earn`.
  `purchase` is treated as **spending**: the `points` value you send is
  positive in the request but stored/applied as a negative delta.
- `points` — required, whole number, `1`–`1000` per request (this cap is an
  abuse-prevention control, see §4).
- `idempotency_key` — required, unique string the **client** generates (e.g.
  a UUID) for this specific attempt. Re-sending the same key (network retry,
  double-click, etc.) is detected and ignored — see §5.

**Responses:**
- `201` — transaction applied. Body includes the stored transaction row and
  an updated mini-summary for the user.
- `200` with `"status": "duplicate_ignored"` — the idempotency key was
  already processed; the original transaction is returned, nothing is
  double-applied.
- `400` — validation error (bad user_id, missing field, out-of-range points,
  purchase that would overdraw the balance, etc.).
- `429` — rate limit or daily points cap exceeded (abuse prevention).
- `500` — unexpected server error.

### `GET /summary/:userId`

Returns a user's current standing.

```json
{
  "user_id": "alice",
  "total_points": 150,
  "transaction_count": 3,
  "active_days_count": 2,
  "rank": 1,
  "score": 200.0,
  "recent_transactions": [ ... up to 10 most recent ... ]
}
```

`404` if the user has never made a transaction.

### `GET /ranking`

Returns the full leaderboard, sorted by score (highest first), with an
optional `?limit=N` (default 50, max 500).

```json
{
  "ranking": [
    {"rank": 1, "user_id": "alice", "total_points": 150, "active_days_count": 2, "score": 200.0, "transaction_count": 3},
    ...
  ],
  "total_users": 2,
  "scoring_formula": "score = total_points * 1.0 + active_days_count * 25.0"
}
```

---

## 3. How ranking is calculated

Ranking is **not** based on raw points alone, because that rewards whoever
fires off the single biggest transaction rather than whoever actually
engages with the system over time — which is easy to game with one scripted
burst.

```
score = total_points  +  (active_days_count × 25)
```

- **`total_points`** — the primary signal: how much value the user has
  actually earned, net of any spending.
- **`active_days_count`** — the number of *distinct calendar days* on which
  the user made at least one transaction. This is the "consistency" factor:
  a user who shows up regularly accumulates this even on days they earn
  modest amounts, and it can't be inflated by spamming many transactions in
  a single day (it only increments once per day, see §4).

Ties on score are broken deterministically (more active days first, then
`user_id` alphabetically) so the leaderboard never "jitters" between two
users with an identical score on repeated requests — determinism is treated
as part of fairness.

The two weights (`WEIGHT_POINTS = 1.0`, `WEIGHT_CONSISTENCY = 25.0`) are
constants at the top of `app.py` and easy to retune; they aren't hardcoded
into the query logic.

---

## 4. Abuse / manipulation prevention

Several independent controls, all in `app.py`:

1. **Per-transaction cap** — `1`–`1000` points per call. A single request
   can't catapult a user to the top.
2. **Rate limiting** — at most 20 transactions per user per rolling 60s
   window. Stops a script from firing thousands of small transactions to
   farm `transaction_count`/active-day credit.
3. **Daily points cap** — a user can earn at most 5,000 points/day in total
   (`DAILY_POINTS_CAP`). Excess transactions are recorded with
   `status = "rejected"` (kept for audit) but never applied to the balance
   or the leaderboard.
4. **Active-days, not transaction count, drives the consistency bonus** — so
   spamming 50 tiny transactions in one day yields the *same* consistency
   credit as one transaction that day. This specifically blocks the most
   obvious gaming strategy (loop a request many times in a tight loop).
5. **Purchases can't overdraw** — a `purchase` that would take a user's
   balance negative is rejected with `400`.
6. **Strict input validation** — `user_id` pattern, type enum, integer-only
   points, JSON content-type — rejects malformed/garbage input before it
   ever touches the database.

---

## 5. How duplicate requests are prevented (idempotency)

The client is required to send an `idempotency_key` (any unique string —
typically a UUID it generates once per logical "attempt", and reuses on
retry). The server:

1. Looks the key up in `transactions` (`idempotency_key` has a `UNIQUE`
   constraint in SQLite).
2. If found → returns the **original** result with
   `status: "duplicate_ignored"` and applies nothing again.
3. If not found → proceeds, and the `INSERT` of the new row (with that key)
   happens inside the *same* SQLite transaction as the balance update, so
   the two can never go out of sync (a crash mid-way rolls both back).
4. As a final backstop against a true race (two requests with the same key
   arriving at almost the same instant), the database's own `UNIQUE`
   constraint will reject the second `INSERT` with an `IntegrityError`,
   which is caught and converted into the same `duplicate_ignored` response.

This was verified directly: firing 10 concurrent identical requests (same
`idempotency_key`) at the running app produces exactly **one** `applied`
result and nine `duplicate_ignored` results, and the user's final balance
reflects the transaction exactly once.

---

## 6. Concurrency / data consistency

- SQLite is run in **WAL mode** with `busy_timeout` set, so reads never
  block writes and concurrent writers wait briefly instead of failing.
- Every transaction write uses `BEGIN IMMEDIATE` so the
  idempotency-check → validation → balance-update sequence is one atomic
  unit — no other write can interleave partway through.
- In addition to SQLite's own write-serialization, the backend keeps an
  **in-process lock per `user_id`** (`database.get_user_lock`). This
  guarantees that two simultaneous requests for the *same* user are fully
  serialized (including the rate-limit and daily-cap counting, which a bare
  SQL statement can't express atomically), while requests for *different*
  users never block each other.
- The `users` table is a denormalized, always-in-sync aggregate
  (`total_points`, `transaction_count`, `active_days_count`) maintained
  alongside the append-only `transactions` ledger. It exists purely so
  `/summary` and `/ranking` are O(1)/O(n) reads instead of re-aggregating
  the full transaction history on every request — but it is always
  recomputable from `transactions` if it ever needed to be rebuilt, since
  every mutation to it happens in the same DB transaction as the ledger
  insert that caused it.

---

## 7. Data model

```
users
  user_id            TEXT PRIMARY KEY
  total_points        INTEGER
  transaction_count    INTEGER
  active_days_count    INTEGER
  last_transaction_at  TEXT
  last_active_day      TEXT
  created_at           TEXT

transactions               (append-only ledger / audit trail)
  id                 INTEGER PRIMARY KEY AUTOINCREMENT
  idempotency_key     TEXT UNIQUE     -- duplicate-prevention key
  user_id             TEXT
  type                TEXT            -- earn | bonus | purchase | adjustment
  points              INTEGER         -- signed: purchases stored negative
  status              TEXT            -- applied | rejected
  rejection_reason    TEXT
  created_at          TEXT

user_active_days
  user_id  TEXT
  day      TEXT    -- YYYY-MM-DD, PRIMARY KEY (user_id, day)
```

`transactions` is the source of truth/audit log (including rejected
attempts, kept for transparency). `users` and `user_active_days` are
maintained incrementally as a cache for fast reads.

---

## 8. Mock data / assumptions

- No seed/mock data is pre-loaded — the leaderboard starts empty and is
  populated entirely through the frontend or direct API calls, so a reviewer
  can watch the ranking change live as they submit transactions.
- "Points" are an abstract unit (could represent loyalty points, XP, credits,
  etc.) — the assignment didn't specify a domain, so the schema is kept
  generic (`type: earn/bonus/purchase/adjustment`) rather than tied to one
  business case.
- Authentication/authorization is out of scope for this assignment — any
  caller can act as any `user_id`. In a production system, `user_id` would
  come from a verified session/token rather than the request body.
- Rate limit and daily cap values (`20`/min, `5000`/day) and the per-transaction
  cap (`1000`) are illustrative defaults, defined as constants at the top of
  `app.py`, intended to be tuned to real product requirements.

---

## 9. Project structure

```
backend/
  app.py            # Flask app: routes, validation, business logic
  database.py       # SQLite connection + schema + per-user lock helper
  requirements.txt
frontend/
  index.html        # single-file static frontend (no build step)
README.md
```
