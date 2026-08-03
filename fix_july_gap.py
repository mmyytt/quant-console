"""修复2026年7月数据断层 → 全周期重采样"""
import os, time, requests, pandas as pd
from datetime import datetime

DATA_DIR = r"C:\Users\myt\Desktop\量化交易\data"
BINANCE_API = "https://api.binance.com/api/v3/klines"

COINS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"
}
PERIODS = {
    "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1D": "1d"
}
JULY_START = int(datetime(2026, 7, 1).timestamp() * 1000)
JULY_END = int(datetime(2026, 7, 31).timestamp() * 1000)

os.makedirs(DATA_DIR, exist_ok=True)


def fetch_july(symbol: str) -> pd.DataFrame:
    """分页抓取7月5m数据"""
    all_rows = []; current = JULY_START; page = 0
    while current < JULY_END:
        url = f"{BINANCE_API}?symbol={symbol}&interval=5m&startTime={current}&limit=1000"
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200: break
            data = r.json()
            if not data: break
            page += 1; all_rows.extend(data)
            last_ts = data[-1][0]
            print(f"  {symbol} page {page}: {len(data)} bars, up to {pd.to_datetime(last_ts, unit='ms')}")
            if len(data) < 1000: break
            current = last_ts + 1; time.sleep(0.15)
        except Exception as e:
            print(f"  Error: {e}"); break
    if not all_rows: return pd.DataFrame()
    df = pd.DataFrame(all_rows, columns="ts open high low close vol a b c d e f".split())
    df = df[["ts","open","high","low","close","vol"]]
    for c in df.columns[1:]: df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms")
    return df.set_index("ts").sort_index().dropna()


def resample_to_periods(df_5m: pd.DataFrame) -> dict:
    """从5m重采样生成所有周期"""
    result = {"5m": df_5m}
    for label, rule in [("15m", "15min"), ("1h", "1h"), ("4h", "4h"), ("1D", "1d")]:
        df_r = df_5m.resample(rule, label="left", closed="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"
        }).dropna()
        result[label] = df_r
    return result


for coin, symbol in COINS.items():
    print(f"\n{'='*50}\n[{coin}] Fixing July 2026 gap...\n{'='*50}")

    # 1) 抓取7月5m数据
    df_july = fetch_july(symbol)
    if len(df_july) == 0:
        print(f"  FAILED: no data from API"); continue
    print(f"  Fetched {len(df_july):,} bars ({df_july.index[0]} ~ {df_july.index[-1]})")

    # 2) 读取现有5m, 合并去重
    pq_5m = os.path.join(DATA_DIR, f"{coin}_5m.parquet")
    if os.path.exists(pq_5m):
        df_existing = pd.read_parquet(pq_5m)
        tc = df_existing.columns[0]; df_existing[tc] = pd.to_datetime(df_existing[tc])
        df_existing = df_existing.set_index(tc).sort_index()
        df_existing = df_existing[~df_existing.index.duplicated()]
    else:
        df_existing = pd.DataFrame()

    if len(df_existing) > 0:
        # 移除旧7月数据, 用新数据替换
        df_existing = df_existing[(df_existing.index < "2026-07-01") | (df_existing.index > "2026-07-31")]
        df_5m_all = pd.concat([df_existing, df_july]).sort_index()
        df_5m_all = df_5m_all[~df_5m_all.index.duplicated()]
    else:
        df_5m_all = df_july

    print(f"  Merged 5m: {len(df_5m_all):,} total ({df_5m_all.index[0]} ~ {df_5m_all.index[-1]})")

    # 3) 重采样全周期
    all_periods = resample_to_periods(df_5m_all)

    # 4) 保存
    for label, df in all_periods.items():
        pq = os.path.join(DATA_DIR, f"{coin}_{label}.parquet")
        df.reset_index().to_parquet(pq, index=False)
        # 检查7月数据
        july_bars = len(df["2026-07-01":"2026-07-31"])
        size_mb = os.path.getsize(pq) / 1024 / 1024
        print(f"  {label}: {len(df):,} bars, {size_mb:.1f}MB, July={july_bars} bars, last={df.index[-1]}")

print(f"\n{'='*50}\nAll done! July 2026 gaps fixed for all timeframes.\n{'='*50}")
