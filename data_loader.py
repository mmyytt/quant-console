"""
数据自动下载器 — 云部署用
首次: 全量下载历史K线, 存为 parquet
后续: 增量补全到最新日期 (历史缓存 + 增量补全)
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
    确保某币种的 15m 数据存在且更新到最新。
    增量逻辑: 读取已有parquet -> 获取本地最新日期 -> 只下载缺失的月份 -> 合并。

    Returns:
        parquet 文件路径
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    pq_path = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    info = COINS.get(coin)
    if not info:
        raise ValueError(f"Unknown coin: {coin}")

    # 读取已有数据, 获取最新日期
    existing = None
    last_ts = None
    if os.path.exists(pq_path) and os.path.getsize(pq_path) > 500000:
        existing = pd.read_parquet(pq_path)
        time_col = existing.columns[0]
        existing[time_col] = pd.to_datetime(existing[time_col])
        existing = existing.set_index(time_col)
        existing = existing[~existing.index.duplicated()]
        existing = existing.sort_index()
        last_ts = existing.index.max()
        print(f"[DataLoader] {coin}: {len(existing):,} bars cached, last={last_ts}")

    # 计算需要下载的月份范围
    symbol = info["symbol"]
    base_url = f"{BINANCE_BASE}/{symbol}/15m/{symbol}-15m-"
    now = datetime.now()

    if last_ts is not None:
        start_year, start_month = last_ts.year, last_ts.month
    else:
        start_year, start_month = info["start"], 1

    # 需要下载的月份列表
    months_to_fetch = []
    for year in range(start_year, now.year + 1):
        m_start = start_month if year == start_year else 1
        m_end = now.month if year == now.year else 12
        for month in range(m_start, m_end + 1):
            months_to_fetch.append((year, month))

    if not months_to_fetch:
        print(f"[DataLoader] {coin}: already up to date")
        return pq_path

    print(f"[DataLoader] {coin}: fetching {len(months_to_fetch)} months ({months_to_fetch[0]} ~ {months_to_fetch[-1]})")

    new_dfs = []
    for year, month in months_to_fetch:
        fname = f"{year}-{month:02d}"
        url = base_url + fname + ".zip"
        for retry in range(max_retries):
            try:
                r = requests.get(url, timeout=60)
                if r.status_code != 200 or len(r.content) < 500:
                    break
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
                    # 去重: 只保留比已有数据更新的部分
                    if last_ts is not None:
                        df = df[df.index > last_ts]
                    if len(df) > 0:
                        new_dfs.append(df)
                        print(f"  {fname}: {len(df)} new bars OK")
                break
            except Exception as e:
                if retry == max_retries - 1:
                    print(f"  {fname}: FAILED ({e})")
                time.sleep(1)
        time.sleep(0.1)

    # 合并
    if new_dfs:
        df_new = pd.concat(new_dfs).sort_index()
        df_new = df_new[~df_new.index.duplicated()]
        if existing is not None:
            df_all = pd.concat([existing, df_new]).sort_index()
            df_all = df_all[~df_all.index.duplicated()]
        else:
            df_all = df_new

        df_all = df_all.reset_index()
        df_all.to_parquet(pq_path, index=False)
        print(f"[DataLoader] {coin} updated: {len(df_all):,} total bars (added {len(df_new):,})")
    else:
        print(f"[DataLoader] {coin}: no new data to add")

    return pq_path


if __name__ == "__main__":
    for coin in COINS:
        ensure_data(coin)
    print("All data ready!")
