# 多采样率剪辑时间轴与真实混音档案（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
三个独立服务：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。
3. **地方台外送节目制作线**（`audiomix/export_*.py`，五个模块）：许可台账、原声使用
   核算、句单/备用/排班、分地区签发柜台、地区成片与制作单交付。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  export_licenses.py  外送线 1：使用许可台账（稳定身份+半开区、独立撤回、只追加修订）
  export_usage.py     外送线 2：逐输出采样原声使用核算（精确权重、淡化零点、片尾保持）
  export_schedule.py  外送线 3：句单/备用录音/录音班、合法排班枚举与 Pareto 得失
  export_counter.py   外送线 4：分地区独立柜台（预览留班、原子签发、改派、冻结）
  export_program.py   外送线 5：地区成片双路径渲染、WAV+制作单交付、窗口后冻结
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义、外送制作线案例
demo/            规模案例、CLI 端到端走查（含外送线）
run_all_checks.sh 一键全量验证
```

## 核心语义（均为精确有理数，杜绝浮点边界误差）

- 素材：单声道有符号 16bit PCM，采样率仅 8000/16000/48000；
  `asset_id = SHA-256(PCM 正文)`，同字节内容永远同一身份，绑定摘要与样本数。
- clip：`clip_id` 与素材独立；同一素材可被任意多 clip 反复引用；多 clip 允许重叠。
  时长 `(b-a)/(源采样率×速度)`，参数全部为正/非负有理数。
- 编辑整笔拒绝：未知素材、源区间 `[a,b)` 越界、淡化之和超过 clip 时长等，
  单次/批量操作要么全部生效要么状态不变。
- 输出样本：固定 16kHz，总时长 T 显式给定，输出恰为所有满足
  `0 ≤ n/16000 < T` 的样本（数量 `ceil(16000T)`），区间一律左闭右开，
  不因浮点四舍五入多一个样本。
- 源映射：实数样本位置 `p = a + (t-start)×源采样率×速度`；相邻样本线性插值；
  `p` 到达最后一个样本之后（仍在 clip 时长内）保持末样本值，绝不读区间外。
- 淡化：增益乘 0→1 线性淡入、1→0 线性淡出；淡化时长为 0 时因子恒 1；
  边界用严格小于判定（`d < fade_in`、`remain < fade_out`）。
- **舍入只做一次**：所有有效 clip 的浮点贡献全部相加后，才做“恰半取偶”
  （banker's rounding）一次，再饱和到 `[-32768, 32767]`。单片段先裁剪/先舍入
  再相加是不同语义，本工程不采用。
- 块：每块 1600 样本（0.1s）。块存档记录峰值、实际削顶样本数、所用 clip/asset。

## 复用依据（为什么更正后不会误用旧块）

块键 `block_key = SHA-256(规范JSON{输出采样率, 块序号, 该块重叠到的全部 clip
完整身份与全部转换参数, 这些 clip 引用素材的摘要})`。

- 刻意**不含 product_id、不含 T**：时间轴更正产生新产品时，依赖逐项相同的
  未受影响块仍可复用；块内容与总时长无关。
- 任何重叠 clip 的参数变化或素材正文（摘要）变化都会令键改变 → 新产品不可能
  命中旧块。规模案例中移动一个 clip，1200 块里仅 2 块重渲染、1198 块复用。

## 命令速览（有理数接受 `1/3`、`0.25`、`7` 等写法）

```bash
# 素材登记（序列/斜坡/正弦/静音，自动生成 PCM WAV）
python3 -m audiomix.cli asset-seq --root DIR/registry --rate 16000 --values 0,100,200,300
python3 -m audiomix.cli asset-list --root DIR/registry

# 编辑
python3 -m audiomix.cli tl-insert --root DIR/registry --clip-id c1 \
    --asset-id AID --a 0 --b 4 --start 1/4000 --speed 2 --gain 3/2 \
    --fade-in 1/16000 --fade-out 1/8000
python3 -m audiomix.cli tl-move   --root DIR/registry --clip-id c1 --new-start 1/8000
python3 -m audiomix.cli tl-modify --root DIR/registry --clip-id c1 --set gain=1 --set speed=1
python3 -m audiomix.cli tl-delete --root DIR/registry --clip-id c1
python3 -m audiomix.cli tl-query  --root DIR/registry --lo 0 --hi 1/8000
python3 -m audiomix.cli tl-at     --root DIR/registry --time 1/16000   # 不渲染点查询
python3 -m audiomix.cli tl-publish --root DIR/registry --out product.json

# 校验/渲染/读取/追溯
python3 -m audiomix.cli verify-product --product product.json --registry-root DIR/registry
python3 -m audiomix.cli render-compare --product product.json \
    --registry-root DIR/registry --T 120          # 分块渲染 vs 独立逐样本解释器
python3 -m audiomix.cli render --archive-root DIR/archive --product product.json \
    --registry-root DIR/registry --T 120 --render-id r1
python3 -m audiomix.cli read  --archive-root DIR/archive --render-id r1 --start 0 --end 1600
python3 -m audiomix.cli trace --archive-root DIR/archive --render-id r1 --start 0 --end 3200
python3 -m audiomix.cli export-wav --archive-root DIR/archive --render-id r1 --out out.wav
python3 -m audiomix.cli verify-integrity --archive-root DIR/archive
```

完整端到端示例：`bash demo/cli_walkthrough.sh`；全量验证：`bash run_all_checks.sh`。

## 地方台外送节目制作线

在已发布产品与素材仓之上新增的一条制作线，回答“怎样消去撤回的原声、还能保住同拍
采访”。输入全部本地合成，许可/排班/签单均可复核、可重开。

### 许可与原声使用（授权员视角）

- 许可按 **原素材稳定身份（asset_id=正文 SHA-256）+ 源样本半开区间 `[lo,hi)`** 签发，
  带适用地区与左闭右开有效窗；同一时段可并存任意多张独立许可，撤回一张只置它自己为
  revoked，不影响重叠的其余许可（`export_licenses.py`，只追加修订事件）。
- 只有**权重真正非零**的源采样才算使用（`export_usage.py`，权重为精确 Fraction）：
  沿用有理速度、0→1/1→0 线性淡化与相邻样本线性插值；淡化零点不使用任何源采样；
  `p` 到末样本之后的**片尾保持也算使用**。
- 两段相消成静音不改变各自的使用记账 —— 缺权那一段照样被静掉，不能据此认定两边原声
  都可用。`export-audit` 可单独查清某个 clip 在**哪些输出采样点**用了尚未授权的原声。
- gain 只改变响度，不改变“用了哪些原声”，因此不计入使用权重。

### 句单、替录与排班（制作员视角）

- 旁白（`narration`，可独立剪）：逐输出点门控，只静掉受限贡献，句子其余部分保留。
- 整句采访（`interview`）：原声一旦有未授权点，要么**整句替换**为已授权备用录音，
  要么（仅播出单允许时）**整句静掉**，绝不交付半句话。
- 每份备用录音标明能替哪些句、占用哪个录音师的哪个时段；同一时段全局只能实际交一条。
  排班枚举全部合法安排，并在“占用工时（班次数）”与“保留句数”两维给出 **Pareto 前沿**：
  推荐安排先最大化保留句数、再最小化工时；`alternative` 给一份两项得失不同、且互不
  支配（不浪费工时也不少保留句子）的合法安排。

### 分地区柜台与窗口冻结

- 各地区独立柜台共享全局班表：预览只做**短时留班（可被真实签单抢占）**，未签发可改约；
  `counter-issue` 原子签发，当场逐班重新调度 —— 被别台先签的班改派能录该句的开放班，
  可选句无班可改则整句静掉，必播句无班可改才整单错过且不落地任何占用。
- 两台竞争最后一个早班时先签发者得；`counter-continue` 说明另一柜台何时能继续、哪份
  须换班或赶不上当班。出票后按签单实际落定安排渲染，`arrangement_from_ticket` 可重开
  当时真实节目。
- 播出窗口过后 `export-freeze` 冻结实际签发版本（WAV+制作单快照），事后再撤回许可也
  不改写已冻结节目。

```bash
# 许可（半开源区间、地区、有效窗）
python3 -m audiomix.cli lic-grant --ledger DIR/licenses.json --license-id L1 \
    --asset-id AID --lo 0 --hi 1 --regions jia,yi --valid-from 0 --valid-until 1000
python3 -m audiomix.cli lic-revoke --ledger DIR/licenses.json --license-id L1 --at 20

# 授权报账 / 排班 Pareto / 柜台预览、签发、继续办理 / 交付 / 冻结
python3 -m audiomix.cli export-audit   --product p.json --registry-root DIR/registry \
    --ledger DIR/licenses.json --sentences s.json --backups b.json --shifts h.json \
    --studio-root DIR/studio --office DIR/office.json --T 1/4000 --region jia --at 10
python3 -m audiomix.cli export-plan     # 同上参数
python3 -m audiomix.cli counter-issue   # 同上参数 --ticket-id T1
python3 -m audiomix.cli export-deliver  # 同上参数 --ticket-id T1 --render-id r1
python3 -m audiomix.cli export-freeze   # 全局，不需要 --region
```

外送线端到端走查：`bash demo/export_walkthrough.sh`；语义测试：
`python3 -m tests.test_export_line`（8 个案例，含题面 50/450 小例、相消不认账、
排班 Pareto、柜台竞争/改派、撤回重叠、片尾保持与淡化零点、窗口冻结）。

## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `tests/test_export_line.py`：外送制作线 8 案例 —— 题面 50/450 小例（含片尾保持）、
  相消静音不认账、整句采访排班 Pareto、柜台竞争/改派/错过当班、撤回重叠许可、
  淡化零点与半开边界、窗口后冻结；地区成片的逐样本 Fraction 参考渲染与 NumPy
  向量化渲染必须逐样本一致。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
