"""Deploy winning hypotheses to config.yaml."""

from pathlib import Path

try:
    from ruamel.yaml import YAML
    _yaml = YAML()
    _yaml.preserve_quotes = True
    _use_ruamel = True
except ImportError:
    import yaml
    _use_ruamel = False


def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        if _use_ruamel:
            return _yaml.load(f)
        return yaml.safe_load(f)


def _save_config(config: dict, config_path: str = "config.yaml"):
    """Save config dict to YAML file. Public for strategy version tracking."""
    with open(config_path, "w") as f:
        if _use_ruamel:
            _yaml.dump(config, f)
        else:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def apply_to_config(config_changes: dict, config_path: str = "config.yaml"):
    """Apply hypothesis config changes to config.yaml."""
    config = _load_config(config_path)

    params = config.setdefault("autoresearch_params", {})

    # Apply wallet weight overrides
    if "wallet_weights" in config_changes:
        overrides = params.setdefault("wallet_weights_override", {})
        overrides.update(config_changes["wallet_weights"])

    # Apply sport multipliers
    if "sport_multipliers" in config_changes:
        mults = params.setdefault("sport_multipliers", {})
        mults.update(config_changes["sport_multipliers"])

    # Apply timing rules
    if "timing_rules" in config_changes:
        params["timing_rules"] = config_changes["timing_rules"]

    # Apply sizing changes
    if "kelly_fraction" in config_changes:
        config.setdefault("sizing", {})["kelly_fraction"] = config_changes["kelly_fraction"]

    if "copy_base_size_pct" in config_changes:
        config.setdefault("sizing", {})["copy_base_size_pct"] = config_changes["copy_base_size_pct"]

    # Apply consensus changes
    if "min_consensus" in config_changes:
        config.setdefault("copy_trading", {}).setdefault("consensus", {})["min_traders"] = config_changes["min_consensus"]

    if "max_delay_seconds" in config_changes:
        config.setdefault("copy_trading", {})["max_delay_seconds"] = config_changes["max_delay_seconds"]

    # Apply min edge
    if "min_edge_pct" in config_changes:
        config.setdefault("odds_arb", {})["min_edge_pct"] = config_changes["min_edge_pct"]

    _save_config(config, config_path)
