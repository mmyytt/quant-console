"""
多账户管理
==========
从 .env 读取账户凭证，管理账户列表与切换。

第一阶段账户:
- 主账户 (Read Only):  资产查询 / 持仓查询 / 账户状态监控
- 小资金账户 (Trading): 未来模拟盘 / 小资金实盘测试（当前阶段禁止下单）

凭证来自 .env（不硬编码）:
- OKX_MAIN_API_KEY / OKX_MAIN_SECRET_KEY / OKX_MAIN_PASSPHRASE
- OKX_TEST_API_KEY / OKX_TEST_SECRET_KEY / OKX_TEST_PASSPHRASE
"""
import os

from . import load_env
from .okx_connector import OKXConnector
from i18n import t


# 账户定义（凭证来源 .env，permission 为权限语义标记）
ACCOUNT_DEFS = [
    {
        "id": "main",
        "label_key": "lt_main_account",   # 主账户
        "permission": "read_only",        # Read Only
        "env_prefix": "OKX_MAIN",
    },
    {
        "id": "test",
        "label_key": "lt_test_account",   # 小资金账户
        "permission": "trading",          # Read + Trade
        "env_prefix": "OKX_TEST",
    },
]


class AccountManager:
    """管理多账户连接器（惰性创建）。"""

    def __init__(self):
        load_env()  # 确保 .env 已加载
        self._connectors = {}

    def list_accounts(self) -> list:
        """返回账户列表（含凭证配置状态）。

        Returns:
            [{"id", "label", "permission", "configured"}]
        """
        accounts = []
        for d in ACCOUNT_DEFS:
            api_key = os.environ.get(f"{d['env_prefix']}_API_KEY", "")
            secret = os.environ.get(f"{d['env_prefix']}_SECRET_KEY", "")
            passphrase = os.environ.get(f"{d['env_prefix']}_PASSPHRASE", "")
            accounts.append({
                "id": d["id"],
                "label": t(d["label_key"]),
                "permission": d["permission"],
                "configured": bool(api_key and secret and passphrase),
            })
        return accounts

    def get_connector(self, account_id: str) -> OKXConnector:
        """获取指定账户的连接器（惰性创建并缓存）。"""
        if account_id in self._connectors:
            return self._connectors[account_id]

        d = next((x for x in ACCOUNT_DEFS if x["id"] == account_id), None)
        if d is None:
            raise ValueError(f"Unknown account: {account_id}")

        api_key = os.environ.get(f"{d['env_prefix']}_API_KEY", "")
        secret = os.environ.get(f"{d['env_prefix']}_SECRET_KEY", "")
        passphrase = os.environ.get(f"{d['env_prefix']}_PASSPHRASE", "")

        connector = OKXConnector(api_key, secret, passphrase)
        self._connectors[account_id] = connector
        return connector
