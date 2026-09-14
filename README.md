# InkSeal — 朱墨时序鉴识服务

判断一页文书上手写墨水（ink）与盖印印泥（seal）的先后顺序。仅使用 **Python 标准库**：
`http.server` 发布端点、`json` 解析载荷、`sqlite3` 持久化。

## 为什么不能凭单张照片下结论

手写日期压到印章边缘时，同一交叉点可能既出现「墨水覆在印泥上」的迹象，
又因擦抹、扫描反光在另一处给出相反迹象。本服务因此：

1. 每条显微/多光谱**观察**只变成一条带证据强度（1–5）的有向先后边；
2. 同一交叉点的多条观察按**图层对象身份**合并，跨交叉点取最强证据；
3. 在有向图上求可达偏序，输出**可确定的顺序、互不具有可比性的对象对、节点数最少的矛盾环**；
4. 只要存在阻断性缺陷，一律 `definitive=false`，并把缺陷定位回原始观察与素材路径。

## 为什么还要看素材谱系

一张显微照片裁成两个「交叉区域」、或同一画面经锐化/伪彩/缩放重复送审时，
观察数量会增加，**证据来源却没有变**。服务因此登记采集谱系并按独立来源计数：

1. **采集批次**（batch）归组一次送检的素材；**素材**（material）分原件
   （original）与派生件（derived），记录文件 SHA-256、仪器、采集时刻、波段、
   父素材、处理参数与裁剪范围；观察须指向素材及取证区域 `{x,y,w,h}`；
2. 分析沿父子链与相同 SHA-256 把素材归并为**独立来源**（连通分量），
   边的 `corroborated_by` 按独立来源（原件）计数——同一照片的裁剪与
   再处理件只算一个来源；
3. 谱系缺陷一律阻断结论：摘要重复、循环派生、裁剪重叠、时间倒置、
   校准错配、链路断裂（悬空父引用）；
4. 每份文档可锁定 `requirements`：`min_independent_sources`（每条成立的边
   所需最少独立来源数，默认 1）与 `require_cross_modal`（边的见证须覆盖
   ≥2 种模态，默认 false）；未满足即 inconclusive 并标出观察与素材路径；
5. **未绑定素材的历史观察**各自构成一个 legacy 来源，按冻结旧规则
   以观察计数——旧数据行为不变，旧签结版本仍按其冻结规则复算。

## 阻断条件（任一存在即不下确定结论）

| 缺陷 `kind` | 触发 | 定位 |
|---|---|---|
| `out_of_bounds` | 坐标超出交叉点/文档画布 | intersection |
| `layer_misbind` | 交叉点未绑定恰好 1 墨 + 1 印，或引用不存在的图层 | intersection |
| `calibration_invalid` | 缺仪器/有效期，或有效期早于观察时刻 | observation + intersection |
| `same_point_conflict` | 同一交叉点的有效观察给出相反方向 | intersection + 全部 observations |
| `broken_chain` | 观察挂靠在结构无效的交叉点上；证据强度低于阈值且无补强；素材父引用悬空 | observation(s) / material + 路径 |
| `review_pending` / `review_dispute` | 观察未被两名审查者覆盖或意见相左 | observation |
| `contradiction_cycle` | 推断图成环（给出节点数最少的全部环与其见证观察；并列最短完整保留，不截断） | cycle nodes + observations |
| `duplicate_digest` | 同一 SHA-256 落在非单一派生链的素材之间（重复送审） | materials + 路径 |
| `derivation_cycle` | 派生关系成环，无法锚定根原件 | materials + 路径 |
| `crop_overlap` | 同一父素材的派生裁剪区域正面积相交，且都被有效观察使用 | materials + parent + observations |
| `time_inversion` | 派生物早于其父素材采集，或观察早于其素材采集 | material(s) / observation + 路径 |
| `calibration_mismatch` | 观察校准仪器或 `conditions.band` 与绑定素材不符 | observation + material + 路径 |
| `insufficient_sources` | 成立的边的独立来源数低于文档 `min_independent_sources` | observations + sources |
| `cross_modal_unmet` | 文档要求跨模态而边的见证只覆盖一种模态 | observations + sources |

冲突并不要求删除数据：两名审查者可独立 `accept/exclude`；意见相左时由
`adjudicate` 裁决，**采纳/排除/裁决都强制写理由并派生一条追加式修订**
（`revisions` 表，序号递增，不可改写）。审查与裁决修订的载荷同时冻结
当时观察绑定的素材与独立来源归属。

## 运行

```bash
python3 inkseal.py --db inkseal.db --host 127.0.0.1 --port 8080
python3 -m unittest test_inkseal -v   # 81 个测试
```

## 端点

```
POST /api/documents                       建文档 {summary, reviewers:[恰好2人，
                                           id 去空白后非空且互不相同], canvas?,
                                           requirements?{min_independent_sources,
                                           require_cross_modal}}
GET  /api/documents/{d}                   文档 + 当前复算
POST /api/documents/{d}/layers            {name, kind: ink|seal|other}
POST /api/documents/{d}/intersections     {layer_ids:[l?,l?], coordinate:{x,y}}
POST /api/documents/{d}/batches           登记采集批次 {label, operator?, note?}
POST /api/documents/{d}/materials         登记素材 {kind: original|derived,
                                           sha256, acquired_at, batch?,
                                           instrument?, band?, parent?(derived 必填，
                                           悬空引用允许录入、分析时暴露),
                                           processing?, crop?{x,y,w,h}}
GET  /api/documents/{d}/materials         谱系一览（批次/素材/独立来源/文档要求）
POST /api/documents/{d}/observations      {intersection, direction: ink_first|seal_first,
                                           strength: 1-5, reviewer, modality,
                                           observed_at, conditions,
                                           calibration:{instrument, valid_until},
                                           material?, region?{x,y,w,h}}
                                           （绑定 material 时必须同给 region）
POST /api/observations/{o}/reviews        {reviewer, decision: accept|exclude, rationale}
POST /api/intersections/{i}/adjudicate    {arbiter, accepted_observations, rationale}
                                          （rationale 去空白后须非空，否则 422 且不写入）
GET  /api/documents/{d}/analysis          确定性复算 JSON（含谱系与去重结果）
GET  /api/documents/{d}/svg               当前顺序图 SVG（环节点标红，页脚含来源统计）
POST /api/documents/{d}/signoff           {note?} → 冻结版本
GET  /api/documents/{d}/versions
GET  /api/versions/{v}                    完整冻结快照
GET  /api/versions/{v}/recompute          快照中的复算 JSON
GET  /api/versions/{v}/svg                快照顺序图
GET  /api/versions/{v}/diff?against=v2    两版本差异（含素材与来源数变化）
GET  /api/health
```

## 复算与签结

签结冻结三份 SHA-256 摘要：

- `material_digest` —— 文书/图层/交叉点/观察/批次/素材与文档谱系要求的事实素材；
- `rules_digest` —— 推断规则集（强度阈值、方向语义、谱系归并与阻断清单等，见 `RULES`）；
- `decisions_digest` —— 全部审查与裁决修订。

快照内含全量 analysis（含 `lineage`：批次、素材、独立来源与文档要求），
可独立重放：`GET /api/versions/{v}/recompute`。
规则改动会使新签结的 `rules_digest` 改变，旧版本仍按其冻结规则复算。
`analysis.recompute.digest = sha256(material_digest, rules_digest, decisions_digest)`，
同一状态多次复算结果字节级一致（确定性序列化）。

## 输出示例（analysis 关键字段）

```json
{
  "conclusion": {"definitive": false, "status": "inconclusive",
                 "blocking_defect_count": 1},
  "edges": [{"from": "l1", "to": "l2", "strength": 5, "corroborated_by": 1,
             "sources": ["s1"],
             "witnesses": [{"observation": "o1", "material": "m2",
                            "source": "s1", "...": "..."}]}],
  "lineage": {
    "requirements": {"min_independent_sources": 2, "require_cross_modal": false},
    "batches": [{"id": "b1", "label": "首轮送检"}],
    "materials": [{"id": "m1", "kind": "original", "sha256": "…",
                   "source": "s1", "path": ["m1"]},
                  {"id": "m2", "kind": "derived", "parent": "m1",
                   "crop": {"x": 0, "y": 0, "w": 100, "h": 100},
                   "source": "s1", "path": ["m2", "m1"]}],
    "sources": [{"id": "s1", "materials": ["m1", "m2"], "originals": ["m1"],
                 "observations": ["o1", "o2"], "legacy": false}]},
  "defects": [{"kind": "insufficient_sources",
               "message": "l1 before l2 rests on 1 independent source(s) (s1), …",
               "location": {"observations": ["o1", "o2"], "sources": ["s1"]}}],
  "recompute": {"material_digest": "…", "rules_digest": "…",
                "decisions_digest": "…", "rules": {"…": "…"}}
}
```

## 证据语义

- `ink_first` → 边 `ink → seal`（先写字、后盖印）；`seal_first` → `seal → ink`。
- 同一对图层身份的证据跨交叉点合并，`max(strength) ≥ 4` 才建立为图边，
  否则登记 `broken_chain`，避免把局部污染当成整页顺序。
- 边的 `corroborated_by` 数的是**独立来源**：父链相连或 SHA-256 相同的
  素材归并为一个来源；未绑定素材的观察各自计为一个 legacy 来源（旧规则）。
- 环污染判定：去掉环边后仍可达的对象对才允许进入 `order`，
  环内/仅靠回边成立的对出现在 `tainted_pairs`，且整体结论必为 inconclusive。
- 环枚举先 BFS 求全图最短环长、再只枚举该长度的环：高分支组件的
  绕行长环不会挤掉别处的更短环；并列最短环不设上限，完整集合按
  确定顺序返回。
- 谱系归并确定无歧义：连通分量按最小素材 id 排序编号（s1, s2, …），
  摘要重复只认「非单一派生链」的共享（同链路上的等字节派生属正常），
  裁剪重叠只计正面积交集且双方都已被有效观察使用。

