"""Local component registration; domain owns and version bindings are unique."""
from dataclasses import dataclass
from typing import Protocol

from ..contracts.models import ActivityTemplate, ReasonCode
from .codec import integer, name, owned, require


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    version: str
    owns: tuple[str, ...]
    reads: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()


class LocalExecutor(Protocol):
    """A1 executors are pure rule calculations, not platform/model adapters.

    prepare 只能依赖 template 与 now；外部依赖失败须抛非 INVALID_CONFIG 码。
    prepare/start/advance/cancel/resume may not perform I/O. Future external
    executors need durable host operation inspection, not a replay_safe flag.
    """
    name: str
    version: str
    local_only: bool
    pausable_phases: tuple[str, ...]

    def validate(self, template): ...
    def prepare(self, template, now): ...
    def start(self, instance, now): ...
    def advance(self, instance, now, audience, *, elapsed_delta_s): ...
    def cancel(self, instance, now, audience, *, elapsed_delta_s): ...
    def resume(self, instance, now): ...
    def validate_result(self, instance, result): ...


class ComponentRegistry:
    # Publish denials before exposing their corresponding registrations.
    _REGISTRATIONS = ("settlement_only", "_settlement_only_executors", "modules", "owners", "executors", "templates")

    def __init__(self):
        self.modules, self.owners, self.executors, self.templates = {}, {}, {}, {}
        self.settlement_only = set()
        self._settlement_only_executors = set()
        self._register_settlement_only = False

    def _stage_group(self, *, settlement_only):
        staged = ComponentRegistry()
        for attr in self._REGISTRATIONS:
            setattr(staged, attr, getattr(self, attr).copy())
        staged._register_settlement_only = settlement_only
        return staged

    def _publish_group(self, staged):
        for attr in self._REGISTRATIONS:
            getattr(self, attr).update(getattr(staged, attr))

    def register_module(self, spec, module):
        require(isinstance(spec, ModuleSpec), "module declaration required")
        name(spec.name)
        name(spec.version)
        require(spec.name not in self.modules, "duplicate module")
        for values in (spec.owns, spec.reads, spec.emits, spec.consumes, spec.requires):
            require(type(values) is tuple and len(set(values)) == len(values), "module declarations must be unique tuples")
            for value in values:
                name(value)
        require(not set(spec.owns).intersection(self.owners), "state key already has an owner")
        require(callable(getattr(module, "apply_effect", None)), "domain effect owner required")
        self.modules[spec.name] = (spec, module)
        self.owners.update({key: spec.name for key in spec.owns})

    def register_executor(self, executor):
        key = name(executor.name) + "@" + name(executor.version)
        require(key not in self.executors, "duplicate executor version")
        require(executor.local_only is True, "A1 accepts only pure local executors")
        require(all(callable(getattr(executor, method, None)) for method in
                    ("validate", "prepare", "start", "advance", "cancel", "resume", "validate_result")), "incomplete executor")
        if self._register_settlement_only:
            self._settlement_only_executors.add(key)
        self.executors[key] = executor

    def register_template(self, template):
        require(isinstance(template, ActivityTemplate), "activity template required")
        template = owned(template)
        name(template.id)
        integer(template.version, 1)
        require(template.schema_version == 1, "unsupported template schema")
        key = (template.id, template.version)
        require(key not in self.templates, "duplicate template version")
        require(template.executor in self.executors, "executor is not registered", ReasonCode.MISSING_DEPENDENCY)
        require(len(set(template.requires)) == len(template.requires), "duplicate dependency")
        for dependency in template.requires:
            name(dependency)
        # A1 implements explicit stable intent opportunities, not random/window
        # schedulers. Unsupported mechanisms fail closed instead of half-loading.
        trigger_ids = []
        for trigger in template.triggers:
            require(set(trigger) == {"id", "type"} and trigger["type"] == "intent", "unsupported A1 trigger")
            trigger_ids.append(name(trigger["id"]))
        require(trigger_ids and len(set(trigger_ids)) == len(trigger_ids), "invalid trigger identities")
        require(set(template.gating) <= {"free_s", "resources"}, "unsupported A1 gate")
        integer(template.gating.get("free_s", 0))
        resources = template.gating.get("resources", {})
        require(hasattr(resources, "items"), "resources must be an object")
        for resource, amount in resources.items():
            name(resource)
            integer(amount)
        require(not template.inputs, "input providers are not available in A1", ReasonCode.MISSING_DEPENDENCY)
        require(template.frequency is None or set(template.frequency) == {"cooldown_s"}, "unsupported frequency")
        if template.frequency is not None:
            integer(template.frequency["cooldown_s"])
        require(template.model_policy is None, "A1 cannot run model tasks")
        self.executors[template.executor].validate(template)
        if self._register_settlement_only:
            self.settlement_only.add(key)
        self.templates[key] = template
        return template

    def template(self, ref):
        template = self.templates.get((ref.id, ref.version))
        require(template is not None, "template version unavailable", ReasonCode.MISSING_DEPENDENCY)
        return template

    def is_settlement_only(self, template_id, template_version):
        key = (template_id, template_version)
        template = self.templates.get(key)
        return key in self.settlement_only if template is None else self._is_template_settlement_only(template)

    def _is_template_settlement_only(self, template):
        # Persisted dynamic templates may never be registered on this lease.
        return ((template.id, template.version) in self.settlement_only
                or template.executor in self._settlement_only_executors)

    def creation_template(self, ref):
        template = self.template(ref)
        require(not self.is_settlement_only(template.id, template.version), "capability_disabled", ReasonCode.MISSING_DEPENDENCY)
        require(getattr(self.executor(template), "accepts_new_instances", True) is True,
                "executor version is recovery-only", ReasonCode.MISSING_DEPENDENCY)
        return template

    def executor(self, template):
        executor = self.executors.get(template.executor)
        require(executor is not None, "pinned executor version unavailable", ReasonCode.MISSING_DEPENDENCY)
        return executor

    def unavailable(self, template, capabilities):
        missing = set(template.requires) - set(capabilities)
        for spec, _module in self.modules.values():
            if spec.name in template.requires:
                missing.update(set(spec.requires) - set(capabilities))
        return tuple(sorted(missing))
