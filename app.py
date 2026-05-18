import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from pymongo import MongoClient
from openai import OpenAI
import certifi
import httpx

from flask import Flask, abort, g, jsonify, render_template, request, url_for
try:
    from flask_basicauth import BasicAuth
except ImportError:
    class BasicAuth:
        def __init__(self, app=None):
            self.app = app

        def authenticate(self):
            return True

        def challenge(self):
            return ("Authentication required", 401)

from services.weight_suggestion import suggest_weight
from services.workout_generation import generate_workout, replacement_candidates, role_for_exercise

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
# Auth is handled via before_request to allow /healthz to be public
app.config['BASIC_AUTH_FORCE'] = False

basic_auth = BasicAuth(app)

@app.before_request
def require_auth():
    # Only enforce if credentials are set in environment
    if not app.config.get('BASIC_AUTH_USERNAME') or not app.config.get('BASIC_AUTH_PASSWORD'):
        return
    # Exempt health check and static files if needed (though usually fine to protect static)
    if request.endpoint in ['healthz', 'static']:
        return
    if not basic_auth.authenticate():
        return basic_auth.challenge()

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
        # Use httpx with certifi for secure SSL verification on Render
        http_client = httpx.Client(verify=certifi.where())
        g.openai = OpenAI(api_key=api_key, http_client=http_client)
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
    cursor = (
        db.sessions.find({"completed_at": {"$exists": True, "$ne": None}})
        .sort("completed_at", -1)
        .limit(limit)
    )
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
        "id": row.get("id") or row.get("_id"),
        "workout_id": row.get("workout_id", ""),
        "workout_name": row.get("workout_name", "Workout"),
        "week": safe_int(row.get("week"), 1, minimum=1),
        "completed_at": row.get("completed_at"),
        "completed_at_label": format_relative_date(row.get("completed_at")),
        "duration_seconds": safe_int(row.get("duration_seconds"), 0, minimum=0),
        "duration_label": format_duration(safe_int(row.get("duration_seconds"), 0, minimum=0)),
        "completed_sets": safe_int(row.get("completed_sets"), 0, minimum=0),
        "skipped_sets": safe_int(row.get("skipped_sets"), 0, minimum=0),
        "total_sets": safe_int(row.get("total_sets"), 0, minimum=0),
        "volume_kg": safe_float(row.get("volume_kg"), default=0.0, minimum=0),
        "volume_label": (
            f"{compact_number(safe_float(row.get('volume_kg'), default=0.0, minimum=0))} kg volume"
            if row.get("volume_kg")
            else "Movement logged"
        ),
        "notes": row.get("notes", ""),
        "session_feeling": row.get("session_feeling"),
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


def find_library_exercise(exercise_id, library=None):
    library = library or build_exercise_library()
    return next((exercise for exercise in library if exercise.get("id") == exercise_id), None)


def build_generated_workout_from_ids(library, starting_exercise_id, exercise_ids, coach_tip=None):
    exercise_by_id = {exercise["id"]: exercise for exercise in library}
    start = exercise_by_id.get(starting_exercise_id)
    if start is None:
        return None

    ordered_ids = [starting_exercise_id]
    for exercise_id in exercise_ids or []:
        if isinstance(exercise_id, dict):
            exercise_id = exercise_id.get("id") or exercise_id.get("exercise_id")
        if exercise_id and exercise_id not in ordered_ids:
            ordered_ids.append(exercise_id)

    exercises = []
    total_sets = 0
    for exercise_id in ordered_ids:
        exercise = exercise_by_id.get(exercise_id)
        if not exercise:
            continue
        if exercise.get("preference_status") == "avoid" or exercise.get("is_available") is False:
            continue
        sets = safe_int(exercise.get("sets"), 0, minimum=0)
        if exercises and total_sets >= 12 and total_sets + sets > 18:
            continue
        if total_sets + sets > 18:
            continue
        exercises.append(exercise)
        total_sets += sets
        if total_sets >= 12:
            break

    if not exercises or exercises[0]["id"] != starting_exercise_id or total_sets < 12:
        return None

    return {
        "starting_exercise": start,
        "movement_pattern": start.get("movement_pattern") or start.get("category"),
        "category": start.get("category"),
        "total_sets": total_sets,
        "coach_tip": coach_tip or "",
        "generation_engine": "openai",
        "exercises": [
            {
                **exercise,
                "role": "main" if index == 0 else "accessory",
                "generation_role": role_for_exercise(exercise, is_start=index == 0),
            }
            for index, exercise in enumerate(exercises)
        ],
        "recovery_excluded_categories": [],
    }


def ai_generate_workout(library, starting_exercise_id, recent_sessions, target_set_cap=15):
    if not os.environ.get("OPENAI_API_KEY"):
        return None

    start = find_library_exercise(starting_exercise_id, library)
    if start is None:
        return None

    available = [
        {
            "id": exercise["id"],
            "name": exercise["name"],
            "category": exercise.get("category"),
            "movement_pattern": exercise.get("movement_pattern"),
            "equipment": exercise.get("equipment"),
            "sets": exercise.get("sets"),
            "reps": exercise.get("reps"),
            "muscle_focus": exercise.get("muscle_focus"),
            "preference_status": exercise.get("preference_status"),
        }
        for exercise in library
        if exercise.get("preference_status") != "avoid" and exercise.get("is_available", True)
    ]
    recent = [
        {
            "completed_at": session.get("completed_at"),
            "workout_name": session.get("workout_name"),
            "movement_pattern": session.get("movement_pattern"),
            "exercise_ids": [
                log.get("exercise_id")
                for log in session.get("exercise_logs", [])
                if log.get("exercise_id")
            ],
        }
        for session in (recent_sessions or [])[:8]
    ]
    system_prompt = """You generate concise workout plans for Iron Log.
Return only JSON with:
{
  "exercise_ids": ["starting-id-first", "accessory-id", "..."],
  "coach_tip": "one short reason"
}
Rules: include the starting exercise first, use only IDs from the provided library, keep total working sets between 12 and 18, avoid unavailable/avoid exercises, avoid recently hammered categories where practical, and choose complementary accessories."""
    user_prompt = {
        "starting_exercise": {
            "id": start["id"],
            "name": start["name"],
            "category": start.get("category"),
            "movement_pattern": start.get("movement_pattern"),
        },
        "target_set_cap": target_set_cap,
        "recent_sessions": recent,
        "library": available,
    }

    try:
        response = get_openai_client().chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt)},
            ],
            response_format={"type": "json_object"},
            max_tokens=700,
            temperature=0.35,
        )
        data = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        app.logger.warning(f"AI workout generation failed, using local fallback: {exc}")
        return None

    return build_generated_workout_from_ids(
        library,
        starting_exercise_id,
        data.get("exercise_ids") or data.get("exercises") or [],
        coach_tip=data.get("coach_tip") or data.get("reason"),
    )


def latest_exercise_performance(exercise_id):
    db = get_db()
    cursor = (
        db.sessions.find({"completed_at": {"$exists": True, "$ne": None}})
        .sort("completed_at", -1)
        .limit(200)
    )

    for row in cursor:
        for session_exercise in row.get("session_exercises", []):
            if session_exercise.get("exercise_id") != exercise_id or session_exercise.get("skipped"):
                continue
            sets = session_exercise.get("sets") or []
            completed_sets = [item for item in sets if item.get("completed")]
            if not completed_sets:
                continue
            weights = [
                safe_float(item.get("actual_weight"), default=None, minimum=0)
                for item in completed_sets
            ]
            weights = [weight for weight in weights if weight is not None]
            return {
                "date": row.get("completed_at"),
                "weight": weights[-1] if weights else None,
                "target_sets": len(sets),
                "completed_sets": len(completed_sets),
                "reps": completed_sets[-1].get("suggested_reps") if completed_sets else None,
                "sets": completed_sets,
            }

        for log in row.get("exercise_logs", []):
            if log.get("exercise_id") != exercise_id:
                continue
            return {
                "date": row.get("completed_at"),
                "weight": safe_float(log.get("working_weight"), default=None, minimum=0),
                "target_sets": safe_int(log.get("target_sets"), 0, minimum=0),
                "completed_sets": safe_int(log.get("completed_sets"), 0, minimum=0),
                "reps": log.get("reps"),
                "sets": log.get("set_logs") or [],
            }
    return None


def build_prescribed_sets(exercise, suggestion):
    target_sets = safe_int(exercise.get("sets"), 0, minimum=1, maximum=8)
    return [
        {
            "id": str(uuid.uuid4()),
            "order": index + 1,
            "suggested_weight": suggestion["suggested_weight"],
            "actual_weight": None,
            "suggested_reps": suggestion.get("suggested_reps") or exercise.get("reps"),
            "actual_reps": None,
            "completed": False,
        }
        for index in range(target_sets)
    ]


def build_session_exercise(exercise, order, role=None, replaced_exercise_id=None):
    suggestion = suggest_weight(exercise, latest_exercise_performance(exercise["id"]))
    return {
        "id": str(uuid.uuid4()),
        "exercise_id": exercise["id"],
        "role": role or exercise.get("role") or ("main" if order == 1 else "accessory"),
        "generation_role": exercise.get("generation_role"),
        "order": order,
        "was_regenerated": replaced_exercise_id is not None,
        "replaced_exercise_id": replaced_exercise_id,
        "skipped": False,
        "suggestion": suggestion,
        "sets": build_prescribed_sets(exercise, suggestion),
    }


def public_session(row):
    session = dict(row)
    session["id"] = session.get("id") or session.get("_id")
    if "_id" in session:
        session["_id"] = str(session["_id"])
    return session


def start_generated_session(starting_exercise_id):
    program = load_program()
    library = build_exercise_library(program)
    profile = get_profile()
    cap = 18 if profile.get("preferred_session_minutes", 45) > 50 else 15
    recent_sessions = list_sessions(limit=80)
    generated = ai_generate_workout(
        library,
        starting_exercise_id,
        recent_sessions=recent_sessions,
        target_set_cap=cap,
    )
    if generated is None:
        generated = generate_workout(
            library,
            starting_exercise_id,
            recent_sessions=recent_sessions,
            target_set_cap=cap,
        )
        generated["generation_engine"] = "local"
    else:
        generated.setdefault("generation_engine", "openai")

    local_recovery = generate_workout(
        library,
        starting_exercise_id,
        recent_sessions=recent_sessions,
        target_set_cap=cap,
    )
    generated["recovery_excluded_categories"] = local_recovery.get(
        "recovery_excluded_categories", generated.get("recovery_excluded_categories", [])
    )

    session_id = str(uuid.uuid4())
    start = generated["starting_exercise"]
    session_exercises = [
        build_session_exercise(exercise, index + 1, role=exercise.get("role"))
        for index, exercise in enumerate(generated["exercises"])
    ]
    workout_name = f"{start['name']} Session"
    now = now_iso()
    doc = {
        "_id": session_id,
        "id": session_id,
        "status": "active",
        "source": "generated",
        "workout_id": f"generated-{session_id}",
        "workout_name": workout_name,
        "started_at": now,
        "completed_at": None,
        "date": today_iso(),
        "starting_exercise_id": start["id"],
        "starting_exercise_name": start["name"],
        "movement_pattern": generated["movement_pattern"],
        "category": generated["category"],
        "target_minutes": profile.get("preferred_session_minutes", 45),
        "target_sets": generated["total_sets"],
        "generation_engine": generated.get("generation_engine", "local"),
        "generation_note": generated.get("coach_tip", ""),
        "recovery_excluded_categories": generated.get("recovery_excluded_categories", []),
        "excluded_exercise_ids": [],
        "excluded_equipment": [],
        "session_exercises": session_exercises,
        "created_at": now,
        "updated_at": now,
    }
    get_db().sessions.insert_one(doc)
    return public_session(doc)


def active_session_to_model(row):
    row = public_session(row)
    program = load_program()
    library = build_exercise_library(program)
    exercise_by_id = {exercise["id"]: exercise for exercise in library}
    exercises = []

    for item in sorted(row.get("session_exercises", []), key=lambda ex: ex.get("order", 0)):
        exercise = exercise_by_id.get(item.get("exercise_id"))
        if not exercise:
            continue
        sets = item.get("sets") or []
        reps = sets[0].get("suggested_reps") if sets else exercise.get("reps")
        enriched = {
            **exercise,
            "sets": len(sets) or safe_int(exercise.get("sets"), 0, minimum=1),
            "reps": reps or exercise.get("reps"),
            "session_exercise_id": item.get("id"),
            "role": item.get("role"),
            "was_regenerated": item.get("was_regenerated", False),
            "replaced_exercise_id": item.get("replaced_exercise_id"),
            "skipped": item.get("skipped", False),
            "session_sets": sets,
            "suggestion": item.get("suggestion") or {},
        }
        exercises.append(enriched)

    profile = get_profile()
    today_checkin = get_today_checkin()
    readiness = build_readiness_state(today_checkin)
    return {
        "profile": profile,
        "today_checkin": today_checkin,
        "readiness": readiness,
        "exercise_library": library,
        "initial_done_exercise_ids": [],
        "initial_current_exercise_id": row.get("starting_exercise_id"),
        "active_session_id": row["id"],
        "active_session": row,
        "coach_tip": (
            f"Starting with {row.get('starting_exercise_name', 'your first lift')}. "
            f"{'AI generated this route.' if row.get('generation_engine') == 'openai' else 'Local generator built this route.'} "
            "Log each set as you go and regenerate anything that is busy."
        ),
        "latest_session": None,
        "workout": {
            "id": row.get("workout_id") or row["id"],
            "name": row.get("workout_name") or "Generated Session",
            "description": "Generated from today's starting exercise",
            "hypertrophy_focus": "Generated from your exercise choice, recent history, and equipment preferences.",
            "session_tips": [
                "Use the suggested load as the first target, then adjust if warm-ups say otherwise.",
                "Regenerate a movement when equipment is busy so the session keeps moving.",
                "Log actual reps as honestly as possible; future progression uses those numbers.",
            ],
            "target_minutes": row.get("target_minutes") or profile.get("preferred_session_minutes", 45),
            "warmup": {"label": "Untimed prep", "minutes": 0},
            "cooldown": {"label": "Optional cool-down", "minutes": 0},
            "generated_session": True,
            "generation_engine": row.get("generation_engine", "local"),
            "generation_note": row.get("generation_note", ""),
            "smart_mode": False,
            "exercises": exercises,
        },
    }


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
    row = db.sessions.find_one(
        {"completed_at": {"$exists": True, "$ne": None}},
        sort=[("completed_at", -1)]
    )
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
    session_id = payload.get("session_id") or str(uuid.uuid4())
    db = get_db()
    existing_session = db.sessions.find_one({"_id": session_id})
    workout = find_workout(workout_id)
    if workout is None and workout_id == SMART_WORKOUT_ID:
        workout = {
            "id": SMART_WORKOUT_ID,
            "name": clamp_text(payload.get("workout_name"), "Smart Gym Session", 80),
        }
    if workout is None and existing_session and existing_session.get("source") == "generated":
        workout = {
            "id": workout_id or existing_session.get("workout_id") or f"generated-{session_id}",
            "name": clamp_text(
                payload.get("workout_name") or existing_session.get("workout_name"),
                "Generated Session",
                80,
            ),
        }
    if workout is None:
        abort(404)

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
                "set_logs": exercise.get("set_logs") or [],
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
        "status": "completed",
        "source": payload.get("source") or (existing_session or {}).get("source") or "program",
        "starting_exercise_id": payload.get("starting_exercise_id")
        or (existing_session or {}).get("starting_exercise_id"),
        "movement_pattern": payload.get("movement_pattern")
        or (existing_session or {}).get("movement_pattern"),
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
        if not client.api_key:
            return "Coach is currently offline (Missing API Key).", []
        
        db = get_db()
        # Safely fetch recent sessions
        try:
            recent_full_sessions = list(db.sessions.find().sort("completed_at", -1).limit(3))
        except Exception as db_err:
            app.logger.error(f"DB Fetch Error in Chat: {db_err}")
            recent_full_sessions = []

        detailed_history = []
        for s in recent_full_sessions:
            detailed_history.append({
                "workout_name": s.get("workout_name", "Unknown Workout"),
                "completed_at": str(s.get("completed_at", "Unknown")),
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

        system_prompt = f"""You are the Elite Iron Log AI Coach (Persona: GPT-5.5). 
Your mission: Provide HYPER-PERSONALIZED, DATA-DRIVEN coaching.

CORE INSTRUCTIONS:
1. ANALYSIS: Use the 'Detailed Recent History' and 'Personal Bests'. If the user asks for a workout, check what they did last and suggest a logical progression.
2. PRESCRIPTION: Be specific. Recommend Exercises, Sets, Reps, and Weights.
3. PERSONALITY: Authoritative, elite, and motivating. User: {context['profile']['name']}.

Response Schema (STRICT JSON):
{{
    "reply": "Your conversational coaching advice here.",
    "recommended_exercise_ids": ["id1", "id2"]
}}"""

        available_exercises = [{"id": ex["id"], "name": ex["name"], "category": ex["category"]} 
                               for ex in library if ex.get("preference_status") != "avoid"]
        
        user_prompt = f"""
        User Context: {json.dumps(context['profile'])}
        Readiness: {json.dumps(context['readiness'])}
        Recent History: {json.dumps(detailed_history)}
        Personal Bests: {json.dumps(personal_bests)}
        Library: {json.dumps(available_exercises)}
        
        User Message: "{user_message}"
        """
        
        model_name = os.environ.get("OPENAI_MODEL", "gpt-4o") # Safer default
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            max_tokens=800,
            temperature=0.8
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        return data.get("reply", "I've analyzed your data. Let's get to work."), data.get("recommended_exercise_ids", [])
    except Exception as e:
        app.logger.error(f"Elite Coach AI Error: {e}")
        return f"Coach encountered a cognitive error ({str(e)[:50]}...). I'm switching to local guidance.", []


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
        if not reply_text:
            reply_text = "I've analyzed your stats. Here are my elite recommendations for your next session."

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
    library = build_exercise_library(program)
    return render_template(
        "index.html",
        dashboard=dashboard,
        start_model={
            "exercises": library,
            "profile": dashboard["profile"],
            "stats": dashboard["stats"],
            "readiness": dashboard["readiness"],
            "recent_sessions": dashboard["recent_sessions"],
        },
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


@app.route("/session/<session_id>")
def active_session(session_id):
    row = get_db().sessions.find_one({"_id": session_id})
    if row is None:
        abort(404)
    return render_template(
        "workout.html",
        model=active_session_to_model(row),
        image_base=load_program()["image_base"],
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


@app.route("/api/exercises/suggest-weight")
def api_exercise_suggest_weight():
    exercise_id = request.args.get("exercise_id") or request.args.get("exerciseId")
    exercise = find_library_exercise(exercise_id)
    if exercise is None:
        return jsonify({"error": "Exercise not found"}), 404
    return jsonify(
        suggest_weight(exercise, latest_exercise_performance(exercise_id))
    )


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
    substitute_for_id = payload.get("substitute_for_id")
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
    # Fetch history for the relevant exercise context
    exercise_history = []
    if target_exercise_id:
        exercise_history = get_exercise_history(target_exercise_id, limit=1)
    elif substitute_for_id:
        exercise_history = get_exercise_history(substitute_for_id, limit=1)
    elif current_exercise_id:
        exercise_history = get_exercise_history(current_exercise_id, limit=1)

    system_prompt = f"""You are an elite AI strength and conditioning coach inside the Iron Log app.
Your task is to assign target loads/reps for a SPECIFIC chosen exercise, OR suggest the NEXT best exercise, OR suggest a SUBSTITUTION.

STRICT INSTRUCTIONS:
1. Progressive Overload: If the user is 'Ready', push for a small increase in weight (2.5kg) or 1-2 extra reps compared to their last performance.
2. Volume Management: Avoid over-taxing muscle groups that have high volume in the 'Weekly Category Balance'.
3. Substitution Logic: If 'substitute_for_id' is provided, suggest a different exercise that targets the SAME muscle groups.

Respond ONLY with a valid JSON object matching this schema:
{{
  "exercise_id": "the exact ID of the chosen exercise from the library",
  "target_sets": (integer) recommended sets,
  "target_reps": "(string) recommended reps (e.g. '8-10')",
  "target_weight_kg": (number) recommended weight,
  "target_rest_seconds": (integer) recommended rest,
  "coach_tip": "(string) punchy logic for the choice"
}}

Context:
User Profile Goals: {json.dumps(profile)}
Weekly Category Balance: {json.dumps(weekly_balance)}
User Readiness today: {json.dumps(readiness.get('label'))}
Recent Performance Context: {json.dumps(exercise_history)}
Available Library: {json.dumps(lib_summary)}
"""
    
    if substitute_for_id:
        sub_name = substitute_for_id
        for ex in library:
            if ex["id"] == substitute_for_id:
                sub_name = ex["name"]
                break
        user_msg = f"The user wants to substitute '{sub_name}' (ID: {substitute_for_id}). Suggest a replacement that targets the same muscle groups and prescribe targets."
    elif target_exercise_id:
        user_msg = f"The user has explicitly chosen to do the exercise with ID: '{target_exercise_id}'. Prescribe the target sets, reps, weight, and rest."
    elif current_exercise_id:
        user_msg = f"The user just finished '{current_exercise_id}'. Suggest the next best exercise from the library and prescribe targets."
    else:
        user_msg = "Please suggest the best first exercise for today's session."

    client = get_openai_client()
    if not client.api_key:
        return jsonify({"error": "OpenAI API key not configured"}), 500

    model_name = os.environ.get("OPENAI_MODEL", "gpt-4o")
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg}
            ],
            response_format={"type": "json_object"},
            max_tokens=600,
            temperature=0.7
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        return jsonify(data)
    except Exception as e:
        app.logger.error(f"Smart Engine AI Error: {e}")
        # Fallback to a simple recommendation if AI fails
        basis_id = substitute_for_id or target_exercise_id or current_exercise_id
        fallback = recommend_exercises(
            after_id=basis_id,
            done_ids=done_exercises,
            limit=1,
            library=library,
            unavailable_ids=unavailable_ids
        )
        rec = fallback["recommendations"][0] if fallback["recommendations"] else None
        if rec:
            return jsonify({
                "exercise_id": rec["id"],
                "target_sets": rec["sets"],
                "target_reps": rec["reps"],
                "target_weight_kg": 0,
                "target_rest_seconds": 90,
                "coach_tip": "AI is temporarily unavailable. Using local algorithm to keep you moving."
            })
        return jsonify({"error": str(e)}), 500


def find_session_exercise_index(session_exercises, payload):
    target_session_exercise_id = payload.get("session_exercise_id") or payload.get("sessionExerciseId")
    target_exercise_id = (
        payload.get("exercise_id")
        or payload.get("exerciseId")
        or payload.get("exerciseIdToReplace")
        or payload.get("exercise_id_to_replace")
    )
    for index, item in enumerate(session_exercises):
        if target_session_exercise_id and item.get("id") == target_session_exercise_id:
            return index
        if target_exercise_id and item.get("exercise_id") == target_exercise_id:
            return index
    return -1


def save_session_exercises(session_id, session_exercises, extra=None):
    update = {
        "session_exercises": session_exercises,
        "updated_at": now_iso(),
    }
    if extra:
        update.update(extra)
    get_db().sessions.update_one({"_id": session_id}, {"$set": update})
    return get_db().sessions.find_one({"_id": session_id})


@app.route("/api/sessions/start", methods=["POST"])
def api_start_session():
    payload = request.get_json(silent=True) or {}
    starting_exercise_id = payload.get("starting_exercise_id") or payload.get("startingExerciseId")
    if not starting_exercise_id:
        return jsonify({"error": "starting_exercise_id is required"}), 400
    try:
        session = start_generated_session(starting_exercise_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "session": session,
            "session_url": url_for("active_session", session_id=session["id"]),
        }
    ), 201


@app.route("/api/sessions/<session_id>/sets/<set_id>", methods=["PATCH"])
def api_update_session_set(session_id, set_id):
    payload = request.get_json(silent=True) or {}
    row = get_db().sessions.find_one({"_id": session_id})
    if row is None:
        return jsonify({"error": "Session not found"}), 404
    session_exercises = row.get("session_exercises", [])
    updated_set = None
    for session_exercise in session_exercises:
        for set_item in session_exercise.get("sets", []):
            if set_item.get("id") != set_id:
                continue
            if "actual_weight" in payload or "actualWeight" in payload:
                set_item["actual_weight"] = safe_float(
                    payload.get("actual_weight", payload.get("actualWeight")),
                    default=set_item.get("actual_weight"),
                    minimum=0,
                )
            if "actual_reps" in payload or "actualReps" in payload:
                set_item["actual_reps"] = safe_int(
                    payload.get("actual_reps", payload.get("actualReps")),
                    default=set_item.get("actual_reps") or 0,
                    minimum=0,
                    maximum=200,
                )
            if "completed" in payload:
                set_item["completed"] = bool(payload.get("completed"))
            set_item["updated_at"] = now_iso()
            updated_set = set_item
            break
        if updated_set:
            session_exercise["skipped"] = False
            break
    if updated_set is None:
        return jsonify({"error": "Set not found"}), 404
    row = save_session_exercises(session_id, session_exercises)
    return jsonify({"session": public_session(row), "set": updated_set})


@app.route("/api/sessions/<session_id>/skip-exercise", methods=["POST"])
def api_skip_session_exercise(session_id):
    payload = request.get_json(silent=True) or {}
    row = get_db().sessions.find_one({"_id": session_id})
    if row is None:
        return jsonify({"error": "Session not found"}), 404
    session_exercises = row.get("session_exercises", [])
    index = find_session_exercise_index(session_exercises, payload)
    if index == -1:
        return jsonify({"error": "Exercise not found in session"}), 404
    session_exercises[index]["skipped"] = True
    session_exercises[index]["skipped_at"] = now_iso()
    row = save_session_exercises(session_id, session_exercises)
    return jsonify({"session": public_session(row), "session_exercise": session_exercises[index]})


@app.route("/api/sessions/<session_id>/regenerate-exercise", methods=["POST"])
def api_regenerate_session_exercise(session_id):
    payload = request.get_json(silent=True) or {}
    row = get_db().sessions.find_one({"_id": session_id})
    if row is None:
        return jsonify({"error": "Session not found"}), 404
    session_exercises = row.get("session_exercises", [])
    index = find_session_exercise_index(session_exercises, payload)
    if index == -1:
        return jsonify({"error": "Exercise not found in session"}), 404

    program = load_program()
    library = build_exercise_library(program)
    exercise_by_id = {exercise["id"]: exercise for exercise in library}
    original_item = session_exercises[index]
    original = exercise_by_id.get(original_item.get("exercise_id"))
    if original is None:
        return jsonify({"error": "Original exercise not found"}), 404

    excluded_ids = set(row.get("excluded_exercise_ids", []))
    excluded_ids.add(original["id"])
    used_ids = {
        item.get("exercise_id")
        for item in session_exercises
        if item.get("exercise_id") and item.get("id") != original_item.get("id")
    }
    excluded_equipment = set(row.get("excluded_equipment", []))
    if payload.get("avoid_equipment", True) and original.get("equipment"):
        excluded_equipment.add(original["equipment"])

    candidates = replacement_candidates(
        library,
        original,
        used_ids=used_ids,
        excluded_ids=excluded_ids,
        excluded_equipment=excluded_equipment,
    )
    if not candidates and excluded_equipment:
        candidates = replacement_candidates(
            library,
            original,
            used_ids=used_ids,
            excluded_ids=excluded_ids,
            excluded_equipment=set(row.get("excluded_equipment", [])),
        )
    if not candidates:
        return jsonify({"error": "No replacement available"}), 404

    replacement = candidates[0]
    replacement_item = build_session_exercise(
        replacement,
        original_item.get("order", index + 1),
        role=original_item.get("role"),
        replaced_exercise_id=original["id"],
    )
    session_exercises[index] = replacement_item
    row = save_session_exercises(
        session_id,
        session_exercises,
        {
            "excluded_exercise_ids": sorted(excluded_ids),
            "excluded_equipment": sorted(excluded_equipment),
        },
    )
    model = active_session_to_model(row)
    replacement_exercise = next(
        (
            exercise
            for exercise in model["workout"]["exercises"]
            if exercise.get("session_exercise_id") == replacement_item["id"]
        ),
        None,
    )
    return jsonify(
        {
            "session": public_session(row),
            "session_exercise": replacement_item,
            "exercise": replacement_exercise,
            "model": model,
        }
    )


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
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
