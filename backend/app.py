"""
app.py
------
Flask backend exposing:

    POST /transaction      -> record a points transaction (idempotent)
    GET  /summary/:userId   -> a user's points, rank, and stats
    GET  /ranking           -> leaderboard sorted by a fair, multi-factor score

Run with:  python app.py   (see README for details)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import re
import time
from datetime import datetime, timezone

from database import init_db, get_connection, get_user_lock

app = Flask(__name__)
CORS(app)  # allow the static frontend (served from a different origin) to call this API

# ---------------------------------------------------------------------------
# Configuration / abuse-prevention constants
# ---------------------------------------------------------------------------
MAX_POINTS_PER_TRANSACTION = 1000      # caps the blast radius of a single call
MIN_POINTS_PER_TRANSACTION = 1
MAX_USER_ID_LEN = 64
USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
VALID_TYPES = {"earn", "bonus", "purchase", "adjustment"}

# Simple sliding-window rate limit per user: at most N transactions within
# WINDOW_SECONDS. This stops a single account from flooding the system to
# artificially inflate transaction_count / points in a burst, which would
# otherwise unfairly boost both score factors used in /ranking.
RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60

# Daily points cap per user. Prevents one user from dominating the leaderboard
# via either a single huge transaction or many small ones in one day.
DAILY_POINTS_CAP = 5000

# Ranking weights. Score is a blend of total points (the primary signal) and
# "consistency" (distinct active days), so that someone who shows up
# regularly ranks fairly against someone who earned the same total in one
# single burst. See README for the full rationale.
WEIGHT_POINTS = 1.0
WEIGHT_CONSISTENCY = 25.0  # each distinct active day is worth a flat bonus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def error(message, status=400, **extra):
    body = {"error": message}
    body.update(extra)
    return jsonify(body), status


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def validate_transaction_payload(data):
    """Returns (cleaned_dict, None) on success or (None, error_message)."""
    if not isinstance(data, dict):
        return None, "Request body must be a JSON object."

    user_id = data.get("user_id")
    idempotency_key = data.get("idempotency_key")
    points = data.get("points")
    tx_type = data.get("type", "earn")

    if not user_id or not isinstance(user_id, str):
        return None, "user_id is required and must be a string."
    if not USER_ID_RE.match(user_id):
        return None, "user_id must be 1-64 chars of letters, digits, '_' or '-'."

    if not idempotency_key or not isinstance(idempotency_key, str):
        return None, "idempotency_key is required (a unique client-generated string, e.g. a UUID)."
    if len(idempotency_key) > 128:
        return None, "idempotency_key must be at most 128 characters."

    if tx_type not in VALID_TYPES:
        return None, f"type must be one of {sorted(VALID_TYPES)}."

    if points is None:
        return None, "points is required."
    if isinstance(points, bool) or not isinstance(points, (int, float)):
        return None, "points must be a number."
    if float(points).is_integer() is False:
        return None, "points must be a whole number."
    points = int(points)

    if tx_type == "purchase":
        # purchases spend points -> represented as a negative delta
        if points < MIN_POINTS_PER_TRANSACTION or points > MAX_POINTS_PER_TRANSACTION:
            return None, f"points for a purchase must be between {MIN_POINTS_PER_TRANSACTION} and {MAX_POINTS_PER_TRANSACTION}."
        points = -points
    else:
        if points < MIN_POINTS_PER_TRANSACTION or points > MAX_POINTS_PER_TRANSACTION:
            return None, f"points must be between {MIN_POINTS_PER_TRANSACTION} and {MAX_POINTS_PER_TRANSACTION}."

    return {
        "user_id": user_id,
        "idempotency_key": idempotency_key,
        "points": points,
        "type": tx_type,
    }, None


# ---------------------------------------------------------------------------
# POST /transaction
# ---------------------------------------------------------------------------

@app.route("/transaction", methods=["POST"])
def create_transaction():
    if not request.is_json:
        return error("Content-Type must be application/json.")

    payload, err = validate_transaction_payload(request.get_json(silent=True))
    if err:
        return error(err)

    user_id = payload["user_id"]
    idempotency_key = payload["idempotency_key"]
    points = payload["points"]
    tx_type = payload["type"]

    # In-process per-user lock: serializes the read-validate-write sequence
    # for this specific user (rate limit + daily cap checks + balance write)
    # so two concurrent requests for the SAME user can't both pass the
    # checks before either has committed.
    lock = get_user_lock(user_id)
    with lock:
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")

            # --- Idempotency check -------------------------------------------------
            existing = conn.execute(
                "SELECT * FROM transactions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                conn.execute("ROLLBACK;")
                return jsonify({
                    "status": "duplicate_ignored",
                    "message": "A transaction with this idempotency_key was already processed.",
                    "transaction": dict(existing),
                }), 200

            # --- Rate limit check ---------------------------------------------------
            window_start = time.time() - RATE_LIMIT_WINDOW_SECONDS
            window_start_iso = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            recent_count = conn.execute(
                "SELECT COUNT(*) AS c FROM transactions "
                "WHERE user_id = ? AND created_at >= ?",
                (user_id, window_start_iso),
            ).fetchone()["c"]
            if recent_count >= RATE_LIMIT_MAX_REQUESTS:
                conn.execute("ROLLBACK;")
                return error(
                    "Rate limit exceeded. Too many transactions in a short window.",
                    status=429,
                )

            # --- Daily points cap (abuse prevention for 'earn'/'bonus') -------------
            day = today_str()
            if points > 0:
                earned_today = conn.execute(
                    "SELECT COALESCE(SUM(points),0) AS s FROM transactions "
                    "WHERE user_id = ? AND date(created_at) = ? AND points > 0 AND status='applied'",
                    (user_id, day),
                ).fetchone()["s"]
                if earned_today + points > DAILY_POINTS_CAP:
                    conn.execute(
                        "INSERT INTO transactions (idempotency_key, user_id, type, points, status, rejection_reason) "
                        "VALUES (?, ?, ?, ?, 'rejected', ?)",
                        (idempotency_key, user_id, tx_type, points,
                         f"Daily points cap of {DAILY_POINTS_CAP} exceeded."),
                    )
                    conn.execute("COMMIT;")
                    return error(
                        f"Daily points cap of {DAILY_POINTS_CAP} exceeded for this user.",
                        status=429,
                    )

            # --- Ensure user row exists ---------------------------------------------
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
            )

            # --- Prevent purchases from overdrawing the balance ----------------------
            if points < 0:
                current = conn.execute(
                    "SELECT total_points FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()["total_points"]
                if current + points < 0:
                    conn.execute(
                        "INSERT INTO transactions (idempotency_key, user_id, type, points, status, rejection_reason) "
                        "VALUES (?, ?, ?, ?, 'rejected', ?)",
                        (idempotency_key, user_id, tx_type, points, "Insufficient balance."),
                    )
                    conn.execute("COMMIT;")
                    return error("Insufficient balance for this purchase.", status=400)

            # --- Apply the transaction -----------------------------------------------
            cur = conn.execute(
                "INSERT INTO transactions (idempotency_key, user_id, type, points, status) "
                "VALUES (?, ?, ?, ?, 'applied')",
                (idempotency_key, user_id, tx_type, points),
            )
            tx_id = cur.lastrowid

            was_new_day = conn.execute(
                "SELECT 1 FROM user_active_days WHERE user_id = ? AND day = ?",
                (user_id, day),
            ).fetchone() is None
            if was_new_day:
                conn.execute(
                    "INSERT INTO user_active_days (user_id, day) VALUES (?, ?)",
                    (user_id, day),
                )

            conn.execute(
                "UPDATE users SET "
                "total_points = total_points + ?, "
                "transaction_count = transaction_count + 1, "
                "active_days_count = active_days_count + ?, "
                "last_transaction_at = datetime('now'), "
                "last_active_day = ? "
                "WHERE user_id = ?",
                (points, 1 if was_new_day else 0, day, user_id),
            )

            conn.execute("COMMIT;")

            row = conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (tx_id,)
            ).fetchone()
            user_row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()

            return jsonify({
                "status": "applied",
                "transaction": dict(row),
                "user_summary": {
                    "user_id": user_row["user_id"],
                    "total_points": user_row["total_points"],
                    "transaction_count": user_row["transaction_count"],
                },
            }), 201

        except sqlite3.IntegrityError:
            # Belt-and-suspenders: if a race somehow slipped past the SELECT
            # above (e.g. two processes, not just two threads), the UNIQUE
            # constraint on idempotency_key is the final backstop.
            conn.execute("ROLLBACK;")
            existing = conn.execute(
                "SELECT * FROM transactions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return jsonify({
                "status": "duplicate_ignored",
                "message": "A transaction with this idempotency_key was already processed.",
                "transaction": dict(existing) if existing else None,
            }), 200
        except Exception as e:
            conn.execute("ROLLBACK;")
            return error(f"Internal error while processing transaction: {e}", status=500)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# GET /summary/:userId
# ---------------------------------------------------------------------------

@app.route("/summary/<user_id>", methods=["GET"])
def get_summary(user_id):
    if not USER_ID_RE.match(user_id):
        return error("Invalid user_id format.")

    conn = get_connection()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not user:
            return error(f"User '{user_id}' not found.", status=404)

        recent_tx = conn.execute(
            "SELECT id, type, points, status, created_at FROM transactions "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()

        rank_info = compute_rank_for_user(conn, user_id)

        return jsonify({
            "user_id": user["user_id"],
            "total_points": user["total_points"],
            "transaction_count": user["transaction_count"],
            "active_days_count": user["active_days_count"],
            "last_transaction_at": user["last_transaction_at"],
            "rank": rank_info["rank"],
            "score": rank_info["score"],
            "recent_transactions": [dict(r) for r in recent_tx],
        }), 200
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /ranking
# ---------------------------------------------------------------------------

def fetch_all_scored_users(conn):
    rows = conn.execute(
        "SELECT user_id, total_points, transaction_count, active_days_count "
        "FROM users"
    ).fetchall()
    scored = []
    for r in rows:
        score = (r["total_points"] * WEIGHT_POINTS) + (r["active_days_count"] * WEIGHT_CONSISTENCY)
        scored.append({
            "user_id": r["user_id"],
            "total_points": r["total_points"],
            "transaction_count": r["transaction_count"],
            "active_days_count": r["active_days_count"],
            "score": round(score, 2),
        })
    # Sort by score desc; tie-break by active_days_count desc, then user_id asc
    # for a fully deterministic order (important so ranking doesn't "jitter"
    # for users who are tied -- determinism is itself a fairness property).
    scored.sort(key=lambda u: (-u["score"], -u["active_days_count"], u["user_id"]))
    for i, u in enumerate(scored, start=1):
        u["rank"] = i
    return scored


def compute_rank_for_user(conn, user_id):
    scored = fetch_all_scored_users(conn)
    for u in scored:
        if u["user_id"] == user_id:
            return u
    return {"rank": None, "score": 0}


@app.route("/ranking", methods=["GET"])
def get_ranking():
    try:
        limit = int(request.args.get("limit", 50))
        if limit <= 0 or limit > 500:
            return error("limit must be between 1 and 500.")
    except ValueError:
        return error("limit must be an integer.")

    conn = get_connection()
    try:
        scored = fetch_all_scored_users(conn)
        return jsonify({
            "ranking": scored[:limit],
            "total_users": len(scored),
            "scoring_formula": f"score = total_points * {WEIGHT_POINTS} + active_days_count * {WEIGHT_CONSISTENCY}",
        }), 200
    finally:
        conn.close()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.errorhandler(404)
def not_found(e):
    return error("Resource not found.", status=404)


@app.errorhandler(405)
def method_not_allowed(e):
    return error("Method not allowed.", status=405)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
