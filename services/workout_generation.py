from datetime import datetime, timedelta

from services.weight_suggestion import load_type


SET_CAP_MIN = 12
SET_CAP_MAX = 18

CATEGORY_FLOW = {
    "push": ["push", "shoulders", "pull", "arms"],
    "pull": ["pull", "shoulders", "push", "arms"],
    "shoulders": ["shoulders", "pull", "push", "arms"],
    "arms": ["arms", "push", "pull", "shoulders"],
    "posterior-chain": ["posterior-chain", "core-calves", "pull", "push"],
    "core-calves": ["core-calves", "posterior-chain", "push", "pull"],
}


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def iso_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_hard_categories(recent_sessions, library_by_id, now=None, hours=48):
    now = now or datetime.now()
    cutoff = now - timedelta(hours=hours)
    categories = set()

    for session in recent_sessions or []:
        completed_at = iso_dt(session.get("completed_at"))
        if not completed_at:
            continue
        if completed_at.tzinfo and now.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=None)
        if completed_at < cutoff:
            continue
        for log in session.get("exercise_logs", []):
            if safe_int(log.get("completed_sets")) < 3:
                continue
            exercise = library_by_id.get(log.get("exercise_id"))
            if exercise and exercise.get("category"):
                categories.add(exercise["category"])
    return categories


def previous_accessory_lists(recent_sessions, movement_pattern):
    lists = []
    for session in recent_sessions or []:
        if session.get("movement_pattern") != movement_pattern:
            continue
        ids = [
            item.get("exercise_id")
            for item in session.get("session_exercises", [])
            if item.get("role") == "accessory" and not item.get("skipped")
        ]
        if ids:
            lists.append(ids)
    return lists


def role_for_exercise(exercise, is_start=False):
    if is_start:
        return "main"
    return "compound_accessory" if load_type(exercise) == "compound" else "isolation_accessory"


def candidate_score(start, candidate, hard_categories=None, used_ids=None):
    hard_categories = hard_categories or set()
    used_ids = used_ids or set()
    if candidate.get("id") in used_ids:
        return -1
    if candidate.get("preference_status") == "avoid" or candidate.get("is_available") is False:
        return -1

    start_category = start.get("category")
    flow = CATEGORY_FLOW.get(start_category, ["push", "pull", "shoulders", "arms"])
    category = candidate.get("category")
    score = 0

    if category in flow:
        score += (len(flow) - flow.index(category)) * 24
    else:
        score += 4

    if category in hard_categories and category != start_category:
        score -= 120
    if candidate.get("movement_pattern") == start.get("movement_pattern"):
        score += 10
    if candidate.get("preference_status") == "preferred":
        score += 32
    if candidate.get("last_completed_at"):
        score -= 4
    else:
        score += 6
    score += max(0, 8 - safe_int(candidate.get("category_rank"), 8))
    score += min(5, safe_int(candidate.get("sets"), 0))
    return score


def sorted_candidates(library, start, used_ids=None, hard_categories=None, excluded_equipment=None):
    excluded_equipment = set(excluded_equipment or [])
    scored = []
    for candidate in library:
        if candidate.get("equipment") in excluded_equipment:
            continue
        score = candidate_score(start, candidate, hard_categories, used_ids)
        if score >= 0:
            scored.append((score, candidate))
    scored.sort(
        key=lambda item: (
            -item[0],
            safe_int(item[1].get("category_rank"), 99),
            item[1].get("name", ""),
        )
    )
    return [candidate for _score, candidate in scored]


def avoid_exact_repeat(accessories, candidate_pool, previous_lists, used_ids):
    accessory_ids = [exercise["id"] for exercise in accessories]
    if accessory_ids not in previous_lists or not accessories:
        return accessories

    for candidate in candidate_pool:
        if candidate["id"] in used_ids:
            continue
        replacement = accessories[:-1] + [candidate]
        if [exercise["id"] for exercise in replacement] not in previous_lists:
            return replacement
    return accessories


def generate_workout(library, starting_exercise_id, recent_sessions=None, target_set_cap=15):
    library_by_id = {exercise["id"]: exercise for exercise in library}
    start = library_by_id.get(starting_exercise_id)
    if start is None:
        raise ValueError("Starting exercise not found")
    if start.get("preference_status") == "avoid" or start.get("is_available") is False:
        raise ValueError("Starting exercise is unavailable")

    hard_categories = recent_hard_categories(recent_sessions, library_by_id)
    used_ids = {start["id"]}
    total_sets = safe_int(start.get("sets"), 0)
    cap = max(SET_CAP_MIN, min(SET_CAP_MAX, safe_int(target_set_cap, 15)))
    accessories = []

    pool = sorted_candidates(library, start, used_ids=used_ids, hard_categories=hard_categories)
    for candidate in pool:
        sets = safe_int(candidate.get("sets"), 0)
        if sets <= 0:
            continue
        if total_sets >= SET_CAP_MIN and total_sets + sets > cap:
            continue
        accessories.append(candidate)
        used_ids.add(candidate["id"])
        total_sets += sets
        if total_sets >= SET_CAP_MIN:
            break

    if total_sets < SET_CAP_MIN:
        fallback_pool = sorted_candidates(library, start, used_ids=used_ids, hard_categories=set())
        for candidate in fallback_pool:
            sets = safe_int(candidate.get("sets"), 0)
            if sets <= 0 or total_sets + sets > SET_CAP_MAX:
                continue
            accessories.append(candidate)
            used_ids.add(candidate["id"])
            total_sets += sets
            if total_sets >= SET_CAP_MIN:
                break

    previous_lists = previous_accessory_lists(recent_sessions, start.get("movement_pattern"))
    accessories = avoid_exact_repeat(accessories, pool, previous_lists, used_ids)

    exercises = [start] + accessories
    return {
        "starting_exercise": start,
        "movement_pattern": start.get("movement_pattern") or start.get("category"),
        "category": start.get("category"),
        "total_sets": sum(safe_int(exercise.get("sets"), 0) for exercise in exercises),
        "exercises": [
            {
                **exercise,
                "role": "main" if index == 0 else "accessory",
                "generation_role": role_for_exercise(exercise, is_start=index == 0),
            }
            for index, exercise in enumerate(exercises)
        ],
        "recovery_excluded_categories": sorted(hard_categories - {start.get("category")}),
    }


def role_matches(original, candidate):
    if original.get("category") != candidate.get("category"):
        return False
    if original.get("movement_pattern") and candidate.get("movement_pattern"):
        if original["movement_pattern"] != candidate["movement_pattern"]:
            return False
    if load_type(original) != load_type(candidate):
        return False
    return True


def replacement_candidates(
    library,
    original,
    used_ids=None,
    excluded_ids=None,
    excluded_equipment=None,
):
    used_ids = set(used_ids or [])
    excluded_ids = set(excluded_ids or [])
    excluded_equipment = set(excluded_equipment or [])

    strict = []
    fallback = []
    for candidate in library:
        if candidate["id"] in used_ids or candidate["id"] in excluded_ids:
            continue
        if candidate.get("preference_status") == "avoid" or candidate.get("is_available") is False:
            continue
        if candidate.get("equipment") in excluded_equipment:
            continue
        if role_matches(original, candidate):
            strict.append(candidate)
        elif candidate.get("category") == original.get("category"):
            fallback.append(candidate)

    strict.sort(key=lambda exercise: (exercise.get("equipment") == original.get("equipment"), exercise.get("name", "")))
    fallback.sort(key=lambda exercise: (exercise.get("equipment") == original.get("equipment"), exercise.get("name", "")))
    return strict + fallback
