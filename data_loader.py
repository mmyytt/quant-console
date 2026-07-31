"""
数据自动下载器 — 云部署用
首次运行时自动从 Binance 公开数据源下载历史K线, 存为 parquet 格式。
"""
import os, sys, time, requests, pandas as pd, zipfile, io
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BINANCE_BASE = "https://data.binance.vision/data/spot/monthly/klines"

COINS = {
    "ETH": {"symbol": "ETHUSDT", "start": 2017},
    "BTC": {"symbol": "BTCUSDT", "start": 2017},
    "SOL": {"symbol": "SOLUSDT", "start": 2020},
}


def ensure_data(coin: str, max_retries: int = 3) -> str:
    """
    确保某币种的 15m 数据存在, 不存在则自动下载。

    Returns:
        parquet 文件路径
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    pq_path = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    if os.path.exists(pq_path) and os.path.getsize(pq_path) > 1000000:
        return pq_path

    info = COINS.get(coin)
    if not info:
        raise ValueError(f"Unknown coin: {coin}")

    print(f"[DataLoader] Downloading {coin} 15m data from Binance...")
    symbol = info["symbol"]
    base_url = f"{BINANCE_BASE}/{symbol}/15m/{symbol}-15m-"
    all_dfs = []
    now = datetime.now()

    for year in range(info["start"], now.year + 1):
        for month in range(1, 13):
            if year == now.year and month > now.month:
                break
            fname = f"{year}-{month:02d}"
            url = base_url + fname + ".zip"
            for retry in range(max_retries):
                try:
                    r = requests.get(url, timeout=60)
                    if r.status_code == 200 and len(r.content) > 500:
                        zf = zipfile.ZipFile(io.BytesIO(r.content))
                        df = pd.read_csv(zf.open(zf.namelist()[0]), header=None)
                        if len(df) < 10:
                            break
                        ts_raw = pd.to_numeric(df.iloc[:, 0])
                        if ts_raw.iloc[-1] > 1e15:
                            ts_raw = ts_raw // 1000
                        ts = pd.to_datetime(ts_raw, unit="ms")
                        df = df[[1, 2, 3, 4, 5]].copy()
                        df.columns = ["open", "high", "low", "close", "vol"]
                        df.index = ts
                        for col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                        df = df.dropna()
                        if len(df) > 0:
                            all_dfs.append(df)
                            print(f"  {fname}: {len(df)} bars OK")
                        break
                except Exception as e:
                    if retry == max_retries - 1:
                        print(f"  {fname}: FAILED ({e})")
                    time.sleep(1)
            time.sleep(0.1)

    if not all_dfs:
        raise RuntimeError(f"Failed to download any data for {coin}")

    df_all = pd.concat(all_dfs).sort_index()
    df_all = df_all[~df_all.index.duplicated()]
    df_all = df_all.reset_index()
    df_all.to_parquet(pq_path, index=False)
    print(f"[DataLoader] {coin} saved: {len(df_all):,} bars -> {pq_path}")
    return pq_path


if __name__ == "__main__":
    for coin in COINS:
        ensure_data(coin)
    print("All data ready!")
