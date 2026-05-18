import re


COMPOUND_TOKENS = {
    "bench",
    "press",
    "row",
    "pulldown",
    "pull-up",
    "pullups",
    "deadlift",
    "thrust",
    "dip",
}

ISOLATION_TOKENS = {
    "curl",
    "fly",
    "raise",
    "extension",
    "pushdown",
    "skullcrusher",
    "calf",
    "face pull",
    "rear-delt",
    "rear delt",
    "knee raise",
}


def compact_number(value):
    if value is None:
        return None
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_weight(exercise, weight):
    fmt = exercise.get("weight_format") or "{w} kg"
    if fmt == "bodyweight":
        return "bodyweight"
    return fmt.replace("{w}", compact_number(weight or 0))


def parse_rep_numbers(value):
    return [float(item) for item in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]


def prescribed_rep_target(reps):
    numbers = parse_rep_numbers(reps)
    return max(numbers) if numbers else 0


def round_to_step(value, step):
    if not step or step <= 0:
        return round(value * 2) / 2
    return round(value / step) * step


def load_type(exercise):
    explicit = str(exercise.get("load_type") or exercise.get("type") or "").lower()
    if explicit in {"compound", "isolation"}:
        return explicit

    text = " ".join(
        str(exercise.get(key, ""))
        for key in ("id", "name", "movement_pattern", "muscle_focus")
    ).lower()
    if any(token in text for token in ISOLATION_TOKENS):
        return "isolation"
    if any(token in text for token in COMPOUND_TOKENS):
        return "compound"
    if exercise.get("category") in {"arms", "core-calves"}:
        return "isolation"
    return "compound"


def progression_increment(exercise):
    if (exercise.get("weight_format") or "") == "bodyweight":
        return 0
    return 2.5 if load_type(exercise) == "compound" else 1


def hit_prescription(performance, exercise):
    if not performance:
        return False

    target_sets = int(performance.get("target_sets") or exercise.get("sets") or 0)
    completed_sets = int(performance.get("completed_sets") or 0)
    if target_sets and completed_sets < target_sets:
        return False

    set_logs = performance.get("sets") or performance.get("set_logs") or []
    if set_logs:
        target_reps = prescribed_rep_target(exercise.get("reps") or performance.get("reps"))
        if target_reps <= 0:
            return completed_sets >= target_sets
        completed = [item for item in set_logs if item.get("completed")]
        if target_sets and len(completed) < target_sets:
            return False
        return all(float(item.get("actual_reps") or 0) >= target_reps for item in completed)

    return completed_sets >= target_sets if target_sets else completed_sets > 0


def suggest_weight(exercise, performance=None):
    default_weight = float(exercise.get("weight") or 0)
    step = progression_increment(exercise)
    cap = float(exercise.get("progression_cap") or 0)

    if (exercise.get("weight_format") or "") == "bodyweight":
        return {
            "exercise_id": exercise.get("id"),
            "suggested_weight": 0,
            "suggested_weight_label": "bodyweight",
            "suggested_reps": exercise.get("reps"),
            "load_type": load_type(exercise),
            "increment": 0,
            "source": "bodyweight",
            "reason": "Bodyweight movement. Track reps and control before adding load.",
        }

    if not performance or performance.get("weight") is None:
        suggested = default_weight
        source = "default"
        reason = "First logged use. Starting from the programmed default."
    else:
        last_weight = float(performance.get("weight") or 0)
        if hit_prescription(performance, exercise):
            suggested = last_weight + step
            source = "progressed"
            reason = f"Last time hit the prescription, so add {compact_number(step)} kg."
        else:
            suggested = last_weight
            source = "repeat"
            reason = "Last time was unfinished or reps were missed, so repeat the load."

    if cap > 0:
        suggested = min(suggested, cap)
    rounding = step or float(exercise.get("progression_kg") or 0.5) or 0.5
    suggested = round_to_step(max(0, suggested), rounding)

    return {
        "exercise_id": exercise.get("id"),
        "suggested_weight": suggested,
        "suggested_weight_label": format_weight(exercise, suggested),
        "suggested_reps": exercise.get("reps"),
        "load_type": load_type(exercise),
        "increment": step,
        "source": source,
        "reason": reason,
    }
