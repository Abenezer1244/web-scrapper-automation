"""The application half of the suppression activation guard (design section 8).

A suppression rule may become active only once EVERY enforcement layer in design
section 6 has shipped (authorization service, read paths, durable send
authorization, write-path gates, enforcer sweep, erasure). Until then rules can be
recorded but stay pending. Three locks, all of which must open:

  1. The database. Migration 125's ck_privacy_suppressions_activation_guard allows
     state 'pending' only; P7 replaces it in the same release that raises (2).
  2. The code. IMPLEMENTED_ENFORCEMENT_VERSION says which layers this codebase
     actually has. Only the change that ships the last layer may raise it.
  3. The operator. settings.PRIVACY_ENFORCEMENT_VERSION, raised deliberately per
     environment once (1) and (2) are deployed there.

Requiring (2) and (3) together means an environment variable alone can never
enable activation ahead of the code that enforces it.
"""
from __future__ import annotations

from src.config import settings

# Every section-6 layer shipped (P3 through P6 of the design's order of work).
REQUIRED_ENFORCEMENT_VERSION = 1
# What this codebase implements today: none of the layers (Phase 6 groundwork).
IMPLEMENTED_ENFORCEMENT_VERSION = 0


class ActivationNotAllowedError(RuntimeError):
    """Raised when a caller tries to activate a suppression rule too early."""


def activation_allowed() -> bool:
    return (IMPLEMENTED_ENFORCEMENT_VERSION >= REQUIRED_ENFORCEMENT_VERSION
            and settings.PRIVACY_ENFORCEMENT_VERSION >= REQUIRED_ENFORCEMENT_VERSION)


def assert_activation_allowed() -> None:
    """The check every activation path must make before writing an active rule."""
    if not activation_allowed():
        raise ActivationNotAllowedError(
            "privacy suppression rules cannot be activated yet: the code implements "
            f"enforcement version {IMPLEMENTED_ENFORCEMENT_VERSION}, the setting is "
            f"{settings.PRIVACY_ENFORCEMENT_VERSION}, and both must reach "
            f"{REQUIRED_ENFORCEMENT_VERSION} (design section 8)."
        )
