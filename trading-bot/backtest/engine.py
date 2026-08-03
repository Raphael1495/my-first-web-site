from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import Config
from data.loader import load_history
from strategy.trend_following import compute_indicators, position_size, stop_price


@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    stop: float
    entry_date: pd.Timestamp


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    reason: str  # "stop" | "dead_cross"


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: pd.Series = None
    metrics: dict = field(default_factory=dict)


def _metrics(equity_curve: pd.Series, trades: list) -> dict:
    daily_ret = equity_curve.pct_change().dropna()
    n_years = max((equity_curve.index[-1] - equity_curve.index[0]).days / 365.25, 1e-6)
    cagr = (equity_curve.iloc[-1] / equity_curve.iloc[0]) ** (1 / n_years) - 1
    running_max = equity_curve.cummax()
    mdd = ((equity_curve - running_max) / running_max).min()
    sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0.0
    wins = [t for t in trades if t.pnl > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    return {
        "CAGR": round(float(cagr) * 100, 2),
        "MDD": round(float(mdd) * 100, 2),
        "Sharpe": round(float(sharpe), 2),
        "총 거래 횟수": len(trades),
        "승률(%)": round(win_rate * 100, 1),
        "최종 자산": round(float(equity_curve.iloc[-1]), 0),
    }


def run_backtest(config: Config) -> BacktestResult:
    data = {}
    for symbol in config.symbols:
        df = load_history(symbol, period=config.backtest_period)
        data[symbol] = compute_indicators(df, config.strategy)

    all_dates = sorted(set().union(*[df.index for df in data.values()]))

    cash = config.initial_capital
    open_positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_history = []

    for date in all_dates:
        # 1) 보유 포지션 청산 체크 (손절 -> 데드크로스 순으로 확인)
        for symbol in list(open_positions.keys()):
            df = data[symbol]
            if date not in df.index:
                continue
            row = df.loc[date]
            pos = open_positions[symbol]

            exit_price = None
            reason = None
            if row["Low"] <= pos.stop:
                exit_price = pos.stop
                reason = "stop"
            elif bool(row["dead_cross"]):
                exit_price = row["Close"]
                reason = "dead_cross"

            if exit_price is not None:
                pnl = (exit_price - pos.entry_price) * pos.shares
                cash += exit_price * pos.shares
                trades.append(
                    Trade(symbol, pos.entry_date, date, pos.entry_price, exit_price, pos.shares, pnl, reason)
                )
                del open_positions[symbol]

        # 2) 신규 진입 체크 (리스크 한도 내에서만)
        if len(open_positions) < config.risk.max_open_positions:
            equity_now = cash + sum(
                data[s].loc[date, "Close"] * p.shares for s, p in open_positions.items() if date in data[s].index
            )
            for symbol, df in data.items():
                if symbol in open_positions or date not in df.index:
                    continue
                row = df.loc[date]
                if not bool(row.get("golden_cross", False)) or pd.isna(row["atr"]):
                    continue

                entry_price = row["Close"]
                shares = position_size(equity_now, entry_price, row["atr"], config.strategy, config.risk)
                cost = shares * entry_price
                if shares <= 0 or cost > cash:
                    continue

                cash -= cost
                open_positions[symbol] = Position(
                    symbol, shares, entry_price, stop_price(entry_price, row["atr"], config.strategy), date
                )
                if len(open_positions) >= config.risk.max_open_positions:
                    break

        # 3) 일별 평가자산 기록
        equity = cash + sum(
            data[s].loc[date, "Close"] * p.shares for s, p in open_positions.items() if date in data[s].index
        )
        equity_history.append((date, equity))

    equity_curve = pd.Series(dict(equity_history)).sort_index()
    result = BacktestResult(trades=trades, equity_curve=equity_curve)
    result.metrics = _metrics(equity_curve, trades)
    return result
