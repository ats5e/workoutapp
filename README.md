# Iron Log

Iron Log is a personalized workout tracker built with Flask. It now includes:

- a profile and daily readiness check-in
- persistent workout history in MongoDB
- personalized load suggestions based on your own previous sessions
- 45-minute workout rotation with an upper-body muscle-building bias
- hypertrophy cues for target effort, tempo, muscle focus, and progression
- exercise-first session start with generated workouts
- server-backed set-by-set logging for generated sessions
- movement regeneration and skip actions during a session
- smart gym mode for starting with any available exercise and getting balanced next-exercise options
- view-first categorized exercise library with reps, weights, cues, and optional start/next actions
- draft restore if you leave a workout halfway through
- automatic session logging, achievements, and recent-history analytics
- installable PWA basics with cached assets for better in-gym reliability

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open `http://127.0.0.1:5000`.

If port `5000` is busy:

```bash
PORT=5001 .venv/bin/python app.py
```

## Data storage

- workout programming lives in `workouts.json`
- personal data is stored in MongoDB through PyMongo
- set `MONGODB_URI` to point the app at your Mongo instance

Example:

```bash
MONGODB_URI=mongodb://localhost:27017/ .venv/bin/python app.py
```

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Main app areas

- `/` exercise-first generated-session start
- `/workout/<id>` guided workout flow with exercise overview, set logging, rest timers, and draft restore
- `/session/<id>` active generated workout flow with server-side set persistence
- `/smart` flexible exercise-first workout flow
- `/exercises` categorized exercise library
- `/api/*` JSON endpoints for profile, check-ins, sessions, workouts, exercises, recommendations, and dashboard data
