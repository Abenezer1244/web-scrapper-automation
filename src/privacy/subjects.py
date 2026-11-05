"""Who a lead row names: structured subjects for privacy matching (design section 5).

Recorder cells are free text in two orders and many shapes: "SMITH JOHN A",
"Smith, John A.", "John A. Smith Jr.", "SMITH JOHN A & MARY B", "SMITH JOHN /
DOE JANE", "ACME HOLDINGS LLC", "ESTATE OF JOHN SMITH". A suppression rule must
match the same person in any of them, and must NOT degrade into "any row that
shares two name tokens" (Codex design review).

So a cell becomes a list of Subjects:
  * one per name (multi-owner cells split on " / ", ";", "&", "and", "aka");
  * kind "person" or "entity" (company, trust, estate, government, other);
  * a person carries ``name_pairs``: the (surname, given) readings the text allows.
    Without a comma the order is unknown, so both LAST-FIRST and FIRST-LAST
    readings are kept; a rule matches only an exact pair, never loose tokens;
  * an entity carries its normalised name.

A name that cannot yield a surname AND a given name (a lone token, initials only)
gets no pairs: it can never match automatically, only in operator review.

NORMALIZATION_VERSION is stored on every rule. Change any rule here and it must
change, because rules made under one normalisation must not be compared with
values produced by another.

Pure: no I/O. What to do with an estate's decedent, or a trustee, is an
owner/counsel decision (design section 11, D4); this module only classifies.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

NORMALIZATION_VERSION = "n1"

ROLES = frozenset({"party", "heir", "owner_of_record", "grantor"})

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "esq", "md", "phd", "dds"})
# Recording-office noise that is not part of a name.
_NOISE = frozenset({"et", "al", "etal", "ux", "vir", "deceased", "decd", "the"})
_PARTICLES = frozenset({"de", "del", "della", "la", "las", "los", "van", "von", "der",
                        "den", "da", "di", "du", "le", "st", "mac", "bin"})

_ENTITY_TYPES: tuple[tuple[str, frozenset[str]], ...] = (
    ("trust", frozenset({"trust", "trustee", "trustees", "trusts", "ttee", "revocable",
                         "irrevocable"})),
    ("estate", frozenset({"estate", "estates"})),
    ("government", frozenset({"city", "county", "state", "authority", "district",
                              "department", "dept", "municipality", "federal", "usa",
                              "government", "commission", "port"})),
    ("company", frozenset({"llc", "inc", "corp", "corporation", "company", "co", "lp",
                           "llp", "ltd", "pllc", "pc", "bank", "na", "association", "assn",
                           "holdings", "properties", "investments", "investment", "homes",
                           "realty", "group", "partners", "partnership", "enterprises",
                           "ventures", "capital", "mortgage", "financial", "credit",
                           "union", "church", "ministries", "foundation", "society",
                           "fund", "lender", "servicing"})),
)

_SPLIT_RE = re.compile(r"\s*(?:/|;|\n|\band\b|&|\baka\b|\bfka\b|\ba/k/a\b)\s*", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s,]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Subject:
    role: str
    kind: str                                  # "person" | "entity"
    normalized: str                            # the name as normalised text
    name_pairs: frozenset[tuple[str, str]]     # persons: (surname, given) readings
    entity_type: str | None = None             # entities: trust/estate/government/company/other


def normalize_text(raw: str | None) -> str:
    """NFKC, strip diacritics (so 'José Núñez' equals 'JOSE NUNEZ'), casefold, drop
    punctuation other than the comma, collapse whitespace."""
    if not raw:
        return ""
    text = unicodedata.normalize("NFKD", str(raw))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip(" ,")


def _entity_type(tokens: list[str]) -> str | None:
    joined = " " + " ".join(tokens) + " "
    if " estate of " in joined or " est of " in joined:
        return "estate"
    found = set(tokens)
    for etype, words in _ENTITY_TYPES:
        if etype == "estate" and "of" not in found:
            continue  # "estate" alone (a street, a surname) is not an estate entity
        if found & words:
            return etype
    return None


def _name_tokens(tokens: list[str]) -> list[str]:
    """Drop suffixes, recording noise and single-letter initials."""
    return [t for t in tokens if t not in _SUFFIXES and t not in _NOISE and len(t) > 1]


def _surname_from_end(tokens: list[str]) -> tuple[str, list[str]]:
    """FIRST ... LAST reading, keeping particles: 'maria de la cruz' -> 'de la cruz'."""
    i = len(tokens) - 1
    while i - 1 >= 1 and tokens[i - 1] in _PARTICLES:
        i -= 1
    return " ".join(tokens[i:]), tokens[:i]


def _surname_from_start(tokens: list[str]) -> tuple[str, list[str]]:
    """LAST FIRST reading, keeping particles: 'de la cruz maria' -> 'de la cruz'."""
    i = 0
    while i < len(tokens) - 2 and tokens[i] in _PARTICLES:
        i += 1
    return " ".join(tokens[: i + 1]), tokens[i + 1:]


def _person_pairs(text: str) -> frozenset[tuple[str, str]]:
    if "," in text:
        last, _, rest = text.partition(",")
        last_toks = _name_tokens(last.split())
        given_toks = _name_tokens(rest.replace(",", " ").split())
        if last_toks and given_toks:
            return frozenset({(" ".join(last_toks), given_toks[0])})
        return frozenset()
    toks = _name_tokens(text.split())
    if len(toks) < 2:
        return frozenset()
    pairs = set()
    surname, given = _surname_from_start(toks)       # LAST FIRST M
    if given:
        pairs.add((surname, given[0]))
    surname, given = _surname_from_end(toks)         # First M Last
    if given:
        pairs.add((surname, given[0]))
    return frozenset(pairs)


def subjects_from(raw: str | None, role: str) -> list[Subject]:
    """Every subject one name cell names, in order. Empty or unusable text -> []."""
    if role not in ROLES:
        raise ValueError(f"unknown subject role {role!r}")
    if not raw or not str(raw).strip():
        return []
    subjects: list[Subject] = []
    prior_surnames: set[str] = set()
    for part in _SPLIT_RE.split(str(raw)):
        text = normalize_text(part)
        tokens = text.replace(",", " ").split()
        if not tokens or all(t in _NOISE for t in tokens):
            continue
        etype = _entity_type(tokens)
        if etype:
            subjects.append(Subject(role, "entity", " ".join(tokens), frozenset(), etype))
            prior_surnames = set()
            continue
        pairs = set(_person_pairs(text))
        name_toks = _name_tokens(tokens)
        if len(name_toks) == 1 and prior_surnames:
            # "SMITH JOHN & MARY": a lone given name shares the previous surname(s).
            pairs = {(s, name_toks[0]) for s in prior_surnames}
        subjects.append(Subject(role, "person", " ".join(tokens), frozenset(pairs)))
        if pairs:
            prior_surnames = {s for s, _g in pairs}
    return subjects


def subject_key_values(subject: Subject) -> list[tuple[str, str]]:
    """(kind, normalised value) pairs to key with src.privacy.keys.privacy_hmac.

    A person yields one "surname|given" value per reading; an entity its full
    normalised name. A person with no readings yields nothing (review only).
    """
    if subject.kind == "entity":
        return [("entity", subject.normalized)] if subject.normalized else []
    return [("person", f"{s}|{g}") for s, g in sorted(subject.name_pairs)]
