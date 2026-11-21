"""audiomix 命令行：素材登记 / 编辑 / 发布 / 渲染 / 读取 / 验证 / 外送制作线。

有理数参数统一接受 '1/3'、'0.25'、'7' 等字符串（内部精确 Fraction）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from . import assets as assets_mod
from .archive import ArchiveStore
from .assets import AssetRegistry
from .engine import (
    OUTPUT_RATE, render_full_vectorized, render_reference, validate_product,
)
from .export_counter import DispatchOffice, arrangement_from_ticket
from .export_licenses import LicenseLedger
from .export_program import ExportStudio
from .export_schedule import BackupRecording, Sentence, Shift
from .rationals import as_r, dump_r
from .timeline import TimelineEditor, ValidationError
from .engine import load_product


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _registry(root: str) -> AssetRegistry:
    return AssetRegistry(root)


# ----------------------------- 素材命令 ----------------------------- #

def cmd_asset_seq(p):
    reg = _registry(p.root)
    values = [int(v) for v in p.values.split(",") if v.strip() != ""]
    rec = reg.register(assets_mod.sequence(values), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_ramp(p):
    reg = _registry(p.root)
    rec = reg.register(assets_mod.ramp(p.rate, p.n, p.start, p.stop), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_tone(p):
    reg = _registry(p.root)
    rec = reg.register(assets_mod.tone(p.rate, p.freq, p.seconds, p.amp), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_silence(p):
    reg = _registry(p.root)
    rec = reg.register(assets_mod.silence(p.rate, p.seconds), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_list(p):
    _print([r.to_json() for r in _registry(p.root).all()])


def cmd_asset_show(p):
    _print(_registry(p.root).get(p.asset_id).to_json())


# ----------------------------- 编辑命令 ----------------------------- #

def _editor(p) -> TimelineEditor:
    return TimelineEditor(p.root, _registry(p.root), name=p.name)


def _clip_spec_from_args(p) -> dict:
    spec = {"clip_id": p.clip_id, "asset_id": p.asset_id, "a": p.a, "b": p.b,
            "start": p.start}
    for key in ("speed", "gain", "fade_in", "fade_out"):
        val = getattr(p, key, None)
        if val is not None:
            spec[key] = val
    return spec


def cmd_tl_insert(p):
    ed = _editor(p)
    try:
        ids = ed.insert(_clip_spec_from_args(p))
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"inserted": ids, "editor_seq": ed._seq})


def cmd_tl_move(p):
    ed = _editor(p)
    try:
        ed.move(p.clip_id, p.new_start)
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"moved": p.clip_id, "editor_seq": ed._seq})


def cmd_tl_modify(p):
    ed = _editor(p)
    changes = {}
    for item in p.set:
        k, v = item.split("=", 1)
        k = k.strip()
        if k in ("a", "b"):
            changes[k] = int(v)
        else:
            changes[k] = v
    try:
        ed.modify(p.clip_id, changes)
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"modified": p.clip_id, "editor_seq": ed._seq})


def cmd_tl_delete(p):
    ed = _editor(p)
    try:
        ed.delete(p.clip_id)
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"deleted": p.clip_id, "editor_seq": ed._seq})


def cmd_tl_query(p):
    _print(_editor(p).query_interval(p.lo, p.hi))


def cmd_tl_at(p):
    _print(_editor(p).query_at(p.time))


def cmd_tl_publish(p):
    ed = _editor(p)
    product = ed.publish()
    ed.save_product(product, p.out)
    _print({"product_id": product["product_id"], "path": p.out,
            "clips": len(product["clips"]), "assets": len(product["assets"])})


# ----------------------------- 渲染/读取/验证命令 ----------------------------- #

def _load_pcm_for_product(product_path: str, registry_root: str) -> tuple[dict, dict]:
    from .engine import load_product
    product = load_product(product_path)
    reg = AssetRegistry(registry_root)
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
    return product, pcm


def cmd_render(p):
    store = ArchiveStore(p.archive_root)
    info = store.render_from_files(p.product, AssetRegistry(p.registry_root),
                                   as_r(p.T), render_id=p.render_id)
    _print(info)
    store.close()


def cmd_render_list(p):
    store = ArchiveStore(p.archive_root)
    _print(store.list_renders())
    store.close()


def cmd_render_show(p):
    store = ArchiveStore(p.archive_root)
    _print(store.render_meta(p.render_id))
    store.close()


def cmd_read(p):
    store = ArchiveStore(p.archive_root)
    pcm = store.read_samples(p.render_id, p.start, p.end)
    result = {"render_id": p.render_id, "range": [p.start, p.end],
              "samples": [int(v) for v in pcm]}
    if p.wav:
        # 范围导出 WAV
        import wave, os
        os.makedirs(os.path.dirname(os.path.abspath(p.wav)) or ".", exist_ok=True)
        with wave.open(p.wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(OUTPUT_RATE)
            wf.writeframes(pcm.tobytes())
        result["wav"] = p.wav
    _print(result)
    store.close()


def cmd_trace(p):
    store = ArchiveStore(p.archive_root)
    _print(store.read_trace(p.render_id, p.start, p.end))
    store.close()


def cmd_export_wav(p):
    store = ArchiveStore(p.archive_root)
    path = store.export_wav(p.render_id, p.out)
    _print({"render_id": p.render_id, "wav": path})
    store.close()


def cmd_verify_product(p):
    product, pcm = _load_pcm_for_product(p.product, p.registry_root)
    validate_product(product, pcm)
    _print({"valid": True, "product_id": product["product_id"],
            "clips": len(product["clips"])})


def cmd_verify_integrity(p):
    store = ArchiveStore(p.archive_root)
    store.verify_block_pcm_integrity()
    _print({"integrity_ok": True, "blocks": store.block_count(),
            "renders": len(store.list_renders())})
    store.close()


def cmd_render_compare(p):
    """整段向量化渲染 vs 独立逐样本解释器，逐样本一致性核对。"""
    product, pcm = _load_pcm_for_product(p.product, p.registry_root)
    T = as_r(p.T)
    ref = render_reference(product, pcm, T)
    fast = render_full_vectorized(product, pcm, T)
    mismatch = int(np.count_nonzero(ref != fast))
    _print({"T": dump_r(T), "n_samples": len(ref),
            "mismatch_samples": mismatch,
            "identical": bool(mismatch == 0)})


# ----------------------------- 外送制作线命令 ----------------------------- #

def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _ledger(path):
    return LicenseLedger(path)


def cmd_lic_grant(p):
    ledger = _ledger(p.ledger)
    regions = tuple(p.regions.split(","))
    lic = ledger.grant(p.license_id, p.asset_id, p.lo, p.hi, regions,
                       p.valid_from, p.valid_until, note=p.note)
    _print(lic.to_json())


def cmd_lic_revoke(p):
    ledger = _ledger(p.ledger)
    lic = ledger.revoke(p.license_id, p.at)
    _print(lic.to_json())


def cmd_lic_list(p):
    _print([lic.to_json() for lic in _ledger(p.ledger).all_licenses()])


def _load_studio_inputs(p):
    """从产品/句单/备用/班表 JSON 装载一个 ExportStudio（柜台可选）。"""
    product = load_product(p.product)
    reg = AssetRegistry(p.registry_root)
    ledger = _ledger(p.ledger)
    sentences = [Sentence(s["sentence_id"], tuple(s["clip_ids"]), s["kind"],
                          bool(s["required"])) for s in _load_json(p.sentences)]
    backups = [BackupRecording(
        b["backup_id"], b["asset_id"], tuple(b["covers"]), b["engineer_id"],
        b["shift_id"], int(b["start_tick"]), int(b["end_tick"]),
        b.get("note", "")) for b in _load_json(p.backups)]
    shifts = [Shift(s["engineer_id"], s["shift_id"], int(s["start_tick"]),
                    int(s["end_tick"]), int(s["deadline_tick"]),
                    s.get("note", "")) for s in _load_json(p.shifts)]
    office = DispatchOffice(shifts, backups, p.office) if p.office else None
    studio = ExportStudio(p.studio_root, product, reg, ledger, sentences,
                          backups, as_r(p.T), office=office)
    return studio, ledger, office

def cmd_export_audit(p):
    studio, _l, _o = _load_studio_inputs(p)
    _print(studio.audit_unlicensed(p.region, p.at, clip_id=p.clip_id))


def cmd_export_plan(p):
    studio, _l, _o = _load_studio_inputs(p)
    planner = studio.planner(p.region, p.at)
    arrangements = planner.enumerate_arrangements()
    adopted = planner.recommended(arrangements)
    alternative = planner.alternative(adopted, arrangements)
    _print({
        "region": p.region,
        "all_arrangements": [a.to_json() for a in arrangements],
        "pareto_front": [a.to_json() for a in planner.pareto_front(arrangements)],
        "adopted": adopted.to_json(),
        "alternative": alternative.to_json() if alternative else None,
    })


def _studio_arrangement(studio, region, when, arrangement_path):
    if arrangement_path:
        from audiomix.export_schedule import SentenceDecision, Arrangement
        body = _load_json(arrangement_path)
        decisions = {}
        for d in body["decisions"]:
            slot = None if d["slot"] is None else tuple(d["slot"])
            decisions[d["sentence_id"]] = SentenceDecision(
                d["sentence_id"], d["kind"], d["required"], d["audible"],
                d["action"], d.get("backup_id"), slot)
        return Arrangement(region, decisions,
                           {tuple(s) for s in body["used_slots"]},
                           body["shifts_used"], body["sentences_heard"],
                           body["feasible"], body.get("reason", ""))
    return studio.planner(region, when).recommended()


def cmd_counter_preview(p):
    studio, _l, office = _load_studio_inputs(p)
    arrangement = _studio_arrangement(studio, p.region, p.at, p.arrangement)
    _print(office.preview(p.region, arrangement, p.at))


def cmd_counter_issue(p):
    studio, _l, office = _load_studio_inputs(p)
    arrangement = _studio_arrangement(studio, p.region, p.at, p.arrangement)
    ticket = office.issue(p.region, arrangement, p.at, ticket_id=p.ticket_id)
    _print(ticket.to_json())


def cmd_counter_status(p):
    _studio, _l, office = _load_studio_inputs(p)
    _print(office.status_of(p.region))


def cmd_counter_continue(p):
    _studio, _l, office = _load_studio_inputs(p)
    _print(office.when_can_continue(p.region, p.at))


def cmd_export_deliver(p):
    studio, _l, office = _load_studio_inputs(p)
    ticket = None
    if p.ticket_id:
        ticket = office.tickets[p.ticket_id]
        arrangement = arrangement_from_ticket(ticket)
    else:
        arrangement = _studio_arrangement(studio, p.region, p.at, p.arrangement)
    info = studio.deliver(p.region, p.at, arrangement, ticket=ticket,
                          render_id=p.render_id)
    _print(info)


def cmd_export_freeze(p):
    studio, _l, office = _load_studio_inputs(p)
    office.freeze(p.at)
    _print(studio.freeze_issued(p.at))

# ----------------------------- 参数解析 ----------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="audiomix",
                                 description="多采样率剪辑时间轴与真实混音档案")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_asset(sp, name, fn):
        q = sp.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--rate", type=int, required=True, choices=assets_mod.ALLOWED_RATES)
        q.add_argument("--note", default="")
        q.set_defaults(fn=fn)
        return q

    q = add_asset(sub, "asset-seq", cmd_asset_seq)
    q.add_argument("--values", required=True, help="逗号分隔短整数，如 10,20,30")
    q = add_asset(sub, "asset-ramp", cmd_asset_ramp)
    q.add_argument("--n", type=int, required=True)
    q.add_argument("--start", type=int, default=-30000)
    q.add_argument("--stop", type=int, default=30000)
    q = add_asset(sub, "asset-tone", cmd_asset_tone)
    q.add_argument("--freq", type=float, required=True)
    q.add_argument("--seconds", type=float, required=True)
    q.add_argument("--amp", type=float, default=0.5)
    q = add_asset(sub, "asset-silence", cmd_asset_silence)
    q.add_argument("--seconds", type=float, required=True)

    q = sub.add_parser("asset-list"); q.add_argument("--root", required=True)
    q.set_defaults(fn=cmd_asset_list)
    q = sub.add_parser("asset-show"); q.add_argument("--root", required=True)
    q.add_argument("--asset-id", required=True); q.set_defaults(fn=cmd_asset_show)

    def tl_parser(name, fn):
        q = sub.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--name", default="main")
        q.set_defaults(fn=fn)
        return q

    q = tl_parser("tl-insert", cmd_tl_insert)
    q.add_argument("--clip-id", required=True); q.add_argument("--asset-id", required=True)
    q.add_argument("--a", type=int, required=True); q.add_argument("--b", type=int, required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--speed"); q.add_argument("--gain")
    q.add_argument("--fade-in", dest="fade_in"); q.add_argument("--fade-out", dest="fade_out")

    q = tl_parser("tl-move", cmd_tl_move)
    q.add_argument("--clip-id", required=True); q.add_argument("--new-start", required=True)
    q = tl_parser("tl-modify", cmd_tl_modify)
    q.add_argument("--clip-id", required=True)
    q.add_argument("--set", action="append", required=True,
                   help="key=value，可多次；key 取 a/b/start/speed/gain/fade_in/fade_out/asset_id")
    q = tl_parser("tl-delete", cmd_tl_delete)
    q.add_argument("--clip-id", required=True)
    q = tl_parser("tl-query", cmd_tl_query)
    q.add_argument("--lo", required=True); q.add_argument("--hi", required=True)
    q = tl_parser("tl-at", cmd_tl_at)
    q.add_argument("--time", required=True)
    q = tl_parser("tl-publish", cmd_tl_publish)
    q.add_argument("--out", required=True)

    q = sub.add_parser("render")
    q.add_argument("--archive-root", required=True)
    q.add_argument("--product", required=True)
    q.add_argument("--registry-root", required=True)
    q.add_argument("--T", required=True); q.add_argument("--render-id")
    q.set_defaults(fn=cmd_render)

    q = sub.add_parser("render-list"); q.add_argument("--archive-root", required=True)
    q.set_defaults(fn=cmd_render_list)
    q = sub.add_parser("render-show"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True); q.set_defaults(fn=cmd_render_show)

    q = sub.add_parser("read"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True)
    q.add_argument("--start", type=int, required=True); q.add_argument("--end", type=int, required=True)
    q.add_argument("--wav"); q.set_defaults(fn=cmd_read)

    q = sub.add_parser("trace"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True)
    q.add_argument("--start", type=int, required=True); q.add_argument("--end", type=int, required=True)
    q.set_defaults(fn=cmd_trace)

    q = sub.add_parser("export-wav"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True); q.add_argument("--out", required=True)
    q.set_defaults(fn=cmd_export_wav)

    q = sub.add_parser("verify-product"); q.add_argument("--product", required=True)
    q.add_argument("--registry-root", required=True); q.set_defaults(fn=cmd_verify_product)

    q = sub.add_parser("verify-integrity"); q.add_argument("--archive-root", required=True)
    q.set_defaults(fn=cmd_verify_integrity)

    q = sub.add_parser("render-compare"); q.add_argument("--product", required=True)
    q.add_argument("--registry-root", required=True); q.add_argument("--T", required=True)
    q.set_defaults(fn=cmd_render_compare)

    # ---- 外送制作线 ---- #
    q = sub.add_parser("lic-grant")
    q.add_argument("--ledger", required=True); q.add_argument("--license-id", required=True)
    q.add_argument("--asset-id", required=True)
    q.add_argument("--lo", type=int, required=True); q.add_argument("--hi", type=int, required=True)
    q.add_argument("--regions", required=True, help="逗号分隔地区，* 为通配")
    q.add_argument("--valid-from", type=int, required=True, dest="valid_from")
    q.add_argument("--valid-until", type=int, required=True, dest="valid_until")
    q.add_argument("--note", default=""); q.set_defaults(fn=cmd_lic_grant)

    q = sub.add_parser("lic-revoke")
    q.add_argument("--ledger", required=True); q.add_argument("--license-id", required=True)
    q.add_argument("--at", type=int, required=True); q.set_defaults(fn=cmd_lic_revoke)

    q = sub.add_parser("lic-list"); q.add_argument("--ledger", required=True)
    q.set_defaults(fn=cmd_lic_list)

    def export_inputs(sp, with_region=True):
        sp.add_argument("--product", required=True)
        sp.add_argument("--registry-root", required=True)
        sp.add_argument("--ledger", required=True)
        sp.add_argument("--sentences", required=True, help="句单 JSON")
        sp.add_argument("--backups", required=True, help="备用录音 JSON")
        sp.add_argument("--shifts", required=True, help="录音班 JSON")
        sp.add_argument("--studio-root", required=True)
        sp.add_argument("--office"); sp.add_argument("--T", required=True)
        if with_region:
            sp.add_argument("--region", required=True)
        sp.add_argument("--at", type=int, required=True)

    q = sub.add_parser("export-audit"); export_inputs(q)
    q.add_argument("--clip-id"); q.set_defaults(fn=cmd_export_audit)

    q = sub.add_parser("export-plan"); export_inputs(q)
    q.set_defaults(fn=cmd_export_plan)

    q = sub.add_parser("counter-preview"); export_inputs(q)
    q.add_argument("--arrangement", help="安排 JSON；缺省用推荐安排")
    q.set_defaults(fn=cmd_counter_preview)

    q = sub.add_parser("counter-issue"); export_inputs(q)
    q.add_argument("--arrangement"); q.add_argument("--ticket-id")
    q.set_defaults(fn=cmd_counter_issue)

    q = sub.add_parser("counter-status"); export_inputs(q)
    q.set_defaults(fn=cmd_counter_status)

    q = sub.add_parser("counter-continue"); export_inputs(q)
    q.set_defaults(fn=cmd_counter_continue)

    q = sub.add_parser("export-deliver"); export_inputs(q)
    q.add_argument("--arrangement"); q.add_argument("--ticket-id")
    q.add_argument("--render-id"); q.set_defaults(fn=cmd_export_deliver)

    q = sub.add_parser("export-freeze"); export_inputs(q, with_region=False)
    q.set_defaults(fn=cmd_export_freeze)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
