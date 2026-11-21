"""外送制作线（2/5）：原声使用核算。

回答授权员的问题：某个 clip 在输出的哪些采样点用到了哪些源采样、各自权重多少、
在某地区是否已授权。全部沿用时间轴既有语义（见 engine.py / timeline.py）：

- 源实数位置 ``p = a + (t-start)*rate*speed``，相邻样本线性插值；
- ``p`` 到达末样本之后（仍在 clip 时长内）保持末样本 —— 片尾保持也算使用；
- 淡化是 0→1 线性淡入、1→0 线性淡出，边界严格小于判定；
- **只有权重真正非零的源采样才算“使用”**。淡化因子为 0 的输出点不使用任何源采样；
  若两个 clip 相消成静音，各自的源使用仍然分别记账，不能据此认定两边原声都可用。

权重是“该源采样对该输出点贡献的插值系数 × 淡化因子”（不含 gain，gain 只是响度，
不改变用了哪些原声）。所有权重为精确 Fraction，授权判定不经过浮点。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .export_licenses import LicenseLedger
from .rationals import load_r

OUTPUT_RATE = 16000


@dataclass(frozen=True)
class SourceUse:
    """一个输出采样点对一个源采样的一次实际使用（权重严格为正）。"""
    output_index: int
    clip_id: str
    asset_id: str
    source_index: int
    weight: Fraction


def clip_view(clip: dict[str, Any], source_rate: int) -> dict[str, Any]:
    """把产品中的 clip JSON 转成带 Fraction 的内部视图。"""
    speed = load_r(clip["speed"])
    start = load_r(clip["start"])
    a, b = int(clip["a"]), int(clip["b"])
    duration = Fraction(b - a, source_rate) / speed
    return {
        "clip_id": clip["clip_id"],
        "asset_id": clip["asset_id"],
        "a": a,
        "b": b,
        "rate": source_rate,
        "start": start,
        "end": start + duration,
        "speed": speed,
        "gain": load_r(clip["gain"]),
        "fade_in": load_r(clip["fade_in"]),
        "fade_out": load_r(clip["fade_out"]),
    }


def _fade_factor(c: dict[str, Any], t: Fraction) -> Fraction:
    """与 engine._fade_factor 完全同语义，但返回精确 Fraction。"""
    d = t - c["start"]
    fin = Fraction(1)
    if c["fade_in"] > 0 and d < c["fade_in"]:
        fin = d / c["fade_in"]
    fout = Fraction(1)
    if c["fade_out"] > 0:
        remain = c["end"] - t
        if remain < c["fade_out"]:
            fout = remain / c["fade_out"]
    return fin * fout


def active_output_range(c: dict[str, Any], n_samples: int) -> range:
    """clip 实际可能有贡献的输出样本号区间（左闭右开），与 engine 的 ceil 边界一致。"""
    S = c["start"] * OUTPUT_RATE
    E = c["end"] * OUTPUT_RATE
    lo = -(-S.numerator // S.denominator)          # ceil(S)
    hi = -(-E.numerator // E.denominator)          # ceil(E)
    return range(max(0, lo), min(n_samples, hi))


def source_uses_at(c: dict[str, Any], n: int) -> list[tuple[int, Fraction]]:
    """输出样本 n 上该 clip 实际使用的 (源采样号, 严格正权重) 列表。

    线性插值至多使用两个相邻源采样；片尾保持只使用末样本一个。淡化因子为 0
    （淡入/淡出的零点）时返回空列表 —— 该输出点没有使用任何原声。
    """
    t = Fraction(n, OUTPUT_RATE)
    if not (c["start"] <= t < c["end"]):
        return []
    fade = _fade_factor(c, t)
    if fade == 0:
        return []
    p = c["a"] + (t - c["start"]) * c["rate"] * c["speed"]
    i = p.numerator // p.denominator                # floor(p)，p >= a >= 0
    if i >= c["b"] - 1:                             # 片尾保持：末样本权重 1
        return [(c["b"] - 1, fade)]
    frac = p - i
    uses: list[tuple[int, Fraction]] = []
    w0 = fade * (1 - frac)
    if w0 > 0:
        uses.append((i, w0))
    w1 = fade * frac
    if w1 > 0:                                      # frac 可能为 0（抽点），严格判定
        uses.append((i + 1, w1))
    return uses


def iter_clip_uses(c: dict[str, Any], n_samples: int):
    """逐输出点枚举一个 clip 的全部实际使用（权重严格非零）。"""
    for n in active_output_range(c, n_samples):
        for source_index, weight in source_uses_at(c, n):
            yield SourceUse(n, c["clip_id"], c["asset_id"], source_index, weight)


def clip_outputs_using_asset(c: dict[str, Any], n_samples: int) -> set[int]:
    """该 clip 真正使用了原声的输出采样点集合（淡化零点自动排除）。"""
    return {u.output_index for u in iter_clip_uses(c, n_samples)}


# ----------------------------- 授权核算 ----------------------------- #

def unlicensed_report(views: list[dict[str, Any]], n_samples: int,
                      ledger: LicenseLedger, region: str, when: int,
                      clip_id: str | None = None) -> dict[str, Any]:
    """授权员视图：逐 clip 列出在输出哪些采样点使用了尚未授权的原声。

    返回每个问题输出点上缺失授权的具体源采样（含精确权重与覆盖许可数）。
    许可按 (asset_id, 源采样) 独立判定；同一输出点上一个 clip 缺权即整段不可交付，
    由制作线负责静掉/替换，本函数只负责如实报账。
    """
    report: dict[str, list[dict[str, Any]]] = {}
    for c in views:
        if clip_id is not None and c["clip_id"] != clip_id:
            continue
        bad_points: list[dict[str, Any]] = []
        for n in active_output_range(c, n_samples):
            uses = source_uses_at(c, n)
            if not uses:
                continue
            missing = []
            for source_index, weight in uses:
                covers = ledger.covering(c["asset_id"], source_index, region, when)
                if not covers:
                    missing.append({
                        "source_index": source_index,
                        "weight": [weight.numerator, weight.denominator],
                        "covering_licenses": [],
                    })
            if missing:
                all_uses = []
                for source_index, weight in uses:
                    covers = ledger.covering(c["asset_id"], source_index, region, when)
                    all_uses.append({
                        "source_index": source_index,
                        "weight": [weight.numerator, weight.denominator],
                        "covering_licenses": [lic.license_id for lic in covers],
                    })
                bad_points.append({"output_index": n, "uses": all_uses,
                                   "unlicensed_sources": missing})
        if bad_points:
            report[c["clip_id"]] = bad_points
    return {
        "region": region,
        "at_tick": when,
        "ledger_revision": ledger.revision,
        "clips_with_unlicensed_use": sorted(report),
        "details": report,
    }


def licensed_source_set(c: dict[str, Any], n_samples: int,
                        ledger: LicenseLedger, region: str, when: int) -> set[int]:
    """该 clip 在本地区被有效许可覆盖到的全部源采样（用于渲染门控）。"""
    licensed: set[int] = set()
    for u in iter_clip_uses(c, n_samples):
        if ledger.is_licensed(u.asset_id, u.source_index, region, when):
            licensed.add(u.source_index)
    return licensed


def clip_is_fully_licensed(c: dict[str, Any], n_samples: int,
                           ledger: LicenseLedger, region: str, when: int) -> bool:
    """clip 的每个真正使用点上的每个严格正权重源采样都已授权，才算整段可用。"""
    for u in iter_clip_uses(c, n_samples):
        if not ledger.is_licensed(u.asset_id, u.source_index, region, when):
            return False
    return True


def unlicensed_output_points(c: dict[str, Any], n_samples: int,
                             ledger: LicenseLedger, region: str,
                             when: int) -> list[int]:
    """单独查清某个 clip 在哪些输出采样点使用了尚未授权的原声。"""
    points: set[int] = set()
    for n in active_output_range(c, n_samples):
        for source_index, _w in source_uses_at(c, n):
            if not ledger.is_licensed(c["asset_id"], source_index, region, when):
                points.add(n)
                break
    return sorted(points)
