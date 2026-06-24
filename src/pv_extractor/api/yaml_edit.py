"""Comment-preserving YAML edits for config.yaml and config/models.yaml.

Both files are the user-facing single sources of truth and are densely
commented, so the GUI edits them with ruamel.yaml round-tripping instead
of rewriting them (PyYAML would strip every comment). Every edit is
validated by re-parsing through the typed loaders BEFORE the file is
written; an invalid edit never lands on disk."""

from __future__ import annotations

import io
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from pv_extractor.io_guard import guarded_open_write, open_read

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 120


class YamlEditError(ValueError):
    pass


def _load(path: Path):
    with open_read(path) as fh:
        return _yaml.load(fh.read().decode("utf-8"))


def _dump_text(data) -> str:
    buffer = io.StringIO()
    _yaml.dump(data, buffer)
    return buffer.getvalue()


def set_dotted(data, dotted_path: str, value) -> None:
    """Set a dotted path (e.g. 'llm.auto.retry_model') in a ruamel tree.

    Missing intermediate maps and a missing leaf are CREATED — config keys
    added in a newer release won't yet exist in an older user's config.yaml,
    and the GUI must still be able to set them. This is safe because every
    caller route gates the dotted path against an editable-key whitelist
    first, and update_config_yaml re-validates the whole file through the
    typed Config model before writing, so an invented junk section can never
    land on disk. An existing non-map node in the path is still an error."""
    keys = dotted_path.split(".")
    node = data
    for key in keys[:-1]:
        if key not in node:
            node[key] = CommentedMap()
        elif not hasattr(node[key], "__setitem__"):
            raise YamlEditError(f"{key!r} in {dotted_path!r} is a value, not a section")
        node = node[key]
    node[keys[-1]] = value


def update_config_yaml(path: Path, pv_root: str, updates: dict[str, object]) -> None:
    """Apply dotted-path updates to config.yaml, validate via load_config,
    then write."""
    from pv_extractor.config import Config

    data = _load(path)
    for dotted, value in updates.items():
        set_dotted(data, dotted, value)
    text = _dump_text(data)
    # Validate before writing: a bad edit must never corrupt the file.
    import yaml as pyyaml

    Config.model_validate(pyyaml.safe_load(text) or {})
    with guarded_open_write(path, pv_root) as fh:
        fh.write(text)


def replace_config_yaml(path: Path, pv_root: str, text: str) -> None:
    """Replace config.yaml wholesale (the GUI's advanced raw editor). The
    text is the user's own — comments included — so no ruamel round-trip is
    needed; it is still validated through the typed loader before writing."""
    from pv_extractor.config import Config

    import yaml as pyyaml

    try:
        parsed = pyyaml.safe_load(text)
    except pyyaml.YAMLError as exc:
        raise YamlEditError(f"not valid YAML: {exc}") from exc
    Config.model_validate(parsed or {})
    with guarded_open_write(path, pv_root) as fh:
        fh.write(text)


def update_model_pricing(
    models_path: Path,
    pv_root: str,
    alias: str,
    pricing: dict[str, float],
    last_reviewed: str | None = None,
) -> None:
    """Update one model's pricing_per_mtok block (and optionally the
    menu-level last_reviewed stamp) in config/models.yaml."""
    from pv_extractor.llm.model_registry import ModelMenu

    data = _load(models_path)
    entries = data.get("models") or []
    target = next((e for e in entries if e.get("alias") == alias or e.get("id") == alias), None)
    if target is None:
        raise YamlEditError(f"unknown model alias/id {alias!r} in {models_path}")
    block = target.get("pricing_per_mtok")
    if block is None:
        raise YamlEditError(f"model {alias!r} has no pricing_per_mtok block")
    for key in ("input", "output", "cache_hit", "cache_write_5m", "cache_write_1h"):
        if key in pricing:
            block[key] = float(pricing[key])
    if last_reviewed is not None:
        data["last_reviewed"] = last_reviewed
    text = _dump_text(data)
    import yaml as pyyaml

    ModelMenu.model_validate(pyyaml.safe_load(text) or {})
    with guarded_open_write(models_path, pv_root) as fh:
        fh.write(text)
