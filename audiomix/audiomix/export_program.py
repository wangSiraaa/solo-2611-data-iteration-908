"""外送制作线（5/5）：外送节目制作门面（成片渲染、制作单、WAV 交付、窗口冻结）。

把前四个模块串成一条完整制作线，输入是已有的 audiomix 发布产品（不可变时间轴）+
素材登记仓 + 许可台账 + 句单/备用/班表，输出每个收听地区：

1. **可播放 WAV**：固定 16kHz、单声道 16bit PCM；
2. **可复核制作单**：逐句处置、逐 clip 未授权报账、实际用到的许可编号、实际占用的
   录音班与备用录音编号、真实签单号。

渲染有两条独立代码路径（共用同一份门控计划，不共享渲染代码）：
- :meth:`render_region_reference`：纯 Python 逐样本 Fraction 精算，语义基准；
- :meth:`render_region_vectorized`：NumPy 向量化实现。
测试要求两者逐样本一致（与既有 engine 的双路径策略相同）。

门控语义：
- 旁白句：逐输出点门控，某个点上任一严格正权重源采样未授权 → 该点整条 clip 贡献
  置零（“静掉受限贡献”），其余点照常播出；
- 整句采访：原声整段保留（全授权）或整段静掉，绝不交付半句；替换句整段换成已授权
  备用录音（不再查原声许可）；
- 未挂句单的 clip 同样按逐点授权门控，保证没有任何未授权原声漏出；
- 两段相消成静音不改变各自的使用记账（在 export_usage 层分别核算）。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import numpy as np

from .assets import AssetRegistry, write_wav
from .engine import OUTPUT_RATE, output_sample_count
from .export_counter import DispatchOffice, IssuedTicket
from .export_licenses import LicenseLedger
from .export_schedule import Arrangement, BackupRecording, SchedulePlanner, Sentence
from .export_usage import (
    active_output_range, clip_view, source_uses_at, unlicensed_report,
)
from .rationals import as_r, dump_r


@dataclass(frozen=True)
class ClipGate:
    clip_id: str
    mode: str                  # full / per_sample / mute / replace
    asset_id: str
    backup_id: str | None = None
    backup_asset_id: str | None = None


class ExportError(ValueError):
    pass


class ExportStudio:
    """一个工作室实例持有产品、素材、台账、句单与调度办公室，按地区交付。"""

    def __init__(self, root: str, product: dict[str, Any], registry: AssetRegistry,
                 ledger: LicenseLedger, sentences: list[Sentence],
                 backups: list[BackupRecording], T: Any,
                 office: DispatchOffice | None = None):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.product = product
        self.registry = registry
        self.ledger = ledger
        self.sentences = {s.sentence_id: s for s in sentences}
        self.backups = {b.backup_id: b for b in backups}
        self.T = as_r(T)
        self.n_samples = output_sample_count(self.T)
        self.rates = {a["asset_id"]: int(a["sample_rate"]) for a in product["assets"]}
        self.views = {c["clip_id"]: clip_view(c, self.rates[c["asset_id"]])
                      for c in product["clips"]}
        self.pcm = {a["asset_id"]: registry.load_pcm(a["asset_id"])
                    for a in product["assets"]}
        for bk in backups:  # 备用素材也登记在同一素材仓，统一加载与摘要校验
            if bk.asset_id not in self.pcm:
                self.pcm[bk.asset_id] = registry.load_pcm(bk.asset_id)
        self.office = office
        self.deliveries_dir = os.path.join(root, "deliveries")
        self.frozen_dir = os.path.join(root, "frozen")
        os.makedirs(self.deliveries_dir, exist_ok=True)

    # ----------------------------- 授权报账 ----------------------------- #
    def audit_unlicensed(self, region: str, when: int,
                         clip_id: str | None = None) -> dict[str, Any]:
        return unlicensed_report(list(self.views.values()), self.n_samples,
                                 self.ledger, region, when, clip_id)

    def planner(self, region: str, when: int) -> SchedulePlanner:
        return SchedulePlanner(list(self.sentences.values()),
                               list(self.backups.values()),
                               list(self.office.shifts.values()) if self.office else [],
                               self.product, self.ledger, region, when, self.n_samples)

    # ----------------------------- 门控计划 ----------------------------- #
    def build_gates(self, region: str, when: int,
                    arrangement: Arrangement) -> dict[str, ClipGate]:
        """把排班决定翻译成逐 clip 门控；未挂句单的 clip 走逐点授权门控兜底。"""
        gates: dict[str, ClipGate] = {}
        for sid, dec in arrangement.decisions.items():
            sentence = self.sentences[sid]
            for cid in sentence.clip_ids:
                if cid not in self.views:
                    continue
                view = self.views[cid]
                if dec.action == "replace":
                    bk = self.backups[dec.backup_id]
                    gates[cid] = ClipGate(cid, "replace", view["asset_id"],
                                          dec.backup_id, bk.asset_id)
                elif dec.action == "mute" or dec.action == "blocked":
                    gates[cid] = ClipGate(cid, "mute", view["asset_id"])
                elif sentence.kind == "interview":
                    gates[cid] = ClipGate(cid, "full", view["asset_id"])
                else:
                    gates[cid] = ClipGate(cid, "per_sample", view["asset_id"])
        for cid, view in self.views.items():
            if cid not in gates:  # 未挂句单：逐点门控，绝不漏出未授权原声
                gates[cid] = ClipGate(cid, "per_sample", view["asset_id"])
        return gates

    # ----------------------------- 参考渲染（Fraction 精算） ----------------------------- #
    def render_region_reference(self, region: str, when: int,
                                arrangement: Arrangement) -> np.ndarray:
        gates = self.build_gates(region, when, arrangement)
        total = [Fraction(0) for _ in range(self.n_samples)]
        for cid in sorted(gates):  # 确定求和次序
            gate = gates[cid]
            view = self.views[cid]
            if gate.mode == "mute":
                continue
            if gate.mode == "replace":
                self._accumulate_backup_reference(total, gate, view)
                continue
            for n in active_output_range(view, self.n_samples):
                uses = source_uses_at(view, n)
                if not uses:
                    continue
                if gate.mode == "per_sample":
                    if not all(self.ledger.is_licensed(view["asset_id"], src, region, when)
                               for src, _ in uses):
                        continue
                value = Fraction(0)
                x = self.pcm[view["asset_id"]]
                for src, w in uses:
                    value += Fraction(int(x[src])) * w
                total[n] += value * view["gain"]
        return self._round_saturate(total)

    def _accumulate_backup_reference(self, total: list[Fraction],
                                     gate: ClipGate, view: dict[str, Any]) -> None:
        """备用录音按原句输出位置对齐：备用素材逐样本放到原 clip 的输出跨度上。"""
        bk = self.backups[gate.backup_id]
        backup_pcm = self.pcm[gate.backup_asset_id]
        span = list(active_output_range(view, self.n_samples))
        if not span:
            return
        n0, n1 = span[0], span[-1] + 1
        count = min(n1 - n0, len(backup_pcm))
        for k in range(count):
            total[n0 + k] += Fraction(int(backup_pcm[k])) * view["gain"]

    @staticmethod
    def _round_saturate(total: list[Fraction]) -> np.ndarray:
        out = np.empty(len(total), dtype=np.int16)
        for i, v in enumerate(total):
            # 与 engine 一致：恰半取偶（Fraction 精确判定半值），再饱和
            r = round(v)  # Fraction 支持 banker's rounding
            out[i] = max(-32768, min(32767, int(r)))
        return out

    # ----------------------------- 向量化渲染（NumPy） ----------------------------- #
    def render_region_vectorized(self, region: str, when: int,
                                 arrangement: Arrangement) -> np.ndarray:
        gates = self.build_gates(region, when, arrangement)
        acc = np.zeros(self.n_samples, dtype=np.float64)
        for cid in sorted(gates):
            gate = gates[cid]
            view = self.views[cid]
            if gate.mode == "mute":
                continue
            if gate.mode == "replace":
                self._accumulate_backup_vectorized(acc, gate, view)
                continue
            idx, contrib = self._clip_float_contribution(view)
            if len(idx) == 0:
                continue
            if gate.mode == "per_sample":
                mask = self._vectorized_allowed_mask(view, region, when)
                keep = mask[idx]
                idx, contrib = idx[keep], contrib[keep]
            acc[idx] += contrib
        rounded = np.rint(acc)  # 全片唯一一次舍入：恰半取偶
        return np.clip(rounded, -32768, 32767).astype(np.int16)

    def _clip_float_contribution(self, view: dict[str, Any]):
        """复刻 engine 的有理数源位置/淡化算法，但针对外送门控按需重算贡献。"""
        S = view["start"] * OUTPUT_RATE
        E = view["end"] * OUTPUT_RATE
        lo = -(-S.numerator // S.denominator)
        hi = -(-E.numerator // E.denominator)
        lo, hi = max(lo, 0), min(hi, self.n_samples)
        if lo >= hi:
            return np.empty(0, np.int64), np.empty(0, np.float64)
        nn = np.arange(lo, hi, dtype=np.int64)
        speed = view["speed"]
        rs_num, rs_den = view["rate"] * speed.numerator, speed.denominator
        Snum, Sden = S.numerator, S.denominator
        den = np.int64(OUTPUT_RATE * rs_den * Sden)
        pnum = np.int64(view["a"]) * den + rs_num * (nn * Sden - Snum)
        idx_floor = np.floor_divide(pnum, den)
        frac = (pnum - idx_floor * den).astype(np.float64) / den.astype(np.float64)
        x = self.pcm[view["asset_id"]]
        xi = np.empty(len(nn), dtype=np.float64)
        hold = idx_floor >= (view["b"] - 1)
        xi[hold] = float(x[view["b"] - 1])
        safe = ~hold
        if np.any(safe):
            ii = idx_floor[safe].astype(np.int64)
            xi[safe] = x[ii].astype(np.float64) + frac[safe] * (
                x[ii + 1].astype(np.float64) - x[ii].astype(np.float64))
        factor = np.ones(len(nn), dtype=np.float64)
        d_num = nn * Sden - Snum
        if view["fade_in"] > 0:
            fi = view["fade_in"]
            fden = np.int64(Sden * OUTPUT_RATE * fi.numerator)
            fnum = d_num * fi.denominator
            m = fnum < fden
            factor[m] = (fnum[m] / fden).astype(np.float64)
        if view["fade_out"] > 0:
            fo = view["fade_out"]
            Enum, Eden = E.numerator, E.denominator
            fden = np.int64(Eden * OUTPUT_RATE * fo.numerator)
            fnum = (Enum - nn * Eden) * fo.denominator
            m = fnum < fden
            factor[m] *= (fnum[m] / fden).astype(np.float64)
        return nn, xi * float(view["gain"]) * factor

    def _vectorized_allowed_mask(self, view, region: str, when: int) -> np.ndarray:
        mask = np.zeros(self.n_samples, dtype=bool)
        for n in active_output_range(view, self.n_samples):
            uses = source_uses_at(view, n)
            mask[n] = bool(uses) and all(
                self.ledger.is_licensed(view["asset_id"], src, region, when)
                for src, _ in uses)
        return mask

    def _accumulate_backup_vectorized(self, acc: np.ndarray,
                                      gate: ClipGate, view: dict[str, Any]) -> None:
        backup_pcm = self.pcm[gate.backup_asset_id].astype(np.float64)
        span = list(active_output_range(view, self.n_samples))
        if not span:
            return
        n0, n1 = span[0], span[-1] + 1
        count = min(n1 - n0, len(backup_pcm))
        acc[n0:n0 + count] += backup_pcm[:count] * float(view["gain"])

    # ----------------------------- 交付：WAV + 制作单 ----------------------------- #
    def deliver(self, region: str, when: int, arrangement: Arrangement,
                ticket: IssuedTicket | None = None,
                render_id: str | None = None) -> dict[str, Any]:
        ref = self.render_region_reference(region, when, arrangement)
        vec = self.render_region_vectorized(region, when, arrangement)
        if not np.array_equal(ref, vec):
            raise ExportError(
                f"地区 {region} 双路径渲染不一致：参考={ref.tolist()} 向量化={vec.tolist()}")
        rid = render_id or hashlib.sha256(
            (region + self.product["product_id"] + self.ledger.revision.__str__()
             + json.dumps(arrangement.to_json(), sort_keys=True)).encode()
        ).hexdigest()[:16]
        out_dir = os.path.join(self.deliveries_dir, region, rid)
        os.makedirs(out_dir, exist_ok=True)
        wav_path = os.path.join(out_dir, "program.wav")
        write_wav(wav_path, ref, OUTPUT_RATE)

        sheet = self.production_sheet(region, when, arrangement, ticket, rid, wav_path, ref)
        sheet_path = os.path.join(out_dir, "production_sheet.json")
        with open(sheet_path, "w", encoding="utf-8") as fh:
            json.dump(sheet, fh, ensure_ascii=False, indent=2)
        return {"region": region, "render_id": rid, "wav": wav_path,
                "sheet": sheet_path, "n_samples": int(len(ref)),
                "sentences_heard": arrangement.sentences_heard,
                "shifts_used": arrangement.shifts_used}

    def production_sheet(self, region: str, when: int, arrangement: Arrangement,
                         ticket: IssuedTicket | None, render_id: str,
                         wav_path: str, pcm: np.ndarray) -> dict[str, Any]:
        audit = self.audit_unlicensed(region, when)
        license_ids: set[str] = set()
        gates = self.build_gates(region, when, arrangement)
        for cid, gate in gates.items():
            if gate.mode in ("full", "per_sample"):
                view = self.views[cid]
                for n in active_output_range(view, self.n_samples):
                    for src, _w in source_uses_at(view, n):
                        for lic in self.ledger.covering(view["asset_id"], src, region, when):
                            license_ids.add(lic.license_id)
        used_backups = sorted({
            dec.backup_id for dec in arrangement.decisions.values()
            if dec.action == "replace" and dec.backup_id})
        wav_sha = hashlib.sha256(pcm.astype("<i2").tobytes()).hexdigest()
        return {
            "format": "audiomix.export-sheet/1",
            "region": region,
            "render_id": render_id,
            "at_tick": when,
            "ledger_revision": self.ledger.revision,
            "product_id": self.product["product_id"],
            "output_rate": OUTPUT_RATE,
            "T": dump_r(self.T),
            "n_samples": self.n_samples,
            "wav": {"path": os.path.basename(wav_path), "sha256": wav_sha},
            "sentence_decisions": [arrangement.decisions[sid].to_json()
                                   for sid in sorted(arrangement.decisions)],
            "clip_gates": [gates[cid].__dict__ for cid in sorted(gates)],
            "licenses_used": sorted(license_ids),
            "backups_used": used_backups,
            "ticket": ticket.to_json() if ticket else None,
            "unlicensed_audit": audit,
            "metrics": {"shifts_used": arrangement.shifts_used,
                        "sentences_heard": arrangement.sentences_heard},
        }

    # ----------------------------- 窗口后冻结实际签发版本 ----------------------------- #
    def freeze_issued(self, when: int) -> dict[str, Any]:
        """播出窗口过后：把当时实际签发（未错过）地区的成片与制作单快照冻结。"""
        if self.office is None or not self.office.is_frozen():
            raise ExportError("调度办公室尚未冻结，不能归档")
        snap = {"frozen_at": when, "regions": []}
        for region, counter in sorted(self.office.counters.items()):
            if counter.get("status") != "issued":
                continue
            ticket = self.office.tickets[counter["ticket_id"]]
            src_dir = os.path.join(self.deliveries_dir, region)
            # 找到与该签单 render_id 对应的交付目录
            target = None
            for rid in sorted(os.listdir(src_dir)) if os.path.isdir(src_dir) else []:
                sheet_path = os.path.join(src_dir, rid, "production_sheet.json")
                if os.path.exists(sheet_path):
                    sheet = json.load(open(sheet_path, encoding="utf-8"))
                    if sheet.get("ticket", {}).get("ticket_id") == ticket.ticket_id:
                        target = os.path.join(src_dir, rid)
                        break
            if target is None:
                continue
            dst = os.path.join(self.frozen_dir, region)
            os.makedirs(dst, exist_ok=True)
            for fname in os.listdir(target):
                with open(os.path.join(target, fname), "rb") as fh:
                    payload = fh.read()
                with open(os.path.join(dst, fname), "wb") as fh:
                    fh.write(payload)
            snap["regions"].append(
                {"region": region, "ticket_id": ticket.ticket_id,
                 "frozen_dir": dst,
                 "wav_sha256": hashlib.sha256(
                     open(os.path.join(dst, "program.wav"), "rb").read()).hexdigest()})
        with open(os.path.join(self.frozen_dir, "freeze_manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=2)
        return snap
