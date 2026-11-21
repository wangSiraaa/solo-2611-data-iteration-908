"""地方台外送节目制作线：手算与规则测试。

全部输入为本地合成音频（AssetRegistry 生成的短整数序列/常量素材），不引用任何
外部混音结果。覆盖题面的每一条硬语义：

1. 8000Hz 两源采样 200/600、普通速度、无淡化，16000Hz 输出第二采样为 400 且同时
   使用两个源采样；同拍叠完全获准常量 50；甲地区只许第一个源采样且为可剪旁白 ->
   该拍交 50；乙地区另有覆盖第二个源采样的许可 -> 该拍交 450。
2. 两段相消成静音不能据此认定两边原声都可用（使用分别记账，缺权照样静掉）。
3. 整句采访排班：必播句+可静句，早班可录任意一条、晚班只能录必播句；两种合法
   安排在工时/保留句数上得失不同，且都不浪费工时、不少保留句子（Pareto 前沿）。
4. 分地区柜台：预览留班可被抢占；两台竞争最后一个早班，先签发者得，另一台改派
   晚班或赶不上当班；出票后重开双方真实节目。
5. 撤回一张与另一份许可重叠的许可：只影响唯一依赖它的地区，其余地区成片与排班不变。
6. 片尾保持也算使用；淡化零点权重为零，不算使用；半开区间边界严格。
7. 播出窗口过后冻结实际签发版本，事后撤回许可不改写已冻结节目。
"""
from __future__ import annotations

import json
import os
from fractions import Fraction as F

import numpy as np
import pytest

from audiomix.assets import AssetRegistry, sequence
from audiomix.export_counter import DispatchOffice
from audiomix.export_licenses import LicenseLedger, WILDCARD_REGION
from audiomix.export_program import ExportStudio
from audiomix.export_schedule import BackupRecording, Sentence, Shift
from audiomix.export_usage import source_uses_at
from audiomix.timeline import TimelineEditor


# ----------------------------- 公共构造工具 ----------------------------- #

def _studio(tmp_path, name, clips, sentences, backups, shifts, T, ledger=None):
    reg = AssetRegistry(os.path.join(tmp_path, f"reg_{name}"))
    assets = {}
    for aid, values, rate in clips["assets"]:
        assets[aid] = reg.register(sequence(values), rate, note=aid).asset_id
    ed = TimelineEditor(os.path.join(tmp_path, f"tl_{name}"), reg, name=name)
    for spec in clips["timeline"]:
        spec = dict(spec)
        spec["asset_id"] = assets[spec["asset_id"]]
        ed.insert(spec)
    product = ed.publish()
    ledger = ledger or LicenseLedger(
        os.path.join(tmp_path, f"lic_{name}.json"))
    backup_objs = [BackupRecording(
        backup_id=b["backup_id"], asset_id=assets[b["asset"]],
        covers=tuple(b["covers"]), engineer_id=b["engineer_id"],
        shift_id=b["shift_id"], start_tick=b["start_tick"],
        end_tick=b["end_tick"], note=b.get("note", "")) for b in backups]
    office = DispatchOffice(shifts, backup_objs,
                            os.path.join(tmp_path, f"office_{name}.json"))
    studio = ExportStudio(os.path.join(tmp_path, f"studio_{name}"), product, reg,
                          ledger, sentences, backup_objs, T, office=office)
    return studio, assets, ledger, office, product


# ----------------------------- 1. 题面小例：50 与 450 ----------------------------- #

def test_small_example_50_and_450(tmp_path):
    clips = {
        "assets": [
            ("src8", [200, 600], 8000),       # 8000Hz 两个源采样
            ("bed", [50, 50, 50, 50], 16000),  # 完全获准的常量 50
        ],
        "timeline": [
            {"clip_id": "c_narr", "asset_id": "src8", "a": 0, "b": 2, "start": 0},
            {"clip_id": "c_bed", "asset_id": "bed", "a": 0, "b": 4, "start": 0},
        ],
    }
    sentences = [Sentence("s_narr", ("c_narr",), "narration", required=True)]
    studio, assets, ledger, _office, product = _studio(
        tmp_path, "small", clips, sentences, [], [], T=F(4,16000))

    # 常量床：全地区、全采样获准
    ledger.grant("L-bed", assets["bed"], 0, 4, (WILDCARD_REGION,), 0, 1000)
    # 甲地区只允许第一个源采样（半开 [0,1)）
    ledger.grant("L-jia-0", assets["src8"], 0, 1, ("jia",), 0, 1000)
    # 乙地区另有覆盖第二个源采样的许可
    ledger.grant("L-yi-0", assets["src8"], 0, 1, ("yi",), 0, 1000)
    ledger.grant("L-yi-1", assets["src8"], 1, 2, ("yi",), 0, 1000)

    view = studio.views["c_narr"]
    # 输出样本 0 抽点源采样 0；样本 1 等权使用源采样 0 与 1 -> 插值 400
    assert source_uses_at(view, 0) == [(0, F(1))]
    assert source_uses_at(view, 1) == [(0, F(1, 2)), (1, F(1, 2))]
    assert studio.pcm[assets["src8"]].tolist() == [200, 600]

    planner_jia = studio.planner("jia", 10)
    arr_jia = planner_jia.recommended()
    planner_yi = studio.planner("yi", 10)
    arr_yi = planner_yi.recommended()

    pcm_jia = studio.render_region_reference("jia", 10, arr_jia)
    pcm_yi = studio.render_region_reference("yi", 10, arr_yi)
    vec_jia = studio.render_region_vectorized("jia", 10, arr_jia)
    vec_yi = studio.render_region_vectorized("yi", 10, arr_yi)
    assert np.array_equal(pcm_jia, vec_jia) and np.array_equal(pcm_yi, vec_yi)

    # 甲：样本 0 同拍 200+50=250；样本 1 缺第二源采样 -> 整条旁白静掉，只剩 50；
    #     样本 2、3 均为片尾保持源采样 1，同样缺权静掉，只剩 50
    assert pcm_jia.tolist() == [250, 50, 50, 50]
    # 乙：两源采样均获准 -> 样本 1 = 400+50 = 450；样本 2、3 片尾保持 600+50=650
    assert pcm_yi.tolist() == [250, 450, 650, 650]

    # 授权员单独查清旁白 clip 在甲地区的未授权输出点：样本 1/2/3（后两者为片尾保持）
    from audiomix.export_usage import unlicensed_output_points
    assert unlicensed_output_points(view, studio.n_samples, ledger, "jia", 10) == [1, 2, 3]
    audit = studio.audit_unlicensed("jia", 10, clip_id="c_narr")
    assert audit["clips_with_unlicensed_use"] == ["c_narr"]
    assert {p["output_index"] for p in audit["details"]["c_narr"]} == {1, 2, 3}
    # 乙地区没有任何未授权报账
    assert studio.audit_unlicensed("yi", 10)["clips_with_unlicensed_use"] == []
    print("  [OK] 小例：甲地区该拍交 50，乙地区该拍交 450，片尾保持同样受控")


# ----------------------------- 2. 相消成静音不认账 ----------------------------- #

def test_cancellation_does_not_grant_rights(tmp_path):
    clips = {
        "assets": [
            ("plus", [400, 400], 16000),
            ("minus", [-400, -400], 16000),
        ],
        "timeline": [
            {"clip_id": "c_plus", "asset_id": "plus", "a": 0, "b": 2, "start": 0},
            {"clip_id": "c_minus", "asset_id": "minus", "a": 0, "b": 2, "start": 0},
        ],
    }
    sentences = [
        Sentence("s_plus", ("c_plus",), "narration", required=True),
        Sentence("s_minus", ("c_minus",), "narration", required=True),
    ]
    studio, assets, ledger, _o, _p = _studio(
        tmp_path, "cancel", clips, sentences, [], [], T=F(2,16000))
    # 只有 plus 获准，minus 没有任何许可；两者相加本应为 0（静音）
    ledger.grant("L-plus", assets["plus"], 0, 2, (WILDCARD_REGION,), 0, 1000)

    audit = studio.audit_unlicensed("r", 10)
    assert audit["clips_with_unlicensed_use"] == ["c_minus"]
    arr = studio.planner("r", 10).recommended()
    pcm = studio.render_region_reference("r", 10, arr)
    # 不能因为相消成静音就放行 minus：minus 被整条静掉，只剩 plus 的 400
    assert pcm.tolist() == [400, 400]
    print("  [OK] 相消静音不认账：缺权的 minus 被静掉，只剩 plus 400")


# ----------------------------- 3. 整句采访排班 Pareto ----------------------------- #

def test_interview_scheduling_pareto(tmp_path):
    clips = {
        "assets": [
            ("iv", [1000, 1000, 1000, 1000], 16000),
            ("bk_early", [110, 110, 110, 110], 16000),
            ("bk_late", [220, 220, 220, 220], 16000),
        ],
        "timeline": [
            {"clip_id": "c_req", "asset_id": "iv", "a": 0, "b": 2, "start": 0},
            {"clip_id": "c_opt", "asset_id": "iv", "a": 2, "b": 4,
             "start": "2/16000"},
        ],
    }
    sentences = [
        Sentence("s_req", ("c_req",), "interview", required=True),   # 必播
        Sentence("s_opt", ("c_opt",), "interview", required=False),  # 可整句静掉
    ]
    shifts = [
        Shift("eng1", "early", 100, 200, deadline_tick=500, note="早班可录任意一条"),
        Shift("eng1", "late", 200, 300, deadline_tick=500, note="晚班只能录必播句"),
    ]
    backups = [
        {"backup_id": "B-req-early", "asset": "bk_early", "covers": ["s_req"],
         "engineer_id": "eng1", "shift_id": "early", "start_tick": 100, "end_tick": 200},
        {"backup_id": "B-opt-early", "asset": "bk_early", "covers": ["s_opt"],
         "engineer_id": "eng1", "shift_id": "early", "start_tick": 100, "end_tick": 200},
        {"backup_id": "B-req-late", "asset": "bk_late", "covers": ["s_req"],
         "engineer_id": "eng1", "shift_id": "late", "start_tick": 200, "end_tick": 300},
    ]
    studio, _a, ledger, office, _p = _studio(
        tmp_path, "sched", clips, sentences, backups, shifts, T=F(4,16000))
    # 采访原声在该地区无任何许可 -> 必须走替换/静掉
    planner = studio.planner("r", 10)
    arrangements = planner.enumerate_arrangements()
    front = planner.pareto_front(arrangements)
    metrics = sorted({(a.shifts_used, a.sentences_heard) for a in front})
    # 早班单独完成必播（1 班/1 句）；早班录可选+晚班录必播（2 班/2 句）
    assert metrics == [(1, 1), (2, 2)]
    adopted = planner.recommended()       # 先保句数 -> 2 班 2 句
    alternative = planner.alternative(adopted)
    assert (adopted.shifts_used, adopted.sentences_heard) == (2, 2)
    assert (alternative.shifts_used, alternative.sentences_heard) == (1, 1)
    # 两份安排都不浪费工时且不少保留句子（互不支配，均在 Pareto 前沿上）
    assert adopted in front and alternative in front

    # 必播句无备用且无授权 -> 整体不可行
    shifts2 = [Shift("eng9", "only", 100, 200, deadline_tick=500)]
    backups_none: list = []
    from audiomix.export_schedule import SchedulePlanner
    p2 = SchedulePlanner(sentences, backups_none, shifts2, studio.product,
                         ledger, "r2", 10, studio.n_samples)
    with pytest.raises(Exception):
        p2.enumerate_arrangements()
    print("  [OK] 排班 Pareto：(1班,1句) 与 (2班,2句) 互不支配，推荐多保句方案")


# ----------------------------- 4. 柜台竞争最后早班 ----------------------------- #

def _race_studio(tmp_path):
    clips = {
        "assets": [
            ("iv", [1000] * 8, 16000),
            ("bkA", [11] * 4, 16000),
            ("bkB", [22] * 4, 16000),
        ],
        "timeline": [
            {"clip_id": "c_req", "asset_id": "iv", "a": 0, "b": 4, "start": 0},
            {"clip_id": "c_opt", "asset_id": "iv", "a": 4, "b": 8,
             "start": "4/16000"},
        ],
    }
    sentences = [
        Sentence("s_req", ("c_req",), "interview", required=True),
        Sentence("s_opt", ("c_opt",), "interview", required=False),
    ]
    shifts = [
        Shift("eng1", "early", 100, 200, deadline_tick=500, note="最后一个早班"),
        Shift("eng1", "late", 200, 300, deadline_tick=500, note="晚班只能录必播句"),
        Shift("eng2", "late2", 200, 300, deadline_tick=300, note="第二录音师晚班"),
    ]
    backups = [
        {"backup_id": "B-req-early", "asset": "bkA", "covers": ["s_req"],
         "engineer_id": "eng1", "shift_id": "early", "start_tick": 100, "end_tick": 200},
        {"backup_id": "B-opt-early", "asset": "bkA", "covers": ["s_opt"],
         "engineer_id": "eng1", "shift_id": "early", "start_tick": 100, "end_tick": 200},
        {"backup_id": "B-req-late", "asset": "bkB", "covers": ["s_req"],
         "engineer_id": "eng1", "shift_id": "late", "start_tick": 200, "end_tick": 300},
        {"backup_id": "B-req-late2", "asset": "bkB", "covers": ["s_req"],
         "engineer_id": "eng2", "shift_id": "late2", "start_tick": 200, "end_tick": 300},
    ]
    return _studio(tmp_path, "race", clips, sentences, backups, shifts, T=F(8,16000))


def test_counter_race_and_reissue(tmp_path):
    studio, _a, ledger, office, _product = _race_studio(tmp_path)
    # 丙、丁的采访原声都无许可；两台都想要最后早班
    plan_bing = studio.planner("bing", 10)
    arr_bing = plan_bing.recommended()       # 2 班 2 句：早(可选)+晚(必播)
    alt_bing = plan_bing.alternative(arr_bing)
    assert (alt_bing.shifts_used, alt_bing.sentences_heard) == (1, 1)
    plan_ding = studio.planner("ding", 10)
    arr_ding = plan_ding.recommended()

    # 双方先预览（留班是软的，可被抢占）
    pv_bing = office.preview("bing", arr_bing, 50)
    pv_ding = office.preview("ding", arr_ding, 60)
    assert any(s["soft_held_by"] == "bing" for s in pv_bing["bookable_shifts"])
    # 预览不产生真实占用
    assert office.occupied == {}

    # 丙先签发：拿到早班（录可选句）与晚班（录必播句）
    ticket_bing = office.issue("bing", arr_bing, 70, ticket_id="T-bing")
    assert not ticket_bing.missed
    final_slots_bing = {tuple(o["final_slot"]) for o in ticket_bing.slot_outcomes}
    assert ("eng1", "early") in final_slots_bing and ("eng1", "late") in final_slots_bing
    with pytest.raises(Exception):
        office.issue("bing", arr_bing, 71)  # 已签发不可撤销

    # 丁随后签发：eng1 早/晚班均被丙先签 -> 必播句改派 eng2/late2
    # （截稿 300，当前 tick 80 来得及）；可选句无班可改派，按播出单整句静掉
    ticket_ding = office.issue("ding", arr_ding, 80, ticket_id="T-ding")
    assert not ticket_ding.missed
    outcome_req = next(o for o in ticket_ding.slot_outcomes
                       if o["sentence_id"] == "s_req")
    assert outcome_req["requested_slot"] == ["eng1", "late"]
    assert outcome_req["final_slot"] == ["eng2", "late2"]
    assert "改派" in outcome_req["note"]
    outcome_opt = next(o for o in ticket_ding.slot_outcomes
                       if o["sentence_id"] == "s_opt")
    assert outcome_opt["final_slot"] is None and "整句静掉" in outcome_opt["note"]

    # 另一柜台何时能继续办理：全部班次截稿（500/300）过后再来 -> 赶不上当班
    office2 = DispatchOffice(list(office.shifts.values()),
                             list(studio.backups.values()),
                             os.path.join(tmp_path, "office_race2.json"))
    cont = office2.when_can_continue("ding", 600)
    assert cont["can_issue_now"] is False  # 全部班次已过截稿 -> 赶不上当班
    # 截稿前：仍有开放班次，可继续办理
    assert office2.when_can_continue("ding", 80)["can_issue_now"] is True

    # 出票后重开双方所得的真实节目（WAV + 制作单），按签单实际落定安排渲染
    from audiomix.export_counter import arrangement_from_ticket
    actual_bing = arrangement_from_ticket(ticket_bing)
    actual_ding = arrangement_from_ticket(ticket_ding)
    assert actual_ding.sentences_heard == 1   # 必播句保住、可选句整句静掉
    assert actual_ding.shifts_used == 1
    d_bing = studio.deliver("bing", 70, actual_bing, ticket=ticket_bing,
                            render_id="r-bing")
    d_ding = studio.deliver("ding", 80, actual_ding, ticket=ticket_ding,
                            render_id="r-ding")
    for d in (d_bing, d_ding):
        assert os.path.exists(d["wav"]) and os.path.exists(d["sheet"])
        sheet = json.load(open(d["sheet"], encoding="utf-8"))
        assert sheet["ticket"]["ticket_id"] in ("T-bing", "T-ding")
        assert sheet["ticket"]["missed"] is False
    # 丁台制作单必须写明用了哪次替录、哪个班、哪句被整句静掉
    sheet_ding = json.load(open(d_ding["sheet"], encoding="utf-8"))
    assert sheet_ding["backups_used"] == ["B-req-late2"]
    assert any(dec["action"] == "mute" for dec in sheet_ding["sentence_decisions"])
    print("  [OK] 柜台竞争：丙得 eng1 早+晚班，丁必播句改派 eng2 晚班、可选句整句静掉")


def test_counter_miss_when_no_alternative(tmp_path):
    studio, _a, ledger, office, _product = _race_studio(tmp_path)
    # 丙采用 2 班方案并先签，独占 eng1 早班与晚班
    arr_bing = studio.planner("bing", 10).recommended()
    office.issue("bing", arr_bing, 70, ticket_id="T-bing")
    # 另一个柜台只能走 eng1 早班/晚班：全局班表里没有 eng2 的备用可改派
    backups_eng1 = [b for b in studio.backups.values() if b.engineer_id == "eng1"]
    office_ding = DispatchOffice(list(office.shifts.values()), backups_eng1,
                                 os.path.join(tmp_path, "office_ding_only.json"))
    # 把丙的真实占用同步到这个柜台视角（同一工作室的全局占用）
    office_ding.occupied = dict(office.occupied)
    from audiomix.export_schedule import SchedulePlanner
    planner_ding = SchedulePlanner(
        list(studio.sentences.values()), backups_eng1,
        list(office.shifts.values()), studio.product, ledger, "ding", 10,
        studio.n_samples)
    arr_ding = planner_ding.recommended()
    ticket = office_ding.issue("ding", arr_ding, 90, ticket_id="T-ding-miss")
    assert ticket.missed  # 必播句的班全被占且无班可改派 -> 赶不上当班
    assert office_ding.status_of("ding")["counter_status"] == "missed"
    # 失败不落地任何真实占用
    assert all(region == "bing" for region, _ in office_ding.occupied.values())
    print("  [OK] 无班可改派：丁台赶不上当班，且不污染全局真实占用")


# ----------------------------- 5. 撤回重叠许可 ----------------------------- #

def test_revoke_overlapping_license(tmp_path):
    clips = {
        "assets": [("iv", [300, 700], 8000)],
        "timeline": [
            {"clip_id": "c_iv", "asset_id": "iv", "a": 0, "b": 2, "start": 0},
        ],
    }
    sentences = [Sentence("s_iv", ("c_iv",), "narration", required=True)]
    studio, assets, ledger, _o, _p = _studio(
        tmp_path, "revoke", clips, sentences, [], [], T=F(4,16000),
        ledger=LicenseLedger(os.path.join(tmp_path, "lic_revoke.json")))
    # L-shared：通配许可，两地都靠它覆盖源采样 1；L-yi-only：乙地区另有独立许可
    ledger.grant("L-shared", assets["iv"], 0, 2, (WILDCARD_REGION,), 0, 1000,
                 note="与另一份许可在乙地区重叠")
    ledger.grant("L-yi-only", assets["iv"], 1, 2, ("yi",), 0, 1000)
    arr_bing = studio.planner("bing", 10).recommended()
    arr_yi = studio.planner("yi", 10).recommended()
    before_bing = studio.render_region_reference("bing", 10, arr_bing).copy()
    before_yi = studio.render_region_reference("yi", 10, arr_yi).copy()
    assert before_bing.tolist() == [300, 500, 700, 700]
    assert before_yi.tolist() == [300, 500, 700, 700]

    ledger.revoke("L-shared", 20)  # 只撤回这一张；L-yi-only 不受影响
    arr_bing2 = studio.planner("bing", 10).recommended()
    arr_yi2 = studio.planner("yi", 10).recommended()
    after_bing = studio.render_region_reference("bing", 10, arr_bing2)
    after_yi = studio.render_region_reference("yi", 10, arr_yi2)
    # 乙地区仍被 L-yi-only（及 [0,1) 现已无覆盖 -> 源采样 0 失权）核对：
    # 撤回后源采样 0 两地都失权；乙地区源采样 1 仍获准
    assert after_yi.tolist() == [0, 0, 700, 700]      # 乙：仅片尾保持的源 1 可用
    assert after_bing.tolist() == [0, 0, 0, 0]        # 丙：整段原声失权
    assert before_yi.tolist() != after_yi.tolist()
    # 乙地区排班决定不变（旁白逐点门控，不占班）；丙地区报账新增全部问题点
    assert studio.audit_unlicensed("yi", 10)["clips_with_unlicensed_use"] == ["c_iv"]
    audit_b = studio.audit_unlicensed("bing", 10)["details"]["c_iv"]
    assert {p["output_index"] for p in audit_b} == {0, 1, 2, 3}
    print("  [OK] 撤回重叠许可：独立许可 L-yi-only 继续覆盖乙地区源采样 1")


# ----------------------------- 6. 片尾保持 / 淡化零点 / 半开边界 ----------------------------- #

def test_tail_hold_fade_zero_half_open(tmp_path):
    from fractions import Fraction as F
    clips = {
        "assets": [("x", [200, 600], 8000)],
        "timeline": [
            # 带 1/16000 淡入与淡出的两采样 clip（参数用精确有理串）
            {"clip_id": "c", "asset_id": "x", "a": 0, "b": 2, "start": 0,
             "fade_in": "1/16000", "fade_out": "1/16000"},
        ],
    }
    sentences = [Sentence("s", ("c",), "narration", required=True)]
    studio, assets, ledger, _o, _p = _studio(
        tmp_path, "edge", clips, sentences, [], [], T=F(4, 16000))
    view = studio.views["c"]
    # 样本 0 淡入因子为 0 -> 不使用任何源采样
    assert source_uses_at(view, 0) == []
    # 样本 1：严格小于判定下仍在淡入窗内（d=1/16000 不小于 fade_in=1/16000，
    # 淡入因子恰为 1），等权使用两个源采样
    assert source_uses_at(view, 1) == [(0, F(1, 2)), (1, F(1, 2))]
    # 样本 2：片尾保持源采样 1，淡出因子 1（remain=2/16000 不小于 fade_out）
    assert source_uses_at(view, 2) == [(1, F(1))]
    # 样本 3：片尾保持，remain=1/16000 不小于 fade_out -> 淡出因子仍为 1
    assert source_uses_at(view, 3) == [(1, F(1))]

    # 只发 [0,1) 半开许可：源采样 1 不在内 -> 片尾保持照样报未授权
    ledger.grant("L0", assets["x"], 0, 1, (WILDCARD_REGION,), 0, 1000)
    pts = {p["output_index"] for p in
           studio.audit_unlicensed("r", 10)["details"]["c"]}
    assert pts == {1, 2, 3}   # 样本 0 权重为零不算使用
    # 边界严格：[1,2) 许可不覆盖源采样 0
    ledger.grant("L1", assets["x"], 1, 2, (WILDCARD_REGION,), 0, 1000)
    assert studio.audit_unlicensed("r", 10)["clips_with_unlicensed_use"] == []
    print("  [OK] 片尾保持算使用、淡化零点不算、半开区间边界严格")


# ----------------------------- 7. 窗口后冻结实际签发版本 ----------------------------- #

def test_freeze_keeps_issued_version(tmp_path):
    studio, _a, ledger, office, _product = _race_studio(tmp_path)
    arr_bing = studio.planner("bing", 10).recommended()
    arr_ding = studio.planner("ding", 10).recommended()
    tb = office.issue("bing", arr_bing, 70, ticket_id="T-bing")
    td = office.issue("ding", arr_ding, 80, ticket_id="T-ding")
    from audiomix.export_counter import arrangement_from_ticket
    actual_bing = arrangement_from_ticket(tb)
    actual_ding = arrangement_from_ticket(td)
    d_bing = studio.deliver("bing", 70, actual_bing, ticket=tb, render_id="r-bing")
    d_ding = studio.deliver("ding", 80, actual_ding, ticket=td, render_id="r-ding")
    wav_before = open(d_bing["wav"], "rb").read()

    office.freeze(900)
    snap = studio.freeze_issued(900)
    frozen_wav = open(os.path.join(studio.frozen_dir, "bing", "program.wav"), "rb").read()
    assert frozen_wav == wav_before
    assert {r["region"] for r in snap["regions"]} == {"bing", "ding"}

    # 冻结后柜台拒绝任何新签发/预览
    with pytest.raises(Exception):
        office.preview("wu", arr_bing, 950)
    # 事后再撤回许可，已冻结节目字节不变
    for lic in ledger.all_licenses():
        if lic.status == "active":
            ledger.revoke(lic.license_id, 960)
    assert open(os.path.join(studio.frozen_dir, "bing", "program.wav"),
                "rb").read() == wav_before
    manifest = json.load(open(os.path.join(studio.frozen_dir,
                                           "freeze_manifest.json"), encoding="utf-8"))
    assert manifest["frozen_at"] == 900
    print("  [OK] 窗口后冻结：事后撤回许可不改写实际签发版本")


# ----------------------------- 直接执行入口 ----------------------------- #

if __name__ == "__main__":
    import tempfile
    base = "/tmp/audiomix_export_verify"
    os.makedirs(base, exist_ok=True)
    for fn in [
        test_small_example_50_and_450,
        test_cancellation_does_not_grant_rights,
        test_interview_scheduling_pareto,
        test_counter_race_and_reissue,
        test_counter_miss_when_no_alternative,
        test_revoke_overlapping_license,
        test_tail_hold_fade_zero_half_open,
        test_freeze_keeps_issued_version,
    ]:
        with tempfile.TemporaryDirectory(dir=base) as td:
            fn(td)
    print("\n外送制作线全部测试通过")
