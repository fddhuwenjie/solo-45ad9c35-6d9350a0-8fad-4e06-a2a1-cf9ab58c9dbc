# InkSeal — 朱墨时序鉴识服务

判断一页文书上手写墨水（ink）与盖印印泥（seal）的先后顺序。仅使用 **Python 标准库**：
`http.server` 发布端点、`json` 解析载荷、`sqlite3` 持久化。

## 为什么不能凭单张照片下结论

手写日期压到印章边缘时，同一交叉点可能既出现「墨水覆在印泥上」的迹象，
又因擦抹、扫描反光在另一处给出相反迹象。本服务因此：

1. 每条显微/多光谱**观察**只变成一条带证据强度（1–5）的有向先后边；
2. 同一交叉点的多条观察按**图层对象身份**合并，跨交叉点取最强证据并计印证数；
3. 在有向图上求可达偏序，输出**可确定的顺序、互不具有可比性的对象对、节点数最少的矛盾环**；
4. 只要存在阻断性缺陷，一律 `definitive=false`，并把缺陷定位回原始观察。

## 阻断条件（任一存在即不下确定结论）

| 缺陷 `kind` | 触发 | 定位 |
|---|---|---|
| `out_of_bounds` | 坐标超出交叉点/文档画布 | intersection |
| `layer_misbind` | 交叉点未绑定恰好 1 墨 + 1 印，或引用不存在的图层 | intersection |
| `calibration_invalid` | 缺仪器/有效期，或有效期早于观察时刻 | observation + intersection |
| `same_point_conflict` | 同一交叉点的有效观察给出相反方向 | intersection + 全部 observations |
| `broken_chain` | 观察挂靠在结构无效的交叉点上；或证据强度低于阈值且无补强 | observation(s) |
| `review_pending` / `review_dispute` | 观察未被两名审查者覆盖或意见相左 | observation |
| `contradiction_cycle` | 推断图成环（给出节点数最少的全部环与其见证观察；并列最短完整保留，不截断） | cycle nodes + observations |

冲突并不要求删除数据：两名审查者可独立 `accept/exclude`；意见相左时由
`adjudicate` 裁决，**采纳/排除/裁决都强制写理由并派生一条追加式修订**
（`revisions` 表，序号递增，不可改写）。

## 运行

```bash
python3 inkseal.py --db inkseal.db --host 127.0.0.1 --port 8080
python3 -m unittest test_inkseal -v   # 56 个测试
```

## 端点

```
POST /api/documents                       建文档 {summary, reviewers:[恰好2人，
                                           id 去空白后非空且互不相同], canvas?}
GET  /api/documents/{d}                   文档 + 当前复算
POST /api/documents/{d}/layers            {name, kind: ink|seal|other}
POST /api/documents/{d}/intersections     {layer_ids:[l?,l?], coordinate:{x,y}}
POST /api/documents/{d}/observations      {intersection, direction: ink_first|seal_first,
                                           strength: 1-5, reviewer, modality,
                                           observed_at, conditions,
                                           calibration:{instrument, valid_until}}
POST /api/observations/{o}/reviews        {reviewer, decision: accept|exclude, rationale}
POST /api/intersections/{i}/adjudicate    {arbiter, accepted_observations, rationale}
                                          （rationale 去空白后须非空，否则 422 且不写入）
GET  /api/documents/{d}/analysis          确定性复算 JSON
GET  /api/documents/{d}/svg               当前顺序图 SVG（环节点标红）
POST /api/documents/{d}/signoff           {note?} → 冻结版本
GET  /api/documents/{d}/versions
GET  /api/versions/{v}                    完整冻结快照
GET  /api/versions/{v}/recompute          快照中的复算 JSON
GET  /api/versions/{v}/svg                快照顺序图
GET  /api/versions/{v}/diff?against=v2    两版本差异
GET  /api/health
```

## 复算与签结

签结冻结三份 SHA-256 摘要：

- `material_digest` —— 文书/图层/交叉点/观察的事实素材；
- `rules_digest` —— 推断规则集（强度阈值、方向语义、阻断清单等，见 `RULES`）；
- `decisions_digest` —— 全部审查与裁决修订。

快照内含全量 analysis，可独立重放：`GET /api/versions/{v}/recompute`。
规则改动会使新签结的 `rules_digest` 改变，旧版本仍按其冻结规则复算。
`analysis.recompute.digest = sha256(material_digest, rules_digest, decisions_digest)`，
同一状态多次复算结果字节级一致（确定性序列化）。

## 输出示例（analysis 关键字段）

```json
{
  "conclusion": {"definitive": true, "status": "determined",
                 "statement": "l1 before l2; l3 before l2"},
  "order": [{"earlier": "l1", "later": "l2",
             "observations": ["o1", "o2"], "via_edges": [["l1","l2"]]}],
  "incomparable_pairs": [["l1", "l3"]],
  "cycles": [],
  "edges": [{"from": "l1", "to": "l2", "strength": 5, "corroborated_by": 2,
             "witnesses": [{"observation": "o1", "intersection": "i1", "...": "..."}]}],
  "defects": [],
  "recompute": {"material_digest": "…", "rules_digest": "…",
                "decisions_digest": "…", "rules": {"…": "…"}}
}
```

## 证据语义

- `ink_first` → 边 `ink → seal`（先写字、后盖印）；`seal_first` → `seal → ink`。
- 同一对图层身份的证据跨交叉点合并，`max(strength) ≥ 4` 才建立为图边，
  否则登记 `broken_chain`，避免把局部污染当成整页顺序。
- 环污染判定：去掉环边后仍可达的对象对才允许进入 `order`，
  环内/仅靠回边成立的对出现在 `tainted_pairs`，且整体结论必为 inconclusive。
- 环枚举先 BFS 求全图最短环长、再只枚举该长度的环：高分支组件的
  绕行长环不会挤掉别处的更短环；并列最短环不设上限，完整集合按
  确定顺序返回。
