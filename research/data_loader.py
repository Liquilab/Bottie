"""Load and parse trading data from trades.jsonl and wallet histories."""

import json
from pathlib import Path
from typing import Optional

import pandas as pd


def load_trades(path: str = "data/trades.jsonl") -> pd.DataFrame:
    """Load our trade log into a DataFrame."""
    p = Path(path)
    if not p.exists():
        return pd.DataFrame()

    records = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True)
    return df


def load_wallet_histories(directory: str = "data/wallet_trades/") -> pd.DataFrame:
    """Load historical trades from watched wallets."""
    p = Path(directory)
    if not p.exists():
        return pd.DataFrame()

    frames = []
    for file in p.glob("*.jsonl"):
        records = []
        for line in file.read_text().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if records:
            df = pd.DataFrame(records)
            df["wallet_file"] = file.stem
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def load_config(path: str = "config.yaml") -> dict:
    """Load the YAML config."""
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)
