#!/usr/bin/env bash
# 地方台外送节目制作线端到端走查（全部输入为本地合成音频）：
#   许可签发 -> 未授权报账 -> 两地区排班（Pareto 得失）-> 柜台预览/竞争/改派
#   -> WAV+制作单交付 -> 撤回重叠许可 -> 窗口后冻结实际签发版本
set -euo pipefail
ROOT=/tmp/audiomix_export_cli
rm -rf "$ROOT"
REG="$ROOT/registry"
LEDGER="$ROOT/licenses.json"
OFFICE="$ROOT/office.json"
STUDIO="$ROOT/studio"
PROD="$ROOT/product.json"
PY="python3 -m audiomix.cli"

echo "== 1. 合成素材：8kHz 源 [200,600]、16kHz 常量床 50、两条已授权备用 =="
SRC8=$($PY asset-seq --root "$REG" --rate 8000  --values 200,600 --note src8  | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
BED=$($PY asset-seq  --root "$REG" --rate 16000 --values 50,50,50,50 --note bed | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
IV=$($PY asset-seq   --root "$REG" --rate 16000 --values 1000,1000,1000,1000 --note interview | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
BKA=$($PY asset-seq  --root "$REG" --rate 16000 --values 110,110,110,110 --note backupA | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
BKB=$($PY asset-seq  --root "$REG" --rate 16000 --values 220,220,220,220 --note backupB | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')

echo "== 2. 时间轴：旁白句（8k 源）+ 常量床；两条整句采访（必播/可静） =="
$PY tl-insert --root "$REG" --name exp --clip-id c_narr --asset-id "$SRC8" --a 0 --b 2 --start 0
$PY tl-insert --root "$REG" --name exp --clip-id c_bed  --asset-id "$BED"  --a 0 --b 4 --start 0
$PY tl-insert --root "$REG" --name exp --clip-id c_req --asset-id "$IV" --a 0 --b 2 --start 0
$PY tl-insert --root "$REG" --name exp --clip-id c_opt --asset-id "$IV" --a 2 --b 4 --start 2/16000
$PY tl-publish --root "$REG" --name exp --out "$PROD" >/dev/null

cat > "$ROOT/sentences.json" <<JSON
[
 {"sentence_id":"s_narr","clip_ids":["c_narr"],"kind":"narration","required":true},
 {"sentence_id":"s_req","clip_ids":["c_req"],"kind":"interview","required":true},
 {"sentence_id":"s_opt","clip_ids":["c_opt"],"kind":"interview","required":false}
]
JSON
cat > "$ROOT/shifts.json" <<JSON
[
 {"engineer_id":"eng1","shift_id":"early","start_tick":100,"end_tick":200,"deadline_tick":500,"note":"最后一个早班，可录任意一条"},
 {"engineer_id":"eng1","shift_id":"late","start_tick":200,"end_tick":300,"deadline_tick":500,"note":"晚班只能录必播句"},
 {"engineer_id":"eng2","shift_id":"late2","start_tick":200,"end_tick":300,"deadline_tick":300,"note":"第二录音师晚班"}
]
JSON
cat > "$ROOT/backups.json" <<JSON
[
 {"backup_id":"B-req-early","asset_id":"$BKA","covers":["s_req"],"engineer_id":"eng1","shift_id":"early","start_tick":100,"end_tick":200},
 {"backup_id":"B-opt-early","asset_id":"$BKA","covers":["s_opt"],"engineer_id":"eng1","shift_id":"early","start_tick":100,"end_tick":200},
 {"backup_id":"B-req-late","asset_id":"$BKB","covers":["s_req"],"engineer_id":"eng1","shift_id":"late","start_tick":200,"end_tick":300},
 {"backup_id":"B-req-late2","asset_id":"$BKB","covers":["s_req"],"engineer_id":"eng2","shift_id":"late2","start_tick":200,"end_tick":300}
]
JSON

echo "== 3. 许可：常量床通配；旁白源采样 0 通配，源采样 1 仅乙地区 =="
$PY lic-grant --ledger "$LEDGER" --license-id L-bed   --asset-id "$BED"  --lo 0 --hi 4 --regions '*' --valid-from 0 --valid-until 1000
$PY lic-grant --ledger "$LEDGER" --license-id L-src0  --asset-id "$SRC8" --lo 0 --hi 1 --regions '*' --valid-from 0 --valid-until 1000
$PY lic-grant --ledger "$LEDGER" --license-id L-src1-yi --asset-id "$SRC8" --lo 1 --hi 2 --regions yi --valid-from 0 --valid-until 1000
# 采访原声不发任何许可：整句采访必须替换为已授权备用或（可选项）整句静掉

common=(--product "$PROD" --registry-root "$REG" --ledger "$LEDGER"
        --sentences "$ROOT/sentences.json" --backups "$ROOT/backups.json"
        --shifts "$ROOT/shifts.json" --studio-root "$STUDIO" --office "$OFFICE"
        --T 4/16000 --at 10)

echo "== 4. 授权员报账：甲地区旁白缺源采样 1（含片尾保持输出点） =="
$PY export-audit "${common[@]}" --region jia | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  问题 clip:",d["clips_with_unlicensed_use"]);print("  未授权输出点:",sorted({p["output_index"] for p in d["details"]["c_narr"]}))'

echo "== 5. 两地区排班：Pareto 前沿与推荐/备选安排 =="
for R in jia yi; do
  $PY export-plan "${common[@]}" --region "$R" | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("  地区",d["region"],"前沿得失:",sorted({(a["shifts_used"],a["sentences_heard"]) for a in d["pareto_front"]}))
print("    推荐:",(d["adopted"]["shifts_used"],d["adopted"]["sentences_heard"]),
      " 备选:",None if d["alternative"] is None else (d["alternative"]["shifts_used"],d["alternative"]["sentences_heard"]))'
done

echo "== 6. 柜台竞争：丙先签发占 eng1 早+晚班，丁必播句改派 eng2、可选句静掉 =="
$PY counter-issue "${common[@]}" --region bing --ticket-id T-bing >/dev/null
$PY counter-issue "${common[@]}" --region ding --ticket-id T-ding | python3 -c '
import sys,json
t=json.load(sys.stdin)
print("  丁 missed =",t["missed"])
for o in t["slot_outcomes"]: print("   ",o["sentence_id"],o["requested_slot"],"->",o["final_slot"],o["note"])'
echo "  另一柜台截稿后能否继续:"
$PY counter-continue "${common[@]}" --region wu --at 600 | python3 -c 'import sys,json;print("   can_issue_now =",json.load(sys.stdin)["can_issue_now"])'

echo "== 7. 交付可播放 WAV 与可复核制作单（按真实签单落定安排） =="
for R in bing ding; do
  TID=$([ "$R" = bing ] && echo T-bing || echo T-ding)
  $PY export-deliver "${common[@]}" --region "$R" --ticket-id "$TID" --render-id "r-$R" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  ",d["region"],d["wav"],"样本数",d["n_samples"],"保留句",d["sentences_heard"])'
done

echo "== 8. 小例核对：甲地区该拍交 50、乙地区该拍交 450（直接渲染，不占班） =="
MINI_PROD="$ROOT/product_mini.json"
python3 - "$PROD" "$MINI_PROD" <<'PY'
import json, sys
from audiomix.timeline import TimelineEditor
full, out = sys.argv[1], sys.argv[2]
product = json.load(open(full, encoding="utf-8"))
mini = dict(product)
mini["clips"] = [c for c in product["clips"] if c["clip_id"] in ("c_narr", "c_bed")]
keep = {c["asset_id"] for c in mini["clips"]}
mini["assets"] = [a for a in product["assets"] if a["asset_id"] in keep]
mini["product_id"] = TimelineEditor.canonical_product_id(mini)
json.dump(mini, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
python3 - "$ROOT" "$MINI_PROD" "$REG" "$LEDGER" <<'PY'
import json, os, sys
from fractions import Fraction as F
from audiomix.assets import AssetRegistry
from audiomix.engine import load_product
from audiomix.export_licenses import LicenseLedger
from audiomix.export_program import ExportStudio
from audiomix.export_schedule import Sentence
root, prod_path, reg_root, ledger_path = sys.argv[1:5]
product = load_product(prod_path)
reg = AssetRegistry(reg_root)
ledger = LicenseLedger(ledger_path)
studio = ExportStudio(os.path.join(root, "mini"), product, reg, ledger,
                      [Sentence("s_narr", ("c_narr",), "narration", True)],
                      [], F(4, 16000))
for region, expect in (("jia", [250, 50, 50, 50]), ("yi", [250, 450, 650, 650])):
    arr = studio.planner(region, 10).recommended()
    got = studio.render_region_reference(region, 10, arr).tolist()
    assert got == expect, (region, got, expect)
    print(f"   {region}: {got} == {expect}")
PY

echo "== 9. 撤回一张许可，只影响依赖它的授权；随后窗口冻结实际签发版本 =="
$PY lic-revoke --ledger "$LEDGER" --license-id L-src0 --at 20 | python3 -c 'import sys,json;print("  L-src0 状态:",json.load(sys.stdin)["status"])'
$PY lic-list --ledger "$LEDGER" | python3 -c 'import sys,json;print("  仍有效许可:",[x["license_id"] for x in json.load(sys.stdin) if x["status"]=="active"])'
$PY export-freeze "${common[@]}" --at 900 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  冻结地区:",sorted(r["region"] for r in d["regions"]))'
echo "  冻结后再尝试预览（应被拒绝）:"
set +e
$PY counter-preview "${common[@]}" --region wu >/dev/null 2>&1
echo "    退出码=$?（非 0 表示柜台已冻结）"
set -e

echo
echo "================ 外送制作线 CLI 端到端走查通过 ================"
