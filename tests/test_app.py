import os
import unittest
from unittest.mock import patch, MagicMock
import mongomock

import app


class IronLogAppTests(unittest.TestCase):
    def setUp(self):
        self.mock_mongo_client = mongomock.MongoClient()
        self.db_patcher = patch('app.get_db', return_value=self.mock_mongo_client.ironlog)
        self.db_patcher.start()
        
        app.init_db()

        self.mock_openai_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].message.content = '{"reply": "I checked your history. We will use coach memory.", "recommended_exercise_ids": ["machine-shoulder-press", "lateral-raises", "wide-grip-lat-pulldown", "seated-cable-row"]}'
        self.mock_openai_client.chat.completions.create.return_value = mock_response
        
        self.openai_patcher = patch('app.get_openai_client', return_value=self.mock_openai_client)
        self.openai_patcher.start()

        self.client = app.app.test_client()

    def tearDown(self):
        self.db_patcher.stop()
        self.openai_patcher.stop()

    def test_home_and_workout_pages_render(self):
        home = self.client.get("/")
        workout = self.client.get("/workout/day-1-push")
        exercises = self.client.get("/exercises")
        smart = self.client.get("/smart?start=seated-db-shoulder-press")
        coach = self.client.get("/coach")

        self.assertEqual(home.status_code, 200)
        self.assertEqual(workout.status_code, 200)
        self.assertEqual(exercises.status_code, 200)
        self.assertEqual(smart.status_code, 200)
        self.assertEqual(coach.status_code, 200)
        self.assertIn(b"Daily Check-in", home.data)
        self.assertIn(b"Exercise Overview", workout.data)
        self.assertIn(b"Target RIR", workout.data)
        self.assertIn(b"Suggested", workout.data)
        self.assertIn(b"Session Notes", workout.data)
        self.assertIn(b"Exercise Library", exercises.data)
        self.assertIn(b"Selected Exercise", exercises.data)
        self.assertIn(b"View", exercises.data)
        self.assertIn(b"Smart Gym Session", smart.data)
        self.assertIn(b"Smart Gym Coach", smart.data)
        self.assertIn(b"Train selected", smart.data)
        self.assertIn(b"AI Coach", coach.data)
        self.assertNotIn(b"Start Walk", workout.data)

    def test_exercise_library_and_recommendations(self):
        library = self.client.get("/api/exercises").get_json()
        recommendations = self.client.get(
            "/api/recommendations?after=seated-db-shoulder-press&limit=6"
        ).get_json()

        self.assertGreaterEqual(library["total"], 30)
        self.assertTrue(any(group["id"] == "push" for group in library["groups"]))
        self.assertTrue(any(group["id"] == "pull" for group in library["groups"]))
        shoulder_press = next(
            exercise
            for exercise in library["exercises"]
            if exercise["id"] == "seated-db-shoulder-press"
        )
        machine_press = next(
            exercise
            for exercise in library["exercises"]
            if exercise["id"] == "machine-shoulder-press"
        )
        unavailable_rope_row = next(
            exercise
            for exercise in library["exercises"]
            if exercise["id"] == "cable-rope-rear-delt-row"
        )
        self.assertEqual(shoulder_press["category"], "shoulders")
        self.assertIn("kg", shoulder_press["display_weight_label"])
        self.assertEqual(machine_press["preference_status"], "preferred")
        self.assertTrue(machine_press["is_available"])
        self.assertEqual(unavailable_rope_row["preference_status"], "avoid")
        self.assertFalse(unavailable_rope_row["is_available"])
        self.assertEqual(recommendations["after"]["id"], "seated-db-shoulder-press")
        self.assertGreaterEqual(len(recommendations["recommendations"]), 4)
        self.assertNotIn(
            "seated-db-shoulder-press",
            [exercise["id"] for exercise in recommendations["recommendations"]],
        )
        self.assertNotIn(
            "cable-rope-rear-delt-row",
            [exercise["id"] for exercise in recommendations["recommendations"]],
        )

    def test_exercise_preferences_change_smart_recommendations(self):
        self.client.post(
            "/api/sessions",
            json={
                "workout_id": "smart-session",
                "workout_name": "Smart Gym Session",
                "completed_at": "2026-05-12T09:00:00Z",
                "exercise_logs": [
                    {
                        "exercise_id": "neutral-grip-pulldown",
                        "exercise_name": "Neutral-grip Lat Pulldown",
                        "reps": "10",
                        "target_sets": 3,
                        "completed_sets": 3,
                        "skipped_sets": 0,
                        "working_weight": 50,
                        "working_weight_label": "50 kg",
                    }
                ],
            },
        )
        preference = self.client.post(
            "/api/exercise-preferences/seated-cable-row",
            json={"status": "avoid", "notes": "Cable station setup does not work for me."},
        ).get_json()
        library = self.client.get("/api/exercises").get_json()
        recommendations = self.client.get(
            "/api/recommendations?after=machine-shoulder-press&limit=12"
        ).get_json()

        seated_row = next(
            exercise
            for exercise in library["exercises"]
            if exercise["id"] == "seated-cable-row"
        )
        self.assertEqual(preference["preference"]["status"], "avoid")
        self.assertEqual(seated_row["preference_status"], "avoid")
        self.assertFalse(seated_row["is_available"])
        self.assertNotIn(
            "seated-cable-row",
            [exercise["id"] for exercise in recommendations["recommendations"]],
        )

    def test_ai_coach_chat_uses_memory_and_recommendations(self):
        response = self.client.post(
            "/api/coach/chat",
            json={"message": "I did machine shoulder press and lateral raises yesterday."},
        )
        data = response.get_json()
        context = self.client.get("/api/coach/context").get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("coach memory", data["reply"])
        self.assertTrue(
            any(exercise["id"] == "machine-shoulder-press" for exercise in data["mentioned_exercises"])
        )
        self.assertGreaterEqual(len(data["recommendations"]), 3)
        self.assertGreaterEqual(context["memory_count"], 2)

    def test_recommendations_route_around_busy_equipment_and_week_balance(self):
        self.client.post(
            "/api/sessions",
            json={
                "workout_id": "smart-session",
                "workout_name": "Smart Gym Session",
                "exercise_logs": [
                    {
                        "exercise_id": "barbell-bench-press",
                        "exercise_name": "Barbell Bench Press",
                        "reps": "8",
                        "target_sets": 12,
                        "completed_sets": 12,
                        "skipped_sets": 0,
                        "working_weight": 50,
                        "working_weight_label": "50 kg",
                    }
                ],
            },
        )
        recommendations = self.client.get(
            "/api/recommendations?after=wide-grip-lat-pulldown&unavailable=machine-shoulder-press&limit=3"
        ).get_json()
        context = self.client.get("/api/coach/context").get_json()

        self.assertGreater(context["weekly_balance"][0]["sets"], 0)
        self.assertNotIn(
            "machine-shoulder-press",
            [exercise["id"] for exercise in recommendations["recommendations"]],
        )
        self.assertEqual(recommendations["recommendations"][0]["category"], "shoulders")

    def test_workout_program_matches_requested_rotation(self):
        dashboard = self.client.get("/api/dashboard").get_json()
        program = app.load_program()

        self.assertGreaterEqual(len(program["workouts"]), 6)
        for workout in program["workouts"]:
            self.assertEqual(workout["target_minutes"], 45)
            self.assertEqual(workout["warmup"]["minutes"], 0)
            self.assertEqual(workout["cooldown"]["minutes"], 0)
            self.assertTrue(workout.get("hypertrophy_focus"))
            self.assertGreaterEqual(len(workout.get("session_tips", [])), 3)
            for exercise in workout["exercises"]:
                self.assertNotIn("squat", exercise["name"].lower())
                self.assertIsInstance(exercise["weight"], (int, float))
                self.assertTrue(exercise.get("target_rir"))
                self.assertTrue(exercise.get("tempo"))
                self.assertTrue(exercise.get("muscle_focus"))
                self.assertTrue(exercise.get("hypertrophy_tip"))
                self.assertTrue(exercise.get("progression_rule"))

        for workout in dashboard["workouts"]:
            self.assertEqual(workout["total_session_minutes"], 45)

    def test_profile_and_checkin_update_dashboard(self):
        profile_response = self.client.post(
            "/api/profile",
            json={
                "name": "Jack",
                "training_goal": "Build strength",
                "focus_area": "Posterior chain",
                "preferred_session_minutes": 95,
            },
        )
        checkin_response = self.client.post(
            "/api/checkins/today",
            json={
                "energy": 4,
                "sleep": 5,
                "soreness": 2,
                "stress": 2,
                "motivation": 5,
                "bodyweight_kg": 84.2,
                "step_count": 9012,
                "notes": "Feeling good",
            },
        )
        dashboard = self.client.get("/api/dashboard").get_json()

        self.assertEqual(profile_response.status_code, 200)
        self.assertEqual(checkin_response.status_code, 200)
        self.assertEqual(dashboard["profile"]["name"], "Jack")
        self.assertGreaterEqual(dashboard["readiness"]["score"], 80)
        self.assertIn("Jack", dashboard["coach_note"]["title"])

    def test_session_save_enriches_history_and_workout_context(self):
        save_response = self.client.post(
            "/api/sessions",
            json={
                "workout_id": "day-1-push",
                "week": 3,
                "duration_seconds": 4280,
                "warmup_seconds": 840,
                "cooldown_seconds": 720,
                "notes": "Bench moved cleanly.",
                "exercise_logs": [
                    {
                        "exercise_id": "barbell-bench-press",
                        "exercise_name": "Barbell Bench Press",
                        "reps": "8",
                        "target_sets": 4,
                        "completed_sets": 4,
                        "skipped_sets": 0,
                        "working_weight": 52.5,
                        "working_weight_label": "52.5 kg",
                        "suggested_weight": 52.5,
                        "suggested_weight_label": "52.5 kg",
                        "notes": "Strong sets",
                    },
                    {
                        "exercise_id": "incline-db-press",
                        "exercise_name": "Incline Dumbbell Press",
                        "reps": "10",
                        "target_sets": 3,
                        "completed_sets": 3,
                        "skipped_sets": 0,
                        "working_weight": 20,
                        "working_weight_label": "20 kg / hand",
                        "suggested_weight": 20,
                        "suggested_weight_label": "20 kg / hand",
                        "notes": "",
                    },
                ],
            },
        )

        workout = self.client.get("/api/workouts/day-1-push").get_json()
        history = self.client.get("/api/history").get_json()

        self.assertEqual(save_response.status_code, 201)
        self.assertEqual(history["sessions"][0]["workout_name"], "Day 1 - Push Strength")
        self.assertEqual(workout["exercises"][0]["last_logged_label"], "52.5 kg")
        self.assertEqual(workout["exercises"][0]["personal_best_label"], "52.5 kg")
        self.assertEqual(workout["latest_session"]["completed_sets"], 7)

    def test_smart_session_can_be_saved(self):
        save_response = self.client.post(
            "/api/sessions",
            json={
                "workout_id": "smart-session",
                "workout_name": "Smart Gym Session",
                "week": 1,
                "duration_seconds": 2700,
                "exercise_logs": [
                    {
                        "exercise_id": "seated-db-shoulder-press",
                        "exercise_name": "Seated Dumbbell Shoulder Press",
                        "reps": "10",
                        "target_sets": 3,
                        "completed_sets": 3,
                        "skipped_sets": 0,
                        "working_weight": 16,
                        "working_weight_label": "16 kg / hand",
                        "suggested_weight": 16,
                        "suggested_weight_label": "16 kg / hand",
                        "notes": "",
                    }
                ],
            },
        )
        history = self.client.get("/api/history").get_json()

        self.assertEqual(save_response.status_code, 201)
        self.assertEqual(history["sessions"][0]["workout_name"], "Smart Gym Session")

    def test_generated_session_starts_from_selected_exercise(self):
        response = self.client.post(
            "/api/sessions/start",
            json={"starting_exercise_id": "barbell-bench-press"},
        )
        data = response.get_json()
        session = data["session"]
        page = self.client.get(data["session_url"])
        total_sets = sum(
            len(exercise["sets"]) for exercise in session["session_exercises"]
        )
        exercise_ids = [
            exercise["exercise_id"] for exercise in session["session_exercises"]
        ]

        self.assertEqual(response.status_code, 201)
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["source"], "generated")
        self.assertEqual(session["starting_exercise_id"], "barbell-bench-press")
        self.assertEqual(exercise_ids[0], "barbell-bench-press")
        self.assertGreaterEqual(total_sets, 12)
        self.assertLessEqual(total_sets, 18)
        self.assertNotIn("cable-rope-rear-delt-row", exercise_ids)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Actual Reps This Set", page.data)
        self.assertIn(b"Regenerate", page.data)

    def test_home_ignores_active_generated_sessions_in_latest_summary(self):
        started = self.client.post(
            "/api/sessions/start",
            json={"starting_exercise_id": "barbell-bench-press"},
        )
        home = self.client.get("/")

        self.assertEqual(started.status_code, 201)
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"What are you starting with today?", home.data)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_generated_session_uses_openai_when_configured(self):
        mock_ai_response = MagicMock()
        mock_ai_response.choices[0].message.content = (
            '{"exercise_ids": ["barbell-bench-press", "incline-db-press", '
            '"cable-fly", "tricep-rope-pushdown"], '
            '"coach_tip": "AI selected a push-focused route."}'
        )
        self.mock_openai_client.chat.completions.create.return_value = mock_ai_response

        response = self.client.post(
            "/api/sessions/start",
            json={"starting_exercise_id": "barbell-bench-press"},
        )
        session = response.get_json()["session"]
        exercise_ids = [
            exercise["exercise_id"] for exercise in session["session_exercises"]
        ]

        self.assertEqual(response.status_code, 201)
        self.assertEqual(session["generation_engine"], "openai")
        self.assertEqual(session["generation_note"], "AI selected a push-focused route.")
        self.assertEqual(
            exercise_ids,
            [
                "barbell-bench-press",
                "incline-db-press",
                "cable-fly",
                "tricep-rope-pushdown",
            ],
        )

    def test_generated_session_patches_sets_and_skips_exercises(self):
        started = self.client.post(
            "/api/sessions/start",
            json={"starting_exercise_id": "barbell-bench-press"},
        ).get_json()["session"]
        first_exercise = started["session_exercises"][0]
        first_set = first_exercise["sets"][0]

        patch_response = self.client.patch(
            f"/api/sessions/{started['id']}/sets/{first_set['id']}",
            json={"actual_weight": 52.5, "actual_reps": 8, "completed": True},
        )
        skip_response = self.client.post(
            f"/api/sessions/{started['id']}/skip-exercise",
            json={"session_exercise_id": started["session_exercises"][1]["id"]},
        )
        row = self.mock_mongo_client.ironlog.sessions.find_one({"_id": started["id"]})

        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(skip_response.status_code, 200)
        self.assertTrue(row["session_exercises"][0]["sets"][0]["completed"])
        self.assertEqual(row["session_exercises"][0]["sets"][0]["actual_reps"], 8)
        self.assertTrue(row["session_exercises"][1]["skipped"])

    def test_generated_session_regenerates_with_exclusions(self):
        started = self.client.post(
            "/api/sessions/start",
            json={"starting_exercise_id": "barbell-bench-press"},
        ).get_json()["session"]
        original = started["session_exercises"][1]
        response = self.client.post(
            f"/api/sessions/{started['id']}/regenerate-exercise",
            json={"session_exercise_id": original["id"]},
        )
        data = response.get_json()
        row = self.mock_mongo_client.ironlog.sessions.find_one({"_id": started["id"]})

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(data["session_exercise"]["exercise_id"], original["exercise_id"])
        self.assertTrue(data["session_exercise"]["was_regenerated"])
        self.assertEqual(data["session_exercise"]["replaced_exercise_id"], original["exercise_id"])
        self.assertIn(original["exercise_id"], row["excluded_exercise_ids"])

    def test_weight_suggestion_progresses_or_repeats_from_history(self):
        self.client.post(
            "/api/sessions",
            json={
                "workout_id": "day-1-push",
                "completed_at": "2026-05-12T09:00:00",
                "exercise_logs": [
                    {
                        "exercise_id": "barbell-bench-press",
                        "exercise_name": "Barbell Bench Press",
                        "reps": "8",
                        "target_sets": 4,
                        "completed_sets": 4,
                        "skipped_sets": 0,
                        "working_weight": 50,
                        "working_weight_label": "50 kg",
                    }
                ],
            },
        )
        progressed = self.client.get(
            "/api/exercises/suggest-weight?exercise_id=barbell-bench-press"
        ).get_json()
        self.client.post(
            "/api/sessions",
            json={
                "workout_id": "day-1-push",
                "completed_at": "2026-05-13T09:00:00",
                "exercise_logs": [
                    {
                        "exercise_id": "barbell-bench-press",
                        "exercise_name": "Barbell Bench Press",
                        "reps": "8",
                        "target_sets": 4,
                        "completed_sets": 2,
                        "skipped_sets": 2,
                        "working_weight": 55,
                        "working_weight_label": "55 kg",
                    }
                ],
            },
        )
        repeated = self.client.get(
            "/api/exercises/suggest-weight?exercise_id=barbell-bench-press"
        ).get_json()

        self.assertEqual(progressed["suggested_weight"], 52.5)
        self.assertEqual(progressed["source"], "progressed")
        self.assertEqual(repeated["suggested_weight"], 55)
        self.assertEqual(repeated["source"], "repeat")

    def test_generated_session_respects_recent_recovery_categories(self):
        self.client.post(
            "/api/sessions",
            json={
                "workout_id": "day-1-push",
                "completed_at": app.now_iso(),
                "exercise_logs": [
                    {
                        "exercise_id": "barbell-bench-press",
                        "exercise_name": "Barbell Bench Press",
                        "reps": "8",
                        "target_sets": 4,
                        "completed_sets": 4,
                        "skipped_sets": 0,
                        "working_weight": 50,
                        "working_weight_label": "50 kg",
                    }
                ],
            },
        )
        session = self.client.post(
            "/api/sessions/start",
            json={"starting_exercise_id": "wide-grip-lat-pulldown"},
        ).get_json()["session"]
        library = {exercise["id"]: exercise for exercise in app.build_exercise_library()}
        accessory_categories = [
            library[item["exercise_id"]]["category"]
            for item in session["session_exercises"][1:]
        ]

        self.assertIn("push", session["recovery_excluded_categories"])
        self.assertNotIn("push", accessory_categories)

    def test_smart_engine_recommend_with_target_exercise(self):
        mock_ai_response = MagicMock()
        mock_ai_response.choices[0].message.content = '{"exercise_id": "seated-db-shoulder-press", "target_sets": 4, "target_reps": "8-10", "target_weight_kg": 22, "target_rest_seconds": 120, "coach_tip": "Increase by 2kg from last session."}'
        self.mock_openai_client.chat.completions.create.return_value = mock_ai_response

        response = self.client.post(
            "/api/smart-engine/recommend",
            json={
                "target_exercise_id": "seated-db-shoulder-press",
                "done_exercises": [],
                "unavailable_ids": [],
                "readiness": {"label": "Ready", "score": 80},
            },
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["exercise_id"], "seated-db-shoulder-press")
        self.assertEqual(data["target_sets"], 4)
        self.assertEqual(data["target_weight_kg"], 22)
        self.assertEqual(data["target_rest_seconds"], 120)
        self.assertIn("coach_tip", data)

    def test_smart_engine_recommend_next_exercise(self):
        mock_ai_response = MagicMock()
        mock_ai_response.choices[0].message.content = '{"exercise_id": "machine-shoulder-press", "target_sets": 3, "target_reps": "10-12", "target_weight_kg": 30, "target_rest_seconds": 90, "coach_tip": "Good follow-up for shoulders."}'
        self.mock_openai_client.chat.completions.create.return_value = mock_ai_response

        response = self.client.post(
            "/api/smart-engine/recommend",
            json={
                "current_exercise_id": "seated-db-shoulder-press",
                "done_exercises": ["seated-db-shoulder-press"],
                "unavailable_ids": [],
                "readiness": {"label": "Ready", "score": 80},
            },
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["exercise_id"], "machine-shoulder-press")
        self.assertEqual(data["target_sets"], 3)
        self.assertEqual(data["target_rest_seconds"], 90)
        self.assertIn("coach_tip", data)


if __name__ == "__main__":
    unittest.main()
