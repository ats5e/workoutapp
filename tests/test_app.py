import os
import tempfile
import unittest

import app


class IronLogAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "ironlog-test.db")
        os.environ["DATABASE_PATH"] = self.db_path
        self.client = app.app.test_client()

    def tearDown(self):
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def test_home_and_workout_pages_render(self):
        home = self.client.get("/")
        workout = self.client.get("/workout/day-1-push")
        exercises = self.client.get("/exercises")
        smart = self.client.get("/smart?start=seated-db-shoulder-press")

        self.assertEqual(home.status_code, 200)
        self.assertEqual(workout.status_code, 200)
        self.assertEqual(exercises.status_code, 200)
        self.assertEqual(smart.status_code, 200)
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
        self.assertEqual(shoulder_press["category"], "shoulders")
        self.assertIn("kg", shoulder_press["display_weight_label"])
        self.assertEqual(recommendations["after"]["id"], "seated-db-shoulder-press")
        self.assertGreaterEqual(len(recommendations["recommendations"]), 4)
        self.assertNotIn(
            "seated-db-shoulder-press",
            [exercise["id"] for exercise in recommendations["recommendations"]],
        )

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


if __name__ == "__main__":
    unittest.main()
