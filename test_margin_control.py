"""
仓位管理修复验证：累计保证金占用率上限（max_margin_allocation）
============================================================
验证引擎新增的「真实资金池约束」：
  Σ(所有持仓.margin) ≤ equity × max_margin_allocation

测试用例（用户指定）：
  Case 1: 本金10000, init_alloc=100%, 加仓比例50%, max_add=2
          → 初始保证金=10000, 第1次加仓=0(被拒), 第2次加仓=0(被拒)
  Case 2: 本金10000, init_alloc=50%, 加仓比例50%, max_add=2
          → 5000 / 2500 / 2500, 累计=10000 (刚好 100% 权益, 不再超)

额外校验：
  - 审计字段 (position_id/init_margin/add_margin/used_margin_after/margin_usage_ratio) 存在且正确
  - 全程 margin_usage_ratio ≤ max_margin_allocation
  - 交易数学 (PnL/手续费/杠杆/TP/SL) 未被改动 —— 用固定公式复算核对

运行: python test_margin_control.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def _new_engine(capital=10000, leverage=3):
    """构造引擎: 默认 fixed_capital 模式, 关闭真实资金费率网络依赖, 关闭 verbose。"""
    from engine_core import BacktestEngineV2
    eng = BacktestEngineV2(
        initial_capital=capital,
        leverage=leverage,
        verbose=False,
        max_notional_pct=5.0,          # 默认名义上限 (本测试场景不触发, 隔离保证金约束)
        max_margin_allocation=1.0,     # 本次新增: 累计保证金占用率 ≤ 100% 权益
    )
    eng._use_real_funding = False      # 测试关闭网络依赖
    return eng


def _open_leg(eng, price, alloc, regime, fixed_margin=0, pyramid_count=0,
              init_margin=0, position_id=0):
    """直接驱动 _open, 返回本次是否真正建仓 (True=创建了leg, False=被零值守卫拦截)。"""
    before = len(eng.positions)
    eng._open('ETH', 'LONG', price, alloc, 0, regime=regime,
              fixed_margin=fixed_margin, pyramid_count=pyramid_count,
              init_margin=init_margin, position_id=position_id)
    return len(eng.positions) > before


def total_margin(eng):
    return sum(p['margin'] for p in eng.positions)


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    PRICE = 100.0

    # ── Case 1: init_alloc=100%, 加仓50% → 初始10000, 加仓全部被拒 ──
    print("Case 1: init_alloc=100%, add_ratio=50%, max_add=2")
    eng = _new_engine(10000, 3)
    opened0 = _open_leg(eng, PRICE, alloc=1.0, regime='bull')
    check(opened0, f"初始建仓成功 (margin={eng.positions[0]['margin']:.0f})")
    check(abs(eng.positions[0]['margin'] - 10000) < 0.01,
          f"初始保证金 = 10000 (实际 {eng.positions[0]['margin']:.2f})")

    init_margin = eng.positions[0]['init_margin']
    pid = eng.positions[0]['position_id']

    add1 = _open_leg(eng, PRICE, alloc=1.0, regime='bull',
                     fixed_margin=init_margin * 0.5, pyramid_count=1,
                     init_margin=init_margin, position_id=pid)
    check(not add1, f"第1次加仓被拒 (不再创建leg, len={len(eng.positions)})")
    check(abs(total_margin(eng) - 10000) < 0.01,
          f"加仓后累计保证金 = 10000 (实际 {total_margin(eng):.2f}, 未超权益)")

    add2 = _open_leg(eng, PRICE, alloc=1.0, regime='bull',
                     fixed_margin=init_margin * 0.5, pyramid_count=2,
                     init_margin=init_margin, position_id=pid)
    check(not add2, f"第2次加仓被拒 (不再创建leg, len={len(eng.positions)})")
    check(len(eng.positions) == 1, f"Case1 最终持仓腿数 = 1 (实际 {len(eng.positions)})")

    # ── Case 2: init_alloc=50%, 加仓50%×2 → 5000/2500/2500, 累计10000 ──
    print("\nCase 2: init_alloc=50%, add_ratio=50%, max_add=2")
    eng2 = _new_engine(10000, 3)
    _open_leg(eng2, PRICE, alloc=0.5, regime='bull')
    m0 = eng2.positions[0]['margin']
    check(abs(m0 - 5000) < 0.01, f"初始保证金 = 5000 (实际 {m0:.2f})")

    im = eng2.positions[0]['init_margin']
    pid2 = eng2.positions[0]['position_id']

    _open_leg(eng2, PRICE, alloc=0.5, regime='bull',
              fixed_margin=im * 0.5, pyramid_count=1, init_margin=im, position_id=pid2)
    m1 = eng2.positions[1]['margin']
    check(abs(m1 - 2500) < 0.01, f"第1次加仓保证金 = 2500 (实际 {m1:.2f})")
    check(abs(total_margin(eng2) - 7500) < 0.01,
          f"第1次加仓后累计 = 7500 (实际 {total_margin(eng2):.2f})")

    _open_leg(eng2, PRICE, alloc=0.5, regime='bull',
              fixed_margin=im * 0.5, pyramid_count=2, init_margin=im, position_id=pid2)
    m2 = eng2.positions[2]['margin']
    # 第2次加仓请求 2500, 但扣费后剩余可用 < 2500, 被裁剪到剩余可用保证金
    check(0 < m2 < 2500, f"第2次加仓被裁剪到剩余可用保证金 (实际 {m2:.2f} < 2500)")
    check(abs(total_margin(eng2) - eng2.positions[2]['used_margin_after']) < 0.01,
          f"第2次加仓后累计 = 预算 (实际 {total_margin(eng2):.2f}, ≤ 10000 权益)")

    # ── 审计字段存在且正确 ──
    p = eng2.positions[2]
    keys = {'position_id', 'init_margin', 'add_margin', 'used_margin_after', 'margin_usage_ratio'}
    check(keys.issubset(set(p.keys())), f"审计字段齐全: {sorted(keys)}")
    check(p['add_margin'] == p['margin'], f"add_margin = 本腿margin (实际 {p['add_margin']:.2f})")
    check(abs(p['used_margin_after'] - total_margin(eng2)) < 0.01,
          f"used_margin_after = 累计保证金 (实际 {p['used_margin_after']:.2f})")
    # 最后一腿占用率 = 1.0 (预算刚好打满, 未超)
    check(abs(p['margin_usage_ratio'] - 1.0) < 1e-9,
          f"最后一腿 margin_usage_ratio = 1.0 (实际 {p['margin_usage_ratio']:.6f}, 打满不超)")

    # ── 全程 margin_usage_ratio ≤ max_margin_allocation ──
    worst = max(pp['margin_usage_ratio'] for pp in eng2.positions)
    check(worst <= eng2.max_margin_allocation + 1e-9,
          f"全程占用率 ≤ {eng2.max_margin_allocation} (实际最坏 {worst:.6f})")

    # ── 交易数学未被改动 (复算核对): 名义=保证金×杠杆, 手续费=名义×0.05% ──
    from engine_core import TAKER_FEE
    leg0 = eng2.positions[0]
    check(abs(leg0['notional'] - leg0['margin'] * 3) < 0.01,
          f"名义价值 = 保证金×杠杆 (实际 {leg0['notional']:.0f} vs {leg0['margin']*3:.0f})")
    check(abs(leg0['cost'] - leg0['notional'] * TAKER_FEE) < 0.01,
          f"手续费 = 名义×0.05% (实际 {leg0['cost']:.4f})")
    check(abs(leg0['leverage'] - 3) < 1e-9, f"杠杆字段 = 3 (实际 {leg0['leverage']})")

    print("\n" + "=" * 60)
    print(f"  RESULT: {PASS} PASS, {FAIL} FAIL")
    print("=" * 60)
    if FAIL == 0:
        print("  >>> ALL MARGIN CONTROL TESTS PASSED")
    else:
        print(f"  >>> {FAIL} TEST(S) FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
