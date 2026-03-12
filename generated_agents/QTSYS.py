import os
import sys
import asyncio
import argparse
import tempfile
from typing import List, Dict, Any, Optional, Tuple, Union
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import math
import time
import signal
import traceback

# Core logging and LLM agent
from loguru import logger
from swarms import Agent

# Data handling
import pandas as pd
import numpy as np

# Trading APIs
import ccxt
from web3 import Web3, HTTPProvider

# ========== CONFIGURATION ===========
MODEL_NAME = os.environ.get('QTSYS_MODEL', 'gpt-4o-mini')
MAX_LOOPS = int(os.environ.get('QTSYS_MAX_LOOPS', '7'))
CCXT_TIMEOUT = int(os.environ.get('CCXT_TIMEOUT', '60'))
CCXT_RETRIES = int(os.environ.get('CCXT_RETRIES', '3'))
WEB3_TIMEOUT = int(os.environ.get('WEB3_TIMEOUT', '90'))
WEB3_RETRIES = int(os.environ.get('WEB3_RETRIES', '3'))
DEFAULT_EXCHANGE = os.environ.get('QTSYS_EXCHANGE', 'binance')
DEFAULT_SYMBOL = os.environ.get('QTSYS_SYMBOL', 'BTC/USDT')
DEFAULT_TIMEFRAME = os.environ.get('QTSYS_TIMEFRAME', '1h')
DEFAULT_BACKTEST_DAYS = int(os.environ.get('QTSYS_LOOKBACK_DAYS', '90'))

# ENV keys for Web3
INFURA_URL = os.environ.get('QTSYS_INFURA_URL', '')  # Ethereum node endpoint

# ========== STRUCTURED DATA TYPES ===========
@dataclass
class SignalResult:
    timestamp: datetime
    action: str  # 'buy', 'sell', 'hold', ...
    indicators: Dict[str, Any]
    price: float

@dataclass
class BacktestMetrics:
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    trades: int

@dataclass
class StrategyInput:
    indicators_config: Dict[str, Dict[str, Any]]
    trade_allocation_pct: float  # e.g., 0.25 for 25% equity
    min_holding_period: int  # bars
    slippage: float  # as pct
    fee: float  # as pct per trade

# ========== UTILS AND HELPERS ===========
def retry_with_backoff(
    func,
    retries: int = 3,
    backoff: float = 2.0,
    *args,
    **kwargs
) -> Any:
    """
    Retry decorator for network ops.
    """
    attempt = 0
    while attempt < retries:
        try:
            return func(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeError, OSError, Exception) as e:
            logger.error(f"Attempt {attempt+1} failed: {e}")
            attempt += 1
            if attempt >= retries:
                logger.exception("All retry attempts failed.")
                raise
            sleep_time = backoff ** attempt
            logger.warning(f"Sleeping for {sleep_time:.2f}s before retry...")
            time.sleep(sleep_time)

# -- CCXT helpers --
def get_ccxt_exchange(exchange_id: str, timeout: int = CCXT_TIMEOUT) -> ccxt.Exchange:
    if exchange_id not in ccxt.exchanges:
        raise ValueError(f"Exchange {exchange_id} not supported by ccxt.")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        'enableRateLimit': True,
        'timeout': timeout * 1000,
    })
    logger.info(f"Instantiated ccxt exchange {exchange_id}.")
    return exchange

def fetch_ohlcv_ccxt(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since: int,
    limit: int = 1000,
    retries: int = CCXT_RETRIES
) -> List[List[Union[int, float]]]:
    exchange = get_ccxt_exchange(exchange_id)
    def _fetch():
        logger.info(f"[CCXT] Downloading {symbol} OHLCV: {timeframe}, since={since}, limit={limit}")
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
    data = retry_with_backoff(_fetch, retries=retries)
    if not data or not isinstance(data, list):
        raise ValueError("OHLCV fetch failed or returned empty list.")
    logger.info(f"[CCXT] Downloaded {len(data)} candles for {symbol}.")
    return data

# -- Web3 helpers (for DeFi/non-retail signal) --
def get_web3_connection() -> Web3:
    if not INFURA_URL:
        raise RuntimeError("Missing QTSYS_INFURA_URL for DeFi data.")
    w3 = Web3(HTTPProvider(INFURA_URL, request_kwargs={'timeout': WEB3_TIMEOUT}))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to Web3 node at {INFURA_URL}")
    logger.info("Connected to Web3 node.")
    return w3

def fetch_total_value_locked(
    contract_address: str,
    abi: List[Dict[str, Any]],
    block_identifier: Optional[int] = None,
    retries: int = WEB3_RETRIES
) -> float:
    w3 = get_web3_connection()
    def _fetch():
        contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)
        tvl = contract.functions.totalSupply().call(block_identifier=block_identifier)
        return float(w3.from_wei(tvl, 'ether'))
    tvl_amt = retry_with_backoff(_fetch, retries=retries)
    logger.info(f"Fetched TVL from DeFi contract {contract_address}: {tvl_amt}")
    return tvl_amt

# ========== DATA PROCESSING / FEATURE ENGINEERING ===========
def ohlcv_to_df(
    ohlcv: List[List[Union[int, float]]],
    columns: List[str] = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
) -> pd.DataFrame:
    if not ohlcv or not isinstance(ohlcv, list):
        raise ValueError("Input OHLCV data must be a list of lists.")
    df = pd.DataFrame(ohlcv, columns=columns)
    df['date'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('date', inplace=True)
    df.drop('timestamp', axis=1, inplace=True)
    logger.info(f"Converted OHLCV to DataFrame of shape {df.shape}.")
    return df

# --- Non-retail indicator engineering ---
def calc_synthetic_volatility(df: pd.DataFrame, span: int = 12) -> pd.Series:
    if len(df) < span:
        logger.warning("Not enough data for synthetic volatility, returning zeros.")
        return pd.Series(np.zeros(len(df)), index=df.index)
    log_returns = np.log(df['close']).diff().fillna(0)
    vol = log_returns.ewm(span=span, adjust=False).std() * np.sqrt(span)
    logger.debug(f"Calculated synthetic volatility, mean={vol.mean():.6f}")
    return vol

def calc_order_flow(df: pd.DataFrame, window: int = 20) -> pd.Series:
    # Institutional-like order flow: up volume minus down volume
    direction = np.sign(df['close'].diff().fillna(0))
    up_volume = (df['volume'] * (direction > 0)).rolling(window).sum()
    down_volume = (df['volume'] * (direction < 0)).rolling(window).sum()
    flow = up_volume - down_volume
    logger.debug(f"Order flow, mean={flow.mean():.2f}")
    return flow

def calc_alpha_factor(df: pd.DataFrame, deFi_metric: Optional[pd.Series] = None) -> pd.Series:
    # Mixture: price momentum * volatility / DeFi TVL
    momentum = df['close'].pct_change(periods=10).fillna(0)
    volatility = calc_synthetic_volatility(df, span=12)
    if deFi_metric is not None:
        t = deFi_metric.reindex(df.index).fillna(method='ffill').fillna(0)
        alpha = momentum * volatility / (t + 1e-6)
        logger.debug(f"Alpha factor, min={alpha.min():.5f}, max={alpha.max():.5f}")
        return alpha
    return momentum * volatility

# ========== STRATEGY SIGNAL ENGINEERING ===========
def engineer_indicators(df: pd.DataFrame, deFi_series: Optional[pd.Series] = None) -> pd.DataFrame:
    """Add multiple non-retail and basic indicators to the DataFrame."""
    out = df.copy()
    out['synthetic_vol'] = calc_synthetic_volatility(df)
    out['order_flow'] = calc_order_flow(df)
    out['alpha_factor'] = calc_alpha_factor(df, deFi_metric=deFi_series)
    # Add a basic momentum for baseline
    out['momentum_10'] = df['close'].pct_change(periods=10)
    out['ma_20'] = df['close'].rolling(20).mean()
    out['rsi_14'] = compute_rsi(df['close'], window=14)
    logger.info(f"Engineered indicators for {len(df)} rows.")
    return out

def compute_rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff().fillna(0)
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(window).mean()
    avg_loss = pd.Series(loss).rolling(window).mean()
    rs = avg_gain / (avg_loss + 1e-8)
    rsi = 100 - (100 / (1 + rs))
    return pd.Series(rsi, index=series.index)

# ========== STRATEGY LOGIC ===========
def signal_mixture(row: pd.Series, thresholds: Dict[str, float]) -> str:
    # Mixture logic: alpha_factor > t and order_flow > t, etc.
    if (
        row['alpha_factor'] > thresholds['alpha']
        and row['order_flow'] > thresholds['order_flow']
        and row['synthetic_vol'] > thresholds['volatility']
        and row['rsi_14'] < thresholds['rsi_oversold']
    ):
        return 'buy'
    if (
        row['alpha_factor'] < -thresholds['alpha']
        and row['order_flow'] < -thresholds['order_flow']
        and row['synthetic_vol'] > thresholds['volatility']
        and row['rsi_14'] > thresholds['rsi_overbought']
    ):
        return 'sell'
    return 'hold'

# ========== BACKTEST ENGINE ===========
def run_backtest(
    df: pd.DataFrame,
    thresholds: Dict[str, float],
    config: StrategyInput
) -> Tuple[List[SignalResult], BacktestMetrics]:
    signals: List[SignalResult] = []
    position = 0  # -1 short, 0 flat, 1 long
    entry_price = None
    holding_period = 0
    equity = 1.0
    peak_equity = 1.0
    max_dd = 0.0
    wins, losses, trades = 0, 0, 0
    last_trade = None

    for date, row in df.iterrows():
        action = signal_mixture(row, thresholds)
        if position == 0:
            # Can open new position
            if action == 'buy':
                position = 1
                entry_price = row['close'] * (1 + config.slippage + config.fee)
                trades += 1
                holding_period = 1
                last_trade = 'buy'
            elif action == 'sell':
                position = -1
                entry_price = row['close'] * (1 - config.slippage - config.fee)
                trades += 1
                holding_period = 1
                last_trade = 'sell'
            else:
                signals.append(SignalResult(timestamp=date, action='hold', indicators=row.to_dict(), price=row['close']))
        else:
            holding_period += 1
            # Exit logic: opposite signal or min holding met
            if ((position == 1 and action == 'sell') or (position == -1 and action == 'buy') or (holding_period >= config.min_holding_period)):
                exit_price = row['close'] * (1 - config.slippage - config.fee) if position == 1 else row['close'] * (1 + config.slippage + config.fee)
                pnl = (exit_price - entry_price)/entry_price if position == 1 else (entry_price - exit_price)/entry_price
                equity *= (1 + config.trade_allocation_pct * pnl)
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                position = 0
                signals.append(SignalResult(timestamp=date, action=f'exit_{last_trade}', indicators=row.to_dict(), price=row['close']))
                holding_period = 0
            else:
                signals.append(SignalResult(timestamp=date, action='hold', indicators=row.to_dict(), price=row['close']))
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / (peak_equity + 1e-9)
        max_dd = max(max_dd, dd)

    total_return = equity - 1.0
    # Calculate Sharpe ratio (assume daily, risk-free = 0)
    returns = pd.Series([sig.price for sig in signals]).pct_change().dropna()
    if len(returns) < 2:
        sharpe_ratio = 0.0
    else:
        sharpe_ratio = float((returns.mean() / (returns.std() + 1e-8)) * np.sqrt(252))
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
    metrics = BacktestMetrics(
        total_return=total_return,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_dd,
        win_rate=win_rate,
        trades=trades
    )
    logger.info(f"Backtest completed: return={total_return:.4f}, sharpe={sharpe_ratio:.3f}, max_dd={max_dd:.3f}, win%={win_rate:.2%}, trades={trades}")
    return signals, metrics

# ========== REPORTING ===========
def signals_to_df(signals: List[SignalResult]) -> pd.DataFrame:
    data = [
        {
            'timestamp': sig.timestamp,
            'action': sig.action,
            'price': sig.price,
            **{f'indic_{k}': v for k, v in sig.indicators.items()}
        } for sig in signals
    ]
    df = pd.DataFrame(data)
    return df

def print_metrics(metrics: BacktestMetrics) -> None:
    logger.info(f"==== BACKTEST METRICS ====")
    logger.info(f"Total Return: {metrics.total_return:.2%}")
    logger.info(f"Sharpe Ratio: {metrics.sharpe_ratio:.3f}")
    logger.info(f"Max Drawdown: {metrics.max_drawdown:.2%}")
    logger.info(f"Win Rate: {metrics.win_rate:.2%}")
    logger.info(f"Total Trades: {metrics.trades}")

# ========== MAIN SWARMS AGENT ===========
agent_name = "QTSys"
agent_description = (
    "Advanced quantitative trading agent for multi-indicator, non-retail signal mixtures "
    "(order flow, volatility, DeFi metrics). Downloads historical data from CeFi/DeFi, engineers "
    "alpha factors and risk metrics, backtests signal mixtures, and reports actionable results. "
    "Designed for quant traders and research alpha monetization."
)
system_prompt = (
    "You are QTSys, a quantitative trading research agent. You automatically: (1) download historical OHLCV "
    "from exchanges using 'ccxt', (2) fetch DeFi protocol metrics via web3 and smart contract queries, "
    "(3) engineer a mixture of proprietary and advanced (non-retail) indicators including synthetic volatility, "
    "order flow, custom alpha factors, (4) generate mixture signals, (5) run a robust backtest on these signals "
    "with realistic slippage/fees, (6) output risk-adjusted performance metrics, trade logs and edge curves. "
    "Never use example or fake data. Never skip risk and statistical analysis. Present all outputs in JSON or CSV. "
    "Document all indicator and logic details along with results."
)

qt_agent = Agent(
    agent_name=agent_name,
    agent_description=agent_description,
    system_prompt=system_prompt,
    model_name=MODEL_NAME,
    max_loops=MAX_LOOPS
)

# ========== ENTRYPOINT CLI & MAIN ===========
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest a quantitative trading system using advanced indicator mixtures"
    )
    parser.add_argument('--exchange', type=str, default=DEFAULT_EXCHANGE, help='CCXT exchange id (default: binance)')
    parser.add_argument('--symbol', type=str, default=DEFAULT_SYMBOL, help='Trading symbol (default: BTC/USDT)')
    parser.add_argument('--timeframe', type=str, default=DEFAULT_TIMEFRAME, help='OHLCV timeframe (default: 1h)')
    parser.add_argument('--lookback_days', type=int, default=DEFAULT_BACKTEST_DAYS, help='Lookback window in days (default: 90)')
    parser.add_argument('--trade_allocation', type=float, default=0.25, help='Fraction of equity per trade')
    parser.add_argument('--min_hold', type=int, default=8, help='Minimum holding period per trade')
    parser.add_argument('--slippage', type=float, default=0.001, help='Slippage pct per trade')
    parser.add_argument('--fee', type=float, default=0.0008, help='Trading fee pct per trade')
    parser.add_argument('--output', type=str, default='', help='CSV output file (optional)')
    return parser.parse_args()

def main():
    args = parse_args()
    logger.info(f"QTSys starting backtest for {args.exchange} {args.symbol} on {args.timeframe}...")
    # 1. Fetch historical OHLCV
    now = int(time.time() * 1000)
    since = now - args.lookback_days * 24 * 60 * 60 * 1000
    try:
        ohlcv = fetch_ohlcv_ccxt(
            exchange_id=args.exchange,
            symbol=args.symbol,
            timeframe=args.timeframe,
            since=since,
            limit=(args.lookback_days * 24 if 'h' in args.timeframe else args.lookback_days)
        )
    except Exception as e:
        logger.exception(f"Failed to fetch market data: {e}")
        return
    df = ohlcv_to_df(ohlcv)

    # 2. Optionally, fetch DeFi metric (e.g. TVL) - for illustration: UniswapV2 ETH/USDC pool
    deFi_series = None
    try:
        # Supply a real ABI and address for a DeFi pool if available (not included due to space)
        if INFURA_URL:
            # This is for a real deployment: would dynamically discover top pools for the symbol
            pool_addr = '0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc'  # UniswapV2 ETH/USDC
            sample_abi = [
                {"inputs":[], "name": "totalSupply", "outputs":[{"internalType": "uint256", "name":"", "type":"uint256"}], "stateMutability": "view", "type": "function"}
            ]
            tvl_eth = fetch_total_value_locked(pool_addr, sample_abi)
            deFi_series = pd.Series([tvl_eth] * len(df), index=df.index)  # Holds value constant for demo
            logger.info(f"Loaded DeFi TVL series for indicator engineering.")
    except Exception as e:
        logger.warning(f"Failed to fetch DeFi metrics: {e}")

    # 3. Feature engineering
    indicators_df = engineer_indicators(df, deFi_series)

    # 4. Strategy config (can be overridden via args)
    thresholds = {
        'alpha': 0.0002,
        'order_flow': 1.0,
        'volatility': 0.0007,
        'rsi_oversold': 35,
        'rsi_overbought': 65,
    }
    sconfig = StrategyInput(
        indicators_config={},
        trade_allocation_pct=args.trade_allocation,
        min_holding_period=args.min_hold,
        slippage=args.slippage,
        fee=args.fee
    )
    # 5. Backtest
    signals, metrics = run_backtest(indicators_df, thresholds, sconfig)
    print_metrics(metrics)
    # 6. Output
    trades_df = signals_to_df(signals)
    if args.output:
        try:
            trades_df.to_csv(args.output, index=False, encoding='utf-8')
            logger.info(f"Trade log written to {args.output}")
        except Exception as e:
            logger.warning(f"Failed to write output CSV: {e}")
    else:
        print(trades_df.tail(20).to_string())
    # Optionally, expose the outputs through Swarms' interface
    # task = {'mode': 'backtest', ...}
    # result = qt_agent.run(task)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("QTSys interrupted by user.")
    except Exception as e:
        logger.error(f"QTSys terminated with exception: {e}")
        traceback.print_exc()
