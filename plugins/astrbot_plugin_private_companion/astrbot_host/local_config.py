"""Root-only local configuration transport and transactional undo journal."""

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import uuid
from zoneinfo import ZoneInfoNotFoundError

from companion_v2.configuration import local_journal
from companion_v2.configuration.life_handoff import LifeConfigurationHandoff
from companion_v2.configuration.life_provider import transferred
from companion_v2.configuration.models import ApplyMode, ConfigLayer, ConfigTarget, ConfigurationError, encode
from companion_v2.execution.models import TaskError, require
from companion_v2.foundation.refs import OperationRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal, AuthorizationTier
from companion_v2.role_profile.configuration import CONTROL_FIELDS
from companion_v2.role_work.life.configuration import SLEEP_FIELDS, values as life_values
from companion_v2.role_work.state.configuration import PERMISSIONS as STATE_GRANT_FIELDS


logger = logging.getLogger(__name__)
SOCKET_DIRECTORY = Path("/AstrBot/data/companion-config")
MAX_VALUE = 32768
MAX_REQUEST = 48 * 1024
MAX_RESPONSE = 256 * 1024
GROUPS = {"role.profile", "relationship.profile", "conversation.presentation", "role.life",
          "role.state", "proactive.contact", "wardrobe"}
LAYERS = {ConfigLayer.INSTALLATION, ConfigLayer.ROLE, ConfigLayer.WINDOW, ConfigLayer.RELATIONSHIP}
SECRET_NAME = re.compile(r"provider|key|token|secret|password|passwd|credential|cookie|authorization", re.I)
SECRET_TEXT = re.compile(
    r"(?:authorization|api[ _-]?key|access[ _-]?key|private[ _-]?key|token|secret|password|passwd|cookie|credential)"
    r"[\w-]{0,40}['\"]?\s*[:=]\s*\S|\bbearer\s+\S|://[^:/@\s]+:[^/@\s]+@|"
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|github_pat_[\w]{12,}|"
    r"xox[baprs]-[\w-]{12,}|(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b", re.I)
READ_ONLY = ({"role.profile." + name for name in CONTROL_FIELDS}
             | {"role.state." + name for name in STATE_GRANT_FIELDS}
             | {"role.life.enabled", "role.state.maintenance_grant_scope",
                "role.state.calendar_enabled", "proactive.contact.news_source_url",
                "proactive.contact.news_source_kind", "proactive.contact.news_source_uid"})
FIELD = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
HEX = re.compile(r"[0-9a-f]+")
DECIMAL = re.compile(r"0|[1-9][0-9]{0,18}")


class Refused(ValueError):
    pass


def decode_request(raw):
    data = json.loads(raw.decode("utf-8"))
    if type(data) is not dict or set(data) != {"args", "body"}:
        raise Refused("invalid_command")
    args, body = data["args"], data["body"]
    if (type(args) is not list or not 1 <= len(args) <= 6
            or any(type(arg) is not str or len(arg) > 128 for arg in args)):
        raise Refused("invalid_command")
    action, *params = args
    if action != "set" and body is not None:
        raise Refused("invalid_command")
    if action == "targets" and not params:
        return action, ()
    if action == "history" and len(params) <= 1:
        number = params[0] if params else "20"
        if DECIMAL.fullmatch(number) and 1 <= int(number) <= 100:
            return action, (int(number),)
    if action == "revert" and len(params) == 1 and len(params[0]) == 32 and HEX.fullmatch(params[0]):
        return action, tuple(params)
    lengths = {"get": 2, "set": 5}
    if action in lengths and len(params) == lengths[action]:
        prefix, field, *rest = params
        if len(prefix) <= 64 and HEX.fullmatch(prefix) and FIELD.fullmatch(field):
            if action == "get":
                return action, (prefix, field)
            revision, edit_token, digest = rest
            if (DECIMAL.fullmatch(revision) and int(revision) < 2**63 and len(edit_token) == len(digest) == 64
                    and HEX.fullmatch(edit_token) and HEX.fullmatch(digest) and type(body) is str):
                try:
                    value_bytes = base64.b64decode(body, validate=True)
                except ValueError:
                    raise Refused("invalid_value") from None
                if len(value_bytes) > MAX_VALUE:
                    raise Refused("value_too_large")
                if hashlib.sha256(value_bytes).hexdigest() != digest:
                    raise Refused("digest_mismatch")
                try:
                    value = json.loads(value_bytes.decode("utf-8"))
                    if type(value) is str:
                        value.encode("utf-8")  # reject escaped lone surrogates
                    if type(value) not in (str, bool, int, float) or (type(value) is float and not math.isfinite(value)):
                        raise ValueError
                except (ValueError, RecursionError):
                    raise Refused("invalid_value") from None
                return action, (prefix, field, int(revision), edit_token, value)
    raise Refused("invalid_command")


def redact(value):
    # Withhold the entire string so a multiline or compound credential cannot
    # leak through the unmatched portion. This is a heuristic for free text.
    return "[REDACTED]" if isinstance(value, str) and SECRET_TEXT.search(value) else value


def restore_target(row, installation):
    document = json.loads(row["target_json"])
    if document["installation_id"] != installation:
        raise Refused("target_unavailable")
    layer = ConfigLayer(document["layer"])
    if layer not in LAYERS:
        raise Refused("target_unavailable")
    scope = document["scope"]
    if isinstance(scope, list):
        platform, kind, native_id = scope
        scope = ScopeRef(installation, platform, ScopeKind(kind), native_id)
    elif scope is not None:
        scope = ScopeRef.restore(scope)
    role = SubjectRef(installation, SubjectKind.BOT, document["role_id"]) if document["role_id"] else None
    subject = (SubjectRef(installation, SubjectKind.HUMAN, document["subject_id"])
               if "subject_id" in document else None)
    target = ConfigTarget(installation, layer, role, scope, document.get("bot_id"), subject)
    if target.key != row["target_key"]:
        raise Refused("target_unavailable")
    return target


class LocalConfig:
    def __init__(self, application):
        self.app = application
        self.service = application.configuration
        self.runtime = self.service.runtime
        self.store = self.service.store

    def _operator(self, at):
        # The transport authenticates UID 0. This identity cannot be minted by
        # the Dashboard or identified by data in the IPC request.
        principal = AuthenticatedHostPrincipal(self.runtime.installation_id, "companion-config-local", "companion", "root")
        directory, policy = self.service.identity, self.service.policy
        actor = directory.resolve_authenticated(principal, observed_at=at)
        if actor.failure is not None and actor.failure.code == "unbound_identity":
            with self.runtime.transaction():
                actor = directory.resolve_authenticated(principal, observed_at=at)
                if actor.failure is not None and actor.failure.code == "unbound_identity":
                    subject = SubjectRef(self.runtime.installation_id, SubjectKind.SERVICE, "companion-config-local")
                    directory.bind(binding_id="companion-config-local", principal=principal, subject=subject,
                                   valid_from=at, expected_revision=directory.revision)
                    policy.set_tier(subject, AuthorizationTier.CORE, expected_revision=policy.revision, observed_at=at)
                    actor = directory.resolve_authenticated(principal, observed_at=at)
        return require(actor)

    def _target(self, prefix):
        rows = self.store.read_all("""SELECT target_key, target_json FROM configuration_targets
            WHERE target_key >= ? AND target_key <= ? ORDER BY target_key LIMIT 2""",
            (prefix.ljust(64, "0"), prefix.ljust(64, "f")))
        if len(rows) != 1:
            raise Refused("ambiguous_target" if rows else "target_unavailable")
        return restore_target(rows[0], self.runtime.installation_id)

    def _field(self, name):
        field = self.service.registry.fields.get(name)
        if (field is None or field.group_id not in GROUPS or not name.startswith(field.group_id + ".")
                or SECRET_NAME.search(name)):
            raise Refused("field_not_allowed")
        return field

    def _writable(self, field, target):
        if (field.field_id in READ_ONLY or re.search(r"(?:^|[._])(?:birthday|u?ids?|identifier)(?:$|[._])", field.field_id)
                or field.apply_mode is ApplyMode.RESTART):
            return False
        if field.group_id == "role.life":
            return (target.layer is ConfigLayer.ROLE) == (field.field_id.rsplit(".", 1)[-1] in SLEEP_FIELDS)
        if field.group_id == "proactive.contact":
            return target.layer in (ConfigLayer.ROLE, ConfigLayer.RELATIONSHIP)
        if field.group_id == "conversation.presentation" and target.scope and target.scope.kind is ScopeKind.GROUP:
            # Group resets may delete legacy rows from several addresses.
            return False
        return True

    def _snapshot(self, actor, target, field, at):
        if target.layer not in field.layers:
            raise Refused("field_scope_not_supported")
        return require(self.service.read(actor, target=target, group_id=field.group_id, observed_at=at))

    def targets(self):
        rows = self.store.read_all("SELECT target_key, target_json FROM configuration_targets ORDER BY target_key LIMIT 2049", ())
        if len(rows) > 2048:
            raise Refused("too_many_targets")
        targets = []
        for row in rows:
            try:
                target = restore_target(row, self.runtime.installation_id)
            except (KeyError, TypeError, ValueError):
                continue  # unsupported layers and invalid historical addresses
            targets.append({"target_key": target.key, "layer": target.layer.value,
                            "role_id": redact(target.role.subject_id) if target.role else None,
                            "subject_id": redact(target.subject.subject_id) if target.subject else None})
        return targets

    def get(self, prefix, name):
        field = self._field(name)
        at = self.app.now()
        actor = self._operator(at)
        with self.store.read_snapshot():
            target = self._target(prefix)
            snapshot = self._snapshot(actor, target, field, at)
            value = snapshot.fields[name]
            return {"target_key": target.key, "field_id": name, "revision": snapshot.revision,
                    "edit_token": snapshot.edit_token, "value": redact(value.value),
                    "explicit": value.explicit, "writable": self._writable(field, target)}

    def history(self, count):
        with self.store.read_snapshot() as db:
            if not local_journal.exists(db):
                return []
            return [dict(row) for row in db.execute("""SELECT change_id, target_key, field_id, revision,
                changed_at, reverts FROM lsr_config_undo ORDER BY sequence DESC LIMIT ?""", (count,))]

    def set(self, prefix, name, revision, edit_token, value):
        if edit_token is None:
            raise Refused("invalid_command")
        field = self._field(name)
        value = field.validate(value)
        return self._mutate(prefix, field, revision, value, edit_token=edit_token)

    def revert(self, change_id):
        return self._mutate(None, None, None, None, reverts=change_id)

    def _mutate(self, prefix, field, expected_revision, value, *, edit_token=None, reverts=None):
        at = self.app.now()
        actor = self._operator(at)
        with self.store.read_snapshot() as db:
            if reverts is not None:
                old = (db.execute("SELECT * FROM lsr_config_undo WHERE change_id = ?", (reverts,)).fetchone()
                       if local_journal.exists(db) else None)
                if old is None:
                    raise Refused("change_unavailable")
                prefix, field, expected_revision = old["target_key"], self._field(old["field_id"]), old["revision"]
                value = field.validate(json.loads(old["old_json"])) if old["old_json"] is not None else None
            handoff = self._life_handoff(field)
            if handoff:
                target, snapshot = self._checked_snapshot(prefix, field, actor, at, expected_revision, edit_token, value)
        change_id = uuid.uuid4().hex
        undo = {"change_id": change_id, "reverts": reverts}
        name = field.field_id
        if handoff:
            # The handoff commits and recovers its own durable decision. Its
            # provider reservation must outlive the host revision check.
            receipt = require(LifeConfigurationHandoff(self.service).patch_local(actor,
                **self._patch_request(target, field, snapshot, change_id, value, at),
                expected_revision=expected_revision, check_generation=self.app.tasks.check_generation, **undo))
        else:
            # Dashboard lock order. BEGIN IMMEDIATE fences the revision, patch
            # and journal against every writer to this SQLite database.
            with self.runtime.transaction(), self.store.transaction() as db:
                require(self.app.tasks.check_generation())
                if self._life_handoff(field):
                    raise Refused("configuration_unavailable")
                target, snapshot = self._checked_snapshot(prefix, field, actor, at, expected_revision, edit_token, value)
                previous = db.execute("""SELECT value_json FROM configuration_values
                    WHERE target_key = ? AND field_id = ?""", (target.key, name)).fetchone()
                previous_json = local_journal.old_value(previous[0] if previous else None)
                receipt = require(self.service.patch(actor,
                    **self._patch_request(target, field, snapshot, change_id, value, at)))
                if set(receipt.changed_fields) - {name}:
                    raise Refused("edit_affects_other_fields")
                if receipt.changed_fields:
                    local_journal.append(db, target_key=target.key, field_id=name, old_json=previous_json,
                                         revision=receipt.revision, changed_at=at.isoformat(), **undo)
        return {"change_id": change_id if receipt.changed_fields else None, "target_key": target.key,
                "field_id": name, "revision": receipt.revision, "apply_mode": field.apply_mode.value,
                "reload_required": False}

    def _life_handoff(self, field):
        return field.group_id in {"role.life", "role.state"} and transferred(self.store)

    def _checked_snapshot(self, prefix, field, actor, at, expected_revision, edit_token, value):
        target = self._target(prefix)
        if not self._writable(field, target):
            raise Refused("field_read_only")
        snapshot = self._snapshot(actor, target, field, at)
        if snapshot.revision != expected_revision or (edit_token is not None and snapshot.edit_token != edit_token):
            raise Refused("stale_revision")
        if field.group_id == "role.life" and value is not None:
            preview = require(self.service.preview_changes(actor, target=target, group_id=field.group_id,
                changes={field.field_id: value}, observed_at=at))
            try:
                life_values(replace(snapshot, fields=preview.after))
            except (ValueError, ZoneInfoNotFoundError) as error:
                raise Refused("invalid_configuration_value") from error
        return target, snapshot

    @staticmethod
    def _patch_request(target, field, snapshot, change_id, value, at):
        op_id = "companion-config:" + change_id
        return dict(target=target, group_id=field.group_id,
            operation=OperationRef(op_id, field.owner_id, target.authorization_scope, VersionRef(op_id, 1), op_id),
            expected_edit_token=snapshot.edit_token,
            changes={field.field_id: value} if value is not None else {},
            reset_to_inherit=(field.field_id,) if value is None else (), observed_at=at)


class LocalConfigServer:
    """Opt in by provisioning the fixed root-only directory before plugin startup."""

    def __init__(self, controller, directory=SOCKET_DIRECTORY):
        self.controller = controller
        self.directory = directory
        self.path = directory / "socket"
        self.server = None
        self.lock_fd = None
        self.bound_inode = None
        self.request = None

    async def start(self):
        if not hasattr(socket, "SO_PEERCRED") or os.geteuid() != 0:
            return
        try:
            info = self.directory.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("unsafe local config directory")
        import fcntl

        listener = None
        try:
            self.lock_fd = os.open(self.directory / "listener.lock",
                                   os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            info = os.fstat(self.lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("unsafe local config lock")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                    raise ValueError("unsafe local config socket")
                self.path.unlink()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(os.fspath(self.path))
            info = self.path.lstat()
            self.bound_inode = (info.st_dev, info.st_ino)
            self.path.chmod(0o600)
            self.server = await asyncio.start_unix_server(self._accept, sock=listener, limit=MAX_REQUEST, backlog=4)
        except BaseException:
            if listener is not None:
                listener.close()
            await self.close()
            raise

    def _accept(self, reader, writer):
        peer = writer.get_extra_info("socket")
        try:
            _pid, uid, _gid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        except (OSError, AttributeError):
            uid = -1
        if uid != 0 or self.request is not None or self.server is None:
            writer.close()
            return
        self.request = asyncio.create_task(self._handle(reader, writer))

    async def _handle(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=3)
            response = await asyncio.to_thread(self._dispatch, raw)
            if len(response) > MAX_RESPONSE:
                response = b'{"ok":false,"error":"response_too_large"}\n'
            writer.write(response)
            await asyncio.wait_for(writer.drain(), timeout=3)
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            self.request = None

    def _dispatch(self, raw):
        try:
            if not raw.endswith(b"\n") or len(raw) > MAX_REQUEST:
                raise Refused("invalid_request")
            try:
                action, params = decode_request(raw)
            except (ValueError, RecursionError) as error:
                raise Refused(str(error) if isinstance(error, Refused) else "invalid_request") from None
            session = self.controller.session
            if self.controller._terminated or session is None or session.application.state != "running":
                raise Refused("configuration_unavailable")
            adapter = LocalConfig(session.application)
            result = getattr(adapter, action)(*params)
            if action in {"set", "revert"} and result["change_id"] is not None:
                try:
                    session.application.notify()
                except RuntimeError as error:
                    # The write already committed and a shutting-down loop may
                    # no longer accept wake-ups. Preserve the change receipt.
                    logger.warning("[CompanionV2] companion-config wake failed error_type=%s", type(error).__name__)
            response = {"ok": True, "result": result}
        except Refused as error:
            response = {"ok": False, "error": str(error)}
        except (TaskError, ConfigurationError) as error:
            code = "stale_revision" if error.code == "configuration_revision_conflict" else error.code
            response = {"ok": False, "error": code}
        except Exception as error:
            logger.warning("[CompanionV2] companion-config request failed error_type=%s", type(error).__name__)
            response = {"ok": False, "error": "configuration_unavailable"}
        return encode(response).encode("ascii") + b"\n"

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.request is not None:
            await self.request
        try:
            if self.bound_inode is not None:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.bound_inode:
                    self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.bound_inode = None
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
