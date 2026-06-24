from __future__ import annotations

import pytest

from pv_extractor.config import LlmConfig, load_config
from pv_extractor.llm.escalate import resolve_settings


def test_llm_grouping_defaults_are_not_force_assist():
    config = LlmConfig()
    assert config.combine_deal_documents is False
    assert config.one_call_per_deal is False


def test_legacy_one_call_per_deal_maps_to_combine_with_warning(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
pv_root: /tmp/pv
output_dir: ./output
db_path: ./output/pv.db
llm:
  one_call_per_deal: true
""",
        encoding="utf-8",
    )
    with pytest.warns(DeprecationWarning, match="combine_deal_documents"):
        config = load_config(config_path)
    assert config.llm.one_call_per_deal is True
    assert config.llm.combine_deal_documents is True


def test_new_combine_key_wins_over_legacy_value(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
pv_root: /tmp/pv
output_dir: ./output
db_path: ./output/pv.db
llm:
  combine_deal_documents: false
  one_call_per_deal: true
""",
        encoding="utf-8",
    )
    with pytest.warns(DeprecationWarning):
        config = load_config(config_path)
    assert config.llm.combine_deal_documents is False


def test_grouping_options_do_not_enable_force_or_force_assist():
    config = LlmConfig(combine_deal_documents=True, one_call_per_deal=True)
    settings = resolve_settings(type("Cfg", (), {"llm": config})())
    assert settings.force is False
    assert settings.force_assist is False
