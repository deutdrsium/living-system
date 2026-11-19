"""Architecture contract v2 batch 1 with unchanged v1 declarations.

models/ports retain v1 record shapes and signatures; v2 holds opt-in additions.
The version is independent of companion.life.v1 and database schema versions.
"""
from .models import CONTRACT_VERSION

__all__ = ("CONTRACT_VERSION",)
