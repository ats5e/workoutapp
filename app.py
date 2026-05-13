import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from pymongo import MongoClient
from openai import OpenAI
import certifi

from flask import Flask, abort, g, jsonify, render_template, request
from flask_basicauth import BasicAuth

BASE_DIR = Path(__file__).parent
WORKOUTS_FILE = BASE_DIR / "workouts.json"
DEFAULT_IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises"

DEFAULT_PROFILE = {
    "name": "",
    "training_goal": "Build muscle with a focused upper-body bias.",
    "focus_area": "45-minute upper-body progression with no squat variations.",
    "preferred_session_minutes": 45,
}

DEFAULT_EXERCISE_PREFERENCES = {
    "machine-shoulder-press": {
        "status": "preferred",
        "notes": "Preferred shoulder press option when the machine is free.",
    },
    "cable-rope-rear-delt-row": {
        "status": "avoid",
        "notes": "Unavailable for you: seated rope-pull setup.",
    },
}

VALID_PREFERENCE_STATUSES = {"neutral", "preferred", "avoid"}

app = Flask(__name__)
app.config['BASIC_AUTH_USERNAME'] = os.environ.get('APP_USERNAME')
app.config['BASIC_AUTH_PASSWORD'] = os.environ.get('APP_PASSWORD')
# Only force auth if credentials are provided in env
if app.config['BASIC_AUTH_USERNAME'] and app.config['BASIC_AUTH_PASSWORD']:
    app.config['BASIC_AUTH_FORCE'] = True

basic_auth = BasicAuth(app)

CATEGORY_META = [
    {
        "id": "push",
        "label": "Push",
        "description": "Chest-led pressing and fly work.",
    },
    {
        "id": "pull",
        "label": "Pull",
        "description": "Back width, rows, and lat-focused work.",
    },
    {
        "id": "shoulders",
        "label": "Shoulders",
        "description": "Delts, rear delts, and shoulder stability.",
    },
    {
        "id": "arms",
        "label": "Arms",
        "description": "Biceps and triceps accessories.",
    },
    {
        "id": "posterior-chain",
        "label": "Posterior Chain",
        "description": "Hip hinge, hamstring, and glute work without squats.",
    },
    {
        "id": "core-calves",
        "label": "Core + Calves",
        "description": "Core bracing and lower-leg accessories.",
    },
]

CATEGORY_LABELS = {category["id"]: category["label"] for category in CATEGORY_META}
CATEGORY_RANKS = {category["id"]: index for index, category in enumerate(CATEGORY_META)}
BALANCED_CATEGORY_IDS = [category["id"] for category in CATEGORY_META]
SMART_WORKOUT_ID = "smart-session"


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


def format_exercise_weight(exercise, weight=None):
    weight = exercise.get("weight") if weight is None else weight
    fmt = exercise.get("weight_format") or "{w} kg"
    if fmt == "bodyweight":
        return "bodyweight"
    return fmt.replace("{w}", compact_number(weight or 0))


def classify_exercise(exercise):
    name_text = f"{exercise.get('id', '')} {exercise.get('name', '')}".lower()
    full_text = f"{name_text} {exercise.get('muscle_focus', '')}".lower()

    if any(token in name_text for token in ["calf", "knee raise", "hanging", "core"]):
        category_id = "core-calves"
        pattern = "core-calves"
    elif any(token in name_text for token in ["deadlift", "hip thrust", "leg curl", "hamstring", "glute"]):
        category_id = "posterior-chain"
        pattern = "hinge"
    elif any(token in name_text for token in ["shoulder", "lateral", "arnold", "rear delt", "face pull"]):
        category_id = "shoulders"
        pattern = "shoulder"
    elif any(token in name_text for token in ["pull-up", "pull up", "pulldown", "row", "lat"]):
        category_id = "pull"
        pattern = "pull"
    elif any(token in name_text for token in ["curl", "tricep", "triceps", "skullcrusher", "extension", "pushdown"]):
        category_id = "arms"
        pattern = "arm isolation"
    elif any(token in name_text for token in ["bench", "chest", "fly", "dip", "press"]):
        category_id = "push"
        pattern = "press"
    elif any(token in full_text for token in ["lat", "mid-back", "upper back"]):
        category_id = "pull"
        pattern = "pull"
    elif any(token in full_text for token in ["tricep", "triceps", "biceps", "forearms"]):
        category_id = "arms"
        pattern = "arm isolation"
    else:
        category_id = "push"
        pattern = "general"

    return {
        "category": category_id,
        "category_label": CATEGORY_LABELS.get(category_id, "Other"),
        "category_rank": CATEGORY_RANKS.get(category_id, 99),
        "movement_pattern": pattern,
    }


def infer_equipment(exercise):
    text = f"{exercise.get('id', '')} {exercise.get('name', '')}".lower()
    if "machine" in text or "pec deck" in text:
        return "Machine"
    if "cable" in text or "rope" in text or "pulldown" in text:
        return "Cable"
    if "dumbbell" in text or "db" in text or "arnold" in text:
        return "Dumbbell"
    if "barbell" in text or "ez-bar" in text or "ez-" in text:
        return "Barbell"
    if "pull-up" in text or "dip" in text or "bodyweight" in text:
        return "Bodyweight"
    return "Gym"


mongo_client = None


def get_db():
    global mongo_client
    if mongo_client is None:
        uri = os.environ.get("MONGODB_URI")
        if not uri:
            uri = "mongodb://localhost:27017/"
        
        # Enhanced connection for stability and Atlas compatibility
        mongo_client = MongoClient(
            uri, 
            tlsCAFile=certifi.where(),
            tls=True,
            retryWrites=True,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000
        )
    return mongo_client.ironlog


def get_openai_client():
    if "openai" not in g:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        g.openai = OpenAI(api_key=api_key)
    return g.openai


def init_db():
    db = get_db()
    if db.profile.count_documents({"_id": 1}) == 0:
        db.profile.insert_one({
            "_id": 1,
            "name": DEFAULT_PROFILE["name"],
            "training_goal": DEFAULT_PROFILE["training_goal"],
            "focus_area": DEFAULT_PROFILE["focus_area"],
            "preferred_session_minutes": DEFAULT_PROFILE["preferred_session_minutes"],
            "updated_at": now_iso()
        })
    for exercise_id, preference in DEFAULT_EXERCISE_PREFERENCES.items():
        if db.exercise_preferences.count_documents({"_id": exercise_id}) == 0:
            db.exercise_preferences.insert_one({
                "_id": exercise_id,
                "status": preference["status"],
                "notes": preference.get("notes", ""),
                "updated_at": now_iso()
            })


@app.before_request
def initialize():
    if not getattr(app, "db_initialized", False):
        init_db()
        app.db_initialized = True


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
    exercise_pool = [dict(exercise) for exercise in data.get("exercise_pool", [])]
    return {
        "image_base": data.get("image_base", DEFAULT_IMAGE_BASE),
        "defaults": defaults,
        "exercise_pool": exercise_pool,
        "workouts": workouts,
    }


def find_workout(workout_id):
    program = load_program()
    for workout in program["workouts"]:
        if workout["id"] == workout_id:
            return workout
    return None


def get_profile():
    db = get_db()
    row = db.profile.find_one({"_id": 1})
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
        "updated_at": now_iso()
    }
    db = get_db()
    db.profile.update_one({"_id": 1}, {"$set": profile}, upsert=True)
    return get_profile()


def normalize_preference_status(value):
    status = str(value or "neutral").strip().lower()
    return status if status in VALID_PREFERENCE_STATUSES else "neutral"


def get_exercise_preferences():
    db = get_db()
    rows = db.exercise_preferences.find()
    return {
        row["_id"]: {
            "exercise_id": row["_id"],
            "status": normalize_preference_status(row.get("status")),
            "notes": row.get("notes", ""),
            "updated_at": row.get("updated_at"),
        }
        for row in rows
    }


def save_exercise_preference(exercise_id, payload):
    preference = {
        "status": normalize_preference_status(payload.get("status")),
        "notes": clamp_text(payload.get("notes"), "", 180),
        "updated_at": now_iso(),
    }
    if not exercise_id:
        abort(400)

    db = get_db()
    db.exercise_preferences.update_one(
        {"_id": exercise_id},
        {"$set": preference},
        upsert=True
    )
    preference["exercise_id"] = exercise_id
    return preference


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
    db = get_db()
    row = db.daily_checkins.find_one({"_id": today_iso()})
    if row:
        row["checkin_date"] = row["_id"]
        return row
    return None


def save_today_checkin(payload):
    checkin = {
        "energy": safe_int(payload.get("energy"), 3, minimum=1, maximum=5),
        "sleep": safe_int(payload.get("sleep"), 3, minimum=1, maximum=5),
        "soreness": safe_int(payload.get("soreness"), 3, minimum=1, maximum=5),
        "stress": safe_int(payload.get("stress"), 3, minimum=1, maximum=5),
        "motivation": safe_int(payload.get("motivation"), 3, minimum=1, maximum=5),
        "bodyweight_kg": safe_float(payload.get("bodyweight_kg"), default=None, minimum=0),
        "step_count": safe_int(payload.get("step_count"), default=0, minimum=0),
        "notes": clamp_text(payload.get("notes"), "", 280),
        "updated_at": now_iso(),
    }
    if checkin["step_count"] == 0:
        checkin["step_count"] = None
    checkin["readiness_score"] = compute_readiness_score(checkin)
    
    db = get_db()
    db.daily_checkins.update_one(
        {"_id": today_iso()},
        {
            "$set": checkin,
            "$setOnInsert": {"created_at": now_iso()}
        },
        upsert=True
    )
    return get_today_checkin()


def list_sessions(limit=12):
    db = get_db()
    cursor = db.sessions.find().sort("completed_at", -1).limit(limit)
    return list(cursor)


def load_session_logs(row):
    return row.get("exercise_logs") or []


def get_exercise_history(exercise_id, limit=3):
    db = get_db()
    # Find sessions containing this exercise
    cursor = db.sessions.find(
        {"exercise_logs.exercise_id": exercise_id},
        {"completed_at": 1, "exercise_logs.$": 1}
    ).sort("completed_at", -1).limit(limit)
    
    history = []
    for row in cursor:
        log = row["exercise_logs"][0]
        history.append({
            "date": row["completed_at"],
            "weight": log.get("working_weight"),
            "sets": log.get("completed_sets"),
            "reps": log.get("reps")
        })
    return history


def load_session_achievements(row):
    return row.get("achievements") or []


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
    total_sessions = db.sessions.count_documents({})
    last_7_days = (date.today() - timedelta(days=6)).isoformat()
    last_30_days = (date.today() - timedelta(days=29)).isoformat()
    sessions_last_7_days = db.sessions.count_documents({"completed_at": {"$gte": last_7_days}})
    
    pipeline = [
        {"$match": {"completed_at": {"$gte": last_30_days}}},
        {"$group": {"_id": None, "total": {"$sum": "$volume_kg"}}}
    ]
    vol_res = list(db.sessions.aggregate(pipeline))
    volume_last_30_days = safe_float(
        vol_res[0]["total"] if vol_res else 0.0,
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

    smart_context = build_smart_context(build_exercise_library(program))

    return {
        "profile": profile,
        "today_checkin": today_checkin,
        "readiness": readiness,
        "workouts": workout_cards,
        "recent_sessions": recent_sessions[:6],
        "stats": stats,
        "smart_context": smart_context,
        "next_workout": next_workout,
        "coach_note": build_home_coach_note(profile, readiness, next_workout),
    }


def enrich_workout(workout):
    history = collect_workout_history()
    preferences = get_exercise_preferences()
    return enrich_workout_with_history(workout, history, preferences)


def enrich_exercise(exercise, history, source_workout=None, preferences=None):
    latest_by_exercise = history["latest_by_exercise"]
    best_by_exercise = history["best_by_exercise"]
    latest = latest_by_exercise.get(exercise["id"], {})
    best = best_by_exercise.get(exercise["id"], {})
    classification = classify_exercise(exercise)
    preference = (preferences or {}).get(exercise["id"], {})
    preference_status = normalize_preference_status(preference.get("status"))
    source = {}
    if source_workout:
        source = {
            "source_workout_id": source_workout.get("id"),
            "source_workout_name": source_workout.get("name"),
        }

    return {
        **exercise,
        **classification,
        **source,
        "equipment": exercise.get("equipment") or infer_equipment(exercise),
        "preference_status": preference_status,
        "preference_note": preference.get("notes", ""),
        "is_available": preference_status != "avoid",
        "display_weight_label": format_exercise_weight(exercise),
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


def enrich_workout_with_history(workout, history, preferences=None):
    latest_workout = history["latest_by_workout"].get(workout["id"])

    enriched = {
        **workout,
        "exercises": [],
        "latest_session": latest_workout,
    }
    for exercise in workout.get("exercises", []):
        enriched["exercises"].append(enrich_exercise(exercise, history, workout, preferences))
    return enriched


def build_exercise_library(program=None):
    program = program or load_program()
    history = collect_workout_history()
    preferences = get_exercise_preferences()
    exercises_by_id = {}

    def add_exercise(exercise, source_workout):
        enriched = enrich_exercise(exercise, history, source_workout, preferences)
        existing = exercises_by_id.get(enriched["id"])
        source = {
            "id": source_workout["id"],
            "name": source_workout["name"],
        }
        if existing:
            existing.setdefault("source_workouts", []).append(source)
            return
        enriched["source_workouts"] = [source]
        exercises_by_id[enriched["id"]] = enriched

    for workout in program["workouts"]:
        for exercise in workout.get("exercises", []):
            add_exercise(exercise, workout)

    pool_source = {"id": "exercise-pool", "name": "Exercise Pool"}
    for exercise in program.get("exercise_pool", []):
        add_exercise(exercise, pool_source)

    return sorted(
        exercises_by_id.values(),
        key=lambda exercise: (
            1 if exercise.get("preference_status") == "avoid" else 0,
            safe_int(exercise.get("category_rank"), 99),
            exercise.get("name", ""),
        ),
    )


def group_exercises_by_category(exercises):
    groups = []
    for category in CATEGORY_META:
        grouped = [
            exercise
            for exercise in exercises
            if exercise.get("category") == category["id"]
        ]
        if grouped:
            groups.append({**category, "exercises": grouped, "count": len(grouped)})
    return groups


def days_since_iso(iso_string):
    if not iso_string:
        return None
    try:
        completed_at = datetime.fromisoformat(str(iso_string).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(completed_at.tzinfo) if completed_at.tzinfo else datetime.now()
    return max(0, (now - completed_at).days)


def recency_sort_key(exercise):
    days_since = days_since_iso(exercise.get("last_completed_at"))
    return -(days_since if days_since is not None else 999)


def build_weekly_category_sets(library=None, days=7):
    library = library or build_exercise_library()
    exercise_by_id = {exercise["id"]: exercise for exercise in library}
    start_date = (date.today() - timedelta(days=days - 1)).isoformat()
    db = get_db()
    cursor = db.sessions.find({"completed_at": {"$gte": start_date}}).sort("completed_at", -1)
    
    sets_by_category = {category_id: 0 for category_id in BALANCED_CATEGORY_IDS}

    for row in cursor:
        for log in row.get("exercise_logs", []):
            exercise_id = log.get("exercise_id")
            exercise = exercise_by_id.get(exercise_id)
            if not exercise:
                continue
            category = exercise.get("category")
            if category not in sets_by_category:
                continue
            sets_by_category[category] += safe_int(
                log.get("completed_sets"), 0, minimum=0
            )

    return sets_by_category


def category_balance_bonus(category_id, weekly_category_sets=None):
    weekly_category_sets = weekly_category_sets or {}
    if category_id not in BALANCED_CATEGORY_IDS:
        return 0
    values = [
        safe_int(weekly_category_sets.get(category), 0, minimum=0)
        for category in BALANCED_CATEGORY_IDS
    ]
    if not values:
        return 0
    max_sets = max(values)
    min_sets = min(values)
    if max_sets == min_sets:
        return 0
    category_sets = safe_int(weekly_category_sets.get(category_id), 0, minimum=0)
    return (max_sets - category_sets) * 7


def recommendation_score(after, candidate, done_ids=None, weekly_category_sets=None):
    done_ids = set(done_ids or [])
    if candidate.get("preference_status") == "avoid" or not candidate.get("is_available", True):
        return -1
    if candidate["id"] == after.get("id") or candidate["id"] in done_ids:
        return -1

    category = after.get("category")
    preferred = {
        "push": ["pull", "shoulders", "arms", "push"],
        "pull": ["push", "shoulders", "arms", "pull"],
        "shoulders": ["pull", "push", "arms", "shoulders"],
        "arms": ["push", "pull", "shoulders", "arms"],
        "posterior-chain": ["pull", "push", "core-calves", "shoulders"],
        "core-calves": ["push", "pull", "shoulders", "arms"],
    }.get(category, ["push", "pull", "shoulders", "arms"])

    score = 0
    if candidate.get("category") in preferred:
        score += (len(preferred) - preferred.index(candidate["category"])) * 20
    if candidate.get("movement_pattern") != after.get("movement_pattern"):
        score += 8
    if candidate.get("category") in {"push", "pull", "shoulders", "arms"}:
        score += 6
    if candidate.get("preference_status") == "preferred":
        score += 26
    score += category_balance_bonus(candidate.get("category"), weekly_category_sets)

    days_since = days_since_iso(candidate.get("last_completed_at"))
    if days_since is None:
        score += 5
    elif days_since <= 1:
        score -= 70
    elif days_since <= 3:
        score -= 24
    elif days_since <= 6:
        score -= 8
    else:
        score += min(10, days_since // 4)

    score += max(0, 6 - safe_int(candidate.get("category_rank"), 0))
    score += min(4, safe_int(candidate.get("sets"), 0))
    return score


def recommend_exercises(
    after_id=None,
    done_ids=None,
    limit=8,
    library=None,
    unavailable_ids=None,
    weekly_category_sets=None,
):
    library = library or build_exercise_library()
    done_ids = set(done_ids or [])
    unavailable_ids = set(unavailable_ids or [])
    weekly_category_sets = weekly_category_sets or build_weekly_category_sets(library)
    after = next((exercise for exercise in library if exercise["id"] == after_id), None)

    if after is None:
        candidates = [
            exercise
            for exercise in library
            if exercise["id"] not in done_ids
            and exercise["id"] not in unavailable_ids
            and exercise.get("preference_status") != "avoid"
            and exercise.get("is_available", True)
            and exercise.get("category") in {"push", "pull", "shoulders", "arms"}
        ]
        candidates.sort(
            key=lambda exercise: (
                0 if exercise.get("preference_status") == "preferred" else 1,
                -category_balance_bonus(exercise.get("category"), weekly_category_sets),
                recency_sort_key(exercise),
                safe_int(exercise.get("category_rank"), 99),
                -safe_int(exercise.get("sets"), 0),
                exercise.get("name", ""),
            )
        )
        return {"after": None, "recommendations": candidates[:limit]}

    scored = []
    for candidate in library:
        if candidate["id"] in unavailable_ids:
            continue
        score = recommendation_score(after, candidate, done_ids, weekly_category_sets)
        if score >= 0:
            scored.append((score, candidate))

    scored.sort(
        key=lambda item: (
            -item[0],
            safe_int(item[1].get("category_rank"), 99),
            item[1].get("name", ""),
        )
    )
    return {
        "after": after,
        "recommendations": [candidate for _score, candidate in scored[:limit]],
    }


def latest_session_exercise_logs():
    db = get_db()
    row = db.sessions.find_one(sort=[("completed_at", -1)])
    if row is None:
        return None, []
    return summarize_session(row), load_session_logs(row)


def build_smart_context(library):
    latest_session, logs = latest_session_exercise_logs()
    preferences = get_exercise_preferences()
    weekly_category_sets = build_weekly_category_sets(library)
    preferred = [
        exercise
        for exercise in library
        if exercise.get("preference_status") == "preferred"
    ]
    avoided = [
        exercise
        for exercise in library
        if exercise.get("preference_status") == "avoid"
    ]

    return {
        "latest_session": latest_session,
        "latest_exercises": [
            {
                "id": log.get("exercise_id"),
                "name": log.get("exercise_name"),
            }
            for log in logs
            if log.get("exercise_id")
        ][:8],
        "available_count": len([exercise for exercise in library if exercise.get("is_available", True)]),
        "preferred_count": len(preferred),
        "avoided_count": len(avoided),
        "preferred_names": [exercise["name"] for exercise in preferred[:4]],
        "avoided_names": [exercise["name"] for exercise in avoided[:4]],
        "preference_count": len(preferences),
        "weekly_category_sets": weekly_category_sets,
        "weekly_balance": [
            {
                "id": category["id"],
                "label": category["label"],
                "sets": weekly_category_sets.get(category["id"], 0),
            }
            for category in CATEGORY_META
        ],
    }


def build_smart_queue(
    library,
    start_exercise=None,
    done_ids=None,
    limit=6,
    basis_exercise=None,
    unavailable_ids=None,
    weekly_category_sets=None,
):
    done_ids = set(done_ids or [])
    unavailable_ids = set(unavailable_ids or [])
    weekly_category_sets = weekly_category_sets or build_weekly_category_sets(library)
    exercises = []
    queued_ids = set()

    if (
        start_exercise
        and start_exercise.get("preference_status") != "avoid"
        and start_exercise["id"] not in unavailable_ids
    ):
        exercises.append(start_exercise)
        queued_ids.add(start_exercise["id"])

    basis = start_exercise or basis_exercise
    while len(exercises) < limit:
        excluded = done_ids | queued_ids
        recommendations = recommend_exercises(
            after_id=basis["id"] if basis else None,
            done_ids=excluded,
            limit=12,
            library=library,
            unavailable_ids=unavailable_ids,
            weekly_category_sets=weekly_category_sets,
        )["recommendations"]
        next_exercise = recommendations[0] if recommendations else None
        if not next_exercise:
            break
        exercises.append(next_exercise)
        queued_ids.add(next_exercise["id"])
        basis = next_exercise

    return exercises


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
        "exercise_library": build_exercise_library(),
        "initial_done_exercise_ids": [],
        "initial_current_exercise_id": None,
        "coach_tip": coach_tip,
        "latest_session": latest_session,
    }
    return model


def build_smart_workout_page_model(start_id=None, done_id=None):
    program = load_program()
    profile = get_profile()
    today_checkin = get_today_checkin()
    readiness = build_readiness_state(today_checkin)
    library = build_exercise_library(program)
    exercise_by_id = {exercise["id"]: exercise for exercise in library}
    weekly_category_sets = build_weekly_category_sets(library)

    start_exercise = exercise_by_id.get(start_id) if start_id else None
    done_exercise = exercise_by_id.get(done_id) if done_id else None
    if start_id and start_exercise is None:
        return None
    if done_id and done_exercise is None:
        return None
    if start_exercise and start_exercise.get("preference_status") == "avoid":
        start_exercise = None

    done_ids = [done_exercise["id"]] if done_exercise else []
    basis = start_exercise or done_exercise
    recommendations = recommend_exercises(
        after_id=basis["id"] if basis else None,
        done_ids=done_ids,
        limit=7,
        library=library,
        weekly_category_sets=weekly_category_sets,
    )["recommendations"]

    exercises = []
    if done_exercise:
        exercises.append(done_exercise)
    for recommendation in build_smart_queue(
        library,
        start_exercise=start_exercise,
        done_ids=done_ids,
        limit=6 if profile["preferred_session_minutes"] <= 50 else 7,
        basis_exercise=done_exercise,
        weekly_category_sets=weekly_category_sets,
    ):
        if recommendation["id"] not in {exercise["id"] for exercise in exercises}:
            exercises.append(recommendation)

    if not exercises:
        exercises = recommend_exercises(
            limit=6,
            library=library,
            weekly_category_sets=weekly_category_sets,
        )["recommendations"]

    initial_current = None
    if start_exercise:
        initial_current = start_exercise["id"]
    elif recommendations:
        initial_current = recommendations[0]["id"]
    elif exercises:
        initial_current = exercises[0]["id"]

    if done_exercise:
        coach_tip = (
            f"Logged {done_exercise['name']} first. The next options balance that work and keep the session moving."
        )
    elif start_exercise:
        coach_tip = (
            f"Starting with {start_exercise['name']}. Use the suggested load, then pick the best available follow-up."
        )
    else:
        coach_tip = "Pick the first available movement, then let the app steer the rest of the 45-minute lift."

    return {
        "profile": profile,
        "today_checkin": today_checkin,
        "readiness": readiness,
        "exercise_library": library,
        "initial_done_exercise_ids": done_ids,
        "initial_current_exercise_id": initial_current,
        "coach_tip": coach_tip,
        "latest_session": None,
        "smart_context": build_smart_context(library),
        "workout": {
            "id": SMART_WORKOUT_ID,
            "name": "Smart Gym Session",
            "description": "Flexible exercise-first session",
            "hypertrophy_focus": "Start with the equipment that is free, then balance the session with complementary upper-body volume.",
            "session_tips": [
                "Use your 15-minute cardio walk before opening the lifting flow.",
                "Choose the first available movement, then alternate stress where possible: press into pull, pull into press, shoulders into back or arms.",
                "Keep the same progression rule: when every set reaches the top of the rep range cleanly, add the listed jump next time.",
            ],
            "target_minutes": 45,
            "warmup": {"label": "Warm-up already done", "minutes": 0},
            "cooldown": {"label": "Optional cool-down", "minutes": 0},
            "smart_mode": True,
            "exercises": exercises,
        },
    }


def build_exercise_library_page_model():
    exercises = build_exercise_library()
    return {
        "categories": CATEGORY_META,
        "groups": group_exercises_by_category(exercises),
        "exercises": exercises,
        "total": len(exercises),
    }


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
    db = get_db()
    previous_same_workout = db.sessions.find_one(
        {"workout_id": payload["workout_id"], "_id": {"$ne": payload.get("id")}},
        sort=[("completed_at", -1)]
    )

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
    workout_id = payload.get("workout_id")
    workout = find_workout(workout_id)
    if workout is None and workout_id == SMART_WORKOUT_ID:
        workout = {
            "id": SMART_WORKOUT_ID,
            "name": clamp_text(payload.get("workout_name"), "Smart Gym Session", 80),
        }
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
    normalized["_id"] = normalized["id"]
    db.sessions.update_one(
        {"_id": normalized["_id"]},
        {"$set": normalized},
        upsert=True
    )

    row = db.sessions.find_one({"_id": normalized["_id"]})
    return {
        "session": summarize_session(dict(row)),
        "achievements": normalized["achievements"],
        "coach_note": build_post_session_coach_note(normalized),
    }


COACH_ALIAS_OVERRIDES = {
    "bench": "barbell-bench-press",
    "bench press": "barbell-bench-press",
    "db shoulder press": "seated-db-shoulder-press",
    "dumbbell shoulder press": "seated-db-shoulder-press",
    "machine shoulder press": "machine-shoulder-press",
    "lat pulldown": "wide-grip-lat-pulldown",
    "pulldown": "wide-grip-lat-pulldown",
    "row": "seated-cable-row",
    "lateral raise": "lateral-raises",
    "lateral raises": "lateral-raises",
    "rope pull": "cable-rope-rear-delt-row",
    "rope pulls": "cable-rope-rear-delt-row",
}


def normalize_search_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def build_exercise_aliases(library):
    by_id = {exercise["id"]: exercise for exercise in library}
    aliases = []
    for alias, exercise_id in COACH_ALIAS_OVERRIDES.items():
        exercise = by_id.get(exercise_id)
        if exercise:
            aliases.append((normalize_search_text(alias), exercise))

    for exercise in library:
        names = {
            exercise.get("name", ""),
            exercise.get("id", "").replace("-", " "),
        }
        normalized_name = normalize_search_text(exercise.get("name", ""))
        if normalized_name.startswith("seated "):
            names.add(normalized_name.replace("seated ", "", 1))
        for token in ["dumbbell", "barbell", "machine", "cable", "db"]:
            names.add(normalized_name.replace(token, "").strip())

        for name in names:
            alias = normalize_search_text(name)
            if len(alias) >= 5:
                aliases.append((alias, exercise))

    deduped = {}
    for alias, exercise in aliases:
        deduped.setdefault(alias, exercise)
    return sorted(deduped.items(), key=lambda item: len(item[0]), reverse=True)


def extract_exercise_mentions(message, library):
    normalized = f" {normalize_search_text(message)} "
    matches = []
    seen = set()
    for alias, exercise in build_exercise_aliases(library):
        if f" {alias} " not in normalized:
            continue
        if exercise["id"] in seen:
            continue
        matches.append(exercise)
        seen.add(exercise["id"])
    return matches


def save_coach_message(role, content, exercise_ids=None):
    message = {
        "_id": str(uuid.uuid4()),
        "role": role,
        "content": clamp_text(content, "", 1200),
        "exercise_ids": list(dict.fromkeys(exercise_ids or []))[:12],
        "created_at": now_iso(),
    }
    db = get_db()
    db.coach_messages.insert_one(message)
    message["id"] = message["_id"]
    return message


def list_coach_messages(limit=12):
    db = get_db()
    cursor = db.coach_messages.find().sort("created_at", -1).limit(limit)
    messages = []
    for doc in cursor:
        doc["id"] = doc["_id"]
        doc["created_at_label"] = format_relative_date(doc["created_at"])
        messages.append(doc)
    return messages


def recent_coach_exercise_ids(limit=10):
    ids = []
    for message in list_coach_messages(limit=30):
        if message["role"] != "user":
            continue
        for exercise_id in message.get("exercise_ids", []):
            if exercise_id not in ids:
                ids.append(exercise_id)
            if len(ids) >= limit:
                return ids
    return ids


def latest_logged_exercise_id():
    _session, logs = latest_session_exercise_logs()
    for log in logs:
        if log.get("exercise_id"):
            return log["exercise_id"]
    recent_ids = recent_coach_exercise_ids(limit=1)
    return recent_ids[0] if recent_ids else None


def exercise_card(exercise):
    return {
        "id": exercise["id"],
        "name": exercise["name"],
        "category": exercise.get("category"),
        "category_label": exercise.get("category_label"),
        "sets": exercise.get("sets"),
        "reps": exercise.get("reps"),
        "target": format_exercise_weight(exercise),
        "equipment": exercise.get("equipment"),
        "preference_status": exercise.get("preference_status"),
        "start_url": f"/smart?start={exercise['id']}",
    }


def build_coach_context(program=None):
    program = program or load_program()
    dashboard = build_dashboard(program)
    library = build_exercise_library(program)
    latest_session, latest_logs = latest_session_exercise_logs()
    recent_messages = list_coach_messages(limit=10)
    basis_id = latest_logged_exercise_id()
    recent_done = recent_coach_exercise_ids(limit=8)
    if basis_id and basis_id not in recent_done:
        recent_done.append(basis_id)
    weekly_category_sets = build_weekly_category_sets(library)
    recommendations = recommend_exercises(
        after_id=basis_id,
        done_ids=recent_done,
        limit=5,
        library=library,
        weekly_category_sets=weekly_category_sets,
    )["recommendations"]

    return {
        "profile": dashboard["profile"],
        "readiness": dashboard["readiness"],
        "next_workout": dashboard["next_workout"],
        "recent_sessions": dashboard["recent_sessions"][:4],
        "latest_session": latest_session,
        "latest_exercises": [
            {"id": log.get("exercise_id"), "name": log.get("exercise_name")}
            for log in latest_logs
            if log.get("exercise_id")
        ][:6],
        "recent_messages": list(reversed(recent_messages)),
        "recommendations": [exercise_card(exercise) for exercise in recommendations],
        "memory_count": len(recent_messages),
        "weekly_balance": build_smart_context(library)["weekly_balance"],
    }


def call_openai_coach(user_message, context, library, limit=5):
    try:
        client = get_openai_client()
        
        db = get_db()
        recent_full_sessions = list(db.sessions.find().sort("completed_at", -1).limit(3))
        detailed_history = []
        for s in recent_full_sessions:
            detailed_history.append({
                "workout_name": s["workout_name"],
                "completed_at": s["completed_at"],
                "exercise_logs": [
                    {
                        "exercise_name": log.get("exercise_name"),
                        "completed_sets": log.get("completed_sets"),
                        "reps": log.get("reps"),
                        "working_weight": log.get("working_weight")
                    } for log in s.get("exercise_logs", [])
                ]
            })
            
        personal_bests = current_personal_best_map()

        system_prompt = f"""
        You are an intelligent AI bodybuilding coach for the Iron Log app.
        User Profile: {context['profile']['name']}
        Training Goal: {context['profile']['training_goal']}
        Focus Area: {context['profile']['focus_area']}
        Current Readiness Score: {context['readiness']['score']}/100 ({context['readiness']['label']}).
        
        Based on the user's detailed recent sessions (which include weights and reps), personal bests, fatigue, and their current message, provide a coaching response and recommend up to {limit} exercise IDs for them to do next.
        You MUST consider previous workouts and weight progressions to plan a workout based on their needs.
        If the user asks to skip an exercise, you MUST NOT recommend it, and suggest another suitable one.
        
        Reply strictly in JSON format:
        {{
            "reply": "Your conversational, encouraging, and intelligent coaching message here.",
            "recommended_exercise_ids": ["exercise-id-1", "exercise-id-2"]
        }}
        """
        
        available_exercises = [{"id": ex["id"], "name": ex["name"], "category": ex["category"]} 
                               for ex in library if ex.get("preference_status") != "avoid"]
        
        user_prompt = f"""
        Detailed Recent History (Weights & Reps): {json.dumps(detailed_history)}
        Personal Bests (Exercise ID -> Max Weight): {json.dumps(personal_bests)}
        Available Exercises: {json.dumps(available_exercises)}
        
        User Message: "{user_message}"
        """
        
        response = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.5"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)
        return data.get("reply", "I've logged that. Let's keep moving."), data.get("recommended_exercise_ids", [])
    except Exception as e:
        print(f"OpenAI Error: {e}")
        return "I had trouble processing that with AI, but I've updated your local memory.", []


def build_coach_reply(user_message):
    program = load_program()
    library = build_exercise_library(program)
    mentions = extract_exercise_mentions(user_message, library)
    mentioned_ids = [exercise["id"] for exercise in mentions]
    if user_message.strip():
        save_coach_message("user", user_message, mentioned_ids)

    context = build_coach_context(program)
    
    reply_text, ai_recommended_ids = call_openai_coach(user_message, context, library, limit=5)
    
    recommendations = []
    exercise_by_id = {ex["id"]: ex for ex in library}
    for eid in ai_recommended_ids:
        if eid in exercise_by_id:
            recommendations.append(exercise_by_id[eid])
            
    if not recommendations:
        basis_id = mentioned_ids[-1] if mentioned_ids else latest_logged_exercise_id()
        done_ids = list(dict.fromkeys(mentioned_ids + recent_coach_exercise_ids(limit=8)))
        recommendations = recommend_exercises(
            after_id=basis_id,
            done_ids=done_ids,
            limit=5,
            library=library,
            weekly_category_sets=build_weekly_category_sets(library),
        )["recommendations"]
        if not reply_text or "trouble processing" in reply_text:
            reply_text = "I've checked your history. Here are some local recommendations to keep the session moving."

    save_coach_message("assistant", reply_text, [exercise["id"] for exercise in recommendations[:4]])

    return {
        "reply": reply_text,
        "mentioned_exercises": [exercise_card(exercise) for exercise in mentions],
        "recommendations": [exercise_card(exercise) for exercise in recommendations],
        "context": build_coach_context(program),
    }


def build_coach_page_model():
    context = build_coach_context(load_program())
    greeting = (
        "Tell me what you did, what equipment is free, or ask what to do next. "
        "I will use your saved sessions, exercise preferences, and muscle-growth goal."
    )
    return {
        "context": context,
        "greeting": greeting,
        "suggested_prompts": [
            "What should I train next today?",
            "I did machine shoulder press and lateral raises yesterday.",
            "Plan a 45 minute muscle growth session from what is free.",
        ],
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


@app.route("/smart")
def smart_workout():
    program = load_program()
    model = build_smart_workout_page_model(
        start_id=request.args.get("start"),
        done_id=request.args.get("done"),
    )
    if model is None:
        abort(404)
    return render_template(
        "workout.html",
        model=model,
        image_base=program["image_base"],
    )


@app.route("/exercises")
def exercises():
    return render_template(
        "exercises.html",
        model=build_exercise_library_page_model(),
        image_base=load_program()["image_base"],
    )


@app.route("/coach")
def coach():
    return render_template("coach.html", model=build_coach_page_model())


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


@app.route("/api/exercises")
def api_exercises():
    model = build_exercise_library_page_model()
    return jsonify(model)


@app.route("/api/exercise-preferences/<exercise_id>", methods=["POST"])
def api_exercise_preference(exercise_id):
    payload = request.get_json(silent=True) or {}
    preference = save_exercise_preference(exercise_id, payload)
    return jsonify({"preference": preference})


@app.route("/api/coach/context")
def api_coach_context():
    return jsonify(build_coach_context(load_program()))


@app.route("/api/coach/chat", methods=["POST"])
def api_coach_chat():
    payload = request.get_json(silent=True) or {}
    message = clamp_text(payload.get("message"), "", 1200)
    if not message:
        return jsonify({"error": "Message is required"}), 400
    return jsonify(build_coach_reply(message))


@app.route("/api/recommendations")
def api_recommendations():
    done = [
        item
        for item in (request.args.get("done") or "").split(",")
        if item
    ]
    unavailable = [
        item
        for item in (request.args.get("unavailable") or "").split(",")
        if item
    ]
    limit = safe_int(request.args.get("limit"), 8, minimum=1, maximum=12)
    return jsonify(
        recommend_exercises(
            after_id=request.args.get("after"),
            done_ids=done,
            limit=limit,
            unavailable_ids=unavailable,
        )
    )


@app.route("/api/smart-engine/recommend", methods=["POST"])
def api_smart_engine_recommend():
    payload = request.get_json(silent=True) or {}
    done_exercises = payload.get("done_exercises", [])
    unavailable_ids = payload.get("unavailable_ids", [])
    target_exercise_id = payload.get("target_exercise_id")
    current_exercise_id = payload.get("current_exercise_id")
    readiness = payload.get("readiness", {})

    program = load_program()
    library = build_exercise_library(program)
    smart_context = build_smart_context(library)
    profile = get_profile()
    weekly_balance = smart_context.get("weekly_balance", [])
    
    available_library = [
        ex for ex in library 
        if ex.get("id") not in unavailable_ids 
        and ex.get("id") not in done_exercises
        and ex.get("preference_status") != "avoid"
        and ex.get("is_available", True)
    ]
    
    lib_summary = [
        f"{ex.get('id')}: {ex.get('name')} (Cat: {ex.get('category_label')}, Default: {ex.get('sets')}x{ex.get('reps')})"
        for ex in available_library
    ]
    
    # Fetch history for the specific target exercise if requested
    exercise_history = []
    if target_exercise_id:
        exercise_history = get_exercise_history(target_exercise_id, limit=1)
    elif current_exercise_id:
        exercise_history = get_exercise_history(current_exercise_id, limit=1)

    system_prompt = f"""You are an elite AI strength and conditioning coach inside the Iron Log app.
Your task is to either assign target loads/reps for a SPECIFIC chosen exercise, OR suggest the NEXT best exercise for the user based on their current session and weekly balance.

Core Coaching Philosophy:
1. Progressive Overload: If the user is 'Ready', push for a small increase in weight (2.5kg) or 1-2 extra reps compared to their last performance.
2. Volume Management: Avoid over-taxing muscle groups that have high volume in the 'Weekly Category Balance'.
3. Efficiency: Keep rest periods appropriate for the lift (heavy compound = more rest, isolation = less rest).

Respond ONLY with a valid JSON object matching this schema, with no markdown formatting or extra text:
{{
  "exercise_id": "the exact ID of the chosen exercise from the library",
  "target_sets": (integer) recommended sets,
  "target_reps": "(string) recommended reps (e.g. '8-10')",
  "target_weight_kg": (number) recommended weight in kg based on their history. (use 0 for bodyweight),
  "target_rest_seconds": (integer) recommended rest time in seconds (e.g. 90, 120),
  "coach_tip": "(string) 1-2 short, punchy sentences explaining the logic (e.g. 'Pushing for +2.5kg today because your readiness is high.')"
}}

Context:
User Profile Goals: {json.dumps(profile)}
Weekly Category Balance (so far this week): {json.dumps(weekly_balance)}
User Readiness today: {json.dumps(readiness.get('label'))}
Recent Performance for this/last exercise: {json.dumps(exercise_history)}
Already completed this session: {json.dumps(done_exercises)}
Available Library: {json.dumps(lib_summary)}
"""
    
    user_msg = "Please prescribe the next exercise and its targets."
    if target_exercise_id:
        user_msg = f"The user has explicitly chosen to do the exercise with ID: '{target_exercise_id}'. Please prescribe the target sets, reps, weight, and rest for this specific exercise."
    elif current_exercise_id:
        current_name = current_exercise_id
        for ex in library:
             if ex["id"] == current_exercise_id:
                  current_name = ex["name"]
                  break
        user_msg = f"The user just finished or is currently doing the exercise: '{current_name}'. Suggest the next best exercise from the library."

    client = get_openai_client()
    if not client.api_key:
        return jsonify({"error": "OpenAI API key not configured"}), 500

    try:
        response = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.5"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg}
            ],
            response_format={"type": "json_object"},
            max_tokens=250,
            temperature=0.7
        )
        content = response.choices[0].message.content
        return jsonify(json.loads(content))
    except Exception as e:
        app.logger.error(f"OpenAI smart engine error: {e}")
        return jsonify({"error": "AI Engine failed to generate recommendation"}), 500


@app.route("/api/sessions", methods=["POST"])
def api_sessions():
    payload = request.get_json(silent=True) or {}
    saved = save_session(payload)
    
    # Generate a premium AI debrief for the session
    debrief = "Great session today! Your progress is being tracked."
    try:
        debrief = generate_coach_debrief(payload)
    except Exception as e:
        app.logger.error(f"Debrief error: {e}")
        
    saved["coach_debrief"] = debrief
    return jsonify(saved), 201


def generate_coach_debrief(session_data):
    profile = get_profile()
    session_summary = {
        "workout_name": session_data.get("workout_name"),
        "duration": f"{session_data.get('duration_seconds', 0) // 60} mins",
        "exercises": [
            f"{log.get('exercise_name')}: {log.get('completed_sets')} sets @ {log.get('working_weight')}kg"
            for log in session_data.get("exercise_logs", [])
        ]
    }
    
    system_prompt = f"""You are an elite strength coach. The user just finished a workout. 
Provide a short (2-3 sentence) professional debrief summarizing their effort and giving them one piece of positive reinforcement or a tip for next time based on their goals: {json.dumps(profile.get('training_goal'))}.
Keep it punchy, motivating, and elite. No markdown."""

    client = get_openai_client()
    if not client.api_key:
        return "Session saved. Configure OpenAI for AI debriefs."

    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.5"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Session Stats: {json.dumps(session_summary)}"}
        ],
        max_tokens=150,
        temperature=0.8
    )
    return response.choices[0].message.content.strip()


@app.route("/healthz")
def healthz():
    ensure_runtime_dirs()
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
