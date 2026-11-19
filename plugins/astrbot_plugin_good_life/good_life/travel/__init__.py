"""Registration and local snapshot projections for shared campus/city travel."""
from dataclasses import dataclass, replace
import json

from ..academics.campus import CLASS_STOP_EFFECT, Campus, ConfirmedGraphCampus, SPEC
from ..contracts.models import ActivityStatus
from ..graph_package_storage import GraphPackageStore
from .position import CAMPUS_KEY, LATEST_POSITION, STATE_KEY, VERSION_KEY, Position, SharedPosition
from .walking import CampusWalk, EFFECT, propose_walk


@dataclass(frozen=True)
class SharedTravel:
    campus: ConfirmedGraphCampus
    position: SharedPosition

    @staticmethod
    def project(ctx):
        row = ctx.reader.one(LATEST_POSITION, (ctx.scope.owner_key,))
        return json.loads(row[0]) if row else None

    def project_campus(self, ctx):
        projected = self.project(ctx)
        position = Position(**projected) if projected else None
        place, revision = self.campus._project_location(position)
        return {"place_id": place, "revision": revision} if place else None


def register_shared_travel(registry, *, settings, graph_store, enabled=False, settle_only=False):
    """Register the local modules before kernel construction."""
    if enabled is not True:
        return None
    position = SharedPosition()
    if settle_only:
        campus = ConfirmedGraphCampus(graph_store, settings.get("academics_graph_id"),
                                      position=position, settle_only=True)
    else:
        campus = Campus.from_settings(graph_store, settings, places=(), paths=(), position=position)
    registry.register_module(replace(SPEC, version="2", owns=(*SPEC.owns, STATE_KEY),
                                     consumes=(*SPEC.consumes, EFFECT, CLASS_STOP_EFFECT)), campus)
    registry.register_executor(CampusWalk(settle_only=settle_only))
    return SharedTravel(campus, position)


def has_position_history(reader):
    return reader.one("SELECT 1 FROM life_metadata WHERE key=?", (VERSION_KEY,)) is not None


def register_shared_travel_group(runtime, settings):
    """Attach the default-off position component to the W6 host lease."""
    from ..runtime import OptionalGroupBinding

    def bind(context):
        settle_only = context.mode == "settle_only"
        travel = register_shared_travel(context.registry, settings=settings,
            graph_store=GraphPackageStore(context.storage), enabled=True, settle_only=settle_only)

        def settle(*, now):
            kernel = context.runtime.kernel
            for scope in context.access.scopes():
                if settle_only:
                    _settle_walks(kernel, scope, now)
                elif context.access.authorize(scope, "kernel.recover"):
                    position = travel.position.read(scope.owner_key)
                    # A committed interruption leaves the stop unknown until confirmed.
                    if position.revision == 0 or (position.status == "unknown" and position.route_id is None
                                                  and travel.campus.home_place_id):
                        with context.storage._lock, context.access.write_guard(scope, "kernel.recover"):
                            travel.campus.initialize_position(scope.owner_key)

        return OptionalGroupBinding(value=travel, settle=settle, capabilities=("campus",),
                                    projections={STATE_KEY: travel.project, CAMPUS_KEY: travel.project_campus})

    runtime.register_optional_group("shared_travel", bind, phase="pre_kernel",
                                    enabled=settings.get("campus_enabled") is True, has_history=has_position_history)


def _settle_walks(kernel, scope, now):
    if not kernel.access.authorize(scope, "kernel.cancel"):
        return
    for item in kernel.repo.active(scope.owner_key):
        if (item.context_key == scope.context_key and item.template_id.startswith("campus_walk_")
                and item.status != ActivityStatus.UNKNOWN
                and kernel.repo.pending_result(scope.owner_key, item.instance_id) is None):
            kernel.cancel(scope, item.instance_id, expected_revision=item.revision,
                          command_id=f"disabled-walk-{item.instance_id}-{item.revision}", now=now)


__all__ = ("Position", "SharedPosition", "SharedTravel", "propose_walk", "register_shared_travel",
           "register_shared_travel_group")
