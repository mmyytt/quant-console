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


def _download_zip(symbol: str, sy: int, ey: int, em: int, interval: str = "15m") -> pd.DataFrame:
    base = f"{BINANCE_BASE}/{symbol}/{interval}/{symbol}-{interval}-"; all_dfs = []
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
def fetch_latest_klines_with_fallback(coin: str, limit: int = 300, interval: str = "15m") -> dict:
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
            url = f"{okx_base}/api/v5/market/candles?instId={okx_inst}&bar={interval}&limit={min(limit, 300)}"
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
                url = f"{bnb_url}/api/v3/klines?symbol={bnb_symbol}&interval={interval}&startTime={start_ms}&endTime={now_ms}&limit=1000"
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
    # CoinGecko OHLC 仅提供 4h/日粒度, 无 5m; 5m 请求跳过避免返回错误粒度
    if interval != "5m":
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
    else:
        print("[CoinGecko] skipped (no 5m OHLC)")

    # === Source 3: yfinance ===
    try:
        yf_symbol = info["yf"]
        print(f"[yfinance] Trying {yf_symbol}...")
        import yfinance as yf
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="60d", interval=interval)
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


def fetch_funding_history(coin: str, use_cache: bool = True) -> pd.Series:
    """
    拉取 OKX 永续合约真实历史资金费率 (公开只读, 无需凭证)。

    P2-1/P2-2: 用 SWAP 合约 instId (`{coin}-USDT-SWAP`) 的 fundingRate,
    替代引擎内硬编码 0.01% 费率。SWAP 上线前的区间无数据 → funding=0。

    Args:
        coin: ETH/BTC/SOL
        use_cache: 优先读本地 parquet 缓存
    Returns:
        pd.Series: index=fundingTime (datetime), values=fundingRate (小数)
    """
    info = COINS.get(coin)
    if not info:
        return pd.Series(dtype=float)
    swap_inst = info["okx"] + "-SWAP"  # 如 "ETH-USDT-SWAP"

    os.makedirs(DATA_DIR, exist_ok=True)
    pq_path = os.path.join(DATA_DIR, f"{coin}_funding.parquet")

    # 1. 本地缓存优先
    if use_cache and os.path.exists(pq_path) and os.path.getsize(pq_path) > 0:
        try:
            cached = pd.read_parquet(pq_path)
            if isinstance(cached, pd.DataFrame):
                cached = cached.iloc[:, 0]
            cached.index = pd.to_datetime(cached.index)
            print(f"[Funding] {coin}: cache {len(cached)} rates, {cached.index[0]} ~ {cached.index[-1]}")
            return cached.sort_index()
        except Exception as e:
            print(f"[Funding] {coin}: cache read failed ({e}), re-fetch")

    # 2. OKX 分页拉取 (response 最新在前, 用 before 翻到更旧)
    rows = []
    for okx_base in ["https://www.okx.com", "https://aws.okx.com"]:
        try:
            rows = []
            before = ""
            for _ in range(2000):  # 最多 2000 页 (20万条, 足够覆盖全历史)
                url = f"{okx_base}/api/v5/public/funding-rate-history?instId={swap_inst}&limit=100"
                if before:
                    url += f"&before={before}"
                r = requests.get(url, timeout=20, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json",
                })
                if r.status_code != 200:
                    break
                data = r.json()
                if data.get("code") != "0" or not data.get("data"):
                    break
                batch = data["data"]
                for x in batch:
                    rows.append({"ts": int(x["fundingTime"]), "rate": float(x["fundingRate"])})
                if len(batch) < 100:
                    break
                before = batch[-1]["fundingTime"]
            if rows:
                break
        except Exception as e:
            print(f"[Funding] {okx_base} EXCEPTION: {type(e).__name__}: {e}")

    if not rows:
        print(f"[Funding] {coin}: no funding data (SWAP 可能未上线或网络失败)")
        return pd.Series(dtype=float)

    df = pd.DataFrame(rows).drop_duplicates("ts")
    s = pd.Series(df["rate"].values,
                  index=pd.to_datetime(df["ts"], unit="ms")).sort_index()
    try:
        s.to_frame("fundingRate").to_parquet(pq_path)
    except Exception as e:
        print(f"[Funding] {coin}: cache write failed ({e})")
    print(f"[Funding] {coin}: {len(s)} rates, {s.index[0]} ~ {s.index[-1]}")
    return s


def ensure_data(coin: str, interval: str = "15m") -> str:
    """
    确保数据存在且更新。interval: '15m' | '5m' (P1-1 真实5m加载)。

    流程:
      1. 读本地parquet缓存 → 历史数据
      2. 若无历史, 下载Binance zip
      3. 三重轮询抓增量 → 合并去重
      4. 保存并返回路径
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    pq_path = os.path.join(DATA_DIR, f"{coin}_{interval}.parquet")
    info = COINS.get(coin)
    if not info: raise ValueError(f"Unknown coin: {coin}")

    # 1. 历史缓存
    existing, last_ts = _read_existing(pq_path)
    if existing is not None:
        print(f"[DataLoader] {coin}: cache {len(existing):,} bars, last={last_ts}")

    # 2. 无历史 → 下载zip (5m 只回补近2年, 避免全历史5m下载过大)
    if existing is None or len(existing) < 1000:
        print(f"[DataLoader] {coin}: downloading history zip ({interval})...")
        now = datetime.now()
        start_y = info["start"]
        if interval == "5m":
            start_y = max(start_y, now.year - 2)
        dz = _download_zip(info["symbol"], start_y, now.year, now.month, interval)
        if len(dz) > 0: existing = dz; last_ts = existing.index.max()

    # 3. 三重轮询增量
    result = fetch_latest_klines_with_fallback(coin, limit=300, interval=interval)
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

    # 5. 断层检测+修复
    df_all = repair_gaps(coin, df_all, interval)

    # 6. 保存
    df_all.reset_index().to_parquet(pq_path, index=False)
    return pq_path


def find_gaps(df: pd.DataFrame, expected_interval_min: int = 15) -> list:
    """
    检测K线数据断层。

    Args:
        df: DatetimeIndex的OHLCV DataFrame
        expected_interval_min: 预期的K线间隔(分钟), 默认15min
    Returns:
        [(gap_start, gap_end, missing_bars), ...] 断层区间列表
    """
    if len(df) < 2:
        return []
    gaps = []
    expected_delta = pd.Timedelta(minutes=expected_interval_min)
    # 允许10%容差
    max_delta = expected_delta * 1.5
    time_diffs = df.index.to_series().diff()
    gap_mask = time_diffs > max_delta
    for idx in gap_mask[gap_mask].index:
        gap_start = df.index[df.index.get_loc(idx) - 1] + expected_delta
        gap_end = idx - expected_delta
        missing = int((gap_end - gap_start) / expected_delta)
        if missing > 0:
            gaps.append((gap_start, gap_end, missing))
    return gaps


def repair_gaps(coin: str, df: pd.DataFrame, expected_interval: str = "15m") -> pd.DataFrame:
    """
    检测并修复数据断层。对每个缺口自动从Binance API补全。

    Args:
        coin: ETH/BTC/SOL
        df: 现有DataFrame (DatetimeIndex)
        expected_interval: K线间隔
    Returns:
        修复后的DataFrame
    """
    interval_map = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    gap_min = interval_map.get(expected_interval, 15)
    gaps = find_gaps(df, gap_min)

    if not gaps:
        print(f"[GapCheck] {coin}: no gaps found")
        return df

    total_missing = sum(g[2] for g in gaps)
    print(f"[GapCheck] {coin}: found {len(gaps)} gaps, {total_missing} missing bars total")

    info = COINS.get(coin)
    if not info:
        return df

    symbol = info["symbol"]
    new_parts = []

    for gap_start, gap_end, missing in gaps:
        start_ms = int(gap_start.timestamp() * 1000)
        end_ms = int(gap_end.timestamp() * 1000)
        print(f"  Gap: {gap_start} ~ {gap_end} ({missing} bars)")

        # 分页抓取缺口数据
        all_rows = []
        current = start_ms
        while current < end_ms:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={expected_interval}&startTime={current}&limit=1000"
            try:
                r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and isinstance(r.json(), list):
                    data = r.json()
                    if not data:
                        break
                    all_rows.extend(data)
                    last_ts = data[-1][0]
                    if len(data) < 1000:
                        break
                    current = last_ts + 1
                    time.sleep(0.3)
                else:
                    break
            except Exception as e:
                print(f"    API error: {e}")
                break

        if all_rows:
            df_gap = pd.DataFrame(all_rows, columns=[
                "ts", "open", "high", "low", "close", "vol",
                "a", "b", "c", "d", "e", "f"
            ])
            df_gap = df_gap[["ts", "open", "high", "low", "close", "vol"]]
            for col in df_gap.columns[1:]:
                df_gap[col] = pd.to_numeric(df_gap[col], errors="coerce")
            df_gap["ts"] = pd.to_datetime(pd.to_numeric(df_gap["ts"]), unit="ms")
            df_gap = df_gap.set_index("ts").dropna()
            new_parts.append(df_gap)
            print(f"    Repaired: {len(df_gap)} bars")

    if new_parts:
        df_repaired = pd.concat([df] + new_parts).sort_index()
        df_repaired = df_repaired[~df_repaired.index.duplicated()]
        print(f"[GapCheck] {coin}: {len(df)} -> {len(df_repaired)} bars after repair")
        return df_repaired

    return df


def force_redownload(coin: str) -> str:
    """删除本地缓存, 强制重新下载全部数据"""
    import os as _os
    pq_path = _os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    if _os.path.exists(pq_path):
        _os.remove(pq_path)
        print(f"[ForceRedownload] {coin}: deleted cache")
    return ensure_data(coin)


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
