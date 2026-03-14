"""Analyze trading performance by various dimensions."""

import pandas as pd


def analyze(df: pd.DataFrame) -> dict:
    """Compute overall performance metrics."""
    if df.empty:
        return {"total_trades": 0}

    resolved = df[df["result"].notna()]
    if resolved.empty:
        return {"total_trades": len(df), "resolved": 0}

    wins = (resolved["result"] == "win").sum()
    losses = (resolved["result"] == "loss").sum()
    total_pnl = resolved["pnl"].sum() if "pnl" in resolved.columns else 0

    return {
        "total_trades": len(df),
        "resolved": len(resolved),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": float(wins / len(resolved)) if len(resolved) > 0 else 0,
        "total_pnl": float(total_pnl),
        "avg_pnl": float(total_pnl / len(resolved)) if len(resolved) > 0 else 0,
    }


def metrics_by(df: pd.DataFrame, column: str) -> dict:
    """Break down metrics by a specific column."""
    if df.empty or column not in df.columns:
        return {}

    result = {}
    for value, group in df.groupby(column):
        resolved = group[group["result"].notna()]
        if resolved.empty:
            continue
        wins = (resolved["result"] == "win").sum()
        pnl = resolved["pnl"].sum() if "pnl" in resolved.columns else 0
        result[str(value)] = {
            "trades": len(resolved),
            "wins": int(wins),
            "win_rate": float(wins / len(resolved)),
            "pnl": float(pnl),
        }
    return result


def per_wallet_performance(wallet_df: pd.DataFrame) -> dict:
    """Analyze performance per watched wallet."""
    if wallet_df.empty:
        return {}

    if "proxy_wallet" in wallet_df.columns:
        col = "proxy_wallet"
    elif "wallet_file" in wallet_df.columns:
        col = "wallet_file"
    else:
        return {}

    result = {}
    for wallet, group in wallet_df.groupby(col):
        resolved = group[group["result"].notna()] if "result" in group.columns else pd.DataFrame()
        wins = int((resolved["result"] == "win").sum()) if not resolved.empty else 0
        pnl = float(resolved["pnl"].sum()) if ("pnl" in resolved.columns and not resolved.empty) else 0.0
        invested = float(resolved["size_usdc"].sum()) if ("size_usdc" in resolved.columns and not resolved.empty) else 0.0
        result[str(wallet)] = {
            "total_trades": len(group),
            "resolved": len(resolved),
            "wins": wins,
            "losses": len(resolved) - wins,
            "win_rate": float(wins / len(resolved)) if len(resolved) > 0 else 0.0,
            "pnl": pnl,
            "roi": float(pnl / invested) if invested > 0 else 0.0,
        }
    return result
