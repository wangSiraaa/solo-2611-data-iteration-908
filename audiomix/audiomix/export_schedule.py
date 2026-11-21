"""外送制作线（3/5）：句单、备用录音、录音班与排班求解。

制作员持有的句单把每个句子标成两类：
- ``narration``（可独立剪的旁白）：只需要静掉受限贡献，逐输出点门控即可，不占录音班；
- ``interview``（整句采访）：不能交付半句话 —— 原声一旦有未授权使用点，要么整句替换
  为已授权的备用录音，要么（仅当播出单允许）整句静掉。

备用录音（BackupRecording）标明：
- 能替哪些句（covers）；
- 占用哪个录音师的哪个时段（engineer_id, shift_id）；同一录音班同一时段只能实际交
  一条，因此同一份备用被多台竞争时必须在签发前完成分配。
- 备用素材一律是“已经授权的备用录音”（precleared 资产，渲染时不再查原声许可）。

排班输出 :class:`Arrangement`，并枚举全部合法安排，按“占用工时（班次数）”与
“听众还能听到几句（保留句数）”两维给出 Pareto 前沿 —— 前沿上的安排互不支配，
也就是题目要求的“两份都不能浪费工时且少保留句子”。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

from .export_licenses import LicenseLedger
from .export_usage import clip_view, unlicensed_output_points


@dataclass(frozen=True)
class Sentence:
    sentence_id: str
    clip_ids: tuple[str, ...]     # 该句由哪些 clip 构成（通常一个）
    kind: str                     # narration / interview
    required: bool                # 播出单：True=必须保住，False=允许整句静掉


@dataclass(frozen=True)
class BackupRecording:
    backup_id: str
    asset_id: str                 # 已授权备用素材（precleared）
    covers: tuple[str, ...]       # 能替哪些句
    engineer_id: str
    shift_id: str
    start_tick: int               # 班次时间窗（左闭右开，逻辑 tick）
    end_tick: int
    note: str = ""

    def slot(self) -> tuple[str, str]:
        return (self.engineer_id, self.shift_id)


@dataclass(frozen=True)
class Shift:
    engineer_id: str
    shift_id: str
    start_tick: int
    end_tick: int
    deadline_tick: int            # 该台截稿：必须在 deadline 之前完成签发
    note: str = ""

    def slot(self) -> tuple[str, str]:
        return (self.engineer_id, self.shift_id)


@dataclass
class SentenceDecision:
    sentence_id: str
    kind: str
    required: bool
    audible: bool                 # 交付成片中听众能否听到该句
    action: str                   # original / per_sample_gate / replace / mute
    backup_id: str | None = None
    slot: tuple[str, str] | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["slot"] = None if self.slot is None else list(self.slot)
        return d


@dataclass
class Arrangement:
    region: str
    decisions: dict[str, SentenceDecision]
    used_slots: set[tuple[str, str]]
    shifts_used: int
    sentences_heard: int
    feasible: bool
    reason: str = ""

    def key(self) -> tuple[str, tuple[Any, ...]]:
        return (self.region, tuple(sorted(
            (sid, d.action, d.backup_id or "")
            for sid, d in sorted(self.decisions.items()))))

    def to_json(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "feasible": self.feasible,
            "reason": self.reason,
            "shifts_used": self.shifts_used,
            "sentences_heard": self.sentences_heard,
            "used_slots": [list(s) for s in sorted(self.used_slots)],
            "decisions": [self.decisions[sid].to_json()
                          for sid in sorted(self.decisions)],
        }


class ScheduleError(ValueError):
    pass


class SchedulePlanner:
    """单地区排班器：给定句单、备用录音、班表与许可台账，枚举合法安排。"""

    def __init__(self, sentences: Iterable[Sentence],
                 backups: Iterable[BackupRecording],
                 shifts: Iterable[Shift],
                 product: dict[str, Any],
                 ledger: LicenseLedger,
                 region: str, when: int,
                 n_samples: int):
        self.sentences = list(sentences)
        self.backups = list(backups)
        self.shifts = {s.slot(): s for s in shifts}
        self.ledger = ledger
        self.region = region
        self.when = when
        self.n_samples = n_samples
        rates = {a["asset_id"]: int(a["sample_rate"]) for a in product["assets"]}
        self.views = {c["clip_id"]: clip_view(c, rates[c["asset_id"]])
                      for c in product["clips"]}
        self._validate_backups()

    def _validate_backups(self) -> None:
        known_sentences = {s.sentence_id for s in self.sentences}
        for bk in self.backups:
            if not set(bk.covers) <= known_sentences:
                raise ScheduleError(f"备用 {bk.backup_id} 引用了未知句: {bk.covers}")
            if bk.slot() not in self.shifts:
                raise ScheduleError(
                    f"备用 {bk.backup_id} 引用未登记录音班 {bk.slot()}")

    # ----------------------------- 句级授权状态 ----------------------------- #
    def _sentence_clip_ids(self, s: Sentence) -> list[str]:
        return [cid for cid in s.clip_ids if cid in self.views]

    def sentence_unlicensed_points(self, s: Sentence) -> dict[str, list[int]]:
        """句中每个 clip 的未授权输出点（旁白逐点静掉、整句采访触发替换/静掉）。"""
        out: dict[str, list[int]] = {}
        for cid in self._sentence_clip_ids(s):
            pts = unlicensed_output_points(
                self.views[cid], self.n_samples, self.ledger, self.region, self.when)
            if pts:
                out[cid] = pts
        return out

    def sentence_is_clean(self, s: Sentence) -> bool:
        return not self.sentence_unlicensed_points(s)

    def _backups_for(self, sentence_id: str) -> list[BackupRecording]:
        return sorted((b for b in self.backups if sentence_id in b.covers),
                      key=lambda b: b.backup_id)

    # ----------------------------- 枚举 ----------------------------- #
    def enumerate_arrangements(self) -> list[Arrangement]:
        """枚举该地区全部合法安排（含静掉可选项的不同保留数与不同班次数）。"""
        options_per_sentence: list[list[SentenceDecision]] = []
        for s in self.sentences:
            options_per_sentence.append(self._options_for(s))

        results: list[Arrangement] = []
        seen: set[tuple[Any, ...]] = set()

        def walk(i: int, decisions: dict[str, SentenceDecision],
                 slots: set[tuple[str, str]]) -> None:
            if i == len(self.sentences):
                arr = self._build(decisions, slots, feasible=True)
                k = tuple(sorted((sid, d.action, d.backup_id or "")
                                 for sid, d in decisions.items()))
                if k not in seen:
                    seen.add(k)
                    results.append(arr)
                return
            s = self.sentences[i]
            for dec in options_per_sentence[i]:
                if dec.slot is not None:
                    if dec.slot in slots:
                        continue  # 同一时段只能实际交一条
                    if self.shifts[dec.slot].deadline_tick < self.when:
                        continue  # 赶不上当班（截稿已过）
                new_slots = slots | ({dec.slot} if dec.slot else set())
                decisions[s.sentence_id] = dec
                walk(i + 1, decisions, new_slots)
                del decisions[s.sentence_id]

        walk(0, {}, set())
        if not results:
            return [self._infeasible()]
        results.sort(key=lambda a: (-a.sentences_heard, a.shifts_used,
                                    tuple(sorted(a.used_slots))))
        return results

    def _options_for(self, s: Sentence) -> list[SentenceDecision]:
        clean = self.sentence_is_clean(s)
        opts: list[SentenceDecision] = []
        if clean:
            # 原声全部已授权：旁白与整句采访都直接用原声，不占班
            opts.append(SentenceDecision(
                s.sentence_id, s.kind, s.required, audible=True,
                action="per_sample_gate" if s.kind == "narration" else "original"))
            return opts

        if s.kind == "narration":
            # 可独立剪的旁白：静掉受限贡献即可，句子仍可听到（其余部分保留）
            opts.append(SentenceDecision(
                s.sentence_id, s.kind, s.required, audible=True,
                action="per_sample_gate"))
            return opts

        # 整句采访：要么整句替换为已授权备用，要么整句静掉
        for bk in self._backups_for(s.sentence_id):
            opts.append(SentenceDecision(
                s.sentence_id, s.kind, s.required, audible=True,
                action="replace", backup_id=bk.backup_id, slot=bk.slot()))
        if not s.required:
            opts.append(SentenceDecision(
                s.sentence_id, s.kind, s.required, audible=False, action="mute"))
        if not opts:
            # 必播句又没有任何可用备用 -> 该句无合法选择，整体不可行
            raise ScheduleError(
                f"必播整句 {s.sentence_id} 在地区 {self.region} 无授权且无可用备用")
        return opts

    def _build(self, decisions: dict[str, SentenceDecision],
               slots: set[tuple[str, str]], feasible: bool,
               reason: str = "") -> Arrangement:
        heard = sum(1 for d in decisions.values() if d.audible)
        return Arrangement(
            region=self.region,
            decisions=dict(decisions),
            used_slots=set(slots),
            shifts_used=len(slots),
            sentences_heard=heard,
            feasible=feasible,
            reason=reason,
        )

    def _infeasible(self) -> Arrangement:
        decisions = {
            s.sentence_id: SentenceDecision(
                s.sentence_id, s.kind, s.required, audible=False, action="blocked")
            for s in self.sentences}
        return self._build(decisions, set(), feasible=False,
                           reason="存在必播整句无法保住（无授权且无可用备用/班次）")

    # ----------------------------- Pareto 前沿与推荐 ----------------------------- #
    @staticmethod
    def pareto_front(arrangements: list[Arrangement]) -> list[Arrangement]:
        """两维得失：shifts_used 越小越好，sentences_heard 越大越好。

        前沿上的任一安排都不被另一安排“同时不更费工时且不更少先”，即不存在
        既浪费工时又少保留句子的安排。
        """
        front: list[Arrangement] = []
        for a in arrangements:
            if not a.feasible:
                continue
            dominated = False
            for b in arrangements:
                if b is a or not b.feasible:
                    continue
                no_worse = (b.shifts_used <= a.shifts_used
                            and b.sentences_heard >= a.sentences_heard)
                strictly_better = (b.shifts_used < a.shifts_used
                                   or b.sentences_heard > a.sentences_heard)
                if no_worse and strictly_better:
                    dominated = True
                    break
            if not dominated:
                front.append(a)
        front.sort(key=lambda a: (-a.sentences_heard, a.shifts_used,
                                  tuple(sorted(a.used_slots))))
        return front

    def recommended(self, arrangements: list[Arrangement] | None = None) -> Arrangement:
        """采用安排：先最大化保留句数（编辑关心），再最小化占用工时（录音师关心）。"""
        arrangements = arrangements or self.enumerate_arrangements()
        feasible = [a for a in arrangements if a.feasible]
        if not feasible:
            return next(a for a in arrangements if not a.feasible)
        return min(feasible, key=lambda a: (-a.sentences_heard, a.shifts_used,
                                            tuple(sorted(a.used_slots))))

    def alternative(self, adopted: Arrangement,
                    arrangements: list[Arrangement] | None = None) -> Arrangement | None:
        """另一份合法安排：必须在工时与保留句数两项上有不同得失（Pareto 前沿上的另一点）。"""
        arrangements = arrangements or self.enumerate_arrangements()
        front = self.pareto_front(arrangements)
        for a in front:
            if a.key() == adopted.key():
                continue
            if (a.shifts_used, a.sentences_heard) != (
                    adopted.shifts_used, adopted.sentences_heard):
                return a
        return None
