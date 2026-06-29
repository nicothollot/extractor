"""FieldHit confidence model (D4): multiplicative components, every factor
tunable in config.extraction.confidence and every component value persisted
on the hit for the audit record.

    confidence = label x parse x page_class x structure x ambiguity

  label       how well the source label matched the accepted spellings:
              exact-normalized -> label_exact; fuzzy quality q in (0,1)
              scales linearly inside [label_fuzzy_floor, label_exact]
  parse       parse_clean when the value parsed verbatim, parse_lenient when
              it needed repair (raw-unit rescale, bare number as years, ...)
  page_class  TEXT pages -> page_class_text (1.0); OCR'd pages ->
              page_class_ocr x the page's mean OCR word confidence
  structure   table cells -> table_factor; prose label:value -> prose_factor
  ambiguity   ambiguity_penalty once, when conflicting candidate values were
              found for the same field (the losers ride along in
              FieldHit.conflicts)
"""

from __future__ import annotations

from pv_extractor.config import ConfidenceConfig
from pv_extractor.models import PageContent


def hit_confidence(
    cfg: ConfidenceConfig,
    *,
    label_quality: float,
    parse_clean: bool,
    page: PageContent | None,
    from_table: bool,
    has_conflicts: bool,
) -> tuple[float, dict[str, float]]:
    """Confidence in [0, 1] plus the component map for the audit record."""
    if label_quality >= 1.0:
        label = cfg.label_exact
    else:
        label = cfg.label_fuzzy_floor + (cfg.label_exact - cfg.label_fuzzy_floor) * max(label_quality, 0.0)

    parse = cfg.parse_clean if parse_clean else cfg.parse_lenient

    if page is not None and page.ocr_engine is not None:
        page_class = cfg.page_class_ocr * (page.ocr_mean_confidence or 0.0)
    else:
        page_class = cfg.page_class_text

    structure = cfg.table_factor if from_table else cfg.prose_factor
    ambiguity = cfg.ambiguity_penalty if has_conflicts else 1.0

    components = {
        "label": round(label, 4),
        "parse": round(parse, 4),
        "page_class": round(page_class, 4),
        "structure": round(structure, 4),
        "ambiguity": round(ambiguity, 4),
    }
    confidence = label * parse * page_class * structure * ambiguity
    return round(min(max(confidence, 0.0), 1.0), 4), components
