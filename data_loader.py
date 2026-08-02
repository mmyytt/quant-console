"""
数据自动下载器 v4 — 三重数据源轮询 (Fallback)
==============================================
优先级: OKX公开API > CoinGecko > yfinance
历史层: Binance月度zip → 本地parquet
增量层: 三重轮询抓最近60天 → 合并去重
"""
import os, sys, time, requests, pandas as pd, zipfile, io, json
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BINANCE_BASE = "https://data.binance.vision/data/spot/monthly/klines"

COINS = {
    "ETH": {"symbol": "ETHUSDT",   "okx": "ETH-USDT",    "cg": "ethereum",  "yf": "ETH-USD", "start": 2017},
    "BTC": {"symbol": "BTCUSDT",   "okx": "BTC-USDT",    "cg": "bitcoin",   "yf": "BTC-USD", "start": 2017},
    "SOL": {"symbol": "SOLUSDT",   "okx": "SOL-USDT",    "cg": "solana",    "yf": "SOL-USD", "start": 2020},
}


def _read_existing(pq_path: str):
    if not os.path.exists(pq_path) or os.path.getsize(pq_path) < 100000:
        return None, None
    df = pd.read_parquet(pq_path)
    tc = df.columns[0]; df[tc] = pd.to_datetime(df[tc])
    df = df.set_index(tc); df = df[~df.index.duplicated()]; df = df.sort_index()
    return df, df.index.max()


def _download_zip(symbol: str, sy: int, ey: int, em: int) -> pd.DataFrame:
    base = f"{BINANCE_BASE}/{symbol}/15m/{symbol}-15m-"; all_dfs = []
    for y in range(sy, ey+1):
        for m in range(1, 13):
            if y == ey and m > em: break
            try:
                r = requests.get(f"{base}{y}-{m:02d}.zip", timeout=60)
                if r.status_code != 200 or len(r.content) < 500: continue
                zf = zipfile.ZipFile(io.BytesIO(r.content))
                df = pd.read_csv(zf.open(zf.namelist()[0]), header=None)
                if len(df) < 10: continue
                tr = pd.to_numeric(df.iloc[:,0])
                ts = pd.to_datetime(tr//1000 if tr.iloc[-1]>1e15 else tr, unit="ms")
                df = df[[1,2,3,4,5]]; df.columns=["open","high","low","close","vol"]; df.index=ts
                for c in df.columns: df[c]=pd.to_numeric(df[c],errors="coerce")
                df=df.dropna()
                if len(df)>0: all_dfs.append(df)
            except: pass; time.sleep(0.05)
    if all_dfs: df=pd.concat(all_dfs).sort_index(); return df[~df.index.duplicated()]
    return pd.DataFrame()


# ============================================================
# 三重数据源轮询
# ============================================================
def fetch_latest_klines_with_fallback(coin: str, limit: int = 300) -> dict:
    """
    三重轮询抓取最新K线。

    Returns:
        {"source": "okx"|"coingecko"|"yfinance"|"failed",
         "df": DataFrame or None,
         "last_ts": "2026-08-02 12:00" or None}
    """
    info = COINS.get(coin)
    if not info: return {"source": "failed", "df": None, "last_ts": None}

    # === Source 1: OKX Public API (多endpoint轮换) ===
    okx_inst = info["okx"]
    okx_endpoints = [
        "https://www.okx.com",
        "https://aws.okx.com",
    ]
    for okx_base in okx_endpoints:
        try:
            url = f"{okx_base}/api/v5/market/candles?instId={okx_inst}&bar=15m&limit={min(limit, 300)}"
            print(f"[OKX] Trying {okx_base}...")
            r = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/json",
            })
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == "0" and data.get("data"):
                    rows = []
                    for x in reversed(data["data"]):
                        rows.append({
                            "ts": int(x[0]), "open": float(x[1]), "high": float(x[2]),
                            "low": float(x[3]), "close": float(x[4]), "vol": float(x[5]),
                        })
                    df = pd.DataFrame(rows)
                    df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms")
                    df = df.set_index("ts").sort_index()
                    print(f"[OKX] SUCCESS ({okx_base}): {len(df)} bars, last={df.index[-1]}")
                    return {"source": "okx", "df": df, "last_ts": str(df.index[-1])}
            print(f"[OKX] {okx_base}: HTTP {r.status_code}, body={r.text[:200]}")
        except Exception as e:
            print(f"[OKX] {okx_base}: EXCEPTION {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()

    # === Source 1.5: Binance Public API ===
    try:
        bnb_symbol = info["symbol"]
        now_ms = int(datetime.now().timestamp() * 1000)
        start_ms = int((datetime.now() - timedelta(days=60)).timestamp() * 1000)
        for bnb_url in ["https://api.binance.com", "https://api1.binance.com", "https://api3.binance.com"]:
            try:
                url = f"{bnb_url}/api/v3/klines?symbol={bnb_symbol}&interval=15m&startTime={start_ms}&endTime={now_ms}&limit=1000"
                print(f"[Binance] Trying {bnb_url}...")
                r = requests.get(url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                })
                if r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) > 0:
                    data = r.json()
                    df = pd.DataFrame(data, columns=["ts","open","high","low","close","vol","a","b","c","d","e","f"])
                    df = df[["ts","open","high","low","close","vol"]]
                    for col in df.columns[1:]: df[col] = pd.to_numeric(df[col], errors="coerce")
                    df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms")
                    df = df.set_index("ts").sort_index().dropna()
                    print(f"[Binance] SUCCESS ({bnb_url}): {len(df)} bars, last={df.index[-1]}")
                    return {"source": "binance", "df": df, "last_ts": str(df.index[-1])}
            except Exception as e:
                print(f"[Binance] {bnb_url}: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[Binance] EXCEPTION: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()

    # === Source 2: CoinGecko ===
    try:
        cg_id = info["cg"]
        url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=60"
        print(f"[CoinGecko] Trying {url[:80]}...")
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close"])
                # CoinGecko uses precise timestamps; approximate vol as 0
                df["vol"] = 0.0  # CoinGecko doesn't provide volume in OHLC
                df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms")
                df = df.set_index("ts").sort_index()
                print(f"[CoinGecko] SUCCESS: {len(df)} bars, last={df.index[-1]}")
                return {"source": "coingecko", "df": df, "last_ts": str(df.index[-1])}
        print(f"[CoinGecko] FAILED: HTTP {r.status_code}")
    except Exception as e:
        print(f"[CoinGecko] EXCEPTION: {e}")

    # === Source 3: yfinance ===
    try:
        yf_symbol = info["yf"]
        print(f"[yfinance] Trying {yf_symbol}...")
        import yfinance as yf
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="60d", interval="15m")
        if df is not None and len(df) > 0:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"vol"})
            for c in ["open","high","low","close","vol"]:
                if c not in df.columns: df[c] = 0.0
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df[["open","high","low","close","vol"]].dropna()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.sort_index()
            print(f"[yfinance] SUCCESS: {len(df)} bars, last={df.index[-1]}")
            return {"source": "yfinance", "df": df, "last_ts": str(df.index[-1])}
        print(f"[yfinance] FAILED: empty df")
    except Exception as e:
        print(f"[yfinance] EXCEPTION: {e}")

    return {"source": "failed", "df": None, "last_ts": None}


def ensure_data(coin: str) -> str:
    """
    确保数据存在且更新。

    流程:
      1. 读本地parquet缓存 → 历史数据
      2. 若无历史, 下载Binance zip
      3. 三重轮询抓增量 → 合并去重
      4. 保存并返回路径
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    pq_path = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    info = COINS.get(coin)
    if not info: raise ValueError(f"Unknown coin: {coin}")

    # 1. 历史缓存
    existing, last_ts = _read_existing(pq_path)
    if existing is not None:
        print(f"[DataLoader] {coin}: cache {len(existing):,} bars, last={last_ts}")

    # 2. 无历史 → 下载zip
    if existing is None or len(existing) < 1000:
        print(f"[DataLoader] {coin}: downloading history zip...")
        now = datetime.now()
        dz = _download_zip(info["symbol"], info["start"], now.year, now.month)
        if len(dz) > 0: existing = dz; last_ts = existing.index.max()

    # 3. 三重轮询增量
    result = fetch_latest_klines_with_fallback(coin, limit=300)
    df_new = result["df"]

    if df_new is not None and len(df_new) > 0:
        print(f"[DataLoader] {coin}: via {result['source']}, {len(df_new)} bars, last={result['last_ts']}")

        if existing is not None:
            # 合并: 历史早于增量起点 + 增量全量
            new_start = df_new.index.min()
            hist = existing[existing.index < new_start]
            df_all = pd.concat([hist, df_new]).sort_index()
            df_all = df_all[~df_all.index.duplicated()]
        else:
            df_all = df_new
    elif existing is not None:
        df_all = existing
        print(f"[DataLoader] {coin}: all sources failed, using cached data")
    else:
        raise RuntimeError(f"[DataLoader] {coin}: all sources failed and no cache")

    # 4. 校验
    last_date = df_all.index.max()
    gap_h = (datetime.now() - last_date).total_seconds() / 3600
    status = "STALE" if gap_h > 24 else "FRESH"
    print(f"[DataLoader] {coin}: {status} last={last_date} gap={gap_h:.1f}h total={len(df_all):,}")

    # 5. 保存
    df_all.reset_index().to_parquet(pq_path, index=False)
    return pq_path


def get_data_freshness(coin: str) -> dict:
    """检查数据新鲜度"""
    pq_path = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    _, last_ts = _read_existing(pq_path)
    if last_ts is None: return {"status": "no_data", "last_ts": None, "gap_hours": 9999}
    gap_h = (datetime.now() - last_ts).total_seconds() / 3600
    return {"status": "stale" if gap_h > 24 else "fresh", "last_ts": str(last_ts), "gap_hours": round(gap_h, 1)}


if __name__ == "__main__":
    for coin in COINS:
        ensure_data(coin)
    print("All data ready!")
