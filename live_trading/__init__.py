"""
Live Trading 模块
=================
第一阶段: 账户连接与状态展示（只读，禁止交易）。

子模块:
- okx_connector:   OKX API 只读连接器
- account_manager: 多账户管理
- portfolio_monitor: 组合快照监控
- trading_dashboard: Streamlit 页面渲染
"""
import os
import sys

# 确保父目录（量化交易目录）在 sys.path 中，以便 import i18n
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


def load_env(env_path=None):
    """加载 .env 到环境变量（幂等，setdefault）。

    Args:
        env_path: .env 文件路径，默认取父目录下的 .env

    Returns:
        env_path 实际使用的 .env 路径
    """
    if env_path is None:
        env_path = os.path.join(_PARENT, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    return env_path
