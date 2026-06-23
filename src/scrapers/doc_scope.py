"""Read-only descriptor of what a connector actually collects, per record type.

SHOW (transparency) ONLY. This describes the documents a scraper pulls so the
wizard can display "for King probate you get Death Certificate", etc. It is NOT
the SELECT capability — selectable doc-type *input* lives in `doc_types.py` with
its own canonical tokens. Keep the two apart: SHOW labels are descriptive (and may
be approximate); SELECT tokens are a verified control contract.

Design rule (Codex-reconciled): there is no central catalog duplicating scraper
behavior. Each scraper/template derives its own `CollectionScope` from the SAME
constants it already uses to filter, so what we show cannot drift from what we
scrape. This module only defines the shared shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# "document_type" — a recorder connector that filters by document type (probate,
#   pre_foreclosure, divorce on recorder portals).
# "dataset"       — a connector that pulls from a structured dataset and does NOT
#   filter by recorder document type (tax_delinquent via Socrata, code_violation
#   via ArcGIS). items is empty; note explains the source.
ScopeKind = Literal["document_type", "dataset"]


@dataclass(frozen=True)
class DocTypeItem:
    """One document type a connector collects.

    label  — human-readable name shown to the customer.
    exact  — True when `label` is the portal's exact option text (dropdown/checkbox
             label we verified). False when it is derived from a keyword predicate
             (an allowlist match), which is approximate by nature.
    """

    label: str
    exact: bool = False


@dataclass(frozen=True)
class CollectionScope:
    """What a connector collects for one record type (read-only, for display)."""

    kind: ScopeKind
    items: tuple[DocTypeItem, ...] = ()
    note: str | None = None

    def to_api(self) -> dict:
        """Serialize to the connector-response shape the frontend consumes."""
        return {
            "kind": self.kind,
            "items": [{"label": i.label, "exact": i.exact} for i in self.items],
            "note": self.note,
        }


def document_types(
    labels_exact: list[tuple[str, bool]],
    *,
    note: str | None = None,
) -> CollectionScope:
    """Build a `document_type` scope from (label, exact) pairs, de-duplicating by
    label (case-insensitive) while preserving first-seen order and the strongest
    `exact` flag seen for that label."""
    seen: dict[str, DocTypeItem] = {}
    for label, exact in labels_exact:
        key = label.strip().upper()
        if not key:
            continue
        prior = seen.get(key)
        if prior is None:
            seen[key] = DocTypeItem(label=label.strip(), exact=exact)
        elif exact and not prior.exact:
            # Upgrade an approximate label to exact if a later source confirms it.
            seen[key] = DocTypeItem(label=prior.label, exact=True)
    return CollectionScope(kind="document_type", items=tuple(seen.values()), note=note)


def dataset(note: str) -> CollectionScope:
    """Build a `dataset` scope (no recorder document types; honest source note)."""
    return CollectionScope(kind="dataset", items=(), note=note)
