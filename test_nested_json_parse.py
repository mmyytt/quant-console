"""
AI 研究舱 JSON 提取回归测试（嵌套数组 / 顶层数组优先）
========================================================
覆盖 parse_hypothesis_array_diag 的 5 类输入，全部应解析出候选策略 dict：
  案例1 标准数组
  案例2 内部 indicators 数组（不得误取 list[str]）
  案例3 markdown 包裹
  案例4 文字说明 + JSON
  案例5 多层嵌套 {"result":{"strategies":[{...}]}}
运行: python test_nested_json_parse.py
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

    # 案例1：标准数组
    t1 = '[{"hypothesis":"a"}]'
    arr1, d1 = rl.parse_hypothesis_array_diag(t1)
    assert arr1 == [{"hypothesis": "a"}], f"案例1失败: {d1}"
    assert d1["error"] is None

    # 案例2：内部 indicators 数组（关键回归：不得误取 ["EMA","RSI"]）
    t2 = '[{"hypothesis":"a","indicators":["EMA","RSI"]}]'
    arr2, d2 = rl.parse_hypothesis_array_diag(t2)
    assert len(arr2) == 1 and arr2[0]["indicators"] == ["EMA", "RSI"], f"案例2误取内部数组: {d2}"
    assert d2["extracted"].startswith('[{'), "案例2 应提取顶层 list[dict]，而非内部 list[str]"
    assert d2["selected"] == 0, f"案例2 应选中 index0: {d2['arrays']}"
    print(f"[OK] 案例2：顶层 list[dict] 优先，未误取内部 indicators 数组（arrays={len(d2['arrays'])}）")

    # 案例3：markdown 包裹
    t3 = "```json\n[{\"hypothesis\":\"a\",\"indicators\":[\"EMA\",\"RSI\"]}]\n```"
    arr3, d3 = rl.parse_hypothesis_array_diag(t3)
    assert len(arr3) == 1 and arr3[0]["hypothesis"] == "a", f"案例3失败: {d3}"

    # 案例4：文字说明 + JSON
    t4 = "以下是策略方向：\n[{\"hypothesis\":\"a\",\"indicators\":[\"EMA\"]}]"
    arr4, d4 = rl.parse_hypothesis_array_diag(t4)
    assert len(arr4) == 1 and arr4[0]["indicators"] == ["EMA"], f"案例4失败: {d4}"

    # 案例5：多层嵌套 {"result":{"strategies":[{...}]}}
    t5 = '{"result":{"strategies":[{"hypothesis":"a","indicators":["EMA","RSI"]}]}}'
    arr5, d5 = rl.parse_hypothesis_array_diag(t5)
    assert len(arr5) == 1 and arr5[0]["hypothesis"] == "a", f"案例5嵌套未解析: {d5}"

    for name, arr, diag in [("案例1", arr1, d1), ("案例3", arr3, d3), ("案例4", arr4, d4), ("案例5", arr5, d5)]:
        assert diag["error"] is None, f"[{name}] 不应有错误: {diag['error']}"
        assert diag["selected"] is not None, f"[{name}] 应有 selected"
        print(f"[OK] {name}：list[dict] 提取成功，selected=index{diag['selected']}")

    # 诊断：list[str] 数组应被正确标记类型且不被选中
    t_bad = '["BOLL","VWAP"]'
    arr_bad, d_bad = rl.parse_hypothesis_array_diag(t_bad)
    assert arr_bad == [] and d_bad["error"], "纯 list[str] 不应被当作候选"
    assert d_bad["arrays"][0]["type"] == "list[str]", f"诊断应标记 list[str]: {d_bad['arrays']}"
    print(f"[OK] 诊断：list[str] 数组被标记类型并拒绝（error={d_bad['error']}）")

    print("\nALL NESTED JSON PARSE TESTS PASSED")


if __name__ == "__main__":
    main()
