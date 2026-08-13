"""
交易中心页面（Live Trading Dashboard）
======================================
Streamlit 页面渲染：账户列表 + 资产 + 持仓 + 交易状态模块接口。

⚠️ 第一阶段约束:
- 仅账户连接与状态展示，禁止任何下单。
- 交易引擎（模拟 / 实盘）仅为接口占位，未启用。
"""
import streamlit as st
import pandas as pd

from i18n import t
from .account_manager import AccountManager
from .portfolio_monitor import PortfolioMonitor


# ----------------------------------------------------------------------
# 显示辅助
# ----------------------------------------------------------------------
def _perm_label(permission: str) -> str:
    """权限语义标记 → 显示文本。"""
    if permission == "read_only":
        return t("lt_read_only")
    if permission == "trading":
        return t("lt_trading_perm")
    return permission


def _dir_label(direction: str) -> str:
    """持仓方向 → 显示文本。"""
    if direction == "long":
        return t("lt_long")
    if direction == "short":
        return t("lt_short")
    return direction  # net 等原样


# ----------------------------------------------------------------------
# 页面渲染
# ----------------------------------------------------------------------
def render():
    """渲染交易中心页面。"""
    st.subheader(t("lt_title"))
    st.caption(t("lt_subtitle"))

    manager = AccountManager()
    monitor = PortfolioMonitor()
    accounts = manager.list_accounts()

    # 无任何已配置账户
    if not any(a["configured"] for a in accounts):
        st.warning(t("lt_no_config"))
        _render_engine_placeholder()
        return

    # ---- 账户列表 + 切换 ----
    configured = [a for a in accounts if a["configured"]]
    labels = {
        a["id"]: f"{a['label']}  ·  {t('lt_permission')}: {_perm_label(a['permission'])}"
        for a in configured
    }

    selected_id = st.radio(
        t("lt_account_list"),
        [a["id"] for a in configured],
        format_func=lambda x: labels[x],
        horizontal=True,
        key="lt_selected_account",
    )

    connector = manager.get_connector(selected_id)

    # ---- 连接测试 ----
    col_test, col_refresh = st.columns(2)
    if col_test.button(t("lt_test_connection"), width="stretch"):
        with st.spinner(t("lt_refresh")):
            st.session_state[f"lt_conn_{selected_id}"] = connector.test_connection()

    if col_refresh.button(t("lt_refresh"), width="stretch"):
        with st.spinner(t("lt_refresh")):
            st.session_state[f"lt_snap_{selected_id}"] = monitor.get_snapshot(connector)

    # 连接状态
    conn_state = st.session_state.get(f"lt_conn_{selected_id}")
    if conn_state is not None:
        if conn_state["ok"]:
            st.success(f"{t('lt_connected')} — {conn_state['msg']}")
        else:
            st.error(f"{t('lt_disconnected')} — {conn_state['msg']}")

    # ---- 账户资产 ----
    snapshot = st.session_state.get(f"lt_snap_{selected_id}")
    if snapshot is None:
        # 首次进入自动加载一次
        with st.spinner(t("lt_refresh")):
            snapshot = monitor.get_snapshot(connector)
            st.session_state[f"lt_snap_{selected_id}"] = snapshot

    st.subheader(t("lt_account_assets"))
    balance = snapshot["balance"]
    if balance:
        c1, c2, c3 = st.columns(3)
        c1.metric(t("lt_total_equity"), f"${balance['total_equity']:,.2f}")
        c2.metric(t("lt_available_balance"), f"${balance['available']:,.2f}")
        c3.metric(t("lt_margin_balance"), f"${balance['margin_balance']:,.2f}")
    else:
        st.info(t("lt_disconnected"))

    # ---- 持仓 ----
    st.subheader(t("lt_positions"))
    positions = snapshot["positions"]
    if positions:
        df = pd.DataFrame({
            t("lt_symbol"): [p["symbol"] for p in positions],
            t("lt_direction"): [_dir_label(p["direction"]) for p in positions],
            t("lt_size"): [p["size"] for p in positions],
            t("lt_entry_price"): [f"{p['entry_price']:,.4f}" for p in positions],
            t("lt_current_price"): [f"{p['current_price']:,.4f}" for p in positions],
            t("lt_upl"): [f"{p['unrealized_pnl']:+,.4f}" for p in positions],
        })
        st.dataframe(df, width="stretch", hide_index=True)
        # 汇总未实现盈亏
        total_upl = snapshot["total_unrealized_pnl"]
        st.caption(
            f"{t('lt_upl')} · Σ {total_upl:+,.4f}  "
            f"({t('lt_positions')}: {snapshot['position_count']})"
        )
    else:
        st.info(t("lt_no_positions"))

    # ---- 交易状态模块接口（占位，禁止下单） ----
    _render_engine_placeholder()


def _render_engine_placeholder():
    """交易引擎接口占位 —— 第一阶段未启用，禁止任何下单。"""
    st.divider()
    st.subheader(t("lt_trading_status"))

    col_sim, col_live = st.columns(2)
    col_sim.info(f"🔬 {t('lt_sim_engine')}\n\n{t('lt_engine_disabled')}")
    col_live.info(f"⚡ {t('lt_live_engine')}\n\n{t('lt_engine_disabled')}")

    st.caption(
        f"{t('lt_trade_log')} / {t('lt_strategy_signal')}: {t('lt_engine_disabled')}"
    )
