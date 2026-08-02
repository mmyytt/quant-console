"""
数据自动下载器 v3 — 云部署用
================================
历史: Binance月度zip (2017-2026)
增量: yfinance (最近60天, 兼容性最强)
校验: 最新数据不超过1天, 否则弹警告
"""
import os, sys, time, requests, pandas as pd, zipfile, io
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BINANCE_BASE = "https://data.binance.vision/data/spot/monthly/klines"

COINS = {
    "ETH": {"symbol": "ETHUSDT", "yf": "ETH-USD", "start": 2017},
    "BTC": {"symbol": "BTCUSDT", "yf": "BTC-USD", "start": 2017},
    "SOL": {"symbol": "SOLUSDT", "yf": "SOL-USD", "start": 2020},
}

YFINANCE_INTERVAL_MAP = {"15m": "15m", "1h": "60m", "4h": "1h", "1d": "1d"}


def _read_existing(pq_path: str):
    """读取已有parquet, 返回(DataFrame或None, last_ts或None)"""
    if not os.path.exists(pq_path) or os.path.getsize(pq_path) < 100000:
        return None, None
    df = pd.read_parquet(pq_path)
    time_col = df.columns[0]
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.set_index(time_col)
    df = df[~df.index.duplicated()]
    df = df.sort_index()
    last = df.index.max()
    return df, last


def _download_history_zip(symbol: str, start_year: int, end_year: int, end_month: int) -> pd.DataFrame:
    """Binance月度zip下载历史 (兜底)"""
    base_url = f"{BINANCE_BASE}/{symbol}/15m/{symbol}-15m-"
    all_dfs = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            if year == end_year and month > end_month:
                break
            fname = f"{year}-{month:02d}"
            try:
                r = requests.get(base_url + fname + ".zip", timeout=60)
                if r.status_code != 200 or len(r.content) < 500:
                    continue
                zf = zipfile.ZipFile(io.BytesIO(r.content))
                df = pd.read_csv(zf.open(zf.namelist()[0]), header=None)
                if len(df) < 10:
                    continue
                ts_raw = pd.to_numeric(df.iloc[:, 0])
                if ts_raw.iloc[-1] > 1e15:
                    ts_raw = ts_raw // 1000
                ts = pd.to_datetime(ts_raw, unit="ms")
                df = df[[1, 2, 3, 4, 5]].copy()
                df.columns = ["open", "high", "low", "close", "vol"]
                df.index = ts
                for c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.dropna()
                if len(df) > 0:
                    all_dfs.append(df)
                    print(f"  ZIP {fname}: {len(df)} bars")
            except Exception as e:
                print(f"  ZIP {fname}: {e}")
            time.sleep(0.05)
    if all_dfs:
        df = pd.concat(all_dfs).sort_index()
        return df[~df.index.duplicated()]
    return pd.DataFrame()


def _fetch_yfinance(symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    """
    用 yfinance 抓取最近N天数据 (绕过Binance IP限制)。

    Args:
        symbol: yfinance ticker, e.g. 'ETH-USD'
        period: 抓取周期, e.g. '60d', '90d'
        interval: K线周期, e.g. '15m', '1h'
    Returns:
        DataFrame with columns [open, high, low, close, vol], DatetimeIndex
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[yfinance] not installed, trying pip install...")
        os.system(f"{sys.executable} -m pip install yfinance -q")
        import yfinance as yf

    print(f"[yfinance] Downloading {symbol} {period} {interval}...")
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval)

    if df is None or len(df) == 0:
        print(f"[yfinance] {symbol}: no data returned")
        return pd.DataFrame()

    # yfinance返回多级列, 取第一级
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 标准化列名
    col_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "vol"}
    df = df.rename(columns=col_map)
    for c in ["open", "high", "low", "close", "vol"]:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df[["open", "high", "low", "close", "vol"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()

    print(f"[yfinance] {symbol}: got {len(df)} bars, {df.index[0]} ~ {df.index[-1]}")
    return df


def ensure_data(coin: str) -> str:
    """
    确保数据存在且更新到最新。

    流程:
      1. 读本地parquet → 历史数据
      2. yfinance拉最近60天 → 增量数据
      3. 合并: 历史(parquet) + 增量(yfinance), 去重
      4. 校验最新数据时间, 超过1天打印警告
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    pq_path = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    info = COINS.get(coin)
    if not info:
        raise ValueError(f"Unknown coin: {coin}")
    symbol_zip = info["symbol"]
    symbol_yf = info["yf"]

    # 1. 读历史缓存
    existing, last_ts = _read_existing(pq_path)
    if existing is not None:
        print(f"[DataLoader] {coin}: cache {len(existing):,} bars, last={last_ts}")

    # 2. 如果没有历史数据, 先下zip
    if existing is None or len(existing) < 1000:
        print(f"[DataLoader] {coin}: downloading history zip...")
        now = datetime.now()
        df_zip = _download_history_zip(symbol_zip, info["start"], now.year, now.month)
        if len(df_zip) > 0:
            existing = df_zip
            last_ts = existing.index.max()

    # 3. yfinance增量: 最近60天
    print(f"[DataLoader] {coin}: fetching recent data via yfinance...")
    df_yf = _fetch_yfinance(symbol_yf, period="60d", interval="15m")

    if len(df_yf) > 0 and existing is not None:
        # 合并: 保留历史中早于yfinace起点的 + yfinance全部
        yf_start = df_yf.index.min()
        hist_part = existing[existing.index < yf_start] if last_ts and last_ts < yf_start else existing
        if last_ts and last_ts >= yf_start:
            # yfinance覆盖了历史尾部, 用yfinance替换重叠部分
            hist_part = existing[existing.index < yf_start]

        df_all = pd.concat([hist_part, df_yf]).sort_index()
        df_all = df_all[~df_all.index.duplicated()]
        print(f"[DataLoader] {coin}: merged {len(df_all):,} bars")
    elif len(df_yf) > 0:
        df_all = df_yf
    elif existing is not None:
        df_all = existing
    else:
        raise RuntimeError(f"[DataLoader] {coin}: failed to load any data")

    # 4. 校验最新时间
    last_date = df_all.index.max()
    gap_hours = (datetime.now() - last_date).total_seconds() / 3600
    print(f"[DataLoader] {coin}: last bar = {last_date}, gap = {gap_hours:.1f}h")

    if gap_hours > 24:
        print(f"[DataLoader] WARNING: {coin} data is {gap_hours:.1f}h stale! yfinance may have failed.")

    # 5. 保存
    df_out = df_all.reset_index()
    df_out.to_parquet(pq_path, index=False)
    print(f"[DataLoader] {coin}: saved {len(df_all):,} bars -> {pq_path}")
    return pq_path


def get_data_freshness(coin: str) -> dict:
    """检查数据新鲜度 (供Streamlit调用)"""
    pq_path = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    _, last_ts = _read_existing(pq_path)
    if last_ts is None:
        return {"status": "no_data", "last_ts": None, "gap_hours": 9999}
    gap_hours = (datetime.now() - last_ts).total_seconds() / 3600
    return {
        "status": "stale" if gap_hours > 24 else "fresh",
        "last_ts": str(last_ts),
        "gap_hours": round(gap_hours, 1),
    }


if __name__ == "__main__":
    for coin in COINS:
        ensure_data(coin)
    print("All data ready!")
