const IRONLOG_KEYS = {
    startDate: "ironlog.startDate",
    weekOffset: "ironlog.weekOffset",
    sessionDraftPrefix: "ironlog.session.",
};

function deepCopy(value) {
    return JSON.parse(JSON.stringify(value));
}

function safeNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function ironlogWeek() {
    let startIso = localStorage.getItem(IRONLOG_KEYS.startDate);
    if (!startIso) {
        startIso = new Date().toISOString().slice(0, 10);
        localStorage.setItem(IRONLOG_KEYS.startDate, startIso);
    }
    const start = new Date(`${startIso}T00:00:00`);
    const now = new Date();
    const days = Math.max(0, Math.floor((now - start) / 86400000));
    const offset = parseInt(localStorage.getItem(IRONLOG_KEYS.weekOffset) || "0", 10);
    return Math.max(1, Math.floor(days / 7) + 1 + offset);
}

function ironlogAdjustWeek(delta) {
    const current = parseInt(localStorage.getItem(IRONLOG_KEYS.weekOffset) || "0", 10);
    localStorage.setItem(IRONLOG_KEYS.weekOffset, String(current + delta));
}

function ironlogResetWeek() {
    localStorage.setItem(IRONLOG_KEYS.startDate, new Date().toISOString().slice(0, 10));
    localStorage.setItem(IRONLOG_KEYS.weekOffset, "0");
}

function computeExerciseWeight(exercise, week) {
    const base = typeof exercise.weight === "number" ? exercise.weight : 0;
    const step = exercise.progression_kg || 0;
    const cap = exercise.progression_cap || 0;
    let target = base + (week - 1) * step;
    if (cap > 0) {
        target = Math.min(target, cap);
    }
    return Math.round(target * 2) / 2;
}

function formatWeight(exercise, computed) {
    const fmt = exercise.weight_format || "{w} kg";
    if (fmt === "bodyweight") {
        return "bodyweight";
    }
    return fmt.replace("{w}", String(computed).replace(/\.0$/, ""));
}

function roundToStep(value, step) {
    if (!step || step <= 0) {
        return Math.round(value * 2) / 2;
    }
    return Math.round(value / step) * step;
}

function defaultCheckin() {
    return {
        energy: 3,
        sleep: 3,
        soreness: 3,
        stress: 3,
        motivation: 3,
        bodyweight_kg: "",
        step_count: "",
        notes: "",
    };
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, {
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
        ...options,
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.error || "Request failed");
    }
    return data;
}

function toneSurfaceClass(tone) {
    if (tone === "emerald") return "border-emerald-500/30 bg-emerald-500/10";
    if (tone === "accent") return "border-accent/30 bg-accent/10";
    if (tone === "amber") return "border-amber-500/30 bg-amber-500/10";
    if (tone === "rose") return "border-rose-500/30 bg-rose-500/10";
    return "border-white/10 bg-white/[0.04]";
}

function toneTextClass(tone) {
    if (tone === "emerald") return "text-emerald-300";
    if (tone === "accent") return "text-accent";
    if (tone === "amber") return "text-amber-300";
    if (tone === "rose") return "text-rose-300";
    return "text-slate-300";
}

function tonePillClass(tone) {
    if (tone === "emerald") return "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30";
    if (tone === "accent") return "bg-accent/15 text-accent border border-accent/30";
    if (tone === "amber") return "bg-amber-500/15 text-amber-300 border border-amber-500/30";
    if (tone === "rose") return "bg-rose-500/15 text-rose-300 border border-rose-500/30";
    return "bg-white/5 text-slate-300 border border-white/10";
}

function homeView(initialDashboard) {
    return {
        dashboard: deepCopy(initialDashboard),
        week: 1,
        weekSubtitle: "",
        profile: deepCopy(initialDashboard.profile || {}),
        checkin: {
            ...defaultCheckin(),
            ...(initialDashboard.today_checkin || {}),
        },
        savingProfile: false,
        savingCheckin: false,
        profileStatus: "",
        checkinStatus: "",
        errorMessage: "",

        init() {
            this.refreshWeek();
        },

        refreshWeek() {
            this.week = ironlogWeek();
            const start = localStorage.getItem(IRONLOG_KEYS.startDate);
            if (!start) {
                this.weekSubtitle = "";
                return;
            }
            const days = Math.max(
                0,
                Math.floor((Date.now() - new Date(`${start}T00:00:00`)) / 86400000)
            );
            this.weekSubtitle = `Day ${days + 1} of this training run`;
        },

        adjustWeek(delta) {
            ironlogAdjustWeek(delta);
            this.refreshWeek();
        },

        resetWeek() {
            if (
                confirm("Reset progression to week 1? This clears your current local week tracking.")
            ) {
                ironlogResetWeek();
                this.refreshWeek();
            }
        },

        displayName() {
            return this.dashboard.profile?.name || "Athlete";
        },

        readinessSurfaceClass() {
            return toneSurfaceClass(this.dashboard.readiness?.tone);
        },

        readinessTextClass() {
            return toneTextClass(this.dashboard.readiness?.tone);
        },

        readinessPillClass() {
            return tonePillClass(this.dashboard.readiness?.tone);
        },

        async saveProfile() {
            this.savingProfile = true;
            this.profileStatus = "";
            this.errorMessage = "";
            try {
                const data = await requestJson("/api/profile", {
                    method: "POST",
                    body: JSON.stringify({
                        name: this.profile.name,
                        training_goal: this.profile.training_goal,
                        focus_area: this.profile.focus_area,
                        preferred_session_minutes: this.profile.preferred_session_minutes,
                    }),
                });
                this.dashboard.profile = data.profile;
                this.profile = deepCopy(data.profile);
                this.profileStatus = "Profile saved";
            } catch (error) {
                this.errorMessage = error.message;
            } finally {
                this.savingProfile = false;
            }
        },

        async saveCheckin() {
            this.savingCheckin = true;
            this.checkinStatus = "";
            this.errorMessage = "";
            try {
                const payload = {
                    energy: safeNumber(this.checkin.energy, 3),
                    sleep: safeNumber(this.checkin.sleep, 3),
                    soreness: safeNumber(this.checkin.soreness, 3),
                    stress: safeNumber(this.checkin.stress, 3),
                    motivation: safeNumber(this.checkin.motivation, 3),
                    bodyweight_kg: this.checkin.bodyweight_kg || null,
                    step_count: this.checkin.step_count || null,
                    notes: this.checkin.notes || "",
                };
                const data = await requestJson("/api/checkins/today", {
                    method: "POST",
                    body: JSON.stringify(payload),
                });
                this.dashboard.today_checkin = data.checkin;
                this.dashboard.readiness = data.readiness;
                this.dashboard.coach_note = data.coach_note;
                this.checkin = {
                    ...defaultCheckin(),
                    ...(data.checkin || {}),
                };
                this.checkinStatus = "Check-in saved";
            } catch (error) {
                this.errorMessage = error.message;
            } finally {
                this.savingCheckin = false;
            }
        },
    };
}

function exerciseLibraryView(initialModel) {
    return {
        model: deepCopy(initialModel),
        query: "",
        activeCategory: "all",
        selectedExerciseId: "",

        init() {
            this.selectedExerciseId = this.exercises[0]?.id || "";
        },

        get categories() {
            return this.model.categories || [];
        },

        get exercises() {
            return this.model.exercises || [];
        },

        categoryCount(categoryId) {
            return this.exercises.filter((exercise) => exercise.category === categoryId).length;
        },

        categoryButtonClass(categoryId) {
            return this.activeCategory === categoryId
                ? "border-accent/40 bg-accent/15 text-accent"
                : "border-white/10 bg-white/5 text-slate-300 active:bg-white/10";
        },

        get filteredExercises() {
            const query = this.query.trim().toLowerCase();
            return this.exercises.filter((exercise) => {
                const categoryMatch =
                    this.activeCategory === "all" || exercise.category === this.activeCategory;
                if (!categoryMatch) return false;
                if (!query) return true;
                const searchable = [
                    exercise.name,
                    exercise.category_label,
                    exercise.muscle_focus,
                    exercise.source_workout_name,
                ]
                    .filter(Boolean)
                    .join(" ")
                    .toLowerCase();
                return searchable.includes(query);
            });
        },

        get selectedExercise() {
            return (
                this.exercises.find((exercise) => exercise.id === this.selectedExerciseId) ||
                this.filteredExercises[0] ||
                null
            );
        },

        selectExercise(exercise) {
            this.selectedExerciseId = exercise.id;
            window.requestAnimationFrame(() => {
                document.getElementById("exercise-detail")?.scrollIntoView({
                    behavior: "smooth",
                    block: "start",
                });
            });
        },

        startUrl(exercise) {
            return `/smart?start=${encodeURIComponent(exercise.id)}`;
        },

        doneUrl(exercise) {
            return `/smart?done=${encodeURIComponent(exercise.id)}`;
        },
    };
}

function workoutSession(initialModel) {
    return {
        model: deepCopy(initialModel),
        workout: deepCopy(initialModel.workout),
        exerciseLibrary: deepCopy(initialModel.exercise_library || []),
        initialDoneExerciseIds: deepCopy(initialModel.initial_done_exercise_ids || []),
        initialCurrentExerciseId: initialModel.initial_current_exercise_id || null,
        readiness: deepCopy(initialModel.readiness || {}),
        profile: deepCopy(initialModel.profile || {}),
        latestSession: initialModel.latest_session || null,
        stage: "strength",
        exerciseIdx: 0,
        setIdx: 0,
        resting: false,
        restRemaining: 0,
        restDuration: 0,
        restInterval: null,
        startTime: 0,
        elapsed: 0,
        elapsedInterval: null,
        audioCtx: null,
        wakeLock: null,
        imageFailed: false,
        week: 1,
        warmupActualSeconds: 0,
        cooldownActualSeconds: 0,
        restoredDraft: false,
        draftSavedAt: "",
        sessionNote: "",
        sessionFeeling: 0,
        exerciseStates: [],
        historyStack: [],
        sessionId: "",
        draftKey: "",
        librarySelectedId: "",
        smartMessage: "",
        saving: false,
        saved: false,
        saveError: "",
        saveResult: null,
        walk: {
            active: false,
            paused: false,
            remaining: 0,
            total: 0,
            interval: null,
            completed: false,
        },

        init() {
            this.week = ironlogWeek();
            this.sessionId =
                (window.crypto && window.crypto.randomUUID && window.crypto.randomUUID()) ||
                `session-${Date.now()}`;
            this.draftKey = `${IRONLOG_KEYS.sessionDraftPrefix}${this.workout.id}`;
            this.exerciseStates = this.workout.exercises.map((exercise) =>
                this.buildExerciseState(exercise)
            );
            this.sessionFeeling = safeNumber(this.model.today_checkin?.energy, 0);

            if (!this.restoreDraft()) {
                this.applyInitialSmartState();
                this.startTime = Date.now();
                if (this.hasTimedWalk(this.workout.warmup)) {
                    this.stage = "warmup";
                    this.initWalk(this.workout.warmup);
                }
            }

            this.librarySelectedId =
                this.currentExercise.id || this.exerciseLibrary[0]?.id || "";

            this.elapsedInterval = setInterval(() => {
                this.elapsed = Math.floor((Date.now() - this.startTime) / 1000);
            }, 1000);

            this.requestWakeLock();
            document.addEventListener("visibilitychange", () => {
                if (document.visibilityState === "visible") {
                    this.requestWakeLock();
                }
            });
            window.addEventListener("beforeunload", () => this.persistDraft());
        },

        applyInitialSmartState() {
            if (!this.workout.smart_mode) return;

            this.initialDoneExerciseIds.forEach((exerciseId) => {
                const index = this.workout.exercises.findIndex(
                    (exercise) => exercise.id === exerciseId
                );
                if (index === -1) return;
                const exercise = this.workout.exercises[index];
                const state = this.exerciseStates[index];
                state.completedSets = safeNumber(exercise.sets, 0);
                state.skippedSets = 0;
                state.recommendationReason = "Marked as completed before this smart session opened.";
            });

            if (this.initialCurrentExerciseId) {
                const currentIndex = this.workout.exercises.findIndex(
                    (exercise) => exercise.id === this.initialCurrentExerciseId
                );
                if (currentIndex !== -1) {
                    this.exerciseIdx = currentIndex;
                    this.setIdx = this.nextSetIndexForExercise(currentIndex);
                    return;
                }
            }

            const nextIndex = this.findNextPendingExerciseIndex(0);
            if (nextIndex !== -1) {
                this.exerciseIdx = nextIndex;
                this.setIdx = this.nextSetIndexForExercise(nextIndex);
            }
        },

        buildSuggestion(exercise) {
            const planWeight = computeExerciseWeight(exercise, this.week);
            const lastWeight =
                typeof exercise.last_logged_weight === "number"
                    ? exercise.last_logged_weight
                    : null;
            const lastCompletedSets = safeNumber(exercise.last_completed_sets, 0);
            const lastTargetSets = safeNumber(exercise.last_target_sets, 0);
            const step = exercise.progression_kg || 0.5;

            let suggested = planWeight;
            let reason =
                this.week > 1 && step > 0
                    ? `Week ${this.week} progression loaded.`
                    : "Starting from the programmed load.";

            if (lastWeight !== null) {
                if (lastTargetSets > 0 && lastCompletedSets < lastTargetSets) {
                    suggested = Math.min(lastWeight, planWeight);
                    reason = "Last time was unfinished, so hold steady and own the reps.";
                } else if (lastWeight > planWeight) {
                    suggested = lastWeight;
                    reason = "Last successful load was ahead of the plan, so we are building from there.";
                } else if (lastWeight === planWeight) {
                    reason = "Same load as your last successful session. Beat it cleanly.";
                }
            }

            if (this.readiness.weight_adjustment === "ease" && step > 0 && suggested > 0) {
                suggested = Math.max(exercise.weight || 0, roundToStep(suggested - step, step));
                reason = "Low-readiness day: one click lighter, cleaner reps.";
            } else if (
                this.readiness.weight_adjustment === "hold" &&
                lastWeight !== null &&
                step > 0
            ) {
                suggested = Math.min(suggested, Math.max(lastWeight, exercise.weight || 0));
                reason = "Steady day: hold the load and make the reps look strong.";
            }

            const cap = exercise.progression_cap || 0;
            if (cap > 0) {
                suggested = Math.min(suggested, cap);
            }

            suggested = roundToStep(suggested, step || 0.5);

            return {
                planWeight,
                suggestedWeight: suggested,
                reason,
                step,
            };
        },

        buildExerciseState(exercise) {
            const suggestion = this.buildSuggestion(exercise);
            return {
                suggestedWeight: suggestion.suggestedWeight,
                suggestedWeightLabel: formatWeight(exercise, suggestion.suggestedWeight),
                planWeight: suggestion.planWeight,
                planWeightLabel: formatWeight(exercise, suggestion.planWeight),
                workingWeight: suggestion.suggestedWeight,
                workingWeightLabel: formatWeight(exercise, suggestion.suggestedWeight),
                recommendationReason: suggestion.reason,
                step: suggestion.step,
                completedSets: 0,
                skippedSets: 0,
                notes: "",
            };
        },

        hasTimedWalk(cfg) {
            return safeNumber(cfg?.minutes, 0) > 0;
        },

        initWalk(cfg) {
            const minutes = safeNumber(cfg?.minutes, 0);
            this.walk.total = minutes * 60;
            this.walk.remaining = minutes * 60;
            this.walk.active = false;
            this.walk.paused = false;
            this.walk.completed = false;
            clearInterval(this.walk.interval);
        },

        captureWalkProgress() {
            return Math.max(0, this.walk.total - this.walk.remaining);
        },

        startWalk() {
            if (this.walk.completed) return;
            this.walk.active = true;
            this.walk.paused = false;
            this.ensureAudio();
            clearInterval(this.walk.interval);
            this.walk.interval = setInterval(() => {
                if (this.walk.paused) return;
                this.walk.remaining -= 1;
                if (this.walk.remaining <= 0) {
                    this.completeWalk();
                }
                this.persistDraft();
            }, 1000);
        },

        pauseWalk() {
            this.walk.paused = !this.walk.paused;
            this.persistDraft();
        },

        completeWalk() {
            clearInterval(this.walk.interval);
            this.walk.remaining = Math.max(0, this.walk.remaining);
            this.walk.active = false;
            this.walk.completed = true;
            this.playChime(true);
            if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
            this.persistDraft();
        },

        skipWalk() {
            clearInterval(this.walk.interval);
            this.walk.active = false;
            this.walk.completed = true;
            this.persistDraft();
        },

        nextStage() {
            if (this.stage === "warmup") {
                this.warmupActualSeconds = this.captureWalkProgress();
                this.stage = "strength";
                clearInterval(this.walk.interval);
            } else if (this.stage === "strength") {
                if (this.hasTimedWalk(this.workout.cooldown)) {
                    this.stage = "cooldown";
                    this.initWalk(this.workout.cooldown);
                } else {
                    this.stage = "done";
                    this.teardown();
                    this.saveSession();
                }
            } else if (this.stage === "cooldown") {
                this.cooldownActualSeconds = this.captureWalkProgress();
                this.stage = "done";
                this.teardown();
                this.saveSession();
            }
            this.persistDraft();
        },

        async requestWakeLock() {
            if (!("wakeLock" in navigator) || this.stage === "done") return;
            try {
                this.wakeLock = await navigator.wakeLock.request("screen");
                this.wakeLock.addEventListener("release", () => {
                    this.wakeLock = null;
                });
            } catch (_error) {
                this.wakeLock = null;
            }
        },

        releaseWakeLock() {
            if (!this.wakeLock) return;
            try {
                this.wakeLock.release();
            } catch (_error) {
                // Ignore release failures.
            }
            this.wakeLock = null;
        },

        teardown() {
            clearInterval(this.restInterval);
            clearInterval(this.elapsedInterval);
            clearInterval(this.walk.interval);
            this.releaseWakeLock();
        },

        snapshotState() {
            return deepCopy({
                stage: this.stage,
                exerciseIdx: this.exerciseIdx,
                setIdx: this.setIdx,
                resting: this.resting,
                restRemaining: this.restRemaining,
                restDuration: this.restDuration,
                imageFailed: this.imageFailed,
                exerciseStates: this.exerciseStates,
            });
        },

        restoreSnapshot(snapshot) {
            clearInterval(this.restInterval);
            this.stage = snapshot.stage;
            this.exerciseIdx = snapshot.exerciseIdx;
            this.setIdx = snapshot.setIdx;
            this.resting = snapshot.resting;
            this.restRemaining = snapshot.restRemaining;
            this.restDuration = snapshot.restDuration;
            this.imageFailed = snapshot.imageFailed;
            this.exerciseStates = deepCopy(snapshot.exerciseStates);
            if (this.resting && this.restRemaining > 0) {
                this.startRest(this.restRemaining);
            }
            this.persistDraft();
        },

        get currentExercise() {
            return this.workout.exercises[this.exerciseIdx] || {};
        },

        get currentExerciseState() {
            return this.exerciseStates[this.exerciseIdx] || {};
        },

        get currentWeight() {
            return this.currentExerciseState.workingWeight || 0;
        },

        get currentWeightLabel() {
            return this.currentExerciseState.workingWeightLabel || formatWeight(this.currentExercise, 0);
        },

        get currentSuggestedWeightLabel() {
            return (
                this.currentExerciseState.suggestedWeightLabel ||
                formatWeight(this.currentExercise, this.currentExerciseState.suggestedWeight || 0)
            );
        },

        get completedSets() {
            return this.exerciseStates.reduce((sum, exercise) => sum + (exercise.completedSets || 0), 0);
        },

        get skippedSets() {
            return this.exerciseStates.reduce((sum, exercise) => sum + (exercise.skippedSets || 0), 0);
        },

        get processedSets() {
            return this.completedSets + this.skippedSets;
        },

        get totalSets() {
            return this.workout.exercises.reduce((sum, exercise) => sum + (exercise.sets || 0), 0);
        },

        get progressPct() {
            if (this.totalSets === 0) return 0;
            return Math.min(100, (this.processedSets / this.totalSets) * 100);
        },

        exerciseProcessedSets(index) {
            const state = this.exerciseStates[index] || {};
            return safeNumber(state.completedSets, 0) + safeNumber(state.skippedSets, 0);
        },

        exerciseProgressPct(index) {
            const exercise = this.workout.exercises[index] || {};
            const sets = safeNumber(exercise.sets, 0);
            if (sets === 0) return 0;
            return Math.min(100, (this.exerciseProcessedSets(index) / sets) * 100);
        },

        exerciseProgressLabel(index) {
            const exercise = this.workout.exercises[index] || {};
            const processed = this.exerciseProcessedSets(index);
            return `${Math.min(processed, exercise.sets || 0)}/${exercise.sets || 0} sets`;
        },

        exerciseWeightLabel(index) {
            const exercise = this.workout.exercises[index] || {};
            const state = this.exerciseStates[index] || {};
            return state.workingWeightLabel || formatWeight(exercise, state.workingWeight || 0);
        },

        libraryWeightLabel(exercise) {
            const suggestion = this.buildSuggestion(exercise || {});
            return formatWeight(exercise || {}, suggestion.suggestedWeight || 0);
        },

        findLibraryExercise(exerciseId) {
            return this.exerciseLibrary.find((exercise) => exercise.id === exerciseId) || null;
        },

        get smartSelectedExercise() {
            return this.findLibraryExercise(this.librarySelectedId) || this.currentExercise;
        },

        ensureExerciseInWorkout(exercise) {
            const existingIndex = this.workout.exercises.findIndex(
                (item) => item.id === exercise.id
            );
            if (existingIndex !== -1) return existingIndex;

            const cloned = deepCopy(exercise);
            this.workout.exercises.push(cloned);
            this.exerciseStates.push(this.buildExerciseState(cloned));
            return this.workout.exercises.length - 1;
        },

        startWithExercise(exerciseId) {
            const exercise = this.findLibraryExercise(exerciseId);
            if (!exercise) return;
            const index = this.ensureExerciseInWorkout(exercise);
            this.selectExercise(index);
            this.librarySelectedId = exercise.id;
            this.smartMessage = `Current exercise set to ${exercise.name}.`;
        },

        logExerciseDone(exerciseId) {
            const exercise = this.findLibraryExercise(exerciseId);
            if (!exercise) return;
            const index = this.ensureExerciseInWorkout(exercise);
            this.historyStack.push(this.snapshotState());
            const state = this.exerciseStates[index];
            state.completedSets = Math.max(
                safeNumber(state.completedSets, 0),
                safeNumber(exercise.sets, 0)
            );
            state.skippedSets = 0;
            state.recommendationReason = "Logged as already completed in smart gym mode.";
            this.exerciseIdx = index;
            this.setIdx = this.nextSetIndexForExercise(index);
            this.resting = false;
            clearInterval(this.restInterval);

            const next = this.buildSmartRecommendations(exercise, 1)[0];
            if (next) {
                const nextIndex = this.ensureExerciseInWorkout(next);
                this.exerciseIdx = nextIndex;
                this.setIdx = this.nextSetIndexForExercise(nextIndex);
                this.librarySelectedId = next.id;
                this.smartMessage = `After ${exercise.name}, next best option is ${next.name}.`;
            } else {
                this.smartMessage = `${exercise.name} is logged. No fresh recommendation is left in the library.`;
            }
            this.imageFailed = false;
            this.persistDraft();
        },

        recommendationScore(after, candidate, index) {
            if (!after || !candidate || candidate.id === after.id) return -1;
            const completedIds = new Set(
                this.workout.exercises
                    .map((exercise, exerciseIndex) =>
                        this.isExerciseComplete(exerciseIndex) ? exercise.id : null
                    )
                    .filter(Boolean)
            );
            if (completedIds.has(candidate.id)) return -1;

            const preferred = {
                push: ["pull", "shoulders", "arms", "push"],
                pull: ["push", "shoulders", "arms", "pull"],
                shoulders: ["pull", "push", "arms", "shoulders"],
                arms: ["push", "pull", "shoulders", "arms"],
                "posterior-chain": ["pull", "push", "core-calves", "shoulders"],
                "core-calves": ["push", "pull", "shoulders", "arms"],
            }[after.category] || ["push", "pull", "shoulders", "arms"];

            let score = 0;
            const categoryIndex = preferred.indexOf(candidate.category);
            if (categoryIndex !== -1) {
                score += (preferred.length - categoryIndex) * 20;
            }
            if (candidate.movement_pattern !== after.movement_pattern) score += 8;
            if (["push", "pull", "shoulders", "arms"].includes(candidate.category)) score += 6;
            score += Math.max(0, 6 - safeNumber(candidate.category_rank, 0));
            score += Math.min(4, safeNumber(candidate.sets, 0));
            score -= index * 0.01;
            return score;
        },

        buildSmartRecommendations(after, limit = 6) {
            if (!this.exerciseLibrary.length) return [];
            const basis = after || this.currentExercise;
            return this.exerciseLibrary
                .map((candidate, index) => ({
                    exercise: candidate,
                    score: this.recommendationScore(basis, candidate, index),
                }))
                .filter((item) => item.score >= 0)
                .sort((a, b) => b.score - a.score || a.exercise.name.localeCompare(b.exercise.name))
                .slice(0, limit)
                .map((item) => item.exercise);
        },

        get smartRecommendations() {
            return this.buildSmartRecommendations(this.currentExercise, 6);
        },

        get primarySmartRecommendation() {
            return this.smartRecommendations[0] || null;
        },

        get alternateSmartRecommendations() {
            return this.smartRecommendations.slice(1, 5);
        },

        exerciseOverviewClass(index) {
            if (index === this.exerciseIdx) {
                return "border-accent/40 bg-accent/10";
            }
            if (this.isExerciseComplete(index)) {
                return "border-emerald-500/20 bg-emerald-500/10";
            }
            return "border-white/10 bg-slate-950/45 active:bg-white/5";
        },

        nextSetIndexForExercise(index) {
            const exercise = this.workout.exercises[index] || {};
            const maxSetIndex = Math.max(0, safeNumber(exercise.sets, 1) - 1);
            return Math.min(this.exerciseProcessedSets(index), maxSetIndex);
        },

        isExerciseComplete(index) {
            const exercise = this.workout.exercises[index] || {};
            return this.exerciseProcessedSets(index) >= safeNumber(exercise.sets, 0);
        },

        get currentExerciseDone() {
            return this.isExerciseComplete(this.exerciseIdx);
        },

        get allSetsProcessed() {
            return this.totalSets > 0 && this.processedSets >= this.totalSets;
        },

        findNextPendingExerciseIndex(startIndex = 0) {
            const total = this.workout.exercises.length;
            if (total === 0) return -1;
            for (let offset = 0; offset < total; offset += 1) {
                const index = (startIndex + offset + total) % total;
                if (!this.isExerciseComplete(index)) {
                    return index;
                }
            }
            return -1;
        },

        get nextExercise() {
            if (!this.currentExerciseDone) return this.currentExercise;
            const nextIndex = this.findNextPendingExerciseIndex(this.exerciseIdx + 1);
            return nextIndex === -1 ? null : this.workout.exercises[nextIndex];
        },

        get nextExerciseName() {
            return (this.nextExercise || this.currentExercise).name || "";
        },

        get nextSetLabel() {
            if (!this.currentExerciseDone) {
                return this.nextSetIndexForExercise(this.exerciseIdx) + 1;
            }
            const nextIndex = this.findNextPendingExerciseIndex(this.exerciseIdx + 1);
            return nextIndex === -1 ? 1 : this.nextSetIndexForExercise(nextIndex) + 1;
        },

        get elapsedLabel() {
            const minutes = Math.floor(this.elapsed / 60);
            const seconds = this.elapsed % 60;
            return `${minutes}:${String(seconds).padStart(2, "0")}`;
        },

        get volumeKg() {
            return this.exerciseStates.reduce((sum, state, index) => {
                const exercise = this.workout.exercises[index];
                const reps = safeNumber(String(exercise.reps).match(/\d+/)?.[0], 0);
                return sum + state.workingWeight * reps * state.completedSets;
            }, 0);
        },

        get volumeLabel() {
            const volume = Math.round(this.volumeKg * 10) / 10;
            return `${String(volume).replace(/\.0$/, "")} kg`;
        },

        formatTime(seconds) {
            const total = Math.max(0, Math.floor(seconds));
            const minutes = Math.floor(total / 60);
            const rem = total % 60;
            return `${minutes}:${String(rem).padStart(2, "0")}`;
        },

        readinessSurfaceClass() {
            return toneSurfaceClass(this.readiness?.tone);
        },

        readinessTextClass() {
            return toneTextClass(this.readiness?.tone);
        },

        pillClass(tone) {
            return tonePillClass(tone);
        },

        selectExercise(index) {
            if (!this.workout.exercises[index]) return;
            if (this.exerciseIdx === index && !this.resting) return;
            this.historyStack.push(this.snapshotState());
            clearInterval(this.restInterval);
            this.resting = false;
            this.exerciseIdx = index;
            this.setIdx = this.nextSetIndexForExercise(index);
            this.imageFailed = false;
            this.persistDraft();
        },

        adjustWeight(direction) {
            const current = this.currentExerciseState;
            const step = current.step || this.currentExercise.progression_kg || 0.5;
            if (step <= 0 || this.currentExercise.weight_format === "bodyweight") return;
            const next = Math.max(0, roundToStep(current.workingWeight + step * direction, step));
            current.workingWeight = next;
            current.workingWeightLabel = formatWeight(this.currentExercise, next);
            this.persistDraft();
        },

        resetWeight() {
            const current = this.currentExerciseState;
            current.workingWeight = current.suggestedWeight;
            current.workingWeightLabel = current.suggestedWeightLabel;
            this.persistDraft();
        },

        completeSet() {
            if (this.currentExerciseDone) return;
            this.historyStack.push(this.snapshotState());
            this.currentExerciseState.completedSets += 1;

            if (this.allSetsProcessed) {
                this.playChime(true);
                if (navigator.vibrate) navigator.vibrate([100, 50, 100, 50, 200]);
                this.nextStage();
                return;
            }

            const rest = this.currentExercise.rest_seconds || 90;
            this.startRest(rest);
            this.persistDraft();
        },

        startRest(seconds) {
            this.resting = true;
            this.restDuration = seconds;
            this.restRemaining = seconds;
            this.ensureAudio();
            clearInterval(this.restInterval);
            this.restInterval = setInterval(() => {
                this.restRemaining -= 1;
                if (this.restRemaining === 3) this.playChime(false, 0.15);
                if (this.restRemaining <= 0) {
                    this.finishRest();
                }
                this.persistDraft();
            }, 1000);
        },

        finishRest() {
            clearInterval(this.restInterval);
            this.resting = false;
            this.advance();
            this.playChime(true);
            if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
            this.persistDraft();
        },

        skipRest() {
            clearInterval(this.restInterval);
            this.resting = false;
            this.advance();
            this.persistDraft();
        },

        adjustRest(delta) {
            this.restRemaining = Math.max(0, this.restRemaining + delta);
            this.restDuration = Math.max(0, this.restDuration + delta);
            if (this.restRemaining === 0 && this.resting) {
                this.finishRest();
            }
            this.persistDraft();
        },

        advance() {
            if (!this.currentExerciseDone) {
                this.setIdx = this.nextSetIndexForExercise(this.exerciseIdx);
                return;
            }
            const nextIndex = this.findNextPendingExerciseIndex(this.exerciseIdx + 1);
            if (nextIndex === -1) {
                this.nextStage();
                return;
            }
            this.exerciseIdx = nextIndex;
            this.setIdx = this.nextSetIndexForExercise(nextIndex);
            this.imageFailed = false;
        },

        skipSet() {
            if (this.currentExerciseDone) return;
            this.historyStack.push(this.snapshotState());
            this.currentExerciseState.skippedSets += 1;
            if (this.allSetsProcessed) {
                this.nextStage();
            } else {
                this.advance();
            }
            this.persistDraft();
        },

        prevSet() {
            if (this.historyStack.length === 0) return;
            const snapshot = this.historyStack.pop();
            this.restoreSnapshot(snapshot);
        },

        persistDraft() {
            if (!this.draftKey || this.saved) return;
            const payload = {
                stage: this.stage,
                exerciseIdx: this.exerciseIdx,
                setIdx: this.setIdx,
                resting: this.resting,
                restRemaining: this.restRemaining,
                restDuration: this.restDuration,
                startTime: this.startTime,
                elapsed: this.elapsed,
                sessionId: this.sessionId,
                week: this.week,
                imageFailed: this.imageFailed,
                warmupActualSeconds: this.warmupActualSeconds,
                cooldownActualSeconds: this.cooldownActualSeconds,
                restoredDraft: true,
                sessionNote: this.sessionNote,
                sessionFeeling: this.sessionFeeling,
                exerciseStates: this.exerciseStates,
                historyStack: this.historyStack,
                walk: this.walk,
                savedAt: new Date().toISOString(),
            };
            localStorage.setItem(this.draftKey, JSON.stringify(payload));
            this.draftSavedAt = payload.savedAt;
        },

        restoreDraft() {
            const raw = localStorage.getItem(this.draftKey);
            if (!raw) return false;

            try {
                const draft = JSON.parse(raw);
                if (
                    Array.isArray(draft.exerciseStates) &&
                    draft.exerciseStates.length !== this.workout.exercises.length
                ) {
                    localStorage.removeItem(this.draftKey);
                    return false;
                }
                const shouldRestore = confirm(
                    "Resume your in-progress workout? Your previous session draft is still saved."
                );
                if (!shouldRestore) {
                    localStorage.removeItem(this.draftKey);
                    return false;
                }

                this.stage = draft.stage || "strength";
                if (this.stage === "warmup" && !this.hasTimedWalk(this.workout.warmup)) {
                    this.stage = "strength";
                }
                if (this.stage === "cooldown" && !this.hasTimedWalk(this.workout.cooldown)) {
                    this.stage = "strength";
                }
                this.exerciseIdx = safeNumber(draft.exerciseIdx, 0);
                this.setIdx = safeNumber(draft.setIdx, 0);
                this.resting = Boolean(draft.resting);
                this.restRemaining = safeNumber(draft.restRemaining, 0);
                this.restDuration = safeNumber(draft.restDuration, 0);
                this.startTime = safeNumber(draft.startTime, Date.now());
                this.elapsed = safeNumber(draft.elapsed, 0);
                this.sessionId = draft.sessionId || this.sessionId;
                this.week = safeNumber(draft.week, this.week);
                this.imageFailed = Boolean(draft.imageFailed);
                this.warmupActualSeconds = safeNumber(draft.warmupActualSeconds, 0);
                this.cooldownActualSeconds = safeNumber(draft.cooldownActualSeconds, 0);
                this.sessionNote = draft.sessionNote || "";
                this.sessionFeeling = safeNumber(draft.sessionFeeling, 0);
                this.exerciseStates = draft.exerciseStates || this.exerciseStates;
                this.historyStack = draft.historyStack || [];
                this.walk = draft.walk || this.walk;
                this.restoredDraft = true;
                this.draftSavedAt = draft.savedAt || "";

                if (this.resting && this.restRemaining > 0) {
                    this.startRest(this.restRemaining);
                }
                if (
                    (this.stage === "warmup" || this.stage === "cooldown") &&
                    this.walk.active &&
                    !this.walk.completed
                ) {
                    this.startWalk();
                    if (this.walk.paused) this.walk.paused = true;
                }
                return true;
            } catch (_error) {
                localStorage.removeItem(this.draftKey);
                return false;
            }
        },

        clearDraft() {
            if (this.draftKey) {
                localStorage.removeItem(this.draftKey);
            }
            this.draftSavedAt = "";
        },

        buildSessionPayload() {
            return {
                session_id: this.sessionId,
                workout_id: this.workout.id,
                workout_name: this.workout.name,
                week: this.week,
                started_at: new Date(this.startTime).toISOString(),
                completed_at: new Date().toISOString(),
                duration_seconds: this.elapsed,
                warmup_seconds: this.warmupActualSeconds,
                cooldown_seconds: this.cooldownActualSeconds,
                readiness_score: this.readiness?.score || null,
                energy: this.model.today_checkin?.energy || null,
                notes: this.sessionNote || "",
                session_feeling: this.sessionFeeling || null,
                exercise_logs: this.workout.exercises.map((exercise, index) => {
                    const state = this.exerciseStates[index];
                    return {
                        exercise_id: exercise.id,
                        exercise_name: exercise.name,
                        reps: exercise.reps,
                        target_sets: exercise.sets,
                        completed_sets: state.completedSets,
                        skipped_sets: state.skippedSets,
                        working_weight: state.workingWeight,
                        working_weight_label: state.workingWeightLabel,
                        suggested_weight: state.suggestedWeight,
                        suggested_weight_label: state.suggestedWeightLabel,
                        notes: state.notes || "",
                    };
                }),
            };
        },

        async saveSession() {
            if (this.saving || this.saved) return;
            this.saving = true;
            this.saveError = "";
            try {
                const data = await requestJson("/api/sessions", {
                    method: "POST",
                    body: JSON.stringify(this.buildSessionPayload()),
                });
                this.saveResult = data;
                this.saved = true;
                this.clearDraft();
            } catch (error) {
                this.saveError = error.message;
                this.persistDraft();
            } finally {
                this.saving = false;
            }
        },

        ensureAudio() {
            if (!this.audioCtx) {
                try {
                    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
                    this.audioCtx = new AudioContextCtor();
                } catch (_error) {
                    return null;
                }
            }
            if (this.audioCtx.state === "suspended") this.audioCtx.resume();
            return this.audioCtx;
        },

        playChime(doubleNote = false, volume = 0.25) {
            const ctx = this.ensureAudio();
            if (!ctx) return;
            const now = ctx.currentTime;
            const tones = doubleNote ? [[880, 0], [1320, 0.18]] : [[660, 0]];
            tones.forEach(([freq, offset]) => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = "sine";
                osc.frequency.value = freq;
                gain.gain.setValueAtTime(0, now + offset);
                gain.gain.linearRampToValueAtTime(volume, now + offset + 0.01);
                gain.gain.exponentialRampToValueAtTime(0.001, now + offset + 0.35);
                osc.connect(gain).connect(ctx.destination);
                osc.start(now + offset);
                osc.stop(now + offset + 0.4);
            });
        },
    };
}
