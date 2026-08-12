"""
OKX API Connector — 只读连接器
===============================
第一阶段: 仅提供账户查询接口（余额 / 持仓 / 状态）。

⚠️ 安全约束:
- 本模块不提供任何下单接口（无 place_order / create_order）。
- 未来如需交易，必须在独立模块中显式开启，并受风控约束。
"""
import requests
import json
import base64
import hmac
import hashlib
from datetime import datetime, timezone

from i18n import t

BASE_URL = "https://www.okx.com"


class OKXConnector:
    """OKX 只读 API 客户端。"""

    def __init__(self, api_key: str, secret: str, passphrase: str):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.base_url = BASE_URL

    # ------------------------------------------------------------------
    # 签名与请求
    # ------------------------------------------------------------------
    def _sign(self, method: str, path: str, body: str = ""):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
             str(datetime.now(timezone.utc).microsecond // 1000).zfill(3) + "Z"
        sign_str = ts + method + path + body
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), sign_str.encode(), hashlib.sha256).digest()
        ).decode()
        return ts, sign

    def _request(self, method: str, path: str, body: str = None):
        """发送签名请求，返回 JSON（异常时返回 {"code": "-1"}）。"""
        ts, sign = self._sign(method, path, body or "")
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        url = self.base_url + path
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=15)
            else:
                resp = requests.post(url, headers=headers, data=body or "", timeout=15)
            return resp.json()
        except Exception as e:
            return {"code": "-1", "msg": str(e)}

    # ------------------------------------------------------------------
    # 账户接口（只读）
    # ------------------------------------------------------------------
    def test_connection(self) -> dict:
        """连接测试：验证 API 凭证有效性。"""
        r = self._request("GET", "/api/v5/account/balance")
        if r.get("code") == "0":
            return {"ok": True, "msg": t("lt_connection_success")}
        # OKX 返回的 msg 为交易所原始信息（英文），直接透传
        return {"ok": False, "msg": r.get("msg") or t("lt_connection_failed")}

    def get_balance(self) -> dict:
        """获取账户余额快照。

        Returns:
            {"total_equity", "available", "margin_balance", "cash"} 或 None（失败）
        """
        r = self._request("GET", "/api/v5/account/balance")
        if r.get("code") != "0" or not r.get("data"):
            return None
        d = r["data"][0]
        # 可用余额聚合自 details（各币种 availEq 的 USD 折算）
        details = d.get("details", [])
        total_avail = sum(float(x.get("availEq", 0) or 0) for x in details)
        total_cash = sum(float(x.get("eqUsd", 0) or 0) for x in details)
        return {
            "total_equity": float(d.get("totalEq", 0) or 0),
            "available": total_avail,
            "margin_balance": float(d.get("imr", 0) or 0),
            "cash": total_cash,
        }

    def get_positions(self) -> list:
        """获取当前持仓（含未实现盈亏）。

        Returns:
            [{"symbol", "direction", "size", "entry_price", "current_price",
              "unrealized_pnl", "margin"}] 或 []（无持仓/失败）
        """
        r = self._request("GET", "/api/v5/account/positions")
        if r.get("code") != "0" or not r.get("data"):
            return []
        positions = []
        for p in r["data"]:
            size = float(p.get("pos", 0) or 0)
            if size <= 0:
                continue
            positions.append({
                "symbol": p.get("instId", ""),
                "direction": p.get("posSide", "net"),
                "size": size,
                "entry_price": float(p.get("avgPx", 0) or 0),
                "current_price": float(p.get("last", 0) or 0),
                "unrealized_pnl": float(p.get("upl", 0) or 0),
                "margin": float(p.get("margin", 0) or 0),
            })
        return positions

    def get_account_status(self) -> dict:
        """获取账户状态（基于余额接口是否可访问判断）。"""
        balance = self.get_balance()
        if balance is None:
            return {"connected": False}
        return {"connected": True, "total_equity": balance["total_equity"]}
