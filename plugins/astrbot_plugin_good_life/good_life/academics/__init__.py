"""Default-off manual timetable and simulated campus class activities."""
from ..kernel.codec import encode
from .models import AcademicsConfig
from .service import Academics, CLASS_CUTOVER, SPEC
from .access import AcademicsAccess
from .attendance import AttendanceConfig, ClassAttendance, STUDY_CONSUMER
from .campus import Campus, ConfirmedGraphCampus
from .coordinator import ClassActivities


def register_academics(registry, academics, *, campus=None):
    from ..activities.class_session import ClassSessionLocal
    from .campus import SPEC as CAMPUS_SPEC
    registry.register_module(SPEC, academics)
    if campus is not None:
        registry.register_module(CAMPUS_SPEC, campus)
    registry.register_executor(ClassSessionLocal())
    from ..activities.study import StudyLocal
    registry.register_executor(StudyLocal())


def _has_class_history(reader):
    from .study_artifacts import VERSION_KEY
    if reader.one("SELECT 1 FROM life_metadata WHERE key=?", (CLASS_CUTOVER,)):
        return True
    if (reader.one("SELECT 1 FROM life_metadata WHERE key=?", (VERSION_KEY,))
            and reader.one("""SELECT 1 FROM life_event_deliveries
                WHERE consumer=? AND acknowledged_at IS NULL LIMIT 1""", (STUDY_CONSUMER,))):
        return True
    return _has_active_academics(reader)


def _has_active_academics(reader, *patterns):
    if not patterns:
        patterns = ("class_session_*", "study_*")
    return (reader.one("SELECT 1 FROM sqlite_master WHERE name='life_kernel_instances'", ()) is not None
            and reader.one("""SELECT 1 FROM life_kernel_instances
                WHERE status IN ('preparing','running','paused','unknown')
                AND EXISTS (SELECT 1 FROM json_each(?) WHERE json_extract(record_json,'$.template_id') GLOB value)
                LIMIT 1""", (encode(patterns),)) is not None)


def register_class_group(runtime, *, enabled, attendance_enabled=False, academics_config=None):
    """Bind the academic effect owner and class policy in the shared host loop."""
    from ..graph_package_storage import GraphPackageStore
    from ..kernel.codec import wire
    from ..runtime import OptionalGroupBinding
    from .service import timestamp

    def bind_academics(context):
        config = academics_config if context.mode == "active" else None
        academics = runtime.academics or Academics(config=config or AcademicsConfig(enabled=True))
        travel_mode = runtime._optional.modes.get("shared_travel")
        if ("shared_travel" in context.groups or travel_mode is not None
                or not _has_active_academics(context.access.reader, "class_session_*")):
            register_academics(context.registry, academics)
        else:
            # Pending v1 effects may still need the original campus table.
            campus = ConfirmedGraphCampus(GraphPackageStore(context.storage), None, settle_only=True)
            register_academics(context.registry, academics, campus=campus)
        return OptionalGroupBinding(value=academics, capabilities=("academics",))

    def bind_classes(context):
        academics = context.groups["academics"]
        kernel = context.kernel
        legacy = kernel.registry.modules.get("campus", (None, None))[1]
        artifacts = (runtime.study_artifact_reader if context.mode == "settle_only"
                     else getattr(academics, "study_artifacts", None))

        def deliver(*, now):
            storage = context.storage
            for scope in context.access.scopes():
                if not context.access.authorize(scope, "kernel.recover"):
                    continue
                for event in storage.pending_events(STUDY_CONSUMER, context_key=scope.context_key):
                    if event["owner_key"] != scope.owner_key:
                        continue
                    with storage._lock, context.access.write_guard(scope, "kernel.recover"):
                        artifacts.consume_attendance(scope.owner_key, scope.context_key, event["payload"], now=now)
                        with storage.transaction():
                            storage.acknowledge(event_id=event["event_id"], consumer=STUDY_CONSUMER, acknowledged_at=now)

        if context.mode == "settle_only":
            with context.storage.transaction() as db:
                db.execute("DELETE FROM life_metadata WHERE key=?", (CLASS_CUTOVER,))
            classes = ClassActivities(kernel, academics, legacy)

            def settle_disabled(*, now):
                classes.settle_disabled(now=now)
                if artifacts is not None:
                    deliver(now=now)

            return OptionalGroupBinding(value=classes, settle=settle_disabled)

        attendance = ClassAttendance(academics, config=(AttendanceConfig(enabled=True) if attendance_enabled
                                                         else AttendanceConfig(enabled=True, base_rate=0)))
        with context.storage.transaction() as db:
            db.execute("INSERT OR IGNORE INTO life_metadata VALUES (?, ?)", (CLASS_CUTOVER, wire(runtime.wall_clock())))
            since = timestamp(db.execute("SELECT value FROM life_metadata WHERE key=?", (CLASS_CUTOVER,)).fetchone()[0])

        classes = ClassActivities(kernel, academics)
        access = AcademicsAccess(context.access, academics, kernel.registry, legacy, reserve_classes=True)
        kernel.access = access

        def update_campus():
            travel = runtime._optional.groups.get("shared_travel")
            access.campus = travel.value.campus if travel else legacy
            classes.campus = access.routing_campus = (access.campus if travel and not access.campus.settle_only else None)

        def settle(*, now):
            update_campus()
            classes.hosted_settle(now=now, attendance=attendance, since=since)

        scheduler = runtime.scheduler
        scheduler.hold_night_sleep = classes.hold_night_sleep

        expose_academics = runtime.academics is None
        if expose_academics:
            runtime.academics = academics

        def close():
            scheduler.hold_night_sleep = None
            if expose_academics:
                runtime.academics = None

        update_campus()
        return OptionalGroupBinding(value=classes, settle=settle, close=close,
            commitments=lambda *, now: classes.hosted_commitments(now=now, attendance=attendance,
                                                                   shadow=runtime.state_model),
            deliver=deliver if artifacts is not None else None)

    runtime.register_optional_group("academics", bind_academics, phase="pre_kernel", enabled=enabled,
                                    has_history=_has_class_history)
    runtime.register_optional_group("classes", bind_classes, requires=("academics",), enabled=enabled,
                                    has_history=_has_class_history)


__all__ = ("Academics", "AcademicsConfig", "AcademicsAccess", "AttendanceConfig", "ClassAttendance",
           "Campus", "ConfirmedGraphCampus", "ClassActivities", "register_academics", "register_class_group")
