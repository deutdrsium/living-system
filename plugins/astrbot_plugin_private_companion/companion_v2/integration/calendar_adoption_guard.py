"""Check a host-owned calendar adoption receipt before binding a provider."""

import json
from threading import get_ident

from companion_v2.execution.models import TaskError


CERTIFICATE_KEY = "life.calendar.adoption.v1"
SCHEMA = "good-life.calendar-adoption-certificate.v1"
HASHES = ("plan_sha256", "identity_sha256", "source_summary_sha256", "adopted_tables_sha256")


def require_verified(runtime, provider):
    """Require the exact certificate written by a future verified cutover."""
    diagnostic = getattr(getattr(runtime, "_read_snapshot_diagnostics", None), "current", None)
    if getattr(runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
        raise TaskError("life_calendar_transaction_active", "calendar adoption needs an independent host snapshot")
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (CERTIFICATE_KEY,))
    try:
        certificate = json.loads(row["value"]) if row is not None else None
        if (type(certificate) is not dict or set(certificate) != {"schema", "source_version", *HASHES}
                or certificate["schema"] != SCHEMA or type(certificate["source_version"]) is not str
                or not certificate["source_version"] or len(certificate["source_version"]) > 256
                or any(type(certificate[name]) is not str or len(certificate[name]) != 64
                       or any(character not in "0123456789abcdef" for character in certificate[name]) for name in HASHES)):
            raise ValueError("invalid calendar adoption receipt")
    except (KeyError, TypeError, ValueError) as error:
        raise TaskError("life_calendar_adoption_unverified", "calendar adoption has not been verified") from error
    try:
        reported = provider.adoption_certificate()
    except Exception as error:
        raise TaskError("life_calendar_adoption_unavailable", "calendar adoption provider is unavailable") from error
    if reported != certificate:
        raise TaskError("life_calendar_adoption_unverified", "calendar adoption receipt does not match the provider")
    return True
