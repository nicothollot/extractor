"""Alias table loading and client/deal name resolution (D5a).

aliases.yaml maps canonical names to known operating names / short names /
codenames. Folder names on the share may not equal the canonical spelling
(e.g. '+Digital Edge' vs canonical 'Digital Edge'), so canonicals are linked
to indexed folder names by normalize_text equality, and each folder name's
expansion set is the folder name plus the alias lists of every linked
canonical. Resolution then cascades exact -> normalized -> fuzzy.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from pv_extractor.io_guard import open_read
from pv_extractor.normalize import normalize_text

logger = logging.getLogger(__name__)


class AliasTable(BaseModel):
    """Parsed aliases.yaml: canonical name -> list of aliases."""

    clients: dict[str, list[str]] = Field(default_factory=dict)
    deals: dict[str, list[str]] = Field(default_factory=dict)


def load_aliases(path: str | Path) -> AliasTable:
    """Load aliases.yaml via the read-only guard; a missing file is not an
    error (alias-free matching still works) and yields an empty table."""
    alias_path = Path(path)
    if not alias_path.is_file():
        logger.warning("alias file not found, matching without aliases: %s", alias_path)
        return AliasTable()
    with open_read(alias_path) as fh:
        data = yaml.safe_load(fh) or {}
    return AliasTable.model_validate(data)


def expansions_for(known_name: str, aliases: dict[str, list[str]]) -> list[str]:
    """Expansion set for one known (folder) name: the name itself plus the
    alias list of every canonical linked to it. A canonical links when its
    own normalized form, or any of its aliases' normalized forms, equals the
    known name's normalized form (canonicals may not equal folder names
    exactly)."""
    target = normalize_text(known_name)
    expansions = [known_name]
    for canonical, alias_list in aliases.items():
        linked = normalize_text(canonical) == target or any(
            normalize_text(alias) == target for alias in alias_list
        )
        if linked:
            expansions.extend(alias_list)
    seen: set[str] = set()
    unique: list[str] = []
    for name in expansions:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def resolve_name(
    query: str,
    known_names: list[str],
    aliases: dict[str, list[str]],
    fuzzy_threshold: int,
) -> tuple[str | None, str, float]:
    """Resolve a user-supplied name against indexed folder names.

    Returns (resolved known_name | None, method, ratio) where method is
    'exact' (casefold equality with any expansion), 'normalized'
    (normalize_text equality), 'fuzzy' (best token_set_ratio over all
    expansions >= fuzzy_threshold) or 'none'.
    """
    query_fold = query.strip().casefold()
    query_norm = normalize_text(query)
    expansion_map = {name: expansions_for(name, aliases) for name in known_names}

    for name, expansions in expansion_map.items():
        if any(query_fold == expansion.strip().casefold() for expansion in expansions):
            return name, "exact", 100.0
    for name, expansions in expansion_map.items():
        if any(query_norm == normalize_text(expansion) for expansion in expansions):
            return name, "normalized", 100.0

    best_name: str | None = None
    best_ratio = 0.0
    for name, expansions in expansion_map.items():
        for expansion in expansions:
            ratio = fuzz.token_set_ratio(query_norm, normalize_text(expansion))
            if ratio > best_ratio:
                best_name, best_ratio = name, ratio
    if best_name is not None and best_ratio >= fuzzy_threshold:
        return best_name, "fuzzy", best_ratio
    return None, "none", best_ratio
