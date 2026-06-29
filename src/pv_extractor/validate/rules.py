"""Table-driven cross-field rules (D5), loaded from rules.yaml.

Each rule row names a `type` the small interpreter below implements; field
references are schema headers, verbatim. Missing inputs mean the rule simply
does not fire — absence is handled by required-field/QA logic, not here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from pv_extractor.io_guard import open_read
from pv_extractor.models import FieldHit, FlagSeverity, ReviewFlag

_EQUATION_RE = re.compile(r"^\s*(.+?)\s*([+-])\s*(.+?)\s*=\s*(.+?)\s*$")
_CODE_RE = re.compile(r"[^a-z0-9_]+")


class RuleSet:
    def __init__(self, rules: list[dict], ranges: dict[str, dict[str, float]]) -> None:
        self.rules = rules
        self.ranges = ranges


def load_rules(path: str | Path) -> RuleSet:
    rules_path = Path(path)
    if not rules_path.is_file():
        return RuleSet([], {})
    with open_read(rules_path) as fh:
        doc = yaml.safe_load(fh) or {}
    return RuleSet(doc.get("rules", []), doc.get("ranges", {}))


def _numeric(values: dict[str, object], header: str) -> float | None:
    value = values.get(header)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _make_flag(rule: dict, description: str) -> ReviewFlag:
    name = str(rule.get("name") or rule.get("type") or "rule").strip().lower().replace("-", "_")
    code = _CODE_RE.sub("_", name).strip("_") or "rule"
    return ReviewFlag(
        category="cross_field",
        description=f"{rule['name']}: {description}",
        severity=FlagSeverity(rule.get("severity", "warning")),
        reviewer_attention=bool(rule.get("reviewer_attention", False)),
        field=rule.get("field"),
        origin="validation",
        code=code,
    )


def run_rules(
    hits: list[FieldHit],
    ruleset: RuleSet,
    routing_table: dict[str, list[str]],
) -> list[ReviewFlag]:
    values: dict[str, object] = {hit.field: hit.value for hit in hits}
    bands_populated = {hit.band for hit in hits if hit.value is not None}
    flags: list[ReviewFlag] = []

    for rule in ruleset.rules:
        rule_type = rule.get("type")
        if rule_type == "weights_sum":
            present = [_numeric(values, f) for f in rule["fields"]]
            present = [v for v in present if v is not None]
            if not present:
                continue
            total = sum(present)
            tolerance = float(rule.get("tolerance", 1.0))
            target = float(rule.get("target", 100.0))
            if abs(total - target) > tolerance:
                flags.append(
                    _make_flag(rule, f"method weights sum to {total:g}, expected {target:g} ± {tolerance:g}")
                )
        elif rule_type == "linear_equation":
            m = _EQUATION_RE.match(rule["expression"])
            if m is None:
                continue
            a, op, b, c = m.group(1), m.group(2), m.group(3), m.group(4)
            va, vb, vc = _numeric(values, a), _numeric(values, b), _numeric(values, c)
            if va is None or vb is None or vc is None:
                continue
            lhs = va + vb if op == "+" else va - vb
            tolerance = float(rule.get("relative_tolerance", 0.02)) * max(abs(vc), 1.0)
            if abs(lhs - vc) > tolerance:
                flags.append(
                    _make_flag(rule, f"{a} {op} {b} = {lhs:g} but {c} = {vc:g} (gap {abs(lhs - vc):g})")
                )
        elif rule_type == "ratio":
            num = _numeric(values, rule["numerator"])
            den = _numeric(values, rule["denominator"])
            result = _numeric(values, rule["result"])
            if num is None or den in (None, 0.0) or result is None:
                continue
            expected = num / den * float(rule.get("scale", 1.0))
            tolerance = float(rule.get("relative_tolerance", 0.02)) * max(abs(expected), 1.0)
            if abs(result - expected) > tolerance:
                flags.append(
                    _make_flag(
                        rule,
                        f"{rule['result']} = {result:g} but {rule['numerator']}/{rule['denominator']} "
                        f"implies {expected:g}",
                    )
                )
        elif rule_type == "min_value":
            value = _numeric(values, rule["field"])
            if value is None:
                continue
            bound = float(rule.get("min", 0.0))
            bad = value <= bound if rule.get("exclusive", False) else value < bound
            if bad:
                flags.append(_make_flag(rule, f"{rule['field']} = {value:g} violates min {bound:g}"))
        elif rule_type == "flag_when_false":
            value = values.get(rule["field"])
            if value is False:
                flags.append(_make_flag(rule, rule.get("description", f"{rule['field']} is False")))
        elif rule_type == "routing_consistency":
            flags.extend(_routing_consistency(rule, values, bands_populated, routing_table))

    return flags


def _routing_consistency(
    rule: dict,
    values: dict[str, object],
    bands_populated: set[str],
    routing_table: dict[str, list[str]],
) -> list[ReviewFlag]:
    """A populated methodology band must be routed by the Primary/Secondary
    (or Tertiary) Methodology; an un-routed populated band is a flag."""
    methodologies = [
        str(values[h])
        for h in ("Primary Methodology", "Secondary Methodology", "Tertiary Methodology")
        if values.get(h)
    ]
    if not methodologies:
        return []
    allowed: set[str] = set()
    for methodology in methodologies:
        allowed.update(routing_table.get(methodology, []))
    # Only methodology-exclusive bands are gated; universal bands listed in
    # the routing table as data locations (RETURNS, CALIBRATION) are exempt.
    routed_universe = {
        band
        for bands in routing_table.values()
        for band in bands
        if band.startswith("METHODOLOGY:") or "COMPS" in band
    }
    flags = []
    for band in sorted(bands_populated & routed_universe):
        if band not in allowed:
            flags.append(
                _make_flag(
                    rule,
                    f"band {band!r} is populated but not routed by methodologies {methodologies}",
                )
            )
    return flags
