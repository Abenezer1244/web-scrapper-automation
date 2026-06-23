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


# ─── Keyword → display-label presentation (SHOW) ─────────────────────────────
# Presentation-ONLY mapping from a scraper's internal doc-type keyword to a
# customer-readable label. This does NOT touch filter logic (the scrapers keep
# matching their own keywords); it only decides how a matched keyword is shown.
#
# Honesty rules (Codex-reconciled):
#  - Broad single-word predicates ("DEATH", "TAX") match more than one specific
#    document, so they are labelled "<X>-related filings", never a precise name.
#  - Cryptic per-county abbreviations ("LETTR", "PTREC", "NTS") are NOT shown
#    raw; they fall into an explicit "Other <type> filings (county-specific
#    codes)" bucket. The full-word concept is already present in the same list.
#  - Every keyword must be classified (a label OR the bucket). A coverage test
#    fails when a new, unclassified keyword appears, so display can't silently
#    mislabel. Unknown keywords degrade to the bucket at runtime (never raise).
#  - These items describe the SIGNALS used to identify collected records, not an
#    exact enumeration — `exact=False` and the note say so.

_PROBATE_LABELS: dict[str, str] = {
    "PROBATE": "Probate filings",
    "LETTERS TESTAMENTARY": "Letters Testamentary",
    "TESTAMENTARY": "Letters Testamentary",
    "LETTERS OF ADMINISTRATION": "Letters of Administration",
    "PERSONAL REPRESENTATIVE": "Personal Representative documents",
    "PERSONAL REP": "Personal Representative documents",
    "ADMINISTRATOR": "Administrator / Executor documents",
    "ADMINISTRATRIX": "Administrator / Executor documents",
    "EXECUTOR": "Administrator / Executor documents",
    "EXECUTRIX": "Administrator / Executor documents",
    "DEATH CERTIFICATE": "Death Certificate",
    "CERTIFICATE OF DEATH": "Death Certificate",
    "DEATH": "Death-related filings",
    "WILL": "Wills",
    "AFFIDAVIT OF HEIRSHIP": "Affidavit of Heirship",
    "HEIR": "Heirship-related filings",
    "TRANSFER ON DEATH": "Transfer on Death Deed",
    "ESTATE OF": "Estate filings",
    "ESTATE": "Estate-related filings",
    "DECEDENT": "Decedent-related filings",
    "DECEASED": "Decedent-related filings",
    "DECREE OF DISTRIBUTION": "Decree of Distribution",
    "AFFIDAVIT OF SUCCESSOR": "Affidavit of Successor",
    "INHERITANCE": "Inheritance filings",
    "COMMUNITY PROPERTY AGREEMENT": "Community Property Agreement",
}
# Cryptic per-county abbreviations -> the "other" bucket (concept already shown).
_PROBATE_BUCKET = {"TOD", "AFFD", "PTREC", "LETTR", "EXEC", "SUCC"}

_PREFORECLOSURE_LABELS: dict[str, str] = {
    "LIS PENDENS": "Lis Pendens",
    "NOTICE OF TRUSTEE SALE": "Notice of Trustee Sale",
    "NOTICE OF TRUSTEE": "Notice of Trustee Sale",
    "TRUSTEE SALE": "Notice of Trustee Sale",
    "TRUSTEE'S SALE": "Notice of Trustee Sale",
    "NOTICE OF DEFAULT": "Notice of Default",
    "NOTICE OF FORECLOSURE": "Notice of Foreclosure",
    "FORECLOSURE": "Foreclosure",
    "NOTICE OF INTENT TO FORFEIT": "Notice of Intent to Forfeit",
}
_PREFORECLOSURE_BUCKET = {"NTS", "NOD", "LISP", "NTSCL"}

_TAX_LABELS: dict[str, str] = {
    "TAX LIEN": "Tax Lien",
    "FEDERAL TAX LIEN": "Federal Tax Lien",
    "CERTIFICATE OF DELINQUENCY": "Certificate of Delinquency",
    "CERTIFICATE OF SALE": "Certificate of Sale",
    "TAX DELINQUENT": "Tax Delinquency filings",
    "TREASURER'S DEED": "Treasurer's Deed",
    "TAX": "Tax-related filings",
    "DELINQUENT": "Delinquency-related filings",
    "TREASURER": "Treasurer-related filings",
}
_TAX_BUCKET = {"FTL"}

# record_type -> (label map, bucket set, bucket label)
_PRESENTATION: dict[str, tuple[dict[str, str], set[str], str]] = {
    "probate": (_PROBATE_LABELS, _PROBATE_BUCKET, "Other probate filings (county-specific codes)"),
    "pre_foreclosure": (
        _PREFORECLOSURE_LABELS,
        _PREFORECLOSURE_BUCKET,
        "Other foreclosure-stage filings (county-specific codes)",
    ),
    "tax_delinquent": (_TAX_LABELS, _TAX_BUCKET, "Other tax filings (county-specific codes)"),
}

# Divorce scope is NOT derived from a connector's keyword list: divorce-capable
# scrapers gate on the shared `divorce.is_divorce_doc` classifier, which is wider
# (legal separation, domestic-partnership dissolution) and narrower (rejects bare
# corporate dissolution) than any raw keyword list. These are the classifier's
# real marital positives, shown as signals.
_DIVORCE_SIGNALS: tuple[str, ...] = (
    "Divorce",
    "Dissolution of Marriage",
    "Decree of Dissolution",
    "Legal Separation",
    "Dissolution of Domestic Partnership",
)

_SIGNALS_NOTE = (
    "Document-type signals used to identify collected records; exact wording "
    "varies by county and some signals match a family of related filings."
)
_DIVORCE_NOTE = (
    "Marital divorce / dissolution / legal-separation decrees, identified by the "
    "shared divorce classifier (business/entity dissolutions are excluded)."
)


def classify_keyword(record_type: str, keyword: str) -> str | None:
    """Return the display label for a keyword, the literal sentinel '<bucket>' when
    it belongs in the county-specific-codes bucket, or None when the keyword is not
    classified for this record type (the coverage test asserts this never happens)."""
    maps = _PRESENTATION.get(record_type)
    if maps is None:
        return None
    labels, bucket, _ = maps
    up = keyword.strip().upper()
    if up in labels:
        return labels[up]
    if up in bucket:
        return "<bucket>"
    return None


def divorce_scope() -> CollectionScope:
    """The shared divorce SHOW scope (classifier-derived, not keyword-derived)."""
    return CollectionScope(
        kind="document_type",
        items=tuple(DocTypeItem(label=s, exact=False) for s in _DIVORCE_SIGNALS),
        note=_DIVORCE_NOTE,
    )


def from_keyword_map(
    doc_type_map: dict[str, list[str]],
    record_type: str,
) -> CollectionScope | None:
    """Build a SHOW scope for `record_type` from a template's own keyword map.

    Divorce ignores the map and uses the shared classifier scope. For probate /
    pre_foreclosure / tax_delinquent, each keyword is mapped to a display label or
    the explicit county-specific-codes bucket; unknown keywords degrade to the
    bucket (never raise) so production stays safe while the coverage test catches
    drift. Returns None when the connector does not handle `record_type`.
    """
    if record_type == "divorce":
        return divorce_scope() if record_type in doc_type_map else None
    if record_type not in doc_type_map:
        return None
    maps = _PRESENTATION.get(record_type)
    if maps is None:
        return None
    _, _, bucket_label = maps
    items: list[DocTypeItem] = []
    seen: set[str] = set()
    needs_bucket = False
    for kw in doc_type_map[record_type]:
        label = classify_keyword(record_type, kw)
        if label is None or label == "<bucket>":
            needs_bucket = True
            continue
        key = label.upper()
        if key not in seen:
            seen.add(key)
            items.append(DocTypeItem(label=label, exact=False))
    if needs_bucket:
        items.append(DocTypeItem(label=bucket_label, exact=False))
    if not items:
        return None
    return CollectionScope(kind="document_type", items=tuple(items), note=_SIGNALS_NOTE)
