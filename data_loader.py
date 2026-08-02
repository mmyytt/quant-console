"""
数据自动下载器 v2 — 云部署用
================================
策略: 历史缓存(Binance月度zip) + 实时增量(Binance REST API)
1. 读取本地parquet, 取 last_timestamp
2. Binance /api/v3/klines 从 last_timestamp 分页拉到 now()
3. 合并去重保存
4. 无本地文件时: zip下载最近365天 + API补全到今天
"""
import os, sys, time, requests, pandas as pd, zipfile, io
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BINANCE_BASE = "https://data.binance.vision/data/spot/monthly/klines"
BINANCE_API = "https://api.binance.com/api/v3/klines"

COINS = {
    "ETH": {"symbol": "ETHUSDT", "start": 2017},
    "BTC": {"symbol": "BTCUSDT", "start": 2017},
    "SOL": {"symbol": "SOLUSDT", "start": 2020},
}


def _read_existing(pq_path: str):
    """读取已有parquet, 返回(DataFrame或None, last_timestamp或None)"""
    if not os.path.exists(pq_path) or os.path.getsize(pq_path) < 100000:
        return None, None
    df = pd.read_parquet(pq_path)
    time_col = df.columns[0]
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.set_index(time_col)
    df = df[~df.index.duplicated()]
    df = df.sort_index()
    return df, df.index.max()


def _fetch_api(symbol: str, start_ms: int, end_ms: int, interval: str = "15m") -> pd.DataFrame:
    """
    Binance REST API 分页抓取K线。
    每次最多1000根, 循环请求直到覆盖 [start_ms, end_ms]。
    """
    all_rows = []
    current_start = start_ms
    page = 0

    while current_start < end_ms:
        url = f"{BINANCE_API}?symbol={symbol}&interval={interval}&startTime={current_start}&limit=1000"
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                print(f"  API page {page}: HTTP {resp.status_code}")
                break
            data = resp.json()
            if not data or not isinstance(data, list):
                break

            page += 1
            all_rows.extend(data)

            last_ts = data[-1][0]  # 最后一条的时间戳(ms)
            print(f"  Page {page}: {len(data)} bars, {pd.to_datetime(last_ts, unit='ms')}")

            if len(data) < 1000:
                break  # 最后一页

            current_start = last_ts + 1  # 下一批从下1ms开始
            time.sleep(0.3)  # 限速

        except Exception as e:
            print(f"  API page {page} error: {e}")
            break

    if not all_rows:
        return pd.DataFrame()

    # 转为DataFrame
    df = pd.DataFrame(all_rows, columns=[
        "ts", "open", "high", "low", "close", "vol",
        "close_time", "quote_vol", "trades", "taker_buy_vol",
        "taker_buy_quote_vol", "ignore"
    ])
    ts = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms")
    df = df[["open", "high", "low", "close", "vol"]].copy()
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.index = ts
    df = df.dropna()
    df = df.sort_index()
    return df


def _download_history_zip(symbol: str, start_year: int, end_year: int, end_month: int) -> pd.DataFrame:
    """从Binance月度zip下载历史数据 (兜底用)"""
    base_url = f"{BINANCE_BASE}/{symbol}/15m/{symbol}-15m-"
    all_dfs = []

    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            if year == end_year and month > end_month:
                break
            fname = f"{year}-{month:02d}"
            url = base_url + fname + ".zip"
            try:
                r = requests.get(url, timeout=60)
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
                for col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
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


def ensure_data(coin: str) -> str:
    """
    确保数据存在且更新到最新。

    流程:
      1. 读本地parquet → last_ts
      2. API从 last_ts 拉到 now()
      3. 合并去重保存
      4. 无本地数据: zip下载历史 + API补全
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    pq_path = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    info = COINS.get(coin)
    if not info:
        raise ValueError(f"Unknown coin: {coin}")
    symbol = info["symbol"]

    existing, last_ts = _read_existing(pq_path)
    now = datetime.now()
    now_ms = int(now.timestamp() * 1000)

    if existing is not None:
        print(f"[DataLoader] {coin}: {len(existing):,} bars cached, last={last_ts}")

    # === 没有历史数据: 先下载zip ===
    if existing is None or len(existing) < 1000:
        print(f"[DataLoader] {coin}: downloading history zip...")
        end_year, end_month = now.year, now.month
        start_year = info["start"]
        df_zip = _download_history_zip(symbol, start_year, end_year, end_month)
        if len(df_zip) > 0:
            existing = df_zip
            last_ts = existing.index.max()
            print(f"[DataLoader] {coin}: zip done, {len(existing):,} bars, last={last_ts}")

    # === API增量: 从last_ts拉到now ===
    if last_ts is not None:
        gap_hours = (now - last_ts).total_seconds() / 3600
        if gap_hours > 0.5:  # 超过30分钟才需要更新
            start_ms = int(last_ts.timestamp() * 1000) + 60000  # 从last后1分钟开始
            print(f"[DataLoader] {coin}: fetching {gap_hours:.1f}h of new data via API...")
            df_new = _fetch_api(symbol, start_ms, now_ms, "15m")

            if len(df_new) > 0:
                print(f"[DataLoader] {coin}: API got {len(df_new)} new bars, "
                      f"{df_new.index[0]} ~ {df_new.index[-1]}")
                existing = pd.concat([existing, df_new]).sort_index()
                existing = existing[~existing.index.duplicated()]
                print(f"[DataLoader] {coin}: merged -> {len(existing):,} total bars")
            else:
                print(f"[DataLoader] {coin}: no new data from API")
        else:
            print(f"[DataLoader] {coin}: gap only {gap_hours:.1f}h, skipping API")
    else:
        # 完全没有数据: 直接API抓最近365天
        print(f"[DataLoader] {coin}: no data at all, fetching last 365 days via API...")
        start_ms = int((now - timedelta(days=365)).timestamp() * 1000)
        existing = _fetch_api(symbol, start_ms, now_ms, "15m")
        if len(existing) > 0:
            print(f"[DataLoader] {coin}: API got {len(existing):,} bars")

    if existing is None or len(existing) == 0:
        raise RuntimeError(f"[DataLoader] {coin}: failed to load any data")

    # 保存
    df_out = existing.reset_index()
    df_out.to_parquet(pq_path, index=False)
    print(f"[DataLoader] {coin}: saved {len(existing):,} bars -> {pq_path}")
    return pq_path


if __name__ == "__main__":
    for coin in COINS:
        ensure_data(coin)
    print("All data ready!")
