import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request

BASE_DIR = Path(__file__).parent
WORKOUTS_FILE = BASE_DIR / "workouts.json"
DEFAULT_IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises"

DEFAULT_PROFILE = {
    "name": "",
    "training_goal": "Build muscle with a focused upper-body bias.",
    "focus_area": "45-minute upper-body progression with no squat variations.",
    "preferred_session_minutes": 45,
}

app = Flask(__name__)


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def today_iso():
    return date.today().isoformat()


def safe_int(value, default=0, minimum=None, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def safe_float(value, default=None, minimum=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def compact_number(value):
    if value is None:
        return None
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def clamp_text(value, fallback="", max_length=280):
    text = (value or fallback or "").strip()
    return text[:max_length]


def parse_reps_number(value):
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else 0.0


def format_duration(seconds):
    minutes = round(max(0, seconds) / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    rem = minutes % 60
    if rem == 0:
        return f"{hours}h"
    return f"{hours}h {rem}m"


def format_relative_date(iso_string):
    if not iso_string:
        return None
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return None
    delta_days = (date.today() - dt.date()).days
    if delta_days <= 0:
        return "Today"
    if delta_days == 1:
        return "Yesterday"
    if delta_days < 7:
        return f"{delta_days}d ago"
    if delta_days < 30:
        weeks = delta_days // 7
        return f"{weeks}w ago"
    return dt.strftime("%b %d")


def get_database_path():
    configured = os.environ.get("DATABASE_PATH")
    if configured:
        return Path(configured)
    return BASE_DIR / "instance" / "ironlog.db"


def ensure_runtime_dirs():
    db_path = get_database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_db():
    if "db" not in g:
        db_path = ensure_runtime_dirs()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL DEFAULT '',
            training_goal TEXT NOT NULL DEFAULT '',
            focus_area TEXT NOT NULL DEFAULT '',
            preferred_session_minutes INTEGER NOT NULL DEFAULT 45,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS daily_checkins (
            checkin_date TEXT PRIMARY KEY,
            energy INTEGER NOT NULL,
            sleep INTEGER NOT NULL,
            soreness INTEGER NOT NULL,
            stress INTEGER NOT NULL,
            motivation INTEGER NOT NULL,
            bodyweight_kg REAL,
            step_count INTEGER,
            notes TEXT NOT NULL DEFAULT '',
            readiness_score INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            workout_id TEXT NOT NULL,
            workout_name TEXT NOT NULL,
            week INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL DEFAULT 0,
            warmup_seconds INTEGER NOT NULL DEFAULT 0,
            cooldown_seconds INTEGER NOT NULL DEFAULT 0,
            completed_sets INTEGER NOT NULL DEFAULT 0,
            skipped_sets INTEGER NOT NULL DEFAULT 0,
            total_sets INTEGER NOT NULL DEFAULT 0,
            volume_kg REAL NOT NULL DEFAULT 0,
            readiness_score INTEGER,
            energy INTEGER,
            notes TEXT NOT NULL DEFAULT '',
            session_feeling INTEGER,
            achievements_json TEXT NOT NULL DEFAULT '[]',
            exercise_logs_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_completed_at
            ON sessions (completed_at DESC);

        CREATE INDEX IF NOT EXISTS idx_sessions_workout_completed
            ON sessions (workout_id, completed_at DESC);
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO profile (
            id, name, training_goal, focus_area, preferred_session_minutes
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            1,
            DEFAULT_PROFILE["name"],
            DEFAULT_PROFILE["training_goal"],
            DEFAULT_PROFILE["focus_area"],
            DEFAULT_PROFILE["preferred_session_minutes"],
        ),
    )
    conn.execute(
        """
        UPDATE profile
        SET preferred_session_minutes = ?
        WHERE id = 1 AND preferred_session_minutes = 90
        """,
        (DEFAULT_PROFILE["preferred_session_minutes"],),
    )
    conn.commit()


def load_data():
    with open(WORKOUTS_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def with_defaults(workout, defaults):
    merged = {
        **workout,
        "exercises": [dict(exercise) for exercise in workout.get("exercises", [])],
    }
    if defaults.get("warmup") and "warmup" not in merged:
        merged["warmup"] = dict(defaults["warmup"])
    if defaults.get("cooldown") and "cooldown" not in merged:
        merged["cooldown"] = dict(defaults["cooldown"])
    return merged


def load_program():
    data = load_data()
    defaults = data.get("defaults", {})
    workouts = [with_defaults(workout, defaults) for workout in data.get("workouts", [])]
    return {
        "image_base": data.get("image_base", DEFAULT_IMAGE_BASE),
        "defaults": defaults,
        "workouts": workouts,
    }


def find_workout(workout_id):
    program = load_program()
    for workout in program["workouts"]:
        if workout["id"] == workout_id:
            return workout
    return None


def get_profile():
    row = get_db().execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if row is None:
        return dict(DEFAULT_PROFILE)
    profile = dict(row)
    profile["preferred_session_minutes"] = safe_int(
        profile.get("preferred_session_minutes"),
        DEFAULT_PROFILE["preferred_session_minutes"],
        minimum=30,
        maximum=180,
    )
    return profile


def save_profile(payload):
    profile = {
        "name": clamp_text(payload.get("name"), "", 60),
        "training_goal": clamp_text(
            payload.get("training_goal"), DEFAULT_PROFILE["training_goal"], 220
        ),
        "focus_area": clamp_text(
            payload.get("focus_area"), DEFAULT_PROFILE["focus_area"], 220
        ),
        "preferred_session_minutes": safe_int(
            payload.get("preferred_session_minutes"),
            DEFAULT_PROFILE["preferred_session_minutes"],
            minimum=30,
            maximum=180,
        ),
    }
    db = get_db()
    db.execute(
        """
        UPDATE profile
        SET name = ?, training_goal = ?, focus_area = ?,
            preferred_session_minutes = ?, updated_at = ?
        WHERE id = 1
        """,
        (
            profile["name"],
            profile["training_goal"],
            profile["focus_area"],
            profile["preferred_session_minutes"],
            now_iso(),
        ),
    )
    db.commit()
    return get_profile()


def compute_readiness_score(payload):
    energy = safe_int(payload.get("energy"), 3, minimum=1, maximum=5)
    sleep = safe_int(payload.get("sleep"), 3, minimum=1, maximum=5)
    soreness = safe_int(payload.get("soreness"), 3, minimum=1, maximum=5)
    stress = safe_int(payload.get("stress"), 3, minimum=1, maximum=5)
    motivation = safe_int(payload.get("motivation"), 3, minimum=1, maximum=5)
    weighted = (
        energy * 30
        + sleep * 25
        + motivation * 20
        + (6 - soreness) * 15
        + (6 - stress) * 10
    )
    return round(weighted / 5)


def build_readiness_state(checkin):
    if not checkin:
        return {
            "score": None,
            "label": "No check-in",
            "headline": "Log a quick readiness check before you lift.",
            "body": "It only takes a few taps and lets the app adjust coaching cues to how you actually feel today.",
            "tone": "slate",
            "weight_adjustment": "normal",
        }

    score = safe_int(checkin.get("readiness_score"), 60, minimum=0, maximum=100)
    if score >= 82:
        return {
            "score": score,
            "label": "Prime",
            "headline": "High-readiness day. Lean into the programmed load.",
            "body": "You are sleeping, recovering, and showing up well. Warm up properly, then take the main lifts seriously.",
            "tone": "emerald",
            "weight_adjustment": "push",
        }
    if score >= 68:
        return {
            "score": score,
            "label": "Ready",
            "headline": "Solid day. Run the plan as written.",
            "body": "This is a good training day. Keep the rest periods honest and aim for crisp, repeatable reps.",
            "tone": "accent",
            "weight_adjustment": "normal",
        }
    if score >= 52:
        return {
            "score": score,
            "label": "Steady",
            "headline": "Medium-readiness day. Own the technique and hold steady.",
            "body": "You can still stack a great session here. Keep one rep in reserve and prioritize clean movement.",
            "tone": "amber",
            "weight_adjustment": "hold",
        }
    return {
        "score": score,
        "label": "Recover",
        "headline": "Low-readiness day. Keep the session moving but back off the aggression.",
        "body": "Treat today as a technique-and-consistency win. Smooth reps, shorter ego, better recovery tomorrow.",
        "tone": "rose",
        "weight_adjustment": "ease",
    }


def get_today_checkin():
    row = get_db().execute(
        "SELECT * FROM daily_checkins WHERE checkin_date = ?", (today_iso(),)
    ).fetchone()
    return dict(row) if row else None


def save_today_checkin(payload):
    checkin = {
        "checkin_date": today_iso(),
        "energy": safe_int(payload.get("energy"), 3, minimum=1, maximum=5),
        "sleep": safe_int(payload.get("sleep"), 3, minimum=1, maximum=5),
        "soreness": safe_int(payload.get("soreness"), 3, minimum=1, maximum=5),
        "stress": safe_int(payload.get("stress"), 3, minimum=1, maximum=5),
        "motivation": safe_int(payload.get("motivation"), 3, minimum=1, maximum=5),
        "bodyweight_kg": safe_float(payload.get("bodyweight_kg"), default=None, minimum=0),
        "step_count": safe_int(payload.get("step_count"), default=0, minimum=0),
        "notes": clamp_text(payload.get("notes"), "", 280),
    }
    if checkin["step_count"] == 0:
        checkin["step_count"] = None
    checkin["readiness_score"] = compute_readiness_score(checkin)
    db = get_db()
    db.execute(
        """
        INSERT INTO daily_checkins (
            checkin_date, energy, sleep, soreness, stress, motivation,
            bodyweight_kg, step_count, notes, readiness_score, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(checkin_date) DO UPDATE SET
            energy = excluded.energy,
            sleep = excluded.sleep,
            soreness = excluded.soreness,
            stress = excluded.stress,
            motivation = excluded.motivation,
            bodyweight_kg = excluded.bodyweight_kg,
            step_count = excluded.step_count,
            notes = excluded.notes,
            readiness_score = excluded.readiness_score,
            updated_at = excluded.updated_at
        """,
        (
            checkin["checkin_date"],
            checkin["energy"],
            checkin["sleep"],
            checkin["soreness"],
            checkin["stress"],
            checkin["motivation"],
            checkin["bodyweight_kg"],
            checkin["step_count"],
            checkin["notes"],
            checkin["readiness_score"],
            now_iso(),
            now_iso(),
        ),
    )
    db.commit()
    return get_today_checkin()


def list_sessions(limit=12):
    rows = get_db().execute(
        "SELECT * FROM sessions ORDER BY completed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_session_logs(row):
    return json.loads(row.get("exercise_logs_json") or "[]")


def load_session_achievements(row):
    return json.loads(row.get("achievements_json") or "[]")


def summarize_session(row):
    achievements = load_session_achievements(row)
    return {
        "id": row["id"],
        "workout_id": row["workout_id"],
        "workout_name": row["workout_name"],
        "week": row["week"],
        "completed_at": row["completed_at"],
        "completed_at_label": format_relative_date(row["completed_at"]),
        "duration_seconds": row["duration_seconds"],
        "duration_label": format_duration(row["duration_seconds"]),
        "completed_sets": row["completed_sets"],
        "skipped_sets": row["skipped_sets"],
        "total_sets": row["total_sets"],
        "volume_kg": row["volume_kg"],
        "volume_label": (
            f"{compact_number(row['volume_kg'])} kg volume"
            if row["volume_kg"]
            else "Movement logged"
        ),
        "notes": row["notes"],
        "session_feeling": row["session_feeling"],
        "achievements": achievements,
    }


def collect_workout_history():
    sessions = list_sessions(limit=60)
    latest_by_workout = {}
    latest_by_exercise = {}
    best_by_exercise = {}
    ordered_summaries = []

    for row in sessions:
        ordered_summaries.append(summarize_session(row))
        workout_id = row["workout_id"]
        if workout_id not in latest_by_workout:
            latest_by_workout[workout_id] = summarize_session(row)

        completed_at = row["completed_at"]
        logs = load_session_logs(row)
        for log in logs:
            exercise_id = log.get("exercise_id")
            if not exercise_id:
                continue

            working_weight = safe_float(log.get("working_weight"), default=None, minimum=0)
            latest_entry = latest_by_exercise.get(exercise_id)
            if latest_entry is None:
                latest_by_exercise[exercise_id] = {
                    "last_logged_weight": working_weight,
                    "last_logged_label": log.get("working_weight_label"),
                    "last_completed_at": completed_at,
                    "last_completed_label": format_relative_date(completed_at),
                    "last_completed_sets": safe_int(log.get("completed_sets"), 0, minimum=0),
                    "last_target_sets": safe_int(log.get("target_sets"), 0, minimum=0),
                    "last_reps": log.get("reps"),
                    "last_notes": log.get("notes") or "",
                }

            if working_weight is None:
                continue
            best_entry = best_by_exercise.get(exercise_id)
            if best_entry is None or working_weight > best_entry["weight"]:
                best_by_exercise[exercise_id] = {
                    "weight": working_weight,
                    "label": log.get("working_weight_label"),
                    "completed_at": completed_at,
                    "completed_label": format_relative_date(completed_at),
                }

    return {
        "latest_by_workout": latest_by_workout,
        "latest_by_exercise": latest_by_exercise,
        "best_by_exercise": best_by_exercise,
        "recent_sessions": ordered_summaries,
    }


def workout_ids_in_order(workouts):
    return [workout["id"] for workout in workouts]


def next_workout_id(workouts, last_workout_id=None):
    order = workout_ids_in_order(workouts)
    if not order:
        return None
    if not last_workout_id or last_workout_id not in order:
        return order[0]
    idx = order.index(last_workout_id)
    return order[(idx + 1) % len(order)]


def compute_cycle_progress(workouts, recent_sessions):
    workout_ids = set(workout_ids_in_order(workouts))
    completed = []
    seen = set()
    for session in recent_sessions:
        workout_id = session["workout_id"]
        if workout_id not in workout_ids or workout_id in seen:
            break
        seen.add(workout_id)
        completed.append(workout_id)
        if len(seen) == len(workout_ids):
            break
    return {
        "completed": len(seen),
        "total": len(workout_ids),
        "label": f"{len(seen)}/{len(workout_ids)} workouts in your current rotation"
        if workout_ids
        else "No workouts loaded",
    }


def build_home_coach_note(profile, readiness, next_workout):
    name = profile.get("name") or "Athlete"
    if next_workout is None:
        return {
            "title": f"{name}, your plan is empty.",
            "body": "Add workouts to workouts.json and this dashboard will turn into your command center.",
        }

    if readiness["score"] is None:
        return {
            "title": f"{name}, your next move is {next_workout['name']}.",
            "body": "Log today's check-in first so the app can tune the coaching cues before you start.",
        }

    if readiness["weight_adjustment"] == "ease":
        return {
            "title": f"{name}, keep {next_workout['name']} smooth today.",
            "body": "Use the first ramp-up sets to settle in, hold the load steady, and treat perfect reps as the win condition.",
        }

    if readiness["weight_adjustment"] == "hold":
        return {
            "title": f"{name}, {next_workout['name']} is still the right call.",
            "body": "Run the session with clean technique and keep one rep in reserve on the big lifts.",
        }

    return {
        "title": f"{name}, you're lined up for {next_workout['name']}.",
        "body": "Your readiness looks solid. Start with crisp ramp-up sets, then go chase confident work sets.",
    }


def build_dashboard(program):
    profile = get_profile()
    today_checkin = get_today_checkin()
    readiness = build_readiness_state(today_checkin)
    history = collect_workout_history()
    recent_sessions = history["recent_sessions"]
    last_session = recent_sessions[0] if recent_sessions else None
    next_id = next_workout_id(
        program["workouts"], last_session["workout_id"] if last_session else None
    )

    workout_cards = []
    next_workout = None
    for workout in program["workouts"]:
        latest = history["latest_by_workout"].get(workout["id"])
        total_session_minutes = (
            safe_int(workout.get("target_minutes"), 60, minimum=0)
            + safe_int(workout.get("warmup", {}).get("minutes"), 0, minimum=0)
            + safe_int(workout.get("cooldown", {}).get("minutes"), 0, minimum=0)
        )
        card = {
            **workout,
            "exercise_count": len(workout.get("exercises", [])),
            "total_session_minutes": total_session_minutes,
            "last_completed_label": latest["completed_at_label"] if latest else "Not logged yet",
            "last_volume_label": latest["volume_label"] if latest else "Fresh start",
            "is_next": workout["id"] == next_id,
            "coach_tag": "Up next" if workout["id"] == next_id else "In rotation",
        }
        workout_cards.append(card)
        if workout["id"] == next_id:
            next_workout = card

    db = get_db()
    total_sessions = safe_int(
        db.execute("SELECT COUNT(*) AS total FROM sessions").fetchone()["total"],
        0,
        minimum=0,
    )
    last_7_days = (date.today() - timedelta(days=6)).isoformat()
    last_30_days = (date.today() - timedelta(days=29)).isoformat()
    sessions_last_7_days = safe_int(
        db.execute(
            "SELECT COUNT(*) AS total FROM sessions WHERE substr(completed_at, 1, 10) >= ?",
            (last_7_days,),
        ).fetchone()["total"],
        0,
        minimum=0,
    )
    volume_last_30_days = safe_float(
        db.execute(
            "SELECT COALESCE(SUM(volume_kg), 0) AS total FROM sessions WHERE substr(completed_at, 1, 10) >= ?",
            (last_30_days,),
        ).fetchone()["total"],
        default=0.0,
        minimum=0,
    )

    stats = {
        "total_sessions": total_sessions,
        "sessions_last_7_days": sessions_last_7_days,
        "volume_last_30_days": volume_last_30_days,
        "volume_last_30_days_label": f"{compact_number(volume_last_30_days)} kg",
        "cycle_progress": compute_cycle_progress(program["workouts"], recent_sessions),
        "last_session_label": last_session["completed_at_label"] if last_session else "No sessions yet",
    }

    return {
        "profile": profile,
        "today_checkin": today_checkin,
        "readiness": readiness,
        "workouts": workout_cards,
        "recent_sessions": recent_sessions[:6],
        "stats": stats,
        "next_workout": next_workout,
        "coach_note": build_home_coach_note(profile, readiness, next_workout),
    }


def enrich_workout(workout):
    history = collect_workout_history()
    latest_by_exercise = history["latest_by_exercise"]
    best_by_exercise = history["best_by_exercise"]
    latest_workout = history["latest_by_workout"].get(workout["id"])

    enriched = {
        **workout,
        "exercises": [],
        "latest_session": latest_workout,
    }
    for exercise in workout.get("exercises", []):
        latest = latest_by_exercise.get(exercise["id"], {})
        best = best_by_exercise.get(exercise["id"], {})
        enriched["exercises"].append(
            {
                **exercise,
                "last_logged_weight": latest.get("last_logged_weight"),
                "last_logged_label": latest.get("last_logged_label"),
                "last_completed_at": latest.get("last_completed_at"),
                "last_completed_label": latest.get("last_completed_label"),
                "last_completed_sets": latest.get("last_completed_sets"),
                "last_target_sets": latest.get("last_target_sets"),
                "last_reps": latest.get("last_reps"),
                "last_notes": latest.get("last_notes"),
                "personal_best_weight": best.get("weight"),
                "personal_best_label": best.get("label"),
                "personal_best_completed_at": best.get("completed_at"),
                "personal_best_completed_label": best.get("completed_label"),
            }
        )
    return enriched


def build_workout_page_model(workout_id):
    workout = find_workout(workout_id)
    if workout is None:
        return None

    profile = get_profile()
    today_checkin = get_today_checkin()
    readiness = build_readiness_state(today_checkin)
    enriched = enrich_workout(workout)
    latest_session = enriched.get("latest_session")

    if readiness["weight_adjustment"] == "ease":
        coach_tip = "Low-readiness day: keep the session smooth, hold the load, and leave a rep in reserve."
    elif readiness["weight_adjustment"] == "hold":
        coach_tip = "Middle-gear day: own the reps, then decide whether to push once the first compound set lands clean."
    else:
        coach_tip = "You look ready. Use the first ramp-up sets to lock in, then attack the main lift with intent."

    model = {
        "profile": profile,
        "today_checkin": today_checkin,
        "readiness": readiness,
        "workout": enriched,
        "coach_tip": coach_tip,
        "latest_session": latest_session,
    }
    return model


def current_personal_best_map():
    best_map = {}
    for row in list_sessions(limit=120):
        for log in load_session_logs(row):
            exercise_id = log.get("exercise_id")
            if not exercise_id:
                continue
            logged_weight = safe_float(log.get("working_weight"), default=None, minimum=0)
            if logged_weight is None:
                continue
            existing = best_map.get(exercise_id)
            if existing is None or logged_weight > existing:
                best_map[exercise_id] = logged_weight
    return best_map


def build_session_achievements(payload):
    achievements = []
    prior_bests = current_personal_best_map()
    previous_same_workout = get_db().execute(
        """
        SELECT volume_kg, completed_at
        FROM sessions
        WHERE workout_id = ? AND id != ?
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (payload["workout_id"], payload["id"]),
    ).fetchone()

    for log in payload["exercise_logs"]:
        exercise_id = log.get("exercise_id")
        working_weight = safe_float(log.get("working_weight"), default=None, minimum=0)
        completed_sets = safe_int(log.get("completed_sets"), 0, minimum=0)
        if not exercise_id or working_weight is None or completed_sets <= 0:
            continue
        if working_weight > prior_bests.get(exercise_id, -1):
            achievements.append(
                f"New logged high on {log.get('exercise_name')}: {log.get('working_weight_label')}."
            )

    if payload["completed_sets"] == payload["total_sets"] and payload["total_sets"] > 0:
        achievements.append("Completed every prescribed set in the session.")

    if payload["readiness_score"] is not None and payload["readiness_score"] < 52:
        achievements.append("Showed up and finished the work on a low-readiness day.")

    if previous_same_workout and payload["volume_kg"] > safe_float(
        previous_same_workout["volume_kg"], default=0.0, minimum=0
    ):
        achievements.append("Beat your last logged volume on this workout.")

    return achievements[:4]


def build_post_session_coach_note(payload):
    if payload["completed_sets"] == payload["total_sets"] and payload["total_sets"] > 0:
        return "Strong session. You cleared the full prescription, banked the work, and gave future-you a clean number to beat."
    if payload["skipped_sets"] > 0:
        return "Useful session. You still moved the needle, and the log now reflects what the day actually gave you."
    return "Session saved. Keep stacking honest reps and let the history chart your momentum."


def save_session(payload):
    workout = find_workout(payload.get("workout_id"))
    if workout is None:
        abort(404)

    session_id = payload.get("session_id") or str(uuid.uuid4())
    exercise_logs = []
    for exercise in payload.get("exercise_logs", []):
        working_weight = safe_float(exercise.get("working_weight"), default=0.0, minimum=0)
        completed_sets = safe_int(exercise.get("completed_sets"), 0, minimum=0)
        skipped_sets = safe_int(exercise.get("skipped_sets"), 0, minimum=0)
        target_sets = safe_int(exercise.get("target_sets"), 0, minimum=0)
        reps = exercise.get("reps")
        reps_value = parse_reps_number(reps)
        volume_kg = round(working_weight * reps_value * completed_sets, 1)
        exercise_logs.append(
            {
                "exercise_id": exercise.get("exercise_id"),
                "exercise_name": clamp_text(exercise.get("exercise_name"), "", 80),
                "reps": reps,
                "completed_sets": completed_sets,
                "skipped_sets": skipped_sets,
                "target_sets": target_sets,
                "working_weight": working_weight,
                "working_weight_label": clamp_text(
                    exercise.get("working_weight_label"), "", 40
                ),
                "suggested_weight": safe_float(
                    exercise.get("suggested_weight"), default=None, minimum=0
                ),
                "suggested_weight_label": clamp_text(
                    exercise.get("suggested_weight_label"), "", 40
                ),
                "notes": clamp_text(exercise.get("notes"), "", 160),
                "volume_kg": volume_kg,
            }
        )

    duration_seconds = safe_int(payload.get("duration_seconds"), 0, minimum=0)
    warmup_seconds = safe_int(payload.get("warmup_seconds"), 0, minimum=0)
    cooldown_seconds = safe_int(payload.get("cooldown_seconds"), 0, minimum=0)
    completed_sets = sum(log["completed_sets"] for log in exercise_logs)
    skipped_sets = sum(log["skipped_sets"] for log in exercise_logs)
    total_sets = sum(log["target_sets"] for log in exercise_logs)
    volume_kg = round(sum(log["volume_kg"] for log in exercise_logs), 1)

    normalized = {
        "id": session_id,
        "workout_id": workout["id"],
        "workout_name": workout["name"],
        "week": safe_int(payload.get("week"), 1, minimum=1),
        "started_at": payload.get("started_at") or now_iso(),
        "completed_at": payload.get("completed_at") or now_iso(),
        "duration_seconds": duration_seconds,
        "warmup_seconds": warmup_seconds,
        "cooldown_seconds": cooldown_seconds,
        "completed_sets": completed_sets,
        "skipped_sets": skipped_sets,
        "total_sets": total_sets,
        "volume_kg": volume_kg,
        "readiness_score": safe_int(
            payload.get("readiness_score"), default=0, minimum=0, maximum=100
        )
        or None,
        "energy": safe_int(payload.get("energy"), default=0, minimum=0, maximum=5) or None,
        "notes": clamp_text(payload.get("notes"), "", 400),
        "session_feeling": safe_int(
            payload.get("session_feeling"), default=0, minimum=0, maximum=5
        )
        or None,
        "exercise_logs": exercise_logs,
    }

    normalized["achievements"] = build_session_achievements(normalized)
    db = get_db()
    db.execute(
        """
        INSERT OR REPLACE INTO sessions (
            id, workout_id, workout_name, week, started_at, completed_at,
            duration_seconds, warmup_seconds, cooldown_seconds,
            completed_sets, skipped_sets, total_sets, volume_kg,
            readiness_score, energy, notes, session_feeling,
            achievements_json, exercise_logs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized["id"],
            normalized["workout_id"],
            normalized["workout_name"],
            normalized["week"],
            normalized["started_at"],
            normalized["completed_at"],
            normalized["duration_seconds"],
            normalized["warmup_seconds"],
            normalized["cooldown_seconds"],
            normalized["completed_sets"],
            normalized["skipped_sets"],
            normalized["total_sets"],
            normalized["volume_kg"],
            normalized["readiness_score"],
            normalized["energy"],
            normalized["notes"],
            normalized["session_feeling"],
            json.dumps(normalized["achievements"]),
            json.dumps(normalized["exercise_logs"]),
        ),
    )
    db.commit()

    row = db.execute("SELECT * FROM sessions WHERE id = ?", (normalized["id"],)).fetchone()
    return {
        "session": summarize_session(dict(row)),
        "achievements": normalized["achievements"],
        "coach_note": build_post_session_coach_note(normalized),
    }


@app.route("/")
def index():
    program = load_program()
    dashboard = build_dashboard(program)
    return render_template(
        "index.html",
        dashboard=dashboard,
        image_base=program["image_base"],
    )


@app.route("/workout/<workout_id>")
def workout(workout_id):
    program = load_program()
    model = build_workout_page_model(workout_id)
    if model is None:
        abort(404)
    return render_template(
        "workout.html",
        model=model,
        image_base=program["image_base"],
    )


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(build_dashboard(load_program()))


@app.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        profile = save_profile(payload)
        return jsonify({"profile": profile})
    return jsonify({"profile": get_profile()})


@app.route("/api/checkins/today", methods=["GET", "POST"])
def api_checkin_today():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        checkin = save_today_checkin(payload)
        return jsonify(
            {
                "checkin": checkin,
                "readiness": build_readiness_state(checkin),
                "coach_note": build_dashboard(load_program())["coach_note"],
            }
        )

    checkin = get_today_checkin()
    return jsonify(
        {
            "checkin": checkin,
            "readiness": build_readiness_state(checkin),
        }
    )


@app.route("/api/history")
def api_history():
    return jsonify({"sessions": collect_workout_history()["recent_sessions"]})


@app.route("/api/workouts")
def api_workouts():
    program = load_program()
    workouts = [enrich_workout(workout) for workout in program["workouts"]]
    return jsonify(
        {
            "defaults": program["defaults"],
            "image_base": program["image_base"],
            "workouts": workouts,
        }
    )


@app.route("/api/workouts/<workout_id>")
def api_workout(workout_id):
    workout = find_workout(workout_id)
    if workout is None:
        abort(404)
    return jsonify(enrich_workout(workout))


@app.route("/api/sessions", methods=["POST"])
def api_sessions():
    payload = request.get_json(silent=True) or {}
    saved = save_session(payload)
    return jsonify(saved), 201


@app.route("/healthz")
def healthz():
    ensure_runtime_dirs()
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
