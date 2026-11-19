"""Bounded calendar policy and known local exits, driven by one kernel tick."""
from datetime import datetime, time, timedelta, timezone

from ..contracts.models import ActivityStatus, CandidateStatus, ReasonCode
from ..kernel.codec import KernelError, digest, integer, moment
from ..storage import StorageReader
from .access import CLASS_WINDOW
from .campus import campus_route_timing, route_when_due
from .models import zone
from .service import timestamp


class ClassActivities:
    def __init__(self, kernel, academics, campus=None, *, early_departure_lead_s=300):
        self.kernel, self.academics, self.campus = kernel, academics, campus
        self.early_departure_lead = timedelta(seconds=integer(early_departure_lead_s))

    def __getattr__(self, name):
        return getattr(self.kernel, name)

    def _sweep(self, scope, now):
        for instance in self.kernel.repo.active(scope.owner_key):
            if instance.context_key != scope.context_key or instance.executor_version != "1" or not instance.template_id.startswith("class_session_"):
                continue
            pending = self.kernel.repo.pending_result(scope.owner_key, instance.instance_id)
            if pending is not None:
                try:
                    self.kernel.commit_result(scope, pending, now=now)
                except KernelError as error:
                    cp = instance.checkpoint
                    moving = cp["arrival_effect"] or cp.get("commute_stop_unknown")
                    if error.code != ReasonCode.STALE_VERSION.value or not moving or not self._origin_changed(scope, cp):
                        raise
                    self.kernel.abandon_result(scope, pending, expected_revision=instance.revision,
                        command_id="class-arrival-conflict:" + instance.instance_id + ":" + str(instance.revision),
                        reason="campus origin changed before local arrival commit", now=now)
                continue
            if instance.status == ActivityStatus.UNKNOWN:
                continue
            cp = instance.checkpoint
            ended = now >= timestamp(cp["session"]["end_at"])
            if ended and instance.status == ActivityStatus.RUNNING:
                if self.kernel._anchor(instance) is None:
                    self.kernel.recover(scope, now=now)
                    instance = self.kernel.repo.instance(scope.owner_key, instance.instance_id)
                if instance.status == ActivityStatus.RUNNING:
                    self.kernel.advance(scope, instance.instance_id, expected_revision=instance.revision,
                        command_id="class-finish:" + instance.instance_id + ":" + str(instance.revision), now=now)
                    continue
            changed_origin = cp["params"]["decision"] == "attend" and not cp["arrived"] and self._origin_changed(scope, cp)
            if ended or changed_origin:
                # A stopped/offline interval has no attendance evidence. Use the
                # ordinary authorized cancel edge even if host resources/agenda
                 # now prevent resuming. The durable command names the exit cause.
                self.kernel.cancel(scope, instance.instance_id, expected_revision=instance.revision,
                    command_id=("class-ended:" if ended else "class-origin-changed:") + instance.instance_id + ":" + str(instance.revision), now=now)
            elif instance.status == ActivityStatus.PAUSED and instance.reason_code == ReasonCode.NO_TIME_WINDOW:
                # A gate preview avoids appending a denied start and
                # command on every tick while the host window is still short.
                snapshot = self.kernel._snapshot(scope, now)
                template, prepared, _audience = self.kernel.repo.pinned(scope.owner_key, instance.instance_id)
                executor = self.kernel.registry.executor(template)
                gate = self.kernel._gate(scope, snapshot, template, now,
                    self.kernel._remaining_duration(executor, instance, now),
                    prepared.required_resources, instance, cooldown=False)
                if not gate.allowed:
                    continue
                self.kernel.start(scope, instance.instance_id, expected_revision=instance.revision,
                    command_id="class-resume:" + instance.instance_id + ":" + str(instance.revision) + ":" + digest(now)[:12], now=now)

    def _origin_changed(self, scope, checkpoint):
        if self.campus is None:
            return False
        place, revision = self.campus.location(scope.owner_key)
        return (place != checkpoint["params"]["route"]["from_place_id"]
                or (checkpoint.get("shared_position") and revision != checkpoint["location_revision"]))

    def _gc_templates(self):
        """Drop only unreferenced dynamic registry entries; SQLite pins are history."""
        if not self.academics._template_refs:
            return
        with self.academics.storage._lock:
            db = self.academics.storage._connection
            live = {(row[0], row[1]) for row in db.execute("""SELECT
                json_extract(template_json,'$.id'), json_extract(template_json,'$.version')
                FROM life_kernel_instances WHERE status NOT IN ('completed','failed','cancelled')
                UNION SELECT json_extract(record_json,'$.template_ref.id'), json_extract(record_json,'$.template_ref.version')
                FROM life_kernel_candidates WHERE status='pending'""")}
            for scope, refs in tuple(self.academics._template_refs.items()):
                for key in tuple(refs):
                    if key not in live:
                        self.kernel.registry.templates.pop(key, None)
                        refs.discard(key)
                if not refs:
                    self.academics._template_refs.pop(scope, None)

    def _rejected(self, scope, session, *, hosted=False):
        from ..activities.class_session import PINNED_DEPARTURE_REASON
        # Only the latest decision can authorize a retry. One query avoids
        # rescanning every earlier retry's gate history on each calendar tick.
        row = self.kernel.repo.reader.one("""SELECT c.candidate_id,c.status,
            json_extract(c.record_json,'$.reasons[0]') AS trigger,
            (SELECT json_extract(g.gate_json,'$.reason_code') FROM life_kernel_gates g
                WHERE g.owner_key=c.owner_key AND g.command_id IN (
                    'candidate-error-' || c.candidate_id, 'prepare-' || c.candidate_id)
                ORDER BY g.stage LIMIT 1) AS reason_code
            FROM life_kernel_candidates c
            WHERE c.owner_key=? AND c.context_key=? AND c.status IN ('rejected','expired')
            AND json_extract(c.record_json,'$.target_ref.kind')='academics.session'
            AND json_extract(c.record_json,'$.target_ref.id')=?
            AND json_extract(c.record_json,'$.target_ref.revision')=? ORDER BY c.rowid DESC LIMIT 1""",
            (scope.owner_key, scope.context_key, session["session_id"], session["revision"]))
        if row is None:
            return False, None
        retryable = ('missing_dependency', 'no_time_window', 'insufficient_resource') if hosted else ('missing_dependency',)
        if ((row['status'] == 'rejected' and row['reason_code'] in retryable)
                or (row['status'] == 'expired' and row['trigger'] == PINNED_DEPARTURE_REASON)):
            return False, row['candidate_id']
        return True, None

    def tick(self, *, now):
        if not self.kernel.config.enabled or self.kernel.closed:
            return self.kernel.tick(now=now)
        from ..activities.class_session import _propose_quoted_class
        failures = []
        now = moment(now)
        for scope in self.kernel.access.scopes():
            try:
                scope = self.kernel._scope(scope, "kernel.propose")
                # A private academic workspace never blocks another domain's context.
                with self.academics.storage._lock:
                    owner = self.academics.storage._connection.execute(
                        "SELECT context_key FROM life_academic_owners WHERE owner_key=?", (scope.owner_key,)).fetchone()
                if owner is not None and owner[0] != scope.context_key:
                    continue
                self._sweep(scope, now)
                if self.kernel.repo.foreground_busy(scope.owner_key) or self.campus is None:
                    continue
                origin, _revision = self.campus.location(scope.owner_key)
                if origin is None:
                    continue  # Missing location remains unknown, not a repeated failing proposal.
                for session in self.academics.next_sessions(scope.owner_key, scope.context_key, now=now):
                    if self.academics.participation(scope.owner_key, scope.context_key, session["session_id"]) is not None:
                        continue
                    rejected, retry = self._rejected(scope, session)
                    if rejected:
                        continue
                    class_start = timestamp(session["start_at"])
                    if class_start - now > timedelta(days=1):
                        continue  # Routes are bounded to one day; no distant proposal is due.
                    try:
                        destination = session["place_id"]
                        calendar_route = getattr(self.campus, "calendar_route", None)
                        if calendar_route is None:
                            timing = campus_route_timing(self.campus, origin, destination, now)
                            route = route_when_due(timing, now, class_start)
                        else:
                            route = calendar_route(origin, destination, now, class_start,
                                timestamp(session["end_at"]), self.early_departure_lead)
                        if route is None:
                            continue
                        arrival = now + timedelta(seconds=route["travel_s"])
                        reason = "travel_delay" if arrival > class_start else "manual_decision"
                        candidate = _propose_quoted_class(self.kernel, self.academics, scope, session, now=now,
                            decision="attend", reason=reason, route=route, retry_key=retry)
                        if candidate is None or candidate.status != CandidateStatus.PENDING:
                            continue
                        break
                    except KernelError as error:
                        failures.append(error.code)
                        continue
            except KernelError as error:
                failures.append(error.code)
            except Exception as error:
                # Keep provider details private and let other scopes, then the
                # wrapped wallet/activity coordinator, make progress as usual.
                failures.append("unexpected_error:" + type(error).__name__)
        result = self.kernel.tick(now=now)
        try:
            self._gc_templates()
        except KernelError as error:
            failures.append(error.code)
        except Exception as error:
            failures.append("unexpected_error:" + type(error).__name__)
        return {**result, "failures": (*failures, *result["failures"])}

    def hosted_settle(self, *, now, attendance, since):
        for scope in self.kernel.access.scopes():
            if not self.kernel.access.authorize(scope, "kernel.recover"):
                continue
            self._sweep(scope, now)
            if not self._owns_workspace(scope):
                continue
            for session in self.academics.unclaimed_ended(scope.owner_key, scope.context_key, since=since, now=now):
                attendance.settle_missed(scope, session, now=now, write_guard=self.kernel.access.write_guard)

    def settle_disabled(self, *, now):
        for scope in self.kernel.access.scopes():
            if not self.kernel.access.authorize(scope, "kernel.cancel"):
                continue
            for instance in self.kernel.repo.active(scope.owner_key):
                if (instance.context_key == scope.context_key and instance.status != ActivityStatus.UNKNOWN
                        and instance.template_id.startswith(("class_session_", "study_"))
                        and self.kernel.repo.pending_result(scope.owner_key, instance.instance_id) is None):
                    self.kernel.cancel(scope, instance.instance_id, expected_revision=instance.revision,
                        command_id=f"disabled-{instance.instance_id}-{instance.revision}", now=now)

    def _owns_workspace(self, scope):
        row = StorageReader(self.academics.storage).one(
            "SELECT context_key FROM life_academic_owners WHERE owner_key=?", (scope.owner_key,))
        return row is not None and row[0] == scope.context_key

    def _wake_class(self, scope, session, attendance, shadow, departure_plan, origin_revision, now):
        storage = self.academics.storage
        with StorageReader(storage).locked():
            wake = attendance._wake(storage._connection, scope, session["session_id"])
        if wake is not None:
            return wake
        start = timestamp(session["start_at"])
        departure, travel_s = departure_plan
        if now < departure - timedelta(seconds=attendance.config.wake_check_lead_s):
            return None
        reader = StorageReader(storage)
        records = ()
        if attendance.config.base_rate and reader.one("SELECT 1 FROM sqlite_master WHERE name='staged_role_activities'", ()):
            records = tuple(row[0] for row in reader.all("""SELECT record_json FROM staged_role_activities
                WHERE owner_key=? AND json_extract(record_json,'$.scope.kind')='private'
                AND (json_extract(record_json,'$.sleep.kind') IS NOT NULL
                     OR json_extract(record_json,'$.work.kind')='sleep')
                ORDER BY updated_at DESC LIMIT 200""", (scope.owner_key,)))
        use_shadow = shadow is not None and shadow.config.sleep
        state = shadow.snapshot(scope.owner_key) if use_shadow else None
        with storage._lock, self.kernel.access.write_guard(scope, "kernel.propose"):
            return attendance.check_wake(self.kernel, scope, session["session_id"], now=now,
                planned_departure_at=departure, planned_travel_s=travel_s, origin_revision=origin_revision,
                sleep_records=records, known_ready_at=departure if now > start else None, a6_enabled=use_shadow,
                a6_sleep_enabled=use_shadow, a6_state=state)

    def hosted_commitments(self, *, now, attendance, shadow=None):
        scopes = {(scope.owner_key, scope.context_key): scope for scope in self.kernel.access.scopes()}
        for scope in scopes.values():
            if not self.kernel.access.authorize(scope, "kernel.propose") or not self._owns_workspace(scope):
                continue
            with self.academics.storage._lock:
                sessions = self.academics.next_sessions(scope.owner_key, scope.context_key, now=now, limit=1)
                if sessions:
                    self._hosted_class(scope, sessions[0], attendance, shadow, now, scopes)
            if self.campus is not None and self.campus.home_place_id:
                self._return_home(scope, now)
        self._gc_templates()

    def _preempt_for_class(self, owner_key, scopes, departure, now):
        for instance in self.kernel.repo.active(owner_key):
            if instance.status not in (ActivityStatus.PREPARING, ActivityStatus.RUNNING):
                continue
            if instance.template_id.startswith("class_session_") or self.kernel.repo.pending_result(owner_key, instance.instance_id):
                continue
            scope = scopes.get((owner_key, instance.context_key))
            if scope is None or not self.kernel.access.authorize(scope, "kernel.cancel"):
                continue
            template, _prepared, _audience = self.kernel.repo.pinned(owner_key, instance.instance_id)
            executor = self.kernel.registry.executor(template)
            if not executor.local_only:
                continue
            # The running checkpoint already accounts for time through its last tick.
            observed_at = instance.updated_at if self.kernel._anchor(instance) is not None else now
            remaining = self.kernel._remaining_duration(executor, instance, observed_at)
            if now >= departure or observed_at + timedelta(seconds=remaining) > departure:
                # Normal cancellation releases resources and preserves partial study work.
                self.kernel.cancel(scope, instance.instance_id, expected_revision=instance.revision,
                    command_id=f"class-preempt-{instance.instance_id}-{instance.revision}", now=now)

    def _hosted_class(self, scope, session, attendance, shadow, now, scopes):
        start, end = timestamp(session["start_at"]), timestamp(session["end_at"])
        if start > now + timedelta(days=1):
            return
        session_id = session["session_id"]
        if self.academics.participation(scope.owner_key, scope.context_key, session_id) is not None:
            return
        location = self.campus.location(scope.owner_key) if self.campus is not None else (None, 0)
        origin, origin_revision = location
        departure_plan = (start, 0)
        if origin and now < start:
            departure_plan = self.campus.class_departure(origin, session["place_id"], now, start, self.early_departure_lead)
        wake = self._wake_class(scope, session, attendance, shadow, departure_plan, origin_revision, now)
        if wake is not None and (wake["session_revision"], wake.get("origin_revision")) == (session["revision"], origin_revision):
            departure_plan = (timestamp(wake["planned_departure_at"]), wake["planned_travel_s"])
        if self.kernel.repo.foreground_busy(scope.owner_key):
            self._preempt_for_class(scope.owner_key, scopes, departure_plan[0], now)
            if self.campus is not None and self.campus.location(scope.owner_key) != location:
                return  # Replan from the position left by the interruption on the next tick.
        if not origin or wake is None or self.kernel.repo.foreground_busy(scope.owner_key):
            return
        if now < departure_plan[0]:
            return
        if wake["wake_delay_s"] and now < timestamp(wake["ready_at"]):
            return
        snapshot = self.kernel.access.snapshot(scope, now)
        window = snapshot.state.values.get(CLASS_WINDOW)
        available_until = timestamp(window.value["free_until"]) if window is not None else snapshot.free_until
        if snapshot.busy is not False or available_until is None or end > available_until:
            return
        rejected, retry = self._rejected(scope, session, hosted=True)
        if rejected:
            return
        try:
            route, _ = campus_route_timing(self.campus, origin, session["place_id"], now,
                                          class_start=start, class_end=end)
        except KernelError:
            return  # Requote on the next tick, or settle the miss at the end.
        candidate = attendance.propose(self.kernel, scope, session_id, now=now, route=route,
                                       retry_key=retry, defer_absence=True, departure_plan=departure_plan)
        self._start_candidate(scope, candidate, now)

    def _start_candidate(self, scope, candidate, now):
        if candidate is None or candidate.status != CandidateStatus.PENDING:
            return
        preparation = self.kernel.prepare_candidate(scope, candidate.candidate_id,
            command_id="prepare-" + candidate.candidate_id, now=now)
        if preparation.instance is not None:
            instance = preparation.instance
            self.kernel.start(scope, instance.instance_id, expected_revision=instance.revision,
                              command_id="start-" + instance.instance_id, now=now)

    def _home_due(self, scope, now):
        if self.campus is None or not self.campus.home_place_id or not self._owns_workspace(scope):
            return False
        # Keep late evening returns eligible across midnight in the timetable timezone.
        with StorageReader(self.academics.storage).locked():
            terms = self.academics._records(self.academics.storage._connection, scope.owner_key, "term")
        tz = zone(next(iter(terms.values()))["timezone"]) if terms else timezone.utc
        tomorrow = (now.astimezone(tz) - timedelta(hours=4)).date() + timedelta(days=1)
        day_end = datetime.combine(tomorrow, time(4), tz)
        snapshot = self.kernel.access.snapshot(scope, now)
        return snapshot.free_until is not None and snapshot.free_until >= day_end

    def _return_home(self, scope, now):
        from ..travel import propose_walk
        if self.kernel.repo.foreground_busy(scope.owner_key) or not self._home_due(scope, now):
            return
        place, revision = self.campus.location(scope.owner_key)
        home = self.campus.home_place_id
        if place is None or place == home or self.kernel.access.snapshot(scope, now).busy is not False:
            return
        try:
            candidate = propose_walk(self.kernel, self.campus, scope, home, now=now,
                                     opportunity_key=f"home-return-{revision}")
        except KernelError:
            return
        ref = candidate.template_ref
        self.academics._template_refs.setdefault((scope.owner_key, scope.context_key), set()).add((ref.id, ref.version))
        self._start_candidate(scope, candidate, now)

    def hold_night_sleep(self, context_key, owner_key, now):
        for scope in self.kernel.access.scopes():
            if (scope.owner_key, scope.context_key) != (owner_key, context_key):
                continue
            snapshot = self.kernel.access.snapshot(scope, now)
            if CLASS_WINDOW in snapshot.state.values and snapshot.free_until == now:
                return True
            if self.campus is None or not self.campus.home_place_id or not self._owns_workspace(scope):
                return False
            home = self.campus.home_place_id
            if any(item.template_id.startswith("campus_walk_") and item.context_key == context_key
                   and item.status in (ActivityStatus.PREPARING, ActivityStatus.RUNNING, ActivityStatus.UNKNOWN)
                   and item.checkpoint["params"]["route"]["destination"] == home
                   for item in self.kernel.repo.active(owner_key)):
                return True
            place, _ = self.campus.location(owner_key)
            if place is not None and place != home:
                try:
                    self.campus.plan_walk(owner_key, self.campus.home_place_id, departure_at=now)
                except KernelError:
                    return False
                return True
        return False
