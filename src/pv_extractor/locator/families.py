"""Version-family grouping among near-duplicate candidates (D5e).

A 'family' is a set of files that are versions/copies of the same document:
same (client, deal, as-of period), and decoration-stripped stems
(normalize.family_stem) that are identical or rapidfuzz.ratio-similar above
the configured threshold. The as-of date is part of the bucket because memos
for adjacent periods differ only in date digits ('... 11.30.24' vs
'... 11.30.25' is ratio 96.7) and documents from different period folders
are never versions of one another. Resolution later compares family HEADS
only, so a final version beating its own v2 never triggers AMBIGUOUS.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from rapidfuzz import fuzz

from pv_extractor.models import CandidateFile
from pv_extractor.normalize import family_stem


def _family_order_key(cand: CandidateFile) -> tuple:
    """Best-first within a family: version rank DESC, version_number DESC
    (None last), copy_number DESC (None last), final_score DESC,
    modified_time DESC (None last)."""
    signal = cand.record.version_signal
    rank = signal.rank if signal else 0
    version_number = signal.version_number if signal else None
    copy_number = signal.copy_number if signal else None
    modified = cand.record.modified_time
    return (
        -rank,
        version_number is None,
        -(version_number or 0),
        copy_number is None,
        -(copy_number or 0),
        -cand.breakdown.final_score,
        modified is None,
        -(modified.timestamp() if modified else 0.0),
    )


def group_into_families(
    cands: list[CandidateFile], ratio_threshold: int
) -> list[list[CandidateFile]]:
    """Partition candidates into version families and rank within each.

    Mutates each candidate's family_key (head's family_stem) and family_rank
    (0 = head). Returns families ordered by head final_score DESC, each
    family ordered head-first.
    """
    stems = [family_stem(cand.record.file_name) for cand in cands]
    parent = list(range(len(cands)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    buckets: dict[tuple[str | None, str | None, date | None], list[int]] = defaultdict(list)
    for idx, cand in enumerate(cands):
        buckets[(cand.record.client, cand.record.deal, cand.record.as_of_date)].append(idx)

    for indices in buckets.values():
        for pos, i in enumerate(indices):
            for j in indices[pos + 1 :]:
                if stems[i] == stems[j] or fuzz.ratio(stems[i], stems[j]) >= ratio_threshold:
                    union(i, j)

    groups: dict[int, list[CandidateFile]] = defaultdict(list)
    for idx, cand in enumerate(cands):
        groups[find(idx)].append(cand)

    families: list[list[CandidateFile]] = []
    for members in groups.values():
        members.sort(key=_family_order_key)
        head_stem = family_stem(members[0].record.file_name)
        for rank, member in enumerate(members):
            member.family_key = head_stem
            member.family_rank = rank
        families.append(members)
    families.sort(key=lambda fam: -fam[0].breakdown.final_score)
    return families
