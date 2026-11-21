"""外送制作线（4/5）：分地区独立签发柜台与录音班调度。

每个地区有独立柜台，共享工作室的同一份全局班表。柜台规则：

- **预览不锁班**：制作单预览时只显示“哪些班次仍能安排”，工作室可给预览台短时留班
  （soft hold，可被真实签单抢占）；尚未签发的台可以任意改约。
- **签发即不可撤销**：签发是一次原子操作 —— 当场重新调度，把本台预约的每个录音班
  与全局“已真实占用”集合核对；被别台先签走的班改派仍可用的同类班，没有可改派的班
  且该句必播则该台赶不上当班（整单失败）。
- **同一时段只能有一份真实签单**：两台都看见最后一个早班时，先签发者得到它，
  另一台在自己的截稿前若能改派晚班则继续办理，否则错过当班。
- **播出窗口过后留住实际签发版本**：freeze() 把当时的真实签单冻结为不可变档案，
  之后许可再撤回也不改写已冻结节目。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict, replace as dc_replace
from typing import Any

from .export_schedule import Arrangement, BackupRecording, Shift


@dataclass
class ShiftRequest:
    sentence_id: str
    backup_id: str
    slot: tuple[str, str]

    def to_json(self) -> dict[str, Any]:
        return {"sentence_id": self.sentence_id, "backup_id": self.backup_id,
                "slot": list(self.slot)}


@dataclass
class IssuedTicket:
    ticket_id: str
    region: str
    issued_tick: int
    arrangement: dict[str, Any]
    # 每个预约班次的最终去向：original_slot -> 实际占用 slot（或 None=错过当班）
    slot_outcomes: list[dict[str, Any]]
    missed: bool
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class CounterError(ValueError):
    pass


class DispatchOffice:
    """全工作室一个调度办公室，内部为每个地区维护独立柜台状态。"""

    def __init__(self, shifts: list[Shift], backups: list[BackupRecording],
                 path: str | None = None):
        self.shifts = {s.slot(): s for s in shifts}
        self.backups = {b.backup_id: b for b in backups}
        self.path = path
        # 全局真实占用：slot -> (region, ticket_id)
        self.occupied: dict[tuple[str, str], tuple[str, str]] = {}
        # 短时留班：slot -> region（可被真实签单抢占，仅用于预览提示）
        self.soft_holds: dict[tuple[str, str], str] = {}
        self.counters: dict[str, dict[str, Any]] = {}
        self.tickets: dict[str, IssuedTicket] = {}
        self.frozen = False
        self.frozen_at: int | None = None
        if path and os.path.exists(path):
            self._load()

    # ----------------------------- 持久化 ----------------------------- #
    def _load(self) -> None:
        data = json.load(open(self.path, encoding="utf-8"))
        self.occupied = {tuple(json.loads(k)): tuple(v)
                         for k, v in data["occupied"].items()}
        self.soft_holds = {tuple(json.loads(k)): v
                           for k, v in data["soft_holds"].items()}
        self.counters = data["counters"]
        self.frozen = data["frozen"]
        self.frozen_at = data.get("frozen_at")
        for tobj in data["tickets"]:
            self.tickets[tobj["ticket_id"]] = IssuedTicket(**tobj)

    def save(self) -> None:
        if self.path is None:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        data = {
            "occupied": {json.dumps(list(k)): list(v)
                         for k, v in self.occupied.items()},
            "soft_holds": {json.dumps(list(k)): v
                           for k, v in self.soft_holds.items()},
            "counters": self.counters,
            "frozen": self.frozen,
            "frozen_at": self.frozen_at,
            "tickets": [t.to_json() for t in self._sorted_tickets()],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=_json_default)
        os.replace(tmp, self.path)

    def _sorted_tickets(self) -> list[IssuedTicket]:
        return [self.tickets[k] for k in sorted(self.tickets)]

    # ----------------------------- 柜台预览 ----------------------------- #
    def _ensure_counter(self, region: str) -> dict[str, Any]:
        return self.counters.setdefault(region, {
            "region": region, "previews": [], "requests": [],
            "ticket_id": None, "status": "idle"})

    def available_shifts(self, region: str, when: int) -> list[dict[str, Any]]:
        """预览：列出当前仍能安排的班次（真实占用与已过截稿的班排除；留班只做标注）。"""
        out = []
        for slot, sh in sorted(self.shifts.items(), key=lambda kv: (kv[1].start_tick,
                                                                    kv[0][1])):
            holder = self.occupied.get(slot)
            held_by = self.soft_holds.get(slot)
            out.append({
                "engineer_id": slot[0], "shift_id": slot[1],
                "window": [sh.start_tick, sh.end_tick],
                "deadline_tick": sh.deadline_tick,
                "occupied": holder is not None,
                "occupied_by": holder[0] if holder else None,
                "soft_held_by": held_by,
                "open_for_this_region": holder is None and sh.deadline_tick >= when,
                "past_deadline": sh.deadline_tick < when,
            })
        return out

    def preview(self, region: str, arrangement: Arrangement, when: int) -> dict[str, Any]:
        """制作单预览：登记意向（可改约），返回仍能安排的班次与短时留班提示。"""
        if self.frozen:
            raise CounterError("播出窗口已过，柜台冻结，不再受理预览")
        counter = self._ensure_counter(region)
        if counter["status"] == "issued":
            raise CounterError(f"地区 {region} 已签发，预约不可撤销")
        requests = self._requests_from_arrangement(arrangement)
        counter["requests"] = [r.to_json() for r in requests]
        counter["status"] = "previewed"
        counter["last_preview_tick"] = when
        # 实现选择：给预览台短时留班（可被真实签单抢占）
        for r in requests:
            self.soft_holds[r.slot] = region
        avail = self.available_shifts(region, when)
        self.save()
        return {
            "region": region,
            "at_tick": when,
            "requests": [r.to_json() for r in requests],
            "bookable_shifts": [a for a in avail if a["open_for_this_region"]],
            "soft_held": [list(r.slot) for r in requests],
            "note": "留班为短时性质，未签发前可被别台真实签单抢占，本台可改约",
        }

    def change_request(self, region: str, arrangement: Arrangement, when: int) -> dict:
        """尚未签发的台改约：清掉旧留班再按新安排预览。"""
        counter = self._ensure_counter(region)
        if counter["status"] == "issued":
            raise CounterError(f"地区 {region} 已签发，不可改约")
        for slot, holder in list(self.soft_holds.items()):
            if holder == region:
                del self.soft_holds[slot]
        return self.preview(region, arrangement, when)

    # ----------------------------- 原子签发 ----------------------------- #
    def issue(self, region: str, arrangement: Arrangement, when: int,
              ticket_id: str | None = None) -> IssuedTicket:
        """原子签发：逐班重新调度，真实占用冲突当场改派；必播句无班可改则整单失败。"""
        if self.frozen:
            raise CounterError("播出窗口已过，柜台冻结，不再受理签发")
        counter = self._ensure_counter(region)
        if counter["status"] == "issued":
            raise CounterError(f"地区 {region} 已有真实签单 {counter['ticket_id']}")

        requests = self._requests_from_arrangement(arrangement)
        decisions_by_id = {sid: dec for sid, dec in arrangement.decisions.items()}
        outcomes: list[dict[str, Any]] = []
        tentative: dict[tuple[str, str], tuple[str, str]] = {}
        actual_decisions = {sid: dec for sid, dec in arrangement.decisions.items()}
        muted_sentences: list[str] = []
        missed = False
        miss_reasons: list[str] = []

        for req in requests:
            sh = self.shifts[req.slot]
            final_slot = req.slot
            final_backup = req.backup_id
            note = "按预约占用"
            busy = (req.slot in self.occupied or req.slot in tentative
                    or sh.deadline_tick < when)
            if busy:
                # 重新调度：找任何能替该句、班仍开放、截稿未过的备用录音
                alt = self._find_alternative(req, tentative, when)
                if alt is None:
                    dec = decisions_by_id[req.sentence_id]
                    if not dec.required:
                        # 播出单允许的可选整句：放弃替换，回退为整句静掉
                        final_slot = None  # type: ignore[assignment]
                        final_backup = None  # type: ignore[assignment]
                        actual_decisions[req.sentence_id] = dc_replace(
                            dec, audible=False, action="mute",
                            backup_id=None, slot=None)
                        muted_sentences.append(req.sentence_id)
                        note = "无班可改派，按播出单整句静掉"
                    else:
                        final_slot = None  # type: ignore[assignment]
                        missed = True
                        miss_reasons.append(
                            f"必播句 {req.sentence_id} 的班 {req.slot} 已被先签/不可用且无班可改派")
                        note = "错过当班"
                else:
                    final_slot, final_backup = alt
                    # 同步实际安排：该句改用另一份备用、占用改派后的班
                    actual_decisions[req.sentence_id] = dc_replace(
                        decisions_by_id[req.sentence_id],
                        backup_id=final_backup, slot=final_slot)
                    note = f"原班 {list(req.slot)} 被占，改用备用 {final_backup} 改派 {list(final_slot)}"
            outcomes.append({"sentence_id": req.sentence_id,
                             "requested_backup": req.backup_id,
                             "backup_id": final_backup,
                             "requested_slot": list(req.slot),
                             "final_slot": None if final_slot is None else list(final_slot),
                             "note": note})
            if final_slot is not None:
                tentative[final_slot] = (region, ticket_id or "pending")

        # 实际落定的安排（可能因改派换备用、因无班把可选句静掉）
        actual_arrangement = Arrangement(
            region=region,
            decisions=actual_decisions,
            used_slots=set(tentative),
            shifts_used=len(tentative),
            sentences_heard=sum(1 for d in actual_decisions.values() if d.audible),
            feasible=not missed,
            reason=("可选句整句静掉: " + ",".join(muted_sentences))
                   if muted_sentences else "")

        tid = ticket_id or ("T-" + hashlib.sha256(
            (region + str(when) + json.dumps([o["final_slot"] for o in outcomes],
                                             sort_keys=True)).encode()
        ).hexdigest()[:10])

        if missed:
            # 整单失败：不落地任何真实占用，柜台保持可改约
            counter["status"] = "missed"
            counter["last_miss_tick"] = when
            ticket = IssuedTicket(
                ticket_id=tid, region=region, issued_tick=when,
                arrangement=actual_arrangement.to_json(), slot_outcomes=outcomes,
                missed=True, reason="；".join(miss_reasons))
            self.tickets[tid] = ticket
            self._release_soft_holds(region)
            self.save()
            return ticket

        # 成功：落地真实占用（同一时段全局唯一），预约自此不可撤销
        for slot in tentative:
            self.occupied[slot] = (region, tid)
        self._release_soft_holds(region)
        ticket = IssuedTicket(
            ticket_id=tid, region=region, issued_tick=when,
            arrangement=actual_arrangement.to_json(), slot_outcomes=outcomes, missed=False)
        self.tickets[tid] = ticket
        counter["status"] = "issued"
        counter["ticket_id"] = tid
        counter["issued_tick"] = when
        self.save()
        return ticket

    def _release_soft_holds(self, region: str) -> None:
        for slot, holder in list(self.soft_holds.items()):
            if holder == region:
                del self.soft_holds[slot]

    def _find_alternative(self, req: ShiftRequest,
                          tentative: dict[tuple[str, str], tuple[str, str]],
                          when: int) -> tuple[tuple[str, str], str] | None:
        """改派条件：存在另一份能替该句的备用录音，其班仍开放（无真实/本次暂定
        占用）、截稿未过，且班窗确实包含该备用的占用窗。跨录音师也允许 ——
        每份备用自己标明占用哪个录音师的哪个时段。"""
        candidates = []
        for bk in self.backups.values():
            if req.sentence_id not in bk.covers:
                continue
            slot = bk.slot()
            if slot not in self.shifts:
                continue
            sh = self.shifts[slot]
            # 改派必须换班：原预约班（含原备用自身的班）已被先签/过期，不能派回；
            # 别台真实占用与本单暂定占用同样排除
            if slot == req.slot or slot in self.occupied or slot in tentative:
                continue
            if sh.deadline_tick < when:
                continue
            if not (sh.start_tick <= bk.start_tick and bk.end_tick <= sh.end_tick):
                continue
            candidates.append((sh.start_tick, slot, bk.backup_id))
        if not candidates:
            return None
        candidates.sort()
        _, slot, backup_id = candidates[0]
        return slot, backup_id

    def _requests_from_arrangement(self, arrangement: Arrangement) -> list[ShiftRequest]:
        reqs = []
        for sid, dec in sorted(arrangement.decisions.items()):
            if dec.action == "replace" and dec.backup_id and dec.slot:
                reqs.append(ShiftRequest(sid, dec.backup_id, dec.slot))
        return reqs

    # ----------------------------- 查询与冻结 ----------------------------- #
    def status_of(self, region: str) -> dict[str, Any]:
        counter = self.counters.get(region)
        ticket_id = counter["ticket_id"] if counter else None
        ticket = self.tickets.get(ticket_id) if ticket_id else None
        return {
            "region": region,
            "counter_status": counter["status"] if counter else "idle",
            "ticket_id": ticket_id,
            "ticket": ticket.to_json() if ticket else None,
            "my_occupied_slots": [list(s) for s, v in self.occupied.items()
                                  if v[0] == region],
        }

    def when_can_continue(self, region: str, when: int) -> dict[str, Any]:
        """另一柜台何时能继续办理：列出当前仍开放、截稿未过的班与最近可签发时刻。"""
        open_shifts = [a for a in self.available_shifts(region, when)
                       if a["open_for_this_region"]]
        return {
            "region": region,
            "at_tick": when,
            "open_shifts": open_shifts,
            "next_bookable_tick": min(
                (self.shifts[(a["engineer_id"], a["shift_id"])].start_tick
                 for a in open_shifts), default=None),
            "can_issue_now": bool(open_shifts),
        }

    def freeze(self, when: int) -> dict[str, Any]:
        """播出窗口过后冻结：留住当时实际签发的版本，之后不再受理任何调度。"""
        self.frozen = True
        self.frozen_at = when
        self.soft_holds.clear()
        self.save()
        return {"frozen": True, "frozen_at": when,
                "tickets": [t.ticket_id for t in self._sorted_tickets()
                            if not t.missed]}

    def is_frozen(self) -> bool:
        return self.frozen


def _json_default(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"不可序列化对象: {obj!r}")


def arrangement_from_ticket(ticket: IssuedTicket) -> Arrangement:
    """从真实签单重建当时落定的安排（窗口后重开真实节目时用）。"""
    from .export_schedule import SentenceDecision
    body = ticket.arrangement
    decisions = {}
    for d in body["decisions"]:
        slot = None if d["slot"] is None else tuple(d["slot"])
        decisions[d["sentence_id"]] = SentenceDecision(
            sentence_id=d["sentence_id"], kind=d["kind"], required=d["required"],
            audible=d["audible"], action=d["action"], backup_id=d["backup_id"],
            slot=slot)
    return Arrangement(
        region=body["region"], decisions=decisions,
        used_slots={tuple(s) for s in body["used_slots"]},
        shifts_used=body["shifts_used"], sentences_heard=body["sentences_heard"],
        feasible=body["feasible"], reason=body.get("reason", ""))
