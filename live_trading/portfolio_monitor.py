"""
组合监控
========
聚合账户余额 / 持仓 / 未实现盈亏快照。
"""
from .okx_connector import OKXConnector


class PortfolioMonitor:
    """账户组合快照监控器。"""

    def get_snapshot(self, connector: OKXConnector) -> dict:
        """获取指定账户的完整快照。

        Args:
            connector: OKX 只读连接器

        Returns:
            {
                "balance": dict | None,
                "positions": list,
                "position_count": int,
                "total_unrealized_pnl": float,
            }
        """
        balance = connector.get_balance()
        positions = connector.get_positions()
        total_upl = sum(p["unrealized_pnl"] for p in positions)
        # 保证金余额优先取持仓 margin 聚合（比 imr 字段更准确反映占用）
        margin_balance = sum(p["margin"] for p in positions)
        if balance is not None:
            balance["margin_balance"] = margin_balance
        return {
            "balance": balance,
            "positions": positions,
            "position_count": len(positions),
            "total_unrealized_pnl": total_upl,
        }
