#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经济/行业周期诊断：派生指标计算 + 阶段规则判定。

把「经济机器」框架里需要反复手算的派生指标（r-g、债务负担变化、信贷脉冲、库存-销售缺口、
产能缺口等）固定成代码，并按六阶段判据逐条记录触发情况，输出可审计的证据表。

脚本只做机械计算与规则触发记录，不替代判断。缺失指标单列为「待补」，不用猜测填充。

用法:
    python cycle_diagnostics.py --sample > indicators.json
    python cycle_diagnostics.py indicators.json
    python cycle_diagnostics.py indicators.json --md
"""

import argparse
import json
import math
import sys
import unicodedata
from typing import Dict, Optional

# ---------------------------------------------------------------- 阈值（可调）

THRESHOLDS = {
    "credit_accel_pp": 0.0,        # 信贷增速变化，>0 视为加速
    "capex_gap_pp": 3.0,           # Capex 增速 - 营收增速，超过视为显著扩产
    "policy_room_pp": 1.0,         # 政策利率距下限 <=1pp 视为空间接近耗尽
    "capacity_gap_pp": 0.0,        # 产能利用率 - 历史均值
    "price_accel_pp": 0.0,         # 价格动量二阶变化
}

# ---------------------------------------------------------------- 派生指标

DERIVED_LABELS = {
    "r_minus_g": ("r−g（融资成本 − 名义增长）", "pp"),
    "real_rate": ("实际利率（融资成本 − 价格动量）", "%"),
    "debt_burden_delta": ("债务负担变化（债务/收入 较上期）", "pp"),
    "credit_accel": ("信贷加速度（信贷增速 较上期）", "pp"),
    "price_accel": ("价格动量二阶变化", "pp"),
    "inv_sales_gap": ("库存 − 销售 增速差", "pp"),
    "capacity_gap": ("产能利用率 − 历史均值", "pp"),
    "capex_gap": ("Capex 增速 − 营收增速", "pp"),
    "policy_room": ("政策空间（政策利率 − 下限）", "pp"),
}

# 判定所需的最低指标集
MINIMAL = [
    "price_momentum",
    "credit_growth",
    "debt_to_income",
    "financing_cost",
    "nominal_growth",
]


def _width(s: str) -> int:
    """按东亚字符宽度计宽，用于终端对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _width(s))
    return s + " " * gap if align == "left" else " " * gap + s


def num(d: Dict, key: str) -> Optional[float]:
    v = d.get(key)
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return a - b


def derive(ind: Dict) -> Dict[str, Optional[float]]:
    """计算派生指标；任一输入缺失则结果为 None。"""
    fin = num(ind, "financing_cost")
    growth = num(ind, "nominal_growth")
    price = num(ind, "price_momentum")
    price_prev = num(ind, "price_momentum_prev")

    d = {}
    d["r_minus_g"] = diff(fin, growth)
    d["real_rate"] = diff(fin, price)
    d["debt_burden_delta"] = diff(
        num(ind, "debt_to_income"), num(ind, "debt_to_income_prev")
    )
    d["credit_accel"] = diff(
        num(ind, "credit_growth"), num(ind, "credit_growth_prev")
    )
    d["price_accel"] = diff(price, price_prev)

    sales = num(ind, "sales_growth")
    if sales is None:
        sales = num(ind, "revenue_growth")
    d["inv_sales_gap"] = diff(num(ind, "inventory_growth"), sales)

    d["capacity_gap"] = diff(
        num(ind, "capacity_utilization"), num(ind, "capacity_utilization_mean")
    )

    rev = num(ind, "revenue_growth")
    d["capex_gap"] = diff(num(ind, "capex_growth"), rev)

    rate = num(ind, "policy_rate")
    floor = num(ind, "policy_rate_floor")
    if floor is None and rate is not None:
        floor = 0.0
    d["policy_room"] = diff(rate, floor)

    return d


# ---------------------------------------------------------------- 触发规则

TRIGGER_LABELS = {
    "credit_up": "信贷增速加快",
    "credit_down": "信贷增速放缓",
    "price_up": "价格动量为正",
    "price_down": "价格动量为负",
    "price_accel": "价格动量加速",
    "inv_gt_sales": "库存增速快于销售（被动补库）",
    "inv_lt_sales": "库存增速慢于销售（去库/被动去库）",
    "cap_high": "产能利用率高于历史均值",
    "cap_low": "产能利用率低于历史均值",
    "capex_surge": "Capex 增速显著快于营收",
    "capex_low": "Capex 未扩张或收缩",
    "lev_up": "债务负担上升",
    "lev_down": "债务负担下降",
    "r_gt_g": "融资成本高于名义增长（r>g）",
    "r_lt_g": "融资成本低于名义增长（r<g）",
    "policy_exhausted": "政策利率空间接近耗尽",
    "collateral_down": "抵押物/资产价格下跌",
    "dso_widen": "应收账期拉长",
}


def _cmp(value: Optional[float], th: float, op: str) -> Optional[bool]:
    if value is None:
        return None
    return value > th if op == "gt" else value < th


def build_triggers(d: Dict[str, Optional[float]], ind: Dict) -> Dict[str, Optional[bool]]:
    t = {}
    t["credit_up"] = _cmp(d["credit_accel"], THRESHOLDS["credit_accel_pp"], "gt")
    t["credit_down"] = _cmp(d["credit_accel"], THRESHOLDS["credit_accel_pp"], "lt")
    t["price_up"] = _cmp(num(ind, "price_momentum"), 0.0, "gt")
    t["price_down"] = _cmp(num(ind, "price_momentum"), 0.0, "lt")
    t["price_accel"] = _cmp(d["price_accel"], THRESHOLDS["price_accel_pp"], "gt")
    t["inv_gt_sales"] = _cmp(d["inv_sales_gap"], 0.0, "gt")
    t["inv_lt_sales"] = _cmp(d["inv_sales_gap"], 0.0, "lt")
    t["cap_high"] = _cmp(d["capacity_gap"], THRESHOLDS["capacity_gap_pp"], "gt")
    t["cap_low"] = _cmp(d["capacity_gap"], THRESHOLDS["capacity_gap_pp"], "lt")
    t["capex_surge"] = _cmp(d["capex_gap"], THRESHOLDS["capex_gap_pp"], "gt")
    t["capex_low"] = _cmp(d["capex_gap"], THRESHOLDS["capex_gap_pp"], "lt")
    t["lev_up"] = _cmp(d["debt_burden_delta"], 0.0, "gt")
    t["lev_down"] = _cmp(d["debt_burden_delta"], 0.0, "lt")
    t["r_gt_g"] = _cmp(d["r_minus_g"], 0.0, "gt")
    t["r_lt_g"] = _cmp(d["r_minus_g"], 0.0, "lt")
    t["policy_exhausted"] = _cmp(d["policy_room"], THRESHOLDS["policy_room_pp"], "lt")
    t["collateral_down"] = _cmp(num(ind, "collateral_price_growth"), 0.0, "lt")
    t["dso_widen"] = _cmp(num(ind, "dso_change"), 0.0, "gt")
    return t


PHASES = {
    "P1": {
        "name": "修复",
        "triggers": ["credit_up", "price_up", "capex_low", "cap_low"],
        "min_ratio": 0.6,
    },
    "P2": {
        "name": "扩张",
        "triggers": ["credit_up", "price_up", "inv_lt_sales", "cap_high"],
        "min_ratio": 0.6,
    },
    "P3": {
        "name": "过热",
        "triggers": ["price_accel", "lev_up", "capex_surge", "cap_high", "inv_gt_sales"],
        "min_ratio": 0.6,
    },
    "P4": {
        "name": "收缩",
        "triggers": ["price_down", "inv_gt_sales", "credit_down", "lev_up"],
        "min_ratio": 0.6,
    },
    "P5": {
        "name": "去杠杆",
        "triggers": [
            "r_gt_g",
            "lev_up",
            "policy_exhausted",
            "price_down",
            "collateral_down",
            "dso_widen",
        ],
        "min_ratio": 0.6,
    },
    "P6": {
        "name": "再平衡",
        "triggers": ["lev_down", "r_lt_g", "price_up", "credit_up"],
        "min_ratio": 0.6,
    },
}

# 同分时的优先顺序：收缩类判断取保守
TIE_ORDER = ["P5", "P4", "P3", "P2", "P1", "P6"]


def score_phases(t: Dict[str, Optional[bool]]) -> Dict[str, Dict]:
    out = {}
    for pid, cfg in PHASES.items():
        hits = available = 0
        detail = []
        for tid in cfg["triggers"]:
            v = t.get(tid)
            if v is None:
                detail.append((tid, None))
                continue
            available += 1
            if v:
                hits += 1
            detail.append((tid, v))
        ratio = (hits / available) if available else 0.0
        # 证据充分性门槛：可用判据太少时，比例再高也不足以定档
        min_avail = max(3, math.ceil(0.6 * len(cfg["triggers"])))
        out[pid] = {
            "name": cfg["name"],
            "hits": hits,
            "available": available,
            "min_available": min_avail,
            "ratio": ratio,
            "detail": detail,
            "qualified": available >= min_avail and ratio >= cfg["min_ratio"],
        }
    return out


def decide(scores: Dict[str, Dict]) -> Dict:
    ranked = sorted(
        scores.items(),
        key=lambda kv: (-kv[1]["ratio"], TIE_ORDER.index(kv[0])),
    )
    primary = ranked[0] if ranked and ranked[0][1]["qualified"] else None
    secondary = None
    for pid, s in ranked[1:]:
        if s["qualified"] and s["available"] > 0 and s["ratio"] >= 0.5:
            secondary = (pid, s)
            break

    notes = []
    if primary is None:
        notes.append("最高得分未达判定阈值，证据不足，请补指标或补充定性判断。")
    p5 = scores.get("P5", {})
    p4 = scores.get("P4", {})
    if p5.get("qualified") and p5["ratio"] >= 0.75:
        notes.append("去杠杆特征显著；请务必人工复核 Q4（政策空间）与 Q5（抵押物）——这两条是分水岭。")
    elif p4.get("qualified") and not p5.get("qualified"):
        notes.append("倾向普通收缩（利率仍有效），不要按去杠杆制定对策。")
    if secondary and secondary[0] == "P5" and not (primary and primary[0] == "P5"):
        notes.append("去杠杆为次可能档：关键看政策利率空间是否耗尽、抵押物是否转跌；任一为真即上调至 P5。")
    return {"primary": primary, "secondary": secondary, "ranked": ranked, "notes": notes}


# ---------------------------------------------------------------- 输出

def render_text(payload, d, t, scores, verdict) -> str:
    obj = payload.get("object", "(未命名对象)")
    mode = payload.get("mode", "macro")
    as_of = payload.get("as_of", "-")
    lines = []
    lines.append("=" * 66)
    lines.append(f"周期诊断  |  对象：{obj}  |  口径：{mode}  |  数据截止：{as_of}")
    lines.append("=" * 66)

    lines.append("\n[1] 派生指标")
    for key, (label, unit) in DERIVED_LABELS.items():
        v = d.get(key)
        val = "待补" if v is None else f"{v:+.2f} {unit}"
        lines.append(f"  {_pad(label, 38)}{val.rjust(10)}")

    lines.append("\n[2] 阶段得分（命中 / 可用判据）")
    for pid, s in sorted(scores.items()):
        flag = " *" if verdict["primary"] and verdict["primary"][0] == pid else ""
        if s["qualified"]:
            status = "达阈值"
        elif s["available"] < s["min_available"]:
            status = f"证据不足(需≥{s['min_available']}条)"
        else:
            status = "-"
        lines.append(
            f"  {pid} {_pad(s['name'], 8)} {s['hits']}/{s['available']}"
            f"  ratio={s['ratio']:.2f}  {status}{flag}"
        )

    lines.append("\n[3] 判定")
    if verdict["primary"]:
        pid, s = verdict["primary"]
        lines.append(f"  主判定：{pid} {s['name']}（{s['hits']}/{s['available']}，ratio={s['ratio']:.2f}）")
        hits = [TRIGGER_LABELS[tid] for tid, v in s["detail"] if v]
        lines.append("  命中判据：" + "；".join(hits))
    else:
        lines.append("  主判定：证据不足")
    if verdict["secondary"]:
        pid, s = verdict["secondary"]
        lines.append(f"  次可能：{pid} {s['name']}（{s['hits']}/{s['available']}）")

    for n in verdict["notes"]:
        lines.append(f"  ! {n}")

    missing_min = [k for k in MINIMAL if num(payload.get("indicators", {}), k) is None]
    if missing_min:
        lines.append("\n[4] 待补指标（缺这些无法定档，不要猜）")
        lines.append("  " + "、".join(missing_min))

    lines.append("\n提示：脚本只记录规则触发，不替代判断。结论须与人工资证据链一致。")
    return "\n".join(lines)


def render_md(payload, d, t, scores, verdict) -> str:
    obj = payload.get("object", "(未命名对象)")
    mode = payload.get("mode", "macro")
    as_of = payload.get("as_of", "-")
    out = []
    out.append(f"### 周期诊断：{obj}")
    out.append(f"口径 `{mode}`，数据截止 `{as_of}`。")
    out.append("")
    out.append("| 派生指标 | 数值 |")
    out.append("|---|---|")
    for key, (label, unit) in DERIVED_LABELS.items():
        v = d.get(key)
        out.append(f"| {label} | {'待补' if v is None else f'{v:+.2f} {unit}'} |")
    out.append("")
    out.append("| 阶段 | 命中/可用 | 得分 | 判定 |")
    out.append("|---|---|---|---|")
    for pid, s in sorted(scores.items()):
        if verdict["primary"] and verdict["primary"][0] == pid:
            mark = "**主判定**"
        elif verdict["secondary"] and verdict["secondary"][0] == pid:
            mark = "次可能"
        elif s["available"] < s["min_available"]:
            mark = f"证据不足（需≥{s['min_available']}条）"
        else:
            mark = "—"
        out.append(f"| {pid} {s['name']} | {s['hits']}/{s['available']} | {s['ratio']:.2f} | {mark} |")
    out.append("")
    if verdict["primary"]:
        pid, s = verdict["primary"]
        hits = "、".join(TRIGGER_LABELS[tid] for tid, v in s["detail"] if v)
        out.append(f"**命中判据**：{hits}")
        out.append("")
    for n in verdict["notes"]:
        out.append(f"> {n}")
    return "\n".join(out)


def sample_payload() -> Dict:
    return {
        "_说明": "所有指标均可缺省；缺省项会被单列为待补。数值单位见各键名说明。",
        "object": "示例：中国制造业",
        "mode": "industry",
        "as_of": "2026-09",
        "indicators": {
            "debt_to_income": None,
            "_debt_to_income": "宏观用 债务/GDP(%)；行业用 有息负债/EBITDA(倍)",
            "debt_to_income_prev": None,
            "nominal_growth": None,
            "_nominal_growth": "名义增速(%)：宏观用名义GDP增速，行业用营收增速",
            "financing_cost": None,
            "_financing_cost": "融资成本(%)：政策利率/加权贷款利率，行业用综合融资成本",
            "price_momentum": None,
            "_price_momentum": "价格动量：CPI/PPI 或行业价格指数同比(%)",
            "price_momentum_prev": None,
            "inventory_growth": None,
            "_inventory_growth": "库存同比(%)：产成品存货 或 库存金额",
            "sales_growth": None,
            "_sales_growth": "销售/营收同比(%)，缺省时回落到 revenue_growth",
            "credit_growth": None,
            "_credit_growth": "信贷增速(%)：社融存量同比 或 行业有息负债同比",
            "credit_growth_prev": None,
            "capacity_utilization": None,
            "_capacity_utilization": "产能利用率(%)，行业专用",
            "capacity_utilization_mean": None,
            "_capacity_utilization_mean": "该指标近10年历史均值(%)",
            "capex_growth": None,
            "_capex_growth": "资本开支/固定资产投资增速(%)",
            "revenue_growth": None,
            "policy_rate": None,
            "_policy_rate": "政策利率(%)；行业可填代表性政策托底强度或留空",
            "policy_rate_floor": 0.0,
            "collateral_price_growth": None,
            "_collateral_price_growth": "抵押物价格同比(%)：房价/股价 或 产能资产估值变化",
            "dso_change": None,
            "_dso_change": "应收周转天数变化(天)，行业专用",
        },
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="经济/行业周期诊断")
    ap.add_argument("input", nargs="?", help="指标 JSON 文件；省略时配合 --sample 使用")
    ap.add_argument("--sample", action="store_true", help="输出填数模板 JSON")
    ap.add_argument("--md", action="store_true", help="以 Markdown 表格输出")
    args = ap.parse_args()

    if args.sample:
        print(json.dumps(sample_payload(), ensure_ascii=False, indent=2))
        return 0

    if not args.input:
        ap.print_help()
        return 2

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        print(f"找不到文件：{args.input}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败：{e}", file=sys.stderr)
        return 1

    ind = {
        k: v
        for k, v in payload.get("indicators", {}).items()
        if not k.startswith("_")
    }
    d = derive(ind)
    t = build_triggers(d, ind)
    scores = score_phases(t)
    verdict = decide(scores)

    if args.md:
        print(render_md(payload, d, t, scores, verdict))
    else:
        print(render_text(payload, d, t, scores, verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
