"""
全量5m历史数据下载器
===================
历史: Binance月度zip (2017-2026, 不限速)
增量: Binance REST API (最近60天)
存储: Parquet 格式
用法: python fetch_data.py [--force]
"""
import os, sys, time, requests, zipfile, io, argparse
import pandas as pd
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BINANCE_ZIP = "https://data.binance.vision/data/spot/monthly/klines"
BINANCE_API = "https://api.binance.com/api/v3/klines"

COINS = {
    "BTC": {"symbol": "BTCUSDT", "start": 2017},
    "ETH": {"symbol": "ETHUSDT", "start": 2017},
    "SOL": {"symbol": "SOLUSDT", "start": 2020},
}


def download_month_zip(symbol: str, year: int, month: int, interval: str = "5m") -> pd.DataFrame:
    """下载单月zip, 返回DataFrame"""
    url = f"{BINANCE_ZIP}/{symbol}/{interval}/{symbol}-{interval}-{year}-{month:02d}.zip"
    try:
        r = requests.get(url, timeout=120)
        if r.status_code != 200 or len(r.content) < 500:
            return pd.DataFrame()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        df = pd.read_csv(zf.open(zf.namelist()[0]), header=None)
        if len(df) < 10:
            return pd.DataFrame()
        ts_raw = pd.to_numeric(df.iloc[:, 0])
        ts = pd.to_datetime(ts_raw // 1000 if ts_raw.iloc[-1] > 1e15 else ts_raw, unit="ms")
        df = df[[1, 2, 3, 4, 5]].copy()
        df.columns = ["open", "high", "low", "close", "vol"]
        df.index = ts
        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna()
    except Exception as e:
        print(f"  ZIP {year}-{month:02d}: {e}")
        return pd.DataFrame()


def fetch_api(symbol: str, start_ms: int, end_ms: int, interval: str = "5m") -> pd.DataFrame:
    """Binance REST API 分页抓取"""
    all_rows = []
    current = start_ms
    page = 0
    while current < end_ms:
        url = f"{BINANCE_API}?symbol={symbol}&interval={interval}&startTime={current}&limit=1000"
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            page += 1
            all_rows.extend(data)
            last_ts = data[-1][0]
            print(f"  API page {page}: {len(data)} bars, up to {pd.to_datetime(last_ts, unit='ms')}")
            if len(data) < 1000:
                break
            current = last_ts + 1
            time.sleep(0.3)
        except Exception as e:
            print(f"  API error: {e}")
            break
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows, columns=["ts","open","high","low","close","vol","a","b","c","d","e","f"])
    df = df[["ts", "open", "high", "low", "close", "vol"]]
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms")
    df = df.set_index("ts").sort_index().dropna()
    return df


def detect_gaps(df: pd.DataFrame, expected_minutes: int = 5) -> list:
    """检测数据断层"""
    if len(df) < 2:
        return []
    gaps = []
    expected = pd.Timedelta(minutes=expected_minutes)
    max_delta = expected * 1.5
    diffs = df.index.to_series().diff()
    for idx in diffs[diffs > max_delta].index:
        gap_start = df.index[df.index.get_loc(idx) - 1] + expected
        gap_end = idx - expected
        missing = int((gap_end - gap_start) / expected)
        if missing > 0:
            gaps.append((gap_start, gap_end, missing))
    return gaps


def download_coin(coin: str, interval: str = "5m", force: bool = False):
    """下载单个币种的5m数据"""
    info = COINS[coin]
    symbol = info["symbol"]
    pq_path = os.path.join(DATA_DIR, f"{coin}_{interval}.parquet")
    os.makedirs(DATA_DIR, exist_ok=True)

    # 读取已有数据
    existing = None
    if not force and os.path.exists(pq_path) and os.path.getsize(pq_path) > 100000:
        existing = pd.read_parquet(pq_path)
        tc = existing.columns[0]
        existing[tc] = pd.to_datetime(existing[tc])
        existing = existing.set_index(tc).sort_index()
        existing = existing[~existing.index.duplicated()]
        print(f"[{coin}] Loaded {len(existing):,} existing bars, last={existing.index[-1]}")

    # 下载历史zip
    now = datetime.now()
    if existing is None or force:
        print(f"[{coin}] Downloading history zips ({info['start']}-{now.year})...")
        all_parts = []
        for year in range(info["start"], now.year + 1):
            for month in range(1, 13):
                if year == now.year and month > now.month:
                    break
                df_m = download_month_zip(symbol, year, month, interval)
                if len(df_m) > 0:
                    all_parts.append(df_m)
                    print(f"  {year}-{month:02d}: {len(df_m):,} bars OK")
                time.sleep(0.05)
        if all_parts:
            existing = pd.concat(all_parts).sort_index()
            existing = existing[~existing.index.duplicated()]
            print(f"[{coin}] Zip done: {len(existing):,} total bars")
        else:
            print(f"[{coin}] WARNING: No zip data downloaded!")

    # API增量
    if existing is not None and len(existing) > 0:
        last_ts = existing.index[-1]
        gap_hours = (now - last_ts).total_seconds() / 3600
        if gap_hours > 1:
            start_ms = int(last_ts.timestamp() * 1000) + 60000
            end_ms = int(now.timestamp() * 1000)
            print(f"[{coin}] Fetching {gap_hours:.1f}h via API...")
            df_api = fetch_api(symbol, start_ms, end_ms, interval)
            if len(df_api) > 0:
                existing = pd.concat([existing, df_api]).sort_index()
                existing = existing[~existing.index.duplicated()]
                print(f"[{coin}] API added {len(df_api):,} bars, total={len(existing):,}")

    # 断层检测
    if existing is not None and len(existing) > 0:
        gaps = detect_gaps(existing, 5)
        if gaps:
            total_missing = sum(g[2] for g in gaps)
            print(f"[{coin}] Found {len(gaps)} gaps ({total_missing} missing bars), repairing...")
            for gap_start, gap_end, missing in gaps:
                s_ms = int(gap_start.timestamp() * 1000)
                e_ms = int(gap_end.timestamp() * 1000)
                df_gap = fetch_api(symbol, s_ms, e_ms, interval)
                if len(df_gap) > 0:
                    existing = pd.concat([existing, df_gap]).sort_index()
                    existing = existing[~existing.index.duplicated()]
                    print(f"  Gap {gap_start:%Y-%m-%d} ~ {gap_end:%Y-%m-%d}: +{len(df_gap)} bars")
            print(f"[{coin}] After repair: {len(existing):,} bars")

    if existing is None or len(existing) == 0:
        print(f"[{coin}] FAILED: no data")
        return

    # 保存
    df_out = existing.reset_index()
    df_out.to_parquet(pq_path, index=False)
    size_mb = os.path.getsize(pq_path) / 1024 / 1024
    print(f"[{coin}] Saved: {len(existing):,} bars, {size_mb:.1f}MB -> {pq_path}")
    print(f"       Range: {existing.index[0]} ~ {existing.index[-1]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    parser.add_argument("--coin", type=str, default="ALL", help="ETH/BTC/SOL/ALL")
    parser.add_argument("--interval", type=str, default="5m", help="K线周期")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  Data Fetcher: {args.interval} K-line, {args.coin}")
    print("=" * 60)

    coins = COINS if args.coin == "ALL" else {args.coin: COINS[args.coin]}
    for coin in coins:
        print(f"\n{'='*40}")
        download_coin(coin, args.interval, args.force)

    print(f"\n{'='*60}")
    print("  All done!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
