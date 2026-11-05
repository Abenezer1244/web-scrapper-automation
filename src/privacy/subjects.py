"""Who a lead row names: structured subjects for privacy matching (design section 5).

A suppression rule must match the same person wherever a record names them, and
must never degrade into "any row sharing two name tokens" (Codex design review).
Recorder cells do not say which word is the surname, so this module does NOT
guess: it reads a cell only in the word order its SOURCE declares
(``src.utils.lead_export.name_order_for``), with the same rules the CSV export
uses for First/Last (``src.utils.lead_formatting``). Rules learnt from production
cells there (entity markers, 'WSDOT R/E SERVICES', 'LUKINS & ANNIS', 'LE MAI H',
estate and role tails) therefore hold here too, and the two can never disagree.

A cell becomes a list of Subjects, one per party and co-owner:
  * ``person``: exactly ONE (surname, given) reading in the declared order;
  * ``entity``: an organization, typed trust / government / company / other;
  * ``unresolved``: anything that cannot be read without guessing (order unknown,
    entity-or-person, a role word, initials only, a surname only shared from a
    co-owner, a non-Latin script). It yields NO key values, so it can never match
    automatically; the matcher sends it to operator review instead (section 6).

A party whose co-owners are all unreadable ('LUKINS & ANNIS') stays ONE unresolved
subject, never fragments. A party mixing a person and an entity ('SMITH JOHN &
WELLS FARGO BANK NA') is split, so the person is never hidden inside the entity.

``capacity`` records an estate ("ESTATE OF ...", "... EST OF") or a fiduciary
("... TTEE", "... PER REP") without deciding anything: whether a decedent's or a
trustee's request suppresses the record is an owner/counsel decision (section 11,
D4). A co-owner whose surname is only shared from the cell ("SMITH JOHN & MARY",
"JOHN AND JANE SMITH") is unresolved (SURNAME_SHARED) with that reading attached
as a suggestion for the reviewer, never keyed automatically (Codex).

NORMALIZATION_VERSION is stored on every rule. Change any rule here (or the
export rules reused from lead_formatting) and it must change, because rules made
under one normalisation must not be compared with values produced by another.
``reused_rules_fingerprint`` hashes those reused rules, and a test pins it beside
the version, so a formatter change cannot slip past unnoticed.

Pure: no I/O. Nothing here is wired into scraping, delivery or enrichment.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import unicodedata
from dataclasses import dataclass

import src.utils.lead_formatting as _lead_formatting

# The export's own name rules, reused on purpose (see the module docstring).
from src.utils.lead_formatting import (
    _ALIAS_TAIL_RE,
    _CAPACITY_RE,
    _CARE_OF_RE,
    _COOWNER_JOIN_RE,
    _ENTITY_TOKENS,
    _HONORIFIC_RE,
    _NAME_SUFFIXES,
    _NAME_TOKEN_RE,
    _ORG_WORDS,
    _PARTY_SEP_RE,
    _ROLE_TAIL_RE,
    _SPACED_ENTITY_RE,
    _TRAILING_ESTATE_RE,
    _VESTING_TAIL_RE,
    NAME_ORDER_COMMA_ONLY,
    NAME_ORDER_NATURAL,
    NAME_ORDER_RECORDER,
    _first_person_tokens,
    _is_entity,
    _looks_like_organization,
    normalize_phone_for_dialer,
)

NORMALIZATION_VERSION = "n2"

ROLES = frozenset({"party", "heir", "owner_of_record", "grantor", "contact"})
NAME_ORDERS = frozenset({NAME_ORDER_RECORDER, NAME_ORDER_NATURAL, NAME_ORDER_COMMA_ONLY})

KIND_PERSON = "person"
KIND_ENTITY = "entity"
KIND_UNRESOLVED = "unresolved"

# Review reasons for an unresolved subject.
NAME_ORDER_UNKNOWN = "name_order_unknown"
ENTITY_OR_PERSON = "entity_or_person"
ROLE_OR_CAPACITY = "role_or_capacity"
GIVEN_NAME_MISSING = "given_name_missing"
UNREADABLE_IN_ORDER = "unreadable_in_declared_order"
SURNAME_SHARED = "surname_shared"
NON_LATIN_SCRIPT = "non_latin_script"

# Public bodies the export blanks only as a capacity word ('KING COUNTY'); here they
# are entity evidence. Words that are also surnames (PORT, KING) are left out.
_GOVERNMENT_WORDS = frozenset({
    "COUNTY", "CITY", "STATE", "STATES", "DEPT", "DEPARTMENT", "AUTHORITY", "DISTRICT",
    "PUBLIC", "FEDERAL", "COMMISSION", "MUNICIPALITY", "GOVERNMENT", "TREASURER",
    "WSDOT", "DNR", "USA", "TRANSIT",
})
_TRUST_WORDS = frozenset({
    "TRUST", "TR", "TRUSTS", "REVOCABLE", "REVOCABL", "IRREVOCABLE", "LIVING",
    "TESTAMENTARY", "TESTMENTARY", "DECEDENTS",
})
_COMPANY_WORDS = frozenset({
    "LLC", "LLP", "LP", "PLLC", "INC", "CORP", "CO", "COMPANY", "CORPORATION",
    "INCORPORATED", "LIMITED", "LTD", "BANK", "NA", "FSB", "MORTGAGE", "HOLDINGS",
    "PROPERTIES", "INVESTMENTS", "INVESTMENT", "CAPITAL", "GROUP", "PARTNERS",
    "PARTNERSHIP", "ENTERPRISES", "REALTY", "SERVICES", "MANAGEMENT", "VENTURES",
    "SERVICING", "LOAN", "DST", "LLLP",
})
# Entity markers that are also real surnames or given names (CHURCH, Korean NA,
# Spanish AMERICA ...). When one of these is a short cell's ONLY entity evidence
# ('CHURCH JOHN', 'BANK, JOHN', 'NA MIN JUN'), the cell is unresolved, not an entity:
# typing a person as an entity would silently miss that person's rule.
_SURNAME_LIKE_MARKERS = frozenset({
    "CHURCH", "BANK", "NA", "CO", "TR", "STATE", "AMERICA", "CENTER", "LIVING",
})
_WEAK_ENTITY_MAX_WORDS = 3  # counted without suffixes and initials ('CO JOHN SMITH JR')
# A person's name never carries these once estate/heirs text is stripped, so a
# single surname-like marker beside one ('CHURCH OF JESUS CHRIST') stays an entity.
_ORG_FUNCTION_WORDS = frozenset({"OF", "FOR", "THE"})

# Words that make a party a ROLE held by unnamed or unreadable people ('UNKNOWN
# HEIRS OF SMITH JOHN', 'SMITH JOHN AS AGENT'). Checked before the organization
# heuristics, whose OF/FOR rule would otherwise type such a cell as an entity.
_PERSON_ROLE_RE = re.compile(
    r"\b(?:HEIRS?|DEVISEES?|UNKNOWN|AS|DBA|FBO|ATTN|GUARDIAN|CONSERVATOR|RECEIVER|"
    r"HUSBAND|WIFE|SPOUSES?|MARRIED|UNMARRIED|INDIVIDUAL)\b",
    re.IGNORECASE,
)
_HARD_ENTITY_WORDS = _COMPANY_WORDS | _TRUST_WORDS | _GOVERNMENT_WORDS
# 'ET AL' / 'ET UX' / 'ET VIR' add other people; the named person is still named.
_ET_AL_RE = re.compile(r"\s+(?:ET\s*(?:AL|UX|VIR)|ETAL|ETUX)\b\.?", re.IGNORECASE)
# Broader than the export's prefix: probate cells also write 'EST OF JANE DOE'.
_ESTATE_PREFIX_RE = re.compile(r"^(?:THE\s+)?EST(?:ATE)?\.?\s+OF(?:\s+|$)", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Z]+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Subject:
    role: str                       # where on the record: party, heir, owner_of_record ...
    source_field: str               # provenance: the column the text came from
    name_order: str | None          # the source's declared order the text was read in
    kind: str                       # person | entity | unresolved
    text: str                       # this party's text, normalised (review display)
    surname: str | None = None      # person only, normalised
    given: str | None = None        # person only, normalised first given name
    entity_type: str | None = None  # entity only: trust | government | company | other
    capacity: str | None = None     # estate | fiduciary | None (D4 decides its effect)
    review_reason: str | None = None  # unresolved only
    # A person's surname/given is THE reading; an unresolved SURNAME_SHARED subject
    # carries the shared-surname reading as a reviewer's suggestion, never a key.


def normalize_text(raw: str | None) -> str:
    """Fold diacritics ('José Núñez' == 'JOSE NUNEZ'), casefold, drop punctuation,
    collapse whitespace. The one normalisation every key value goes through."""
    if not raw:
        return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", str(raw))
                   if not unicodedata.combining(c))
    text = unicodedata.normalize("NFKC", text).casefold()
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", text)).strip()


def person_key_value(surname: str | None, given: str | None) -> str | None:
    """The person key value, 'surname|given', shared by both sides of a match: a
    record's parsed subject and a requester's structured name. Only the first
    given name counts (records rarely carry the middle). None if either is blank."""
    s, g = normalize_text(surname), normalize_text(given).split(" ")[0]
    return f"{s}|{g}" if s and g else None


def entity_key_value(name: str | None) -> str | None:
    return normalize_text(name) or None


def phone_key_value(raw: str | None) -> str | None:
    """A US 10-digit number, the same form the dialer export writes, or None."""
    return normalize_phone_for_dialer(raw) or None


def email_key_value(raw: str | None) -> str | None:
    """A trimmed, casefolded address, or None when it is not an address."""
    value = (raw or "").strip().casefold()
    return value if _EMAIL_RE.match(value) else None


def _fold(raw: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", raw)
                   if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.upper())


def _entity_evidence(party: str) -> set[str]:
    words = set(_words(party))
    return words & (_ENTITY_TOKENS | _ORG_WORDS | _GOVERNMENT_WORDS | _TRUST_WORDS
                    | _COMPANY_WORDS)


def _entity_type(party: str) -> str:
    words = set(_words(party))
    if words & _TRUST_WORDS:
        return "trust"
    if words & _GOVERNMENT_WORDS:
        return "government"
    if words & _COMPANY_WORDS or _SPACED_ENTITY_RE.search(party.upper()):
        return "company"
    return "other"


def _name_words(part: str) -> list[str]:
    """Words of a co-owner part that could be a name: no suffixes, no initials."""
    return [t for t in _NAME_TOKEN_RE.findall(part)
            if t.upper() not in _NAME_SUFFIXES and len(t) > 1]


def _is_weak_entity(text: str) -> bool:
    """Is the only entity evidence one surname-like word in an otherwise
    person-shaped name ('CHURCH JOHN', 'CO JOHN SMITH JR')?"""
    if any(ch.isdigit() for ch in text) or _SPACED_ENTITY_RE.search(text.upper()):
        return False
    if set(_words(text)) & _ORG_FUNCTION_WORDS:
        return False
    evidence = _entity_evidence(text)
    return (len(_name_words(text)) <= _WEAK_ENTITY_MAX_WORDS and len(evidence) == 1
            and evidence <= _SURNAME_LIKE_MARKERS)


def _entity_strength(text: str, cell_len: int) -> str | None:
    """'strong' (an entity), 'weak' (entity or person) or None (not an entity)."""
    if not (_is_entity(text) or _looks_like_organization(text, cell_len)
            or set(_words(text)) & _GOVERNMENT_WORDS):
        return None
    return "weak" if _is_weak_entity(text) else "strong"


def _party_subjects(party: str, raw_len: int, base: dict) -> list[Subject]:
    """Subjects for ONE party (a ' / ' or ';' segment) of a cell."""
    def unresolved(reason: str, text: str | None = None, **extra) -> Subject:
        # Reads `party` at call time: the cleaned text, not the cell as it arrived.
        return Subject(**base, kind=KIND_UNRESOLVED,
                       text=normalize_text(party if text is None else text),
                       review_reason=reason, **extra)

    capacity = None
    as_given = party
    party = _ALIAS_TAIL_RE.sub("", _CARE_OF_RE.sub("", party)).strip()
    if _ESTATE_PREFIX_RE.match(party) or _TRAILING_ESTATE_RE.search(party):
        capacity = "estate"
    elif _ROLE_TAIL_RE.search(party):
        capacity = "fiduciary"
    party = _TRAILING_ESTATE_RE.sub("", party).strip()
    party = _TRAILING_ESTATE_RE.sub("", _ROLE_TAIL_RE.sub("", party)).strip()
    party = _ET_AL_RE.sub("", party).strip()
    has_prefix = bool(_ESTATE_PREFIX_RE.match(party))
    party = _ESTATE_PREFIX_RE.sub("", party).strip()
    if not _words(party):
        # Nothing left once markers are stripped ('ESTATE OF'): still a review
        # item, never silently dropped (Codex).
        return [unresolved(UNREADABLE_IN_ORDER, as_given, capacity=capacity)]

    hard_entity = (bool(set(_words(party)) & _HARD_ENTITY_WORDS)
                   or bool(_SPACED_ENTITY_RE.search(party.upper())))
    if _PERSON_ROLE_RE.search(party) and not hard_entity:
        return [unresolved(ROLE_OR_CAPACITY, capacity=capacity)]
    order = base["name_order"]
    if has_prefix:
        # Same rule as the export: a recorder source writes BOTH orders after
        # 'ESTATE OF' (prod), so only its comma form reads; a mixed source writes
        # natural order after it.
        order = {NAME_ORDER_RECORDER: NAME_ORDER_COMMA_ONLY,
                 NAME_ORDER_COMMA_ONLY: NAME_ORDER_NATURAL}.get(order, order)
    parts = [p.strip() for p in _COOWNER_JOIN_RE.split(party) if p.strip()]

    strength = _entity_strength(party, raw_len)
    if strength and len(parts) > 1:
        # The export blanks a whole cell with an entity in it; here that would hide
        # 'SMITH JOHN' in 'SMITH JOHN & WELLS FARGO BANK NA'. Only a cell whose
        # EVERY part is an entity ('WSDOT R/E SERVICES') stays one entity. Beside a
        # strong entity part, every other part is judged on its own; with no strong
        # part at all, the evidence spans the joiner and the cell goes to review.
        strengths = [_entity_strength(p, raw_len if i == len(parts) - 1 else len(p))
                     for i, p in enumerate(parts)]
        if all(s == "strong" for s in strengths):
            return [Subject(**base, kind=KIND_ENTITY, text=normalize_text(party),
                            entity_type=_entity_type(party))]
        if "strong" not in strengths:
            return [unresolved(ENTITY_OR_PERSON, capacity=capacity)]
        out: list[Subject] = []
        for part, part_strength in zip(parts, strengths, strict=True):
            if part_strength == "strong":
                out.append(Subject(**base, kind=KIND_ENTITY, text=normalize_text(part),
                                   entity_type=_entity_type(part)))
            elif part_strength == "weak":
                out.append(unresolved(ENTITY_OR_PERSON, part, capacity=capacity))
            else:
                out.extend(_guarded_people([part], part, order, base, capacity))
        return out
    if strength == "weak":
        return [unresolved(ENTITY_OR_PERSON, capacity=capacity)]
    if strength == "strong":
        return [Subject(**base, kind=KIND_ENTITY, text=normalize_text(party),
                        entity_type=_entity_type(party))]
    return _guarded_people(parts, party, order, base, capacity)


def _guarded_people(
    parts: list[str], party: str, order: str | None, base: dict, capacity: str | None
) -> list[Subject]:
    """The one way into _read_people: a role, capacity or honorific word anywhere
    in ``party`` ('SMITH JOHN AS AGENT', 'LANGE CARL R MRS') makes it review-only."""
    if (_PERSON_ROLE_RE.search(party) or _CAPACITY_RE.search(party)
            or _HONORIFIC_RE.search(party)):
        return [Subject(**base, kind=KIND_UNRESOLVED, text=normalize_text(party),
                        review_reason=ROLE_OR_CAPACITY, capacity=capacity)]
    return _read_people(parts, party, order, base, capacity)


def _read_people(
    parts: list[str], party: str, order: str | None, base: dict, capacity: str | None
) -> list[Subject]:
    """Persons (or unresolved subjects) for the co-owner ``parts`` of one party."""
    def unresolved(reason: str, text: str, **extra) -> Subject:
        return Subject(**base, kind=KIND_UNRESOLVED, text=normalize_text(text),
                       review_reason=reason, capacity=capacity, **extra)

    if order is None:
        return [unresolved(NAME_ORDER_UNKNOWN, party)]
    readings = [_first_person_tokens(part, order) for part in parts]
    if not any(first and last for first, last in readings):
        # Nothing in the party reads as a person: ONE subject for the reviewer,
        # not fragments ('LUKINS & ANNIS' is a firm, not two people).
        if len(parts) == 1 and readings[0][1]:
            return [unresolved(GIVEN_NAME_MISSING, party)]
        return [unresolved(UNREADABLE_IN_ORDER, party)]

    subjects: list[Subject] = []
    for i, part in enumerate(parts):
        first, last = readings[i]
        if first and last:
            subjects.append(Subject(**base, kind=KIND_PERSON, text=normalize_text(part),
                                    surname=normalize_text(last), given=normalize_text(first),
                                    capacity=capacity))
            continue
        lone = _name_words(part)
        shared = None
        if len(lone) == 1 and "," not in part:
            # A lone given name may share a surname read in the SAME party: the one
            # before it in recorder order ('SMITH JOHN A & MARY B'), the one after it
            # in natural order ('JOHN AND JANE SMITH'). Inferred, so review only.
            if order == NAME_ORDER_RECORDER:
                prior = [r for r in readings[:i] if r[0] and r[1]]
                shared = prior[-1][1] if prior else None
            elif order == NAME_ORDER_NATURAL:
                later = [r for r in readings[i + 1:] if r[0] and r[1]]
                shared = later[0][1] if later else None
        if shared:
            subjects.append(unresolved(SURNAME_SHARED, part, surname=normalize_text(shared),
                                       given=normalize_text(lone[0])))
        elif last and not first:
            subjects.append(unresolved(GIVEN_NAME_MISSING, part))
        else:
            subjects.append(unresolved(UNREADABLE_IN_ORDER, part))
    return subjects


def subjects_from(
    raw: str | None, *, role: str, source_field: str, name_order: str | None
) -> list[Subject]:
    """Every subject one name cell names, in order. Empty text -> [].

    ``name_order`` is the SOURCE's declared order (``name_order_for(record_type,
    county)``); None means unknown, and then no person is ever read.
    """
    if role not in ROLES:
        raise ValueError(f"unknown subject role {role!r}")
    if name_order is not None and name_order not in NAME_ORDERS:
        raise ValueError(f"unknown name order {name_order!r}")
    if not source_field:
        raise ValueError("source_field is required (provenance)")
    if not raw or not str(raw).strip():
        return []
    base = {"role": role, "source_field": source_field, "name_order": name_order}
    folded = _fold(str(raw)).strip()
    if any(c.isalpha() and not c.isascii() for c in folded):
        # CJK, Cyrillic, ...: the export rules read Latin letters only.
        return [Subject(**base, kind=KIND_UNRESOLVED, text=normalize_text(folded),
                        review_reason=NON_LATIN_SCRIPT)]
    text = _VESTING_TAIL_RE.sub("", folded)
    subjects: list[Subject] = []
    for party in _PARTY_SEP_RE.split(text):
        if party.strip():
            subjects.extend(_party_subjects(party.strip(), len(party.strip()), base))
    if not subjects:
        # Non-empty text that left no party (', A MARRIED MAN', ' ; '): only empty
        # input may yield nothing, so this still reaches review.
        subjects.append(Subject(**base, kind=KIND_UNRESOLVED, text=normalize_text(folded),
                                review_reason=UNREADABLE_IN_ORDER))
    return subjects


# Every lead_formatting rule the readings above depend on, directly or through the
# reused functions. reused_rules_fingerprint() hashes them; tests pin the hash.
_REUSED_RULE_NAMES = (
    "_ALIAS_TAIL_RE", "_CAPACITY_RE", "_CARE_OF_RE", "_COOWNER_JOIN_RE", "_ENTITY_TOKENS",
    "_ESTATE_PREFIX_RE", "_EXPORT_PARTICLES", "_FUNCTION_WORDS", "_HISPANIC_SURNAMES",
    "_HONORIFIC_RE", "_NAME_SUFFIXES", "_NAME_TOKEN_RE", "_ORG_WORDS", "_PARTICLE_PAIRS",
    "_PARTY_SEP_RE", "_PHONE_EXT_RE", "_ROLE_TAIL_RE", "_SPACED_ENTITY_RE",
    "_SURNAME_PARTICLES", "_TRAILING_ESTATE_RE", "_TRUNCATED_CELL_LEN", "_VESTING_TAIL_RE",
    "_VIETNAMESE_SURNAMES", "_drop_initial", "_first_person_tokens", "_is_entity",
    "_looks_like_organization", "_recorder_first_last", "normalize_phone_for_dialer",
)


def reused_rules_fingerprint() -> str:
    """SHA-256 over the reused lead_formatting rules (patterns, word sets, function
    source). Pinned in tests next to NORMALIZATION_VERSION."""
    digest = hashlib.sha256()
    for name in _REUSED_RULE_NAMES:
        obj = getattr(_lead_formatting, name)
        if inspect.isfunction(obj):
            data = inspect.getsource(obj)
        elif isinstance(obj, re.Pattern):
            data = f"{obj.pattern}|{obj.flags}"
        elif isinstance(obj, frozenset | set):
            data = repr(sorted(obj, key=repr))
        else:
            data = repr(obj)
        digest.update(f"{name}={data};".encode())
    return digest.hexdigest()


def subject_key_values(subject: Subject) -> list[tuple[str, str]]:
    """(kind, value) pairs to key with ``src.privacy.keys.privacy_hmac``.

    A person yields its one "surname|given" value, an entity its normalised name,
    an unresolved subject nothing (review only).
    """
    if subject.kind == KIND_PERSON:
        value = person_key_value(subject.surname, subject.given)
        return [("person", value)] if value else []
    if subject.kind == KIND_ENTITY:
        value = entity_key_value(subject.text)
        return [("entity", value)] if value else []
    return []
