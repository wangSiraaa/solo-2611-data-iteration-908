"""外送制作线（1/5）：使用许可台账。

授权员面对的语义：
- 许可按“原素材稳定身份 + 源样本半开区间 [lo, hi)”签发；asset_id 就是素材正文
  SHA-256（见 assets.py），同一字节永远同一身份。
- 同一时段可以并存任意多张独立许可；只有地区适用、且在查询时刻仍有效的许可才算数。
- 台账只追加修订事件（签发/撤回），每次事件令 revision +1；撤回一张许可只把它自己
  置为已撤回，绝不影响与其重叠的其余许可。
- 许可的时间窗也是左闭右开 [valid_from, valid_until)，逻辑时刻用整数 tick。
- 地区 ``"*"`` 为通配地区（对全部收听地区适用）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any

WILDCARD_REGION = "*"


@dataclass(frozen=True)
class License:
    license_id: str
    asset_id: str            # 原素材稳定身份
    lo: int                  # 源样本半开区间 [lo, hi)
    hi: int
    regions: tuple[str, ...]  # 适用地区；("*",) 表示全部地区
    valid_from: int          # 生效 tick（含）
    valid_until: int         # 失效 tick（不含）
    note: str = ""
    status: str = "active"   # active / revoked
    issued_at: int = 0
    revoked_at: int | None = None

    def applies_region(self, region: str) -> bool:
        return WILDCARD_REGION in self.regions or region in self.regions

    def in_window(self, when: int) -> bool:
        return self.valid_from <= when < self.valid_until

    def covers_sample(self, source_index: int) -> bool:
        return self.lo <= source_index < self.hi

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["regions"] = list(self.regions)
        return d


def _license_from_json(obj: dict[str, Any]) -> License:
    return License(
        license_id=obj["license_id"],
        asset_id=obj["asset_id"],
        lo=int(obj["lo"]),
        hi=int(obj["hi"]),
        regions=tuple(obj["regions"]),
        valid_from=int(obj["valid_from"]),
        valid_until=int(obj["valid_until"]),
        note=obj.get("note", ""),
        status=obj.get("status", "active"),
        issued_at=int(obj.get("issued_at", 0)),
        revoked_at=(None if obj.get("revoked_at") is None else int(obj["revoked_at"])),
    )


class LicenseError(ValueError):
    pass


class LicenseLedger:
    """只追加事件的许可台账，持久化为单个 JSON（重开后状态完整可复核）。"""

    def __init__(self, path: str | None = None):
        self.path = path
        self.revision = 0
        self._licenses: dict[str, License] = {}
        self.events: list[dict[str, Any]] = []
        if path and os.path.exists(path):
            data = json.load(open(path, encoding="utf-8"))
            self.revision = int(data["revision"])
            self.events = data["events"]
            for lobj in data["licenses"]:
                lic = _license_from_json(lobj)
                self._licenses[lic.license_id] = lic

    # ----------------------------- 修订事件 ----------------------------- #
    def grant(self, license_id: str, asset_id: str, lo: int, hi: int,
              regions: tuple[str, ...] | list[str], valid_from: int,
              valid_until: int, note: str = "", issued_at: int | None = None) -> License:
        if license_id in self._licenses:
            raise LicenseError(f"许可编号已存在: {license_id}")
        if not (0 <= lo < hi):
            raise LicenseError(f"源样本区间必须为非空半开区间，收到 [{lo},{hi})")
        if valid_until <= valid_from:
            raise LicenseError("许可时间窗必须为非空左闭右开区间")
        when = valid_from if issued_at is None else issued_at
        lic = License(
            license_id=license_id, asset_id=asset_id, lo=lo, hi=hi,
            regions=tuple(regions), valid_from=valid_from, valid_until=valid_until,
            note=note, status="active", issued_at=when, revoked_at=None,
        )
        self._licenses[license_id] = lic
        self._record("grant", when, {"license": lic.to_json()})
        return lic

    def revoke(self, license_id: str, when: int) -> License:
        """撤回单张许可；与其重叠的其它许可原样保留。"""
        old = self._licenses.get(license_id)
        if old is None:
            raise LicenseError(f"未知许可: {license_id}")
        if old.status == "revoked":
            raise LicenseError(f"许可已撤回，不能重复撤回: {license_id}")
        lic = License(**{**old.to_json(), "status": "revoked", "revoked_at": when})
        self._licenses[license_id] = lic
        self._record("revoke", when, {"license_id": license_id})
        return lic

    def _record(self, kind: str, when: int, payload: dict[str, Any]) -> None:
        self.revision += 1
        self.events.append({"seq": self.revision, "at": when, "op": kind, **payload})
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        data = {
            "revision": self.revision,
            "events": self.events,
            "licenses": [lic.to_json() for lic in
                         sorted(self._licenses.values(), key=lambda x: x.license_id)],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ----------------------------- 查询 ----------------------------- #
    def get(self, license_id: str) -> License:
        return self._licenses[license_id]

    def all_licenses(self) -> list[License]:
        return [self._licenses[k] for k in sorted(self._licenses)]

    def active_licenses(self, region: str, when: int,
                        asset_id: str | None = None) -> list[License]:
        """地区适用、时刻有效、尚未撤回的全部许可（允许相互重叠）。"""
        out = []
        for lic in self._licenses.values():
            if lic.status != "active" or not lic.in_window(when):
                continue
            if not lic.applies_region(region):
                continue
            if asset_id is not None and lic.asset_id != asset_id:
                continue
            out.append(lic)
        return sorted(out, key=lambda x: x.license_id)

    def covering(self, asset_id: str, source_index: int,
                 region: str, when: int) -> list[License]:
        """覆盖某个具体源采样点的全部有效许可（可能有多张，各自独立）。"""
        return [lic for lic in self.active_licenses(region, when, asset_id)
                if lic.covers_sample(source_index)]

    def is_licensed(self, asset_id: str, source_index: int,
                    region: str, when: int) -> bool:
        return bool(self.covering(asset_id, source_index, region, when))
