"""
Phase 1 解析测试: parse_hypothesis_array + _spec_to_hyp + _position_params_from
===============================================================================
覆盖: 顶层 list[dict] · candidates/list/strategies 包装 · markdown 围栏 ·
      字段顺序无关 · 嵌套 position/risk · move_stop 别名 → _pyr_trail · 死 key 过滤
运行: python test_research_parser_position.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    import research_loop as rl

    # 1) 顶层数组 list[dict]
    top = ('[{"hypothesis":"h1","indicators":["EMA 双均线"],'
           '"position":{"_init_alloc_pct":50}},'
           '{"hypothesis":"h2","indicators":["RSI 相对强弱"]}]')
    arr = rl.parse_hypothesis_array(top)
    assert len(arr) == 2 and arr[0]["hypothesis"] == "h1", f"顶层数组解析失败: {arr}"
    assert isinstance(arr[0]["position"], dict), "position 应保留为 dict"
    print("[OK] 1. 顶层 list[dict] 数组解析正确")

    # 2) candidates 包装
    a1 = rl.parse_hypothesis_array(
        '{"candidates": [{"indicators": ["EMA 双均线"]}, {"indicators": ["RSI 相对强弱"]}]}')
    assert len(a1) == 2 and a1[0]["indicators"] == ["EMA 双均线"], f"candidates 包装失败: {a1}"

    # 3) list 包装
    a2 = rl.parse_hypothesis_array('{"list": [{"indicators": ["EMA 双均线"]}]}')
    assert len(a2) == 1 and a2[0]["indicators"] == ["EMA 双均线"], f"list 包装失败: {a2}"

    # 4) strategies 包装
    a3 = rl.parse_hypothesis_array('{"strategies": [{"indicators": ["斐波那契回调"]}]}')
    assert len(a3) == 1 and a3[0]["indicators"] == ["斐波那契回调"], f"strategies 包装失败: {a3}"
    print("[OK] 2. candidates / list / strategies 三种包装均能解析")

    # 5) markdown 围栏 + 解释文字
    a4 = rl.parse_hypothesis_array(
        '以下是候选策略：\n```json\n{"strategies": [{"indicators": ["量比 Volume Ratio"]}]}\n```\n共1个。')
    assert len(a4) == 1, f"markdown 围栏解析失败: {a4}"
    print("[OK] 3. markdown 围栏 + 解释文字中提取数组正确")

    # 6) 字段顺序无关: 同一逻辑字段不同顺序 → 等价结果
    s_order1 = {"indicators": ["EMA 双均线"], "leverage": 3,
                "position": {"_init_alloc_pct": 70, "_enable_pyramiding": True}, "params": {}}
    s_order2 = {"position": {"_enable_pyramiding": True, "_init_alloc_pct": 70},
                "leverage": 3, "params": {}, "indicators": ["EMA 双均线"]}
    h1 = rl._spec_to_hyp(s_order1, ["EMA 双均线"], "ETH")
    h2 = rl._spec_to_hyp(s_order2, ["EMA 双均线"], "ETH")
    assert h1["leverage"] == h2["leverage"] == 3, "字段顺序不应影响 leverage"
    p1 = rl._position_params_from(s_order1)
    p2 = rl._position_params_from(s_order2)
    assert p1 == p2 == {"_init_alloc_pct": 70, "_enable_pyramiding": True}, \
        f"字段顺序应无关: {p1} vs {p2}"
    print("[OK] 4. 字段顺序无关: 不同顺序产生相同 position/leverage")

    # 7) 嵌套 position + risk 兜底读取 leverage/tp/sl
    s = {"indicators": ["EMA 双均线"],
         "risk": {"leverage": 5, "tp_pct": 9.0, "sl_pct": 4.5},
         "position": {"_init_alloc_pct": 50, "_enable_pyramiding": True,
                      "_pyr_add_pct": 0.5, "_pyr_max": 2}}
    h = rl._spec_to_hyp(s, ["EMA 双均线"], "ETH")
    assert h["leverage"] == 5 and h["tp_pct"] == 9.0 and h["sl_pct"] == 4.5, \
        f"risk 兜底读取失败: {h}"
    pos = rl._position_params_from(s)
    assert pos["_init_alloc_pct"] == 50 and pos["_enable_pyramiding"] is True
    assert pos["_pyr_add_pct"] == 0.5 and pos["_pyr_max"] == 2
    print("[OK] 5. 嵌套 position/risk 解析 + leverage/tp/sl 兜底读取正确")

    # 8) move_stop 别名 → _pyr_trail (顶层 + 嵌套)
    p8 = rl._position_params_from(
        {"indicators": ["EMA 双均线"], "move_stop": True,
         "position": {"_init_alloc_pct": 30}})
    assert p8.get("_pyr_trail") is True, f"move_stop 顶层别名失败: {p8}"
    p9 = rl._position_params_from(
        {"indicators": ["EMA 双均线"], "position": {"_init_alloc_pct": 30, "move_stop": True}})
    assert p9.get("_pyr_trail") is True, f"move_stop 嵌套别名失败: {p9}"
    print("[OK] 6. move_stop 别名映射到 _pyr_trail (顶层/嵌套均生效)")

    # 9) 无仓位参数 → None
    assert rl._position_params_from({"indicators": ["EMA 双均线"]}) is None
    print("[OK] 7. 无仓位参数返回 None")

    # 10) 死 key 过滤: 只保留 POSITION_PARAM_KEYS
    p10 = rl._position_params_from(
        {"indicators": ["EMA 双均线"],
         "position": {"_init_alloc_pct": 30, "_fake_key": 1}})
    assert p10 == {"_init_alloc_pct": 30}, f"死 key 应被过滤: {p10}"
    print("[OK] 8. 死 key 被过滤，仅保留引擎真实读取的仓位 key")

    print("\nALL PARSER POSITION TESTS PASSED")


if __name__ == "__main__":
    main()
