#!/usr/bin/env python3
"""InkSeal 测试套件 —— 仅使用标准库 unittest。"""

import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import inkseal


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

REVIEWERS = [{"id": "rA", "name": "审查者甲"}, {"id": "rB", "name": "审查者乙"}]
CAL_OK = {"instrument": "MS-200", "valid_until": "2026-12-31T00:00:00Z",
          "detail": "年度检定"}
OBS_AT = "2026-09-10T08:00:00Z"
OBS_AT_LATE = "2026-10-01T08:00:00Z"


def make_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = inkseal.Store(path)
    store._path = path
    return store


def close_store(store):
    store.conn.close()
    os.unlink(store._path)


def new_doc(store, canvas=None):
    body = {"summary": "借款合同第2页 签字处", "reviewers": REVIEWERS,
            "examiner": "送检人丙", "canvas": canvas or {"w": 1000, "h": 1400}}
    return inkseal.create_document(store, body)


def layers(store, doc):
    ink = inkseal.add_layer(store, doc, {"name": "签名墨迹", "kind": "ink"})
    seal = inkseal.add_layer(store, doc, {"name": "公章印文", "kind": "seal"})
    date = inkseal.add_layer(store, doc, {"name": "落款日期", "kind": "ink"})
    return ink, seal, date


def intersection(store, doc, lid_a, lid_b, x=100, y=100, canvas=None, note=None):
    iid, _ = inkseal.add_intersection(store, doc, {
        "layer_ids": [f"l{lid_a}", f"l{lid_b}"],
        "coordinate": {"x": x, "y": y}, "canvas": canvas, "note": note})
    return iid


def observe(store, doc, iid, direction="ink_first", strength=5,
            reviewer="rA", calibration=None, observed_at=OBS_AT,
            modality="microscopy"):
    """公开接口现强制绑定素材：为本次观察登记一份独立原件。

    每份观察各自一个原件 -> 各自一个独立来源，印证计数与旧
    「未绑定观察按观察计数」的语义一致，既有断言不受影响。
    """
    mid, _ = material(store, doc)
    return observe_mat(store, doc, iid, mid, direction=direction,
                       strength=strength, reviewer=reviewer,
                       modality=modality, observed_at=observed_at,
                       calibration=CAL_OK if calibration is None else calibration)


def review(store, oid, reviewer="rA", decision="accept", rationale="理由"):
    return inkseal.add_review(store, oid, {
        "reviewer": reviewer, "decision": decision, "rationale": rationale})


def adjudicate(store, iid, arbiter, accepted, rationale):
    return inkseal.adjudicate(store, iid, {
        "arbiter": arbiter,
        "accepted_observations": [f"o{o}" for o in accepted],
        "rationale": rationale})


def defects_by_kind(analysis):
    out = {}
    for d in analysis["defects"]:
        out.setdefault(d["kind"], []).append(d)
    return out


# --------------------------------------------------------------------------- #
# 谱系夹具
# --------------------------------------------------------------------------- #

def sha(seed):
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def batch(store, doc, label="2026-09-01 送检批次", operator="采集员丁"):
    return inkseal.add_batch(store, doc, {"label": label, "operator": operator})


_material_seed = [0]


def material(store, doc, kind="original", seed=None, parent=None, batch_id=None,
             instrument="MS-200", acquired_at="2026-09-01T09:00:00Z", band=None,
             processing=None, crop=None, digest=None):
    if seed is None and digest is None:
        _material_seed[0] += 1
        seed = f"file-{_material_seed[0]}"  # 缺省唯一：不同素材不同字节
    body = {"kind": kind, "sha256": digest or sha(seed),
            "acquired_at": acquired_at}
    if instrument is not None:
        body["instrument"] = instrument
    if batch_id is not None:
        body["batch"] = f"b{batch_id}"
    if band is not None:
        body["band"] = band
    if parent is not None:
        body["parent"] = f"m{parent}"
    if processing is not None:
        body["processing"] = processing
    if crop is not None:
        body["crop"] = crop
    return inkseal.add_material(store, doc, body)


def observe_mat(store, doc, iid, mid, region=None, conditions=None, **kw):
    body = {"intersection": f"i{iid}",
            "direction": kw.pop("direction", "ink_first"),
            "strength": kw.pop("strength", 5),
            "reviewer": kw.pop("reviewer", "rA"),
            "modality": kw.pop("modality", "microscopy"),
            "observed_at": kw.pop("observed_at", OBS_AT),
            "conditions": conditions or {"lighting": "同轴反射光"},
            "calibration": kw.pop("calibration", CAL_OK),
            "material": f"m{mid}",
            "region": region or {"x": 10, "y": 10, "w": 50, "h": 50}}
    body.update(kw)
    oid, _ = inkseal.add_observation(store, doc, body)
    return oid


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def tearDown(self):
        close_store(self.store)


# --------------------------------------------------------------------------- #
# 纯算法
# --------------------------------------------------------------------------- #

def theta28_graph():
    """28 节点、112 条边：高分支六环组件 + 后置 2-环 (27,28)。

    组件一：6 组 (3,5,5,5,3,5) 共 26 节点，相邻组全连接，110 条边；
    自身最短环为 6 且多达 5625 个，足以耗尽旧的 500 枚举上限。
    组件二：27⇄28 二节点环（2 条边），是全图唯一最短环。
    """
    sizes = [3, 5, 5, 5, 3, 5]
    groups, nxt = [], 1
    for size in sizes:
        groups.append(list(range(nxt, nxt + size)))
        nxt += size
    edges = []
    for i in range(6):
        for a in groups[i]:
            for b in groups[(i + 1) % 6]:
                edges.append((a, b))
    edges += [(27, 28), (28, 27)]
    nodes = list(range(1, 29))
    assert len(edges) == 112 and len(nodes) == 28
    return nodes, edges


class GraphTests(unittest.TestCase):
    def test_reachability_chain(self):
        nodes = [1, 2, 3]
        edges = [(1, 2), (2, 3)]
        r = inkseal.reachability(nodes, edges)
        self.assertEqual(r[1], {1, 2, 3})
        self.assertEqual(r[3], {3})

    def test_tarjan_scc(self):
        nodes = [1, 2, 3, 4]
        edges = [(1, 2), (2, 1), (2, 3), (3, 4), (4, 3)]
        comps = sorted(
            (sorted(c) for c in inkseal.tarjan_scc(nodes, edges)),
            key=lambda c: c[0])
        self.assertEqual(comps, [[1, 2], [3, 4]])

    def test_shortest_cycles(self):
        # 短环与绕行长环并存时，只保留节点数最少的环
        self.assertEqual(inkseal.shortest_cycles(
            [[1, 2], [2, 3, 1], [1, 2, 4, 3]]), [[1, 2]])
        # 并列最短全部保留，保持给定（已排序）顺序
        self.assertEqual(inkseal.shortest_cycles(
            [[3, 4], [1, 2], [1, 3, 2]]), [[3, 4], [1, 2]])
        self.assertEqual(inkseal.shortest_cycles([]), [])

    def test_simple_cycle_enumeration_and_normalization(self):
        nodes = [1, 2, 3]
        rings = inkseal.enumerate_simple_cycles(
            nodes, [(1, 2), (2, 3), (3, 1)])
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0], [1, 2, 3])

    def test_shorter_cycle_not_hidden_by_detour_cycles(self):
        # 28 节点 112 边：高分支组件的 5625 个六环不得挤掉 27⇄28 二环
        nodes, edges = theta28_graph()
        rings = inkseal.enumerate_simple_cycles(nodes, edges)
        self.assertEqual(rings, [[27, 28]])

    def test_tied_shortest_cycles_returned_complete(self):
        # 去掉二环后，5625 个并列六节点最短环必须完整返回，一个不少
        import itertools
        nodes, edges = theta28_graph()
        edges = [e for e in edges if 27 not in e and 28 not in e]
        rings = inkseal.enumerate_simple_cycles(nodes, edges)
        self.assertEqual(len(rings), 5625)
        self.assertTrue(all(len(r) == 6 for r in rings))
        # 完整集合 = 六组节点的笛卡尔积（每组恰好取一个节点）
        groups = [[1, 2, 3], [4, 5, 6, 7, 8], [9, 10, 11, 12, 13],
                  [14, 15, 16, 17, 18], [19, 20, 21], [22, 23, 24, 25, 26]]
        expected = {tuple(sorted(c)) for c in itertools.product(*groups)}
        self.assertEqual({tuple(sorted(r)) for r in rings}, expected)
        # 确定顺序：按节点序列字典序，重复调用结果一致
        self.assertEqual(rings, sorted(rings, key=lambda r: [str(x) for x in r]))
        self.assertEqual(rings, inkseal.enumerate_simple_cycles(nodes, edges))

    def test_cycle_girth(self):
        from collections import defaultdict
        nodes, edges = theta28_graph()
        adj = defaultdict(set)
        for a, b in edges:
            adj[a].add(b)
        self.assertEqual(inkseal.cycle_girth(nodes, adj), 2)
        self.assertIsNone(inkseal.cycle_girth([1, 2], defaultdict(set)))

    def test_bfs_path(self):
        adj = {1: [2], 2: [3], 3: []}
        self.assertEqual(inkseal.bfs_path(adj, 1, 3), [1, 2, 3])
        self.assertIsNone(inkseal.bfs_path(adj, 3, 1))


# --------------------------------------------------------------------------- #
# 确定结论
# --------------------------------------------------------------------------- #

class DeterminedTests(StoreFixture):
    def _happy(self, strength=5):
        doc = new_doc(self.store)
        ink, seal, date = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal, 300, 400,
                           note="落款处交叉点")
        o1 = observe(self.store, doc, iid, strength=strength, reviewer="rA")
        o2 = observe(self.store, doc, iid, strength=4, reviewer="rB")
        review(self.store, o1, "rA", "accept", "墨水颗粒连续覆盖印泥，甲确认")
        review(self.store, o2, "rB", "accept", "多光谱下印泥反光中断，乙确认")
        return doc, ink, seal, date, iid, o1, o2

    def test_happy_path_determined(self):
        doc, ink, seal, date, iid, o1, o2 = self._happy()
        a = inkseal.analyze(self.store, doc)
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])
        pairs = {(p["earlier"], p["later"]) for p in a["order"]}
        self.assertEqual(pairs, {(f"l{ink}", f"l{seal}")})
        self.assertEqual(a["cycles"], [])
        edge = a["edges"][0]
        self.assertEqual((edge["from"], edge["to"]), (f"l{ink}", f"l{seal}"))
        self.assertEqual(edge["strength"], 5)
        self.assertEqual(edge["corroborated_by"], 2)
        self.assertEqual({w["observation"] for w in edge["witnesses"]},
                         {f"o{o1}", f"o{o2}"})

    def test_witness_path_observations(self):
        doc, ink, seal, *_ = self._happy()
        a = inkseal.analyze(self.store, doc)
        pair = next(p for p in a["order"]
                    if p["earlier"] == f"l{ink}" and p["later"] == f"l{seal}")
        self.assertEqual(pair["observations"], ["o1", "o2"])

    def test_incomparable_isolated_object(self):
        doc, ink, seal, date, *_ = self._happy()
        a = inkseal.analyze(self.store, doc)
        self.assertIn([f"l{ink}", f"l{date}"], a["incomparable_pairs"])
        self.assertIn([f"l{seal}", f"l{date}"], a["incomparable_pairs"])

    def test_revision_chain_created_for_reviews(self):
        doc, *_ = self._happy()
        revs = self.store.revisions(doc)
        self.assertEqual([r["kind"] for r in revs], ["review", "review"])
        self.assertEqual([r["seq"] for r in revs], [1, 2])
        payload = json.loads(revs[0]["payload"])
        self.assertEqual(payload["decision"], "accept")

    def test_deterministic_recompute(self):
        doc, *_ = self._happy()
        a1 = inkseal.analyze(self.store, doc)
        a2 = inkseal.analyze(self.store, doc)
        self.assertEqual(inkseal.canon(a1), inkseal.canon(a2))


# --------------------------------------------------------------------------- #
# 阻断条件
# --------------------------------------------------------------------------- #

class BlockerTests(StoreFixture):
    def _clean_pair(self, **obs_kw):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, reviewer="rA", **obs_kw)
        o2 = observe(self.store, doc, iid, reviewer="rB", strength=4, **{
            k: v for k, v in obs_kw.items() if k in ("direction", "calibration",
                                                     "observed_at", "modality")})
        review(self.store, o1, "rA", "accept", "甲理由")
        review(self.store, o2, "rB", "accept", "乙理由")
        return doc, ink, seal, iid, o1, o2

    def test_review_pending_blocks(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        observe(self.store, doc, iid)
        a = inkseal.analyze(self.store, doc)
        self.assertFalse(a["conclusion"]["definitive"])
        self.assertIn("review_pending", defects_by_kind(a))
        d = defects_by_kind(a)["review_pending"][0]
        self.assertEqual(d["location"]["observation"], "o1")

    def test_dispute_blocks_and_adjudication_resolves(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, reviewer="rA")
        review(self.store, o1, "rA", "accept", "甲采纳")
        review(self.store, o1, "rB", "exclude", "乙认为反光伪影，排除")
        a = inkseal.analyze(self.store, doc)
        self.assertIn("review_dispute", defects_by_kind(a))
        self.assertFalse(a["conclusion"]["definitive"])
        # 裁决：采纳 o1
        adjudicate(self.store, iid, "rB", [o1], "复检确认颗粒连续性，采纳甲意见")
        a2 = inkseal.analyze(self.store, doc)
        self.assertNotIn("review_dispute", defects_by_kind(a2))
        self.assertTrue(a2["conclusion"]["definitive"], a2["defects"])
        revs = self.store.revisions(doc)
        self.assertEqual(revs[-1]["kind"], "adjudicate")
        payload = json.loads(revs[-1]["payload"])
        self.assertEqual(payload["winning_direction"], "ink_first")

    def test_out_of_bounds_blocks(self):
        doc = new_doc(self.store, canvas={"w": 200, "h": 200})
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal, x=500, y=10)
        o1 = observe(self.store, doc, iid, reviewer="rA")
        review(self.store, o1, "rA", "accept", "x")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("out_of_bounds", kinds)
        self.assertIn("broken_chain", kinds)
        self.assertEqual(kinds["out_of_bounds"][0]["location"]["intersection"],
                         f"i{iid}")
        self.assertFalse(a["conclusion"]["definitive"])
        # 该交叉点标记为结构无效
        self.assertFalse(a["intersections"][0]["structurally_valid"])

    def test_layer_misbind_blocks(self):
        doc = new_doc(self.store)
        ink, seal, date = layers(self.store, doc)
        # 两个 ink 对象绑定，不是 ink+seal
        iid = intersection(self.store, doc, ink, date)
        o1 = observe(self.store, doc, iid, reviewer="rA")
        review(self.store, o1, "rA", "accept", "x")
        a = inkseal.analyze(self.store, doc)
        self.assertIn("layer_misbind", defects_by_kind(a))
        self.assertFalse(a["conclusion"]["definitive"])
        msg = defects_by_kind(a)["layer_misbind"][0]["message"]
        self.assertIn("exactly one ink and one seal", msg)

    def test_unbound_layer_reference_blocks(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid, unknown = inkseal.add_intersection(self.store, doc, {
            "layer_ids": [f"l{ink}", "l99"], "coordinate": {"x": 10, "y": 10}})
        self.assertEqual(unknown, [99])
        o1 = observe(self.store, doc, iid, reviewer="rA")
        review(self.store, o1, "rA", "accept", "x")
        a = inkseal.analyze(self.store, doc)
        self.assertIn("layer_misbind", defects_by_kind(a))

    def test_calibration_expired_blocks(self):
        expired = {"instrument": "MS-200", "valid_until": "2026-08-01T00:00:00Z"}
        doc, ink, seal, iid, o1, o2 = self._clean_pair(calibration=expired)
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("calibration_invalid", kinds)
        locs = {d["location"]["observation"]
                for d in kinds["calibration_invalid"]}
        self.assertEqual(locs, {f"o{o1}", f"o{o2}"})
        self.assertFalse(a["conclusion"]["definitive"])

    def test_calibration_missing_fields_blocks(self):
        bad_cal = {"instrument": "MS-200"}  # 无 valid_until
        doc, *_ = self._clean_pair(calibration=bad_cal)
        a = inkseal.analyze(self.store, doc)
        self.assertIn("calibration_invalid", defects_by_kind(a))

    def test_same_point_conflict_blocks_and_locates(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, direction="ink_first", reviewer="rA")
        o2 = observe(self.store, doc, iid, direction="seal_first",
                     strength=5, reviewer="rB")
        review(self.store, o1, "rA", "accept", "甲：墨水在上")
        review(self.store, o2, "rB", "accept", "乙：印泥在上")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("same_point_conflict", kinds)
        d = kinds["same_point_conflict"][0]
        self.assertEqual(d["location"]["intersection"], f"i{iid}")
        self.assertEqual(d["location"]["observations"], [f"o{o1}", f"o{o2}"])
        self.assertFalse(a["conclusion"]["definitive"])
        # 冲突时不产生任何边
        self.assertEqual(a["edges"], [])

    def test_adjudication_breaks_conflict(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, direction="ink_first", reviewer="rA")
        o2 = observe(self.store, doc, iid, direction="seal_first",
                     strength=5, reviewer="rB")
        review(self.store, o1, "rA", "accept", "甲")
        review(self.store, o2, "rB", "accept", "乙")
        adjudicate(self.store, iid, "rA", [o1], "裁断：甲点位清晰，乙为擦抹伪影")
        a = inkseal.analyze(self.store, doc)
        self.assertNotIn("same_point_conflict", defects_by_kind(a))
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])
        self.assertEqual(a["edges"][0]["from"], f"l{ink}")
        obs_states = {o["id"]: o["state"] for o in a["observations"]}
        self.assertEqual(obs_states[f"o{o1}"], "accepted")
        self.assertEqual(obs_states[f"o{o2}"], "excluded")

    def test_weak_evidence_is_broken_chain(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, strength=3, reviewer="rA")
        review(self.store, o1, "rA", "accept", "图像模糊，仅弱迹象")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("broken_chain", kinds)
        d = next(d for d in kinds["broken_chain"]
                 if "below edge_min_strength" in d["message"])
        self.assertEqual(d["location"]["observations"], [f"o{o1}"])
        self.assertFalse(a["conclusion"]["definitive"])
        # 另一个交叉点补强 -> 边成立
        iid2 = intersection(self.store, doc, ink, seal, x=200, y=200)
        o2 = observe(self.store, doc, iid2, strength=5, reviewer="rB")
        review(self.store, o2, "rB", "accept", "第二处清晰")
        a2 = inkseal.analyze(self.store, doc)
        self.assertTrue(a2["conclusion"]["definitive"], a2["defects"])
        self.assertEqual(a2["edges"][0]["strength"], 5)

    def test_cycle_detected_minimal(self):
        # 图是墨/印二部图：构造 2 墨 2 印四节点环
        # ink1->seal1->ink2->seal2->ink1，另加一个不相关的 other 旁观对象。
        doc = new_doc(self.store)
        ink1 = inkseal.add_layer(self.store, doc,
                                 {"name": "签名一", "kind": "ink"})
        seal1 = inkseal.add_layer(self.store, doc,
                                  {"name": "印章一", "kind": "seal"})
        ink2 = inkseal.add_layer(self.store, doc,
                                 {"name": "签名二", "kind": "ink"})
        seal2 = inkseal.add_layer(self.store, doc,
                                  {"name": "印章二", "kind": "seal"})
        other = inkseal.add_layer(self.store, doc,
                                  {"name": "骑缝", "kind": "other"})
        i1 = intersection(self.store, doc, ink1, seal1, x=10, y=10)
        i2 = intersection(self.store, doc, ink2, seal1, x=20, y=10)
        i3 = intersection(self.store, doc, ink2, seal2, x=30, y=10)
        i4 = intersection(self.store, doc, ink1, seal2, x=40, y=10)
        obs_specs = [
            (i1, "ink_first"),   # ink1 -> seal1
            (i2, "seal_first"),  # seal1 -> ink2
            (i3, "ink_first"),   # ink2 -> seal2
            (i4, "seal_first"),  # seal2 -> ink1
        ]
        for idx, (iid, direction) in enumerate(obs_specs):
            who = "rA" if idx % 2 == 0 else "rB"
            o = observe(self.store, doc, iid, direction=direction, reviewer=who)
            review(self.store, o, who, "accept", f"观察{idx}")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("contradiction_cycle", kinds)
        self.assertFalse(a["conclusion"]["definitive"])
        cycle = a["cycles"][0]
        self.assertEqual(len(cycle["nodes"]), 4)
        self.assertEqual(len(cycle["observations"]), 4)
        # 旁观对象与环内对象不可比，不进 order
        self.assertTrue(all(f"l{other}" not in (p["earlier"], p["later"])
                            for p in a["order"]))
        # 环内两两出现在 tainted_pairs
        tainted = {tuple(sorted(p)) for p in a["tainted_pairs"]}
        for x, y in ((ink1, seal1), (seal1, ink2), (ink2, seal2),
                     (ink1, seal2), (ink1, ink2), (seal1, seal2)):
            self.assertIn(tuple(sorted((f"l{x}", f"l{y}"))), tainted)

    def test_excluded_observation_is_ignored(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, direction="ink_first", reviewer="rA")
        o2 = observe(self.store, doc, iid, direction="seal_first",
                     strength=3, reviewer="rB")
        review(self.store, o1, "rA", "accept", "甲采纳")
        review(self.store, o2, "rB", "exclude", "乙自承反光，排除")
        a = inkseal.analyze(self.store, doc)
        # o2 被一致排除：不冲突，但 o1 只有一次审查（甲）-> 可确定
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])
        self.assertEqual(a["edges"][0]["from"], f"l{ink}")

    def test_duplicate_review_rejected(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid)
        review(self.store, o1, "rA", "accept", "first")
        with self.assertRaises(inkseal.HttpError) as cm:
            review(self.store, o1, "rA", "exclude", "second")
        self.assertEqual(cm.exception.status, 409)


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #

class ValidationTests(StoreFixture):
    def test_two_reviewers_required(self):
        with self.assertRaises(inkseal.HttpError) as cm:
            inkseal.create_document(self.store, {
                "summary": "x", "reviewers": [{"id": "only-one"}]})
        self.assertEqual(cm.exception.code, "bad_reviewers")

    def test_bad_direction(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        m1, _ = material(self.store, doc)
        with self.assertRaises(inkseal.HttpError) as cm:
            inkseal.add_observation(self.store, doc, {
                "intersection": f"i{iid}", "direction": "sideways",
                "strength": 5, "reviewer": "rA", "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK,
                "material": f"m{m1}",
                "region": {"x": 1, "y": 1, "w": 5, "h": 5}})
        self.assertEqual(cm.exception.status, 422)

    def test_bad_strength(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        m1, _ = material(self.store, doc)
        with self.assertRaises(inkseal.HttpError):
            inkseal.add_observation(self.store, doc, {
                "intersection": f"i{iid}", "direction": "ink_first",
                "strength": 9, "reviewer": "rA", "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK,
                "material": f"m{m1}",
                "region": {"x": 1, "y": 1, "w": 5, "h": 5}})

    def test_unknown_reviewer_cannot_review(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, reviewer="outsider")
        with self.assertRaises(inkseal.HttpError) as cm:
            review(self.store, o1, "rX", "accept", "无权")
        self.assertEqual(cm.exception.code, "unknown_reviewer")

    def test_adjudicate_requires_one_direction(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, "ink_first", reviewer="rA")
        o2 = observe(self.store, doc, iid, "seal_first", reviewer="rB")
        with self.assertRaises(inkseal.HttpError) as cm:
            adjudicate(self.store, iid, "rA", [o1, o2], "两个都要")
        self.assertEqual(cm.exception.code, "inconsistent_award")


# --------------------------------------------------------------------------- #
# 回归：审查者身份去重 / 理由非空白
# --------------------------------------------------------------------------- #

class ReviewerIdentityTests(StoreFixture):
    def test_duplicate_reviewer_ids_rejected(self):
        for dups in ([{"id": "rA"}, {"id": "rA"}],
                     [{"id": "rA"}, {"id": " rA "}],
                     [{"id": " rA", "name": "甲"}, {"id": "rA\t", "name": "乙"}]):
            with self.assertRaises(inkseal.HttpError) as cm:
                inkseal.create_document(self.store,
                                        {"summary": "x", "reviewers": list(dups)})
            self.assertEqual(cm.exception.status, 422)
            self.assertEqual(cm.exception.code, "bad_reviewers")

    def test_blank_or_missing_reviewer_id_rejected(self):
        for bad in ([{"id": "   "}, {"id": "rB"}],
                    [{"id": ""}, {"id": "rB"}],
                    [{"name": "缺id"}, {"id": "rB"}],
                    [{"id": "rA"}, "not-an-object"],
                    [{"id": 7}, {"id": "rB"}]):
            with self.assertRaises(inkseal.HttpError) as cm:
                inkseal.create_document(self.store,
                                        {"summary": "x", "reviewers": list(bad)})
            self.assertEqual(cm.exception.code, "bad_reviewers")

    def test_reviewer_ids_stripped_and_usable(self):
        doc = inkseal.create_document(self.store, {
            "summary": "x",
            "reviewers": [{"id": "  rA ", "name": "甲"},
                          {"id": "rB\t", "name": "乙"}]})
        stored = [r["id"] for r in json.loads(
            self.store.get_document(doc)["reviewers"])]
        self.assertEqual(stored, ["rA", "rB"])
        # 归一化后审查按去空白 id 正常匹配
        ink = inkseal.add_layer(self.store, doc, {"name": "墨", "kind": "ink"})
        seal = inkseal.add_layer(self.store, doc, {"name": "印", "kind": "seal"})
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, reviewer="rA")
        self.assertEqual(review(self.store, o1, "rA", "accept", "甲确认"), 1)


class BlankRationaleTests(StoreFixture):
    def _doc_obs(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        oid = observe(self.store, doc, iid, reviewer="rA")
        return doc, iid, oid

    def _write_counts(self, doc):
        reviews = self.store.execute(
            "SELECT COUNT(*) AS c FROM reviews").fetchone()["c"]
        return reviews, len(self.store.revisions(doc))

    def test_blank_review_rationale_rejected_without_writes(self):
        doc, iid, oid = self._doc_obs()
        before = self._write_counts(doc)
        for blank in ("", "   ", " \t\n "):
            with self.assertRaises(inkseal.HttpError) as cm:
                review(self.store, oid, "rA", "accept", blank)
            self.assertEqual(cm.exception.status, 422)
            self.assertEqual(cm.exception.code, "blank_rationale")
        # 拒绝请求未新增 reviews / revisions
        self.assertEqual(self._write_counts(doc), before)
        a = inkseal.analyze(self.store, doc)
        self.assertIn("review_pending", defects_by_kind(a))

    def test_blank_exclude_rationale_rejected(self):
        doc, iid, oid = self._doc_obs()
        with self.assertRaises(inkseal.HttpError) as cm:
            review(self.store, oid, "rA", "exclude", "  ")
        self.assertEqual(cm.exception.code, "blank_rationale")
        self.assertEqual(self._write_counts(doc), (0, 0))

    def test_blank_adjudication_rationale_rejected_without_writes(self):
        doc, iid, oid = self._doc_obs()
        o2 = observe(self.store, doc, iid, direction="seal_first", reviewer="rB")
        review(self.store, oid, "rA", "accept", "甲采纳")
        review(self.store, o2, "rB", "accept", "乙采纳")
        before = self._write_counts(doc)
        with self.assertRaises(inkseal.HttpError) as cm:
            adjudicate(self.store, iid, "rA", [oid], "   ")
        self.assertEqual(cm.exception.code, "blank_rationale")
        self.assertEqual(self._write_counts(doc), before)

    def test_whitespace_padded_rationale_accepted(self):
        doc, iid, oid = self._doc_obs()
        seq = review(self.store, oid, "rA", "accept", "  墨膜覆盖印泥，可采  ")
        self.assertEqual(seq, 1)


# --------------------------------------------------------------------------- #
# 回归：矛盾环只报节点数最少的环
# --------------------------------------------------------------------------- #

class ShortestCycleTests(StoreFixture):
    def _theta_doc(self):
        """短环 ink1⇄seal1 与绕行长环 ink1→seal2→ink2→seal1→ink1 并存。"""
        doc = new_doc(self.store)
        ink1 = inkseal.add_layer(self.store, doc, {"name": "墨一", "kind": "ink"})
        seal1 = inkseal.add_layer(self.store, doc, {"name": "印一", "kind": "seal"})
        ink2 = inkseal.add_layer(self.store, doc, {"name": "墨二", "kind": "ink"})
        seal2 = inkseal.add_layer(self.store, doc, {"name": "印二", "kind": "seal"})
        specs = [
            (ink1, seal1, "ink_first"),   # 短环边 ink1 -> seal1
            (ink1, seal1, "seal_first"),  # 短环边 seal1 -> ink1
            (ink1, seal2, "ink_first"),   # 绕行边 ink1 -> seal2
            (ink2, seal2, "seal_first"),  # 绕行边 seal2 -> ink2
            (ink2, seal1, "ink_first"),   # 绕行边 ink2 -> seal1
        ]
        obs = []
        for idx, (la, lb, direction) in enumerate(specs):
            iid = intersection(self.store, doc, la, lb, x=10 * (idx + 1), y=10)
            who = "rA" if idx % 2 == 0 else "rB"
            o = observe(self.store, doc, iid, direction=direction, reviewer=who)
            review(self.store, o, who, "accept", f"观察{idx}理由充分")
            obs.append(o)
        return doc, (ink1, seal1, ink2, seal2), obs

    def test_only_shortest_cycle_reported_everywhere(self):
        doc, (ink1, seal1, ink2, seal2), obs = self._theta_doc()
        a = inkseal.analyze(self.store, doc)
        self.assertFalse(a["conclusion"]["definitive"])
        # 只报节点数最少的环，绕行长环不再返回
        self.assertEqual(len(a["cycles"]), 1)
        cycle = a["cycles"][0]
        self.assertEqual(cycle["nodes"], [f"l{ink1}", f"l{seal1}"])
        self.assertEqual(cycle["observations"], [f"o{obs[0]}", f"o{obs[1]}"])
        # 阻断缺陷与同一结果一致：只定位短环及其见证观察
        cyc_defects = defects_by_kind(a)["contradiction_cycle"]
        self.assertEqual(len(cyc_defects), 1)
        msg = cyc_defects[0]["message"]
        for token in (f"l{ink1}", f"l{seal1}", f"o{obs[0]}", f"o{obs[1]}"):
            self.assertIn(token, msg)
        for token in (f"l{ink2}", f"l{seal2}",
                      f"o{obs[2]}", f"o{obs[3]}", f"o{obs[4]}"):
            self.assertNotIn(token, msg)
        # 顺序图 SVG 使用同一结果：仅短环两节点标红
        svg = inkseal.render_svg(a)
        self.assertEqual(svg.count('fill="#fdecea"'), 2)
        self.assertIn("INCONCLUSIVE", svg)
        # 签结冻结的复算 JSON 与当前分析逐字节一致
        _, snap = inkseal.signoff(self.store, doc, "冻结最短环")
        self.assertEqual(inkseal.canon(snap["analysis"]), inkseal.canon(a))

    def test_multiple_shortest_cycles_kept_in_deterministic_order(self):
        doc = new_doc(self.store)
        ink1 = inkseal.add_layer(self.store, doc, {"name": "墨一", "kind": "ink"})
        seal1 = inkseal.add_layer(self.store, doc, {"name": "印一", "kind": "seal"})
        ink2 = inkseal.add_layer(self.store, doc, {"name": "墨二", "kind": "ink"})
        seal2 = inkseal.add_layer(self.store, doc, {"name": "印二", "kind": "seal"})
        specs = [
            (ink1, seal1, "ink_first"), (ink1, seal1, "seal_first"),
            (ink2, seal2, "ink_first"), (ink2, seal2, "seal_first"),
        ]
        for idx, (la, lb, direction) in enumerate(specs):
            iid = intersection(self.store, doc, la, lb, x=10 * (idx + 1), y=10)
            who = "rA" if idx % 2 == 0 else "rB"
            o = observe(self.store, doc, iid, direction=direction, reviewer=who)
            review(self.store, o, who, "accept", f"理由{idx}")
        a1 = inkseal.analyze(self.store, doc)
        a2 = inkseal.analyze(self.store, doc)
        self.assertEqual(inkseal.canon(a1), inkseal.canon(a2))
        # 两个并列最短环全部保留，按确定顺序（节点 id 升序）
        self.assertEqual([c["nodes"] for c in a1["cycles"]],
                         [[f"l{ink1}", f"l{seal1}"], [f"l{ink2}", f"l{seal2}"]])


# --------------------------------------------------------------------------- #
# 回归：全图最短环判定不受高分支组件干扰（28 图层 / 112 边）
# --------------------------------------------------------------------------- #

class ShortestCycle28LayerTests(StoreFixture):
    def _build_28_layer_doc(self):
        """28 图层、112 条边：高分支六环组件 + 后置 l27⇄l28 二节点环。"""
        doc = new_doc(self.store)
        sizes = [3, 5, 5, 5, 3, 5]
        kinds = ["ink", "seal", "ink", "seal", "ink", "seal"]
        groups, kind_of = [], {}
        for gi, (size, kind) in enumerate(zip(sizes, kinds)):
            group = []
            for k in range(size):
                lid = inkseal.add_layer(self.store, doc, {
                    "name": f"组{gi + 1}对象{k + 1}", "kind": kind})
                group.append(lid)
                kind_of[lid] = kind
            groups.append(group)
        l27 = inkseal.add_layer(self.store, doc,
                                {"name": "落款墨迹", "kind": "ink"})
        l28 = inkseal.add_layer(self.store, doc,
                                {"name": "印章印文", "kind": "seal"})
        self.assertEqual((l27, l28), (27, 28))
        kind_of[l27], kind_of[l28] = "ink", "seal"
        edge_specs = []
        for i in range(6):
            for a in groups[i]:
                for b in groups[(i + 1) % 6]:
                    edge_specs.append((a, b))
        edge_specs += [(l27, l28), (l28, l27)]
        self.assertEqual(len(edge_specs), 112)
        obs_by_edge = {}
        for idx, (a, b) in enumerate(edge_specs):
            direction = "ink_first" if kind_of[a] == "ink" else "seal_first"
            iid = intersection(self.store, doc, a, b)
            who = "rA" if idx % 2 == 0 else "rB"
            o = observe(self.store, doc, iid, direction=direction, reviewer=who)
            review(self.store, o, who, "accept", f"边{idx}证据充分")
            obs_by_edge[(a, b)] = o
        return doc, obs_by_edge, l27, l28

    def test_shortest_cycle_wins_over_detour_cycles(self):
        doc, obs_by_edge, l27, l28 = self._build_28_layer_doc()
        a = inkseal.analyze(self.store, doc)
        self.assertFalse(a["conclusion"]["definitive"])
        # 全图最短环是后置的 l27⇄l28；5625 个六环不得挤占结果
        self.assertEqual([c["nodes"] for c in a["cycles"]],
                         [[f"l{l27}", f"l{l28}"]])
        expect_obs = sorted([f"o{obs_by_edge[(l27, l28)]}",
                             f"o{obs_by_edge[(l28, l27)]}"])
        self.assertEqual(a["cycles"][0]["observations"], expect_obs)
        # 阻断缺陷与同一结果一致：仅一条，精确定位短环，无截断提示
        self.assertEqual(a["conclusion"]["blocking_defect_count"], 1)
        cyc = defects_by_kind(a)["contradiction_cycle"]
        self.assertEqual(len(cyc), 1)
        self.assertEqual(cyc[0]["message"],
                         f"ordering cycle through l{l27}, l{l28} based on "
                         f"{', '.join(expect_obs)}")
        # 顺序图 SVG 使用同一结果：仅短环两节点标红
        svg = inkseal.render_svg(a)
        self.assertEqual(svg.count('fill="#fdecea"'), 2)
        # 签结冻结的复算 JSON 与当前分析逐字节一致
        _, snap = inkseal.signoff(self.store, doc, "28层112边")
        self.assertEqual(inkseal.canon(snap["analysis"]), inkseal.canon(a))


# --------------------------------------------------------------------------- #
# 回归：并列最短环完整返回（26 图层 / 110 边 / 5625 个六节点环）
# --------------------------------------------------------------------------- #

class TiedShortestCyclesTests(StoreFixture):
    def _build_26_layer_doc(self):
        """26 图层、110 条边：6 组 (3,5,5,5,3,5) 吹胀环，5625 个并列六节点环。"""
        doc = new_doc(self.store)
        sizes = [3, 5, 5, 5, 3, 5]
        kinds = ["ink", "seal", "ink", "seal", "ink", "seal"]
        groups, kind_of = [], {}
        for gi, (size, kind) in enumerate(zip(sizes, kinds)):
            group = []
            for k in range(size):
                lid = inkseal.add_layer(self.store, doc, {
                    "name": f"组{gi + 1}对象{k + 1}", "kind": kind})
                group.append(lid)
                kind_of[lid] = kind
            groups.append(group)
        edge_specs = []
        for i in range(6):
            for a in groups[i]:
                for b in groups[(i + 1) % 6]:
                    edge_specs.append((a, b))
        self.assertEqual(len(edge_specs), 110)
        for idx, (a, b) in enumerate(edge_specs):
            direction = "ink_first" if kind_of[a] == "ink" else "seal_first"
            iid = intersection(self.store, doc, a, b)
            who = "rA" if idx % 2 == 0 else "rB"
            o = observe(self.store, doc, iid, direction=direction, reviewer=who)
            review(self.store, o, who, "accept", f"边{idx}证据充分")
        return doc

    def test_tied_shortest_cycles_complete_everywhere(self):
        doc = self._build_26_layer_doc()
        a = inkseal.analyze(self.store, doc)
        self.assertFalse(a["conclusion"]["definitive"])
        # 5625 个并列最短环完整返回，不按任何上限截断
        self.assertEqual(len(a["cycles"]), 5625)
        self.assertTrue(all(len(c["nodes"]) == 6 for c in a["cycles"]))
        # 每个环的关联观察与其 6 条边的见证一致
        edge_witnesses = {(e["from"], e["to"]): {w["observation"]
                                                 for w in e["witnesses"]}
                          for e in a["edges"]}
        for c in a["cycles"][:50]:  # 抽查前 50 个
            obs = set()
            for frm, to in c["edges"]:
                obs |= edge_witnesses[(frm, to)]
            self.assertEqual(set(c["observations"]), obs)
        # 阻断缺陷与环一一对应，共 5625 条，无截断提示
        cyc = defects_by_kind(a)["contradiction_cycle"]
        self.assertEqual(len(cyc), 5625)
        self.assertEqual(a["conclusion"]["blocking_defect_count"], 5625)
        self.assertFalse(any("capped" in d["message"] for d in cyc))
        # 顺序图 SVG 使用同一结果：26 个节点全部在环上、全部标红
        svg = inkseal.render_svg(a)
        self.assertEqual(svg.count('fill="#fdecea"'), 26)
        # 复算确定：两次分析逐字节一致；签结快照沿用同一结果
        a2 = inkseal.analyze(self.store, doc)
        self.assertEqual(inkseal.canon(a), inkseal.canon(a2))
        _, snap = inkseal.signoff(self.store, doc, "5625并列环")
        self.assertEqual(inkseal.canon(snap["analysis"]), inkseal.canon(a))


# --------------------------------------------------------------------------- #
# 签结 / 版本差异 / SVG
# --------------------------------------------------------------------------- #

class VersionTests(StoreFixture):
    def _setup(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        o1 = observe(self.store, doc, iid, strength=4, reviewer="rA")
        review(self.store, o1, "rA", "accept", "初版采纳")
        return doc, ink, seal, iid, o1

    def test_signoff_freezes_three_digests(self):
        doc, *_ = self._setup()
        vid, snap = inkseal.signoff(self.store, doc, "初版签结")
        row = self.store.get_version(vid)
        self.assertEqual(row["material_digest"], snap["material"]["digest"])
        self.assertEqual(row["rules_digest"], inkseal.rules_digest())
        self.assertTrue(row["decisions_digest"])
        self.assertTrue(json.loads(row["snapshot"])["rules"]["body"])

    def test_version_immutable_then_diff(self):
        doc, ink, seal, iid, o1 = self._setup()
        v1, snap1 = inkseal.signoff(self.store, doc, "v1")
        # 新增第二处观察 + 第二审查者意见后再签结
        iid2 = intersection(self.store, doc, ink, seal, x=500, y=500)
        o2 = observe(self.store, doc, iid2, strength=5, reviewer="rB")
        review(self.store, o2, "rB", "accept", "新点位采纳")
        v2, snap2 = inkseal.signoff(self.store, doc, "v2")
        d = inkseal.version_diff(snap1, snap2)
        self.assertTrue(d["material_changed"])
        self.assertEqual(d["observations_added"], [f"o{o2}"])
        self.assertEqual(d["intersections_added"], [f"i{iid2}"])
        self.assertEqual(len(d["revisions_added"]), 1)
        self.assertTrue(d["definitive_from"])
        self.assertTrue(d["definitive_to"])

    def test_rules_change_changes_digest(self):
        doc, *_ = self._setup()
        _, snap = inkseal.signoff(self.store, doc, "v")
        d1 = snap["rules"]["digest"]
        inkseal.RULES["edge_min_strength"] = 3
        try:
            v2, snap2 = inkseal.signoff(self.store, doc, "v2")
            self.assertNotEqual(d1, snap2["rules"]["digest"])
        finally:
            inkseal.RULES["edge_min_strength"] = 4

    def test_svg_renders_edges_and_status(self):
        doc, *_ = self._setup()
        a = inkseal.analyze(self.store, doc)
        svg = inkseal.render_svg(a)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("DETERMINED", svg)
        self.assertIn("marker-end", svg)

    def test_svg_inconclusive_status(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal, x=9999, y=1)
        o1 = observe(self.store, doc, iid, reviewer="rA")
        review(self.store, o1, "rA", "accept", "x")
        svg = inkseal.render_svg(inkseal.analyze(self.store, doc))
        self.assertIn("INCONCLUSIVE", svg)


# --------------------------------------------------------------------------- #
# 谱系：登记校验
# --------------------------------------------------------------------------- #

class LineageRegistrationTests(StoreFixture):
    def test_register_batch_and_materials(self):
        doc = new_doc(self.store)
        b1 = batch(self.store, doc)
        m1, dangling = material(self.store, doc, seed="photo-A",
                                batch_id=b1, band="visible")
        self.assertIsNone(dangling)
        m2, _ = material(self.store, doc, kind="derived", parent=m1,
                         processing={"sharpen": 1.2}, crop={"x": 0, "y": 0,
                                                            "w": 100, "h": 80})
        a = inkseal.analyze(self.store, doc)
        lin = a["lineage"]
        self.assertEqual([b["id"] for b in lin["batches"]], [f"b{b1}"])
        mats = {m["id"]: m for m in lin["materials"]}
        self.assertEqual(mats[f"m{m1}"]["kind"], "original")
        self.assertEqual(mats[f"m{m1}"]["batch"], f"b{b1}")
        self.assertEqual(mats[f"m{m2}"]["parent"], f"m{m1}")
        self.assertEqual(mats[f"m{m2}"]["processing"], {"sharpen": 1.2})
        self.assertEqual(mats[f"m{m2}"]["path"], [f"m{m2}", f"m{m1}"])
        # 同一来源分量
        self.assertEqual(mats[f"m{m1}"]["source"], mats[f"m{m2}"]["source"])
        self.assertEqual(len(lin["sources"]), 1)
        self.assertEqual(lin["sources"][0]["originals"], [f"m{m1}"])

    def test_sha256_validated_and_normalized(self):
        doc = new_doc(self.store)
        for bad in ("abc", "z" * 64, 12345, "a" * 63):
            with self.assertRaises(inkseal.HttpError) as cm:
                material(self.store, doc, digest=bad)
            self.assertEqual(cm.exception.code, "bad_sha256", bad)
        upper = sha("photo-A").upper()
        mid, _ = material(self.store, doc, digest=upper)
        row = self.store.execute("SELECT sha256 FROM materials WHERE id=?",
                                 (mid,)).fetchone()
        self.assertEqual(row["sha256"], upper.lower())

    def test_derived_requires_parent_original_forbids_parent(self):
        doc = new_doc(self.store)
        with self.assertRaises(inkseal.HttpError) as cm:
            inkseal.add_material(self.store, doc, {
                "kind": "derived", "sha256": sha("x"),
                "acquired_at": "2026-09-01T09:00:00Z"})
        self.assertEqual(cm.exception.code, "missing_field")
        with self.assertRaises(inkseal.HttpError) as cm:
            inkseal.add_material(self.store, doc, {
                "kind": "original", "sha256": sha("y"), "parent": "m1",
                "acquired_at": "2026-09-01T09:00:00Z"})
        self.assertEqual(cm.exception.code, "bad_parent")

    def test_bad_crop_processing_batch_rejected(self):
        doc = new_doc(self.store)
        m1, _ = material(self.store, doc, seed="photo-A")
        with self.assertRaises(inkseal.HttpError) as cm:
            material(self.store, doc, kind="derived", parent=m1,
                     crop={"x": 0, "y": 0, "w": 0, "h": 10})
        self.assertEqual(cm.exception.code, "bad_crop")
        with self.assertRaises(inkseal.HttpError) as cm:
            material(self.store, doc, kind="derived", parent=m1,
                     processing="sharpen")
        self.assertEqual(cm.exception.code, "bad_processing")
        with self.assertRaises(inkseal.HttpError) as cm:
            material(self.store, doc, seed="photo-B", batch_id=999)
        self.assertEqual(cm.exception.status, 404)

    def test_dangling_parent_allowed_then_flagged(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        m1, dangling = material(self.store, doc, kind="derived", parent=99,
                                seed="crop-of-nowhere")
        self.assertEqual(dangling, 99)
        o1 = observe_mat(self.store, doc, iid, m1)
        review(self.store, o1, "rA", "accept", "甲采纳")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("broken_chain", kinds)
        d = kinds["broken_chain"][0]
        self.assertEqual(d["location"]["materials"], [f"m{m1}"])
        self.assertEqual(d["location"]["material_path"], [f"m{m1}"])
        self.assertFalse(a["conclusion"]["definitive"])
        # 观察挂在断裂谱系上：不计入有效观察
        obs = {o["id"]: o for o in a["observations"]}
        self.assertIn("broken_chain:material lineage broken",
                      obs[f"o{o1}"]["factual_defects"])
        self.assertEqual(a["edges"], [])

    def test_observation_material_region_rules(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        m1, _ = material(self.store, doc, seed="photo-A")
        base = {"intersection": f"i{iid}", "direction": "ink_first",
                "strength": 5, "reviewer": "rA", "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK}
        # 素材与区域缺一不可：两者都省略、只给区域、只给素材均被拒绝
        for extra in ({},
                      {"region": {"x": 1, "y": 1, "w": 5, "h": 5}},
                      {"material": f"m{m1}"}):
            with self.assertRaises(inkseal.HttpError) as cm:
                inkseal.add_observation(self.store, doc, dict(base, **extra))
            self.assertEqual(cm.exception.code, "missing_field", extra)
        # 未知素材 404；区域形状错误 422
        with self.assertRaises(inkseal.HttpError) as cm:
            observe_mat(self.store, doc, iid, 999)
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(inkseal.HttpError) as cm:
            observe_mat(self.store, doc, iid, m1,
                        region={"x": 1, "y": 1, "w": -5, "h": 5})
        self.assertEqual(cm.exception.code, "bad_region")

    def test_document_requirements_validated(self):
        for bad in ({"min_independent_sources": 0},
                    {"min_independent_sources": "2"},
                    {"min_independent_sources": True},
                    {"require_cross_modal": "yes"}):
            with self.assertRaises(inkseal.HttpError) as cm:
                inkseal.create_document(self.store, {
                    "summary": "x", "reviewers": REVIEWERS,
                    "requirements": bad})
            self.assertEqual(cm.exception.code, "bad_requirements", bad)
        doc = inkseal.create_document(self.store, {
            "summary": "x", "reviewers": REVIEWERS,
            "requirements": {"min_independent_sources": 2,
                             "require_cross_modal": True}})
        a = inkseal.analyze(self.store, doc)
        self.assertEqual(a["lineage"]["requirements"],
                         {"min_independent_sources": 2,
                          "require_cross_modal": True})


# --------------------------------------------------------------------------- #
# 谱系：归并与独立来源判定
# --------------------------------------------------------------------------- #

class LineageAnalysisTests(StoreFixture):
    def _doc_with_req(self, **req):
        body = {"summary": "谱系案", "reviewers": REVIEWERS,
                "canvas": {"w": 1000, "h": 1400}}
        if req:
            body["requirements"] = req
        return inkseal.create_document(self.store, body)

    def _two_crop_setup(self, min_sources=None, overlap=False):
        """一张照片裁成两个交叉区域，分别支撑两处观察。"""
        doc = self._doc_with_req(
            **({"min_independent_sources": min_sources} if min_sources else {}))
        ink, seal, _ = layers(self.store, doc)
        b1 = batch(self.store, doc)
        photo, _ = material(self.store, doc, seed="photo-A", batch_id=b1)
        crop_b = {"x": 50, "y": 50, "w": 100, "h": 100} if overlap else \
            {"x": 200, "y": 200, "w": 100, "h": 100}
        ca, _ = material(self.store, doc, kind="derived", parent=photo,
                         crop={"x": 0, "y": 0, "w": 100, "h": 100},
                         processing={"crop": True})
        cb, _ = material(self.store, doc, kind="derived", parent=photo,
                         crop=crop_b, processing={"crop": True})
        i1 = intersection(self.store, doc, ink, seal, x=100, y=100)
        i2 = intersection(self.store, doc, ink, seal, x=300, y=300)
        o1 = observe_mat(self.store, doc, i1, ca, reviewer="rA")
        o2 = observe_mat(self.store, doc, i2, cb, reviewer="rB")
        review(self.store, o1, "rA", "accept", "甲采纳")
        review(self.store, o2, "rB", "accept", "乙采纳")
        return doc, ink, seal, photo, (ca, cb), (o1, o2)

    def test_cropped_photo_counts_as_one_source(self):
        doc, ink, seal, photo, crops, obs = self._two_crop_setup(min_sources=2)
        a = inkseal.analyze(self.store, doc)
        # 两块截图归并为一个独立来源：观察增加了，来源没有变
        edge = a["edges"][0]
        self.assertEqual(edge["corroborated_by"], 1)
        self.assertEqual(edge["sources"], ["s1"])
        self.assertEqual(len(edge["witnesses"]), 2)
        kinds = defects_by_kind(a)
        self.assertIn("insufficient_sources", kinds)
        d = kinds["insufficient_sources"][0]
        self.assertEqual(d["location"]["sources"], ["s1"])
        self.assertEqual(sorted(d["location"]["observations"]),
                         sorted(f"o{o}" for o in obs))
        self.assertFalse(a["conclusion"]["definitive"])
        self.assertEqual(a["conclusion"]["status"], "inconclusive")

    def test_cropped_photo_ok_when_min_sources_default(self):
        doc, *_ = self._two_crop_setup()
        a = inkseal.analyze(self.store, doc)
        self.assertEqual(a["edges"][0]["corroborated_by"], 1)
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])

    def test_reprocessed_resubmission_dedup(self):
        """锐化 / 伪彩 / 缩放重复送审：四次观察仍是一个来源。"""
        doc = self._doc_with_req()
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        photo, _ = material(self.store, doc, seed="photo-A")
        sharp, _ = material(self.store, doc, kind="derived", parent=photo,
                            processing={"sharpen": 1.5})
        pseudo, _ = material(self.store, doc, kind="derived", parent=sharp,
                             processing={"pseudocolor": "jet"})
        scaled, _ = material(self.store, doc, kind="derived", parent=photo,
                             processing={"scale": 2.0})
        obs = []
        for idx, mid in enumerate((photo, sharp, pseudo, scaled)):
            who = "rA" if idx % 2 == 0 else "rB"
            o = observe_mat(self.store, doc, iid, mid, reviewer=who)
            review(self.store, o, who, "accept", f"第{idx}次送审")
            obs.append(o)
        a = inkseal.analyze(self.store, doc)
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])
        edge = a["edges"][0]
        self.assertEqual(edge["corroborated_by"], 1)
        self.assertEqual(edge["sources"], ["s1"])
        self.assertEqual(len(edge["witnesses"]), 4)
        src = a["lineage"]["sources"][0]
        self.assertEqual(len(src["materials"]), 4)
        self.assertEqual(src["observations"], [f"o{o}" for o in obs])

    def test_two_independent_originals_satisfy_min_sources(self):
        doc = self._doc_with_req(min_independent_sources=2)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        ma, _ = material(self.store, doc, seed="photo-A")
        mb, _ = material(self.store, doc, seed="photo-B",
                         instrument="VSC-80", acquired_at="2026-09-02T09:00:00Z")
        cal_b = {"instrument": "VSC-80", "valid_until": "2026-12-31T00:00:00Z"}
        o1 = observe_mat(self.store, doc, iid, ma, reviewer="rA")
        o2 = observe_mat(self.store, doc, iid, mb, reviewer="rB",
                         calibration=cal_b)
        review(self.store, o1, "rA", "accept", "甲采纳")
        review(self.store, o2, "rB", "accept", "乙采纳")
        a = inkseal.analyze(self.store, doc)
        self.assertEqual(a["edges"][0]["corroborated_by"], 2)
        self.assertEqual(a["edges"][0]["sources"], ["s1", "s2"])
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])

    def test_duplicate_digest_blocks(self):
        doc = self._doc_with_req()
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        same = sha("same-file")
        ma, _ = material(self.store, doc, digest=same)
        mb, _ = material(self.store, doc, digest=same,
                         acquired_at="2026-09-02T09:00:00Z")
        o1 = observe_mat(self.store, doc, iid, ma, reviewer="rA")
        o2 = observe_mat(self.store, doc, iid, mb, reviewer="rB")
        review(self.store, o1, "rA", "accept", "甲")
        review(self.store, o2, "rB", "accept", "乙")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("duplicate_digest", kinds)
        d = kinds["duplicate_digest"][0]
        self.assertEqual(d["location"]["materials"], [f"m{ma}", f"m{mb}"])
        # 同一文件重复送审仍归并为一个来源
        self.assertEqual(a["edges"][0]["corroborated_by"], 1)
        self.assertFalse(a["conclusion"]["definitive"])

    def test_derivation_cycle_blocks(self):
        doc = self._doc_with_req()
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        m1, dangling = material(self.store, doc, kind="derived", parent=2,
                                seed="loop-a")
        self.assertEqual(dangling, 2)  # 录入时父素材尚不存在
        m2, _ = material(self.store, doc, kind="derived", parent=m1,
                         seed="loop-b")
        o1 = observe_mat(self.store, doc, iid, m1, reviewer="rA")
        review(self.store, o1, "rA", "accept", "甲")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("derivation_cycle", kinds)
        self.assertNotIn("broken_chain", kinds)  # 父引用现已闭合，只剩环
        d = kinds["derivation_cycle"][0]
        self.assertEqual(d["location"]["materials"], [f"m{m1}", f"m{m2}"])
        self.assertFalse(a["conclusion"]["definitive"])
        self.assertEqual(a["edges"], [])  # 环上素材的观察不进入推断

    def test_crop_overlap_blocks(self):
        doc, ink, seal, photo, (ca, cb), obs = self._two_crop_setup(overlap=True)
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("crop_overlap", kinds)
        d = kinds["crop_overlap"][0]
        self.assertEqual(d["location"]["materials"], [f"m{ca}", f"m{cb}"])
        self.assertEqual(d["location"]["parent"], f"m{photo}")
        self.assertEqual(sorted(d["location"]["observations"]),
                         sorted(f"o{o}" for o in obs))
        self.assertFalse(a["conclusion"]["definitive"])

    def test_non_overlapping_crops_no_defect(self):
        doc, *_ = self._two_crop_setup(overlap=False)
        a = inkseal.analyze(self.store, doc)
        self.assertNotIn("crop_overlap", defects_by_kind(a))
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])

    def test_time_inversion_material_and_observation(self):
        doc = self._doc_with_req()
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        parent, _ = material(self.store, doc, seed="photo-A",
                             acquired_at="2026-09-05T09:00:00Z")
        child, _ = material(self.store, doc, kind="derived", parent=parent,
                            acquired_at="2026-09-01T09:00:00Z")  # 早于父素材
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("time_inversion", kinds)
        self.assertEqual(kinds["time_inversion"][0]["location"]["materials"],
                         [f"m{child}", f"m{parent}"])
        self.assertFalse(a["conclusion"]["definitive"])
        # 观察早于素材采集时刻
        doc2 = self._doc_with_req()
        ink2, seal2, _ = layers(self.store, doc2)
        iid2 = intersection(self.store, doc2, ink2, seal2)
        m, _ = material(self.store, doc2, seed="photo-B",
                        acquired_at="2026-09-20T09:00:00Z")
        o1 = observe_mat(self.store, doc2, iid2, m, observed_at=OBS_AT)
        review(self.store, o1, "rA", "accept", "甲")
        a2 = inkseal.analyze(self.store, doc2)
        kinds2 = defects_by_kind(a2)
        self.assertIn("time_inversion", kinds2)
        loc = kinds2["time_inversion"][0]["location"]
        self.assertEqual(loc["observation"], f"o{o1}")
        self.assertEqual(loc["material"], f"m{m}")
        self.assertFalse(a2["conclusion"]["definitive"])

    def test_calibration_mismatch_instrument_and_band(self):
        doc = self._doc_with_req()
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        m1, _ = material(self.store, doc, seed="photo-A",
                         instrument="VSC-80", band="660nm")
        cal = {"instrument": "MS-200", "valid_until": "2026-12-31T00:00:00Z"}
        o1 = observe_mat(self.store, doc, iid, m1, reviewer="rA",
                         calibration=cal, conditions={"band": "525nm"})
        review(self.store, o1, "rA", "accept", "甲")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("calibration_mismatch", kinds)
        msg = kinds["calibration_mismatch"][0]["message"]
        self.assertIn("VSC-80", msg)
        self.assertIn("525nm", msg)
        loc = kinds["calibration_mismatch"][0]["location"]
        self.assertEqual(loc["observation"], f"o{o1}")
        self.assertEqual(loc["material"], f"m{m1}")
        self.assertFalse(a["conclusion"]["definitive"])
        # 仪器与波段一致时无缺陷
        doc2 = self._doc_with_req()
        ink2, seal2, _ = layers(self.store, doc2)
        iid2 = intersection(self.store, doc2, ink2, seal2)
        m2, _ = material(self.store, doc2, seed="photo-B", band="660nm")
        o2 = observe_mat(self.store, doc2, iid2, m2, reviewer="rA",
                         conditions={"band": "660nm"})
        review(self.store, o2, "rA", "accept", "甲")
        a2 = inkseal.analyze(self.store, doc2)
        self.assertNotIn("calibration_mismatch", defects_by_kind(a2))
        self.assertTrue(a2["conclusion"]["definitive"], a2["defects"])

    def test_cross_modal_requirement(self):
        # 两个独立来源但同为显微模态 -> 跨模态要求未满足
        doc = self._doc_with_req(min_independent_sources=2,
                                 require_cross_modal=True)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        ma, _ = material(self.store, doc, seed="photo-A")
        mb, _ = material(self.store, doc, seed="photo-B")
        o1 = observe_mat(self.store, doc, iid, ma, reviewer="rA")
        o2 = observe_mat(self.store, doc, iid, mb, reviewer="rB")
        review(self.store, o1, "rA", "accept", "甲")
        review(self.store, o2, "rB", "accept", "乙")
        a = inkseal.analyze(self.store, doc)
        kinds = defects_by_kind(a)
        self.assertIn("cross_modal_unmet", kinds)
        self.assertFalse(a["conclusion"]["definitive"])
        # 多光谱 + 显微 -> 满足
        doc2 = self._doc_with_req(min_independent_sources=2,
                                  require_cross_modal=True)
        ink2, seal2, _ = layers(self.store, doc2)
        iid2 = intersection(self.store, doc2, ink2, seal2)
        mc, _ = material(self.store, doc2, seed="photo-C")
        md, _ = material(self.store, doc2, seed="photo-D")
        o3 = observe_mat(self.store, doc2, iid2, mc, reviewer="rA")
        o4 = observe_mat(self.store, doc2, iid2, md, reviewer="rB",
                         modality="multispectral")
        review(self.store, o3, "rA", "accept", "甲")
        review(self.store, o4, "rB", "accept", "乙")
        a2 = inkseal.analyze(self.store, doc2)
        self.assertTrue(a2["conclusion"]["definitive"], a2["defects"])
        self.assertEqual(a2["edges"][0]["corroborated_by"], 2)

    def test_legacy_unbound_observations_keep_old_rule(self):
        """数据库中既有的未绑定历史观察：各自仍是 legacy 独立来源。

        公开接口已强制绑定素材；此类记录只能来自谱系强制前的旧库，
        这里直接写库模拟，分析仍按冻结旧规则以观察计数。
        """
        doc = self._doc_with_req(min_independent_sources=2)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        obs = []
        for who, strength in (("rA", 5), ("rB", 4)):
            cur = self.store.execute(
                "INSERT INTO observations(document_id,intersection_id,direction,"
                "strength,reviewer,modality,observed_at,conditions,calibration,"
                "material_id,region,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc, iid, "ink_first", strength, who, "microscopy", OBS_AT,
                 json.dumps({"lighting": "同轴反射光"}, ensure_ascii=False),
                 json.dumps(CAL_OK, ensure_ascii=False), None, None,
                 inkseal.now_iso()))
            obs.append(cur.lastrowid)
        self.store.commit()
        review(self.store, obs[0], "rA", "accept", "甲")
        review(self.store, obs[1], "rB", "accept", "乙")
        a = inkseal.analyze(self.store, doc)
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])
        edge = a["edges"][0]
        self.assertEqual(edge["corroborated_by"], 2)
        self.assertEqual(edge["sources"], ["s1", "s2"])
        self.assertTrue(all(s["legacy"] for s in a["lineage"]["sources"]))
        obs_view = {o["id"]: o for o in a["observations"]}
        self.assertIsNone(obs_view[f"o{obs[0]}"]["material"])
        self.assertEqual(obs_view[f"o{obs[0]}"]["source"], "s1")

    def test_unbound_observation_cannot_bypass_source_threshold(self):
        """回归：省略 material/region 的新观察被拒绝，来源门槛不可绕过。"""
        doc, ink, seal, photo, crops, obs = self._two_crop_setup(min_sources=2)
        a = inkseal.analyze(self.store, doc)
        self.assertEqual(a["edges"][0]["corroborated_by"], 1)
        self.assertFalse(a["conclusion"]["definitive"])
        iid = self.store.intersections(doc)[0]["id"]
        base = {"intersection": f"i{iid}", "direction": "ink_first",
                "strength": 5, "reviewer": "rA", "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK}
        # 旧缺陷下，这样的观察会变成一个 legacy 独立来源使计数 1 -> 2
        for extra in ({},
                      {"region": {"x": 1, "y": 1, "w": 5, "h": 5}},
                      {"material": f"m{photo}"}):
            with self.assertRaises(inkseal.HttpError) as cm:
                inkseal.add_observation(self.store, doc, dict(base, **extra))
            self.assertEqual(cm.exception.code, "missing_field", extra)
        # 拒绝未写入：观察数、来源数与 inconclusive 结论均不变
        a2 = inkseal.analyze(self.store, doc)
        self.assertEqual(len(a2["observations"]), 2)
        self.assertEqual(len(a2["lineage"]["sources"]), 1)
        self.assertEqual(a2["edges"][0]["corroborated_by"], 1)
        self.assertFalse(a2["conclusion"]["definitive"])
        self.assertIn("insufficient_sources", defects_by_kind(a2))

    def test_deterministic_recompute_with_lineage(self):
        doc, *_ = self._two_crop_setup(min_sources=2)
        a1 = inkseal.analyze(self.store, doc)
        a2 = inkseal.analyze(self.store, doc)
        self.assertEqual(inkseal.canon(a1), inkseal.canon(a2))


# --------------------------------------------------------------------------- #
# 谱系：输出携带（修订 / 签结 / 差异 / SVG / 复算）
# --------------------------------------------------------------------------- #

class LineageOutputTests(StoreFixture):
    def _setup(self, min_sources=1):
        doc = inkseal.create_document(self.store, {
            "summary": "谱系输出案", "reviewers": REVIEWERS,
            "canvas": {"w": 1000, "h": 1400},
            "requirements": {"min_independent_sources": min_sources,
                             "require_cross_modal": False}})
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        b1 = batch(self.store, doc)
        m1, _ = material(self.store, doc, seed="photo-A", batch_id=b1)
        m2, _ = material(self.store, doc, kind="derived", parent=m1,
                         processing={"sharpen": 1.1})
        o1 = observe_mat(self.store, doc, iid, m1, reviewer="rA")
        o2 = observe_mat(self.store, doc, iid, m2, reviewer="rB")
        review(self.store, o1, "rA", "accept", "甲采纳原件")
        review(self.store, o2, "rB", "accept", "乙采纳锐化件")
        return doc, ink, seal, iid, b1, (m1, m2), (o1, o2)

    def test_revision_payloads_carry_lineage(self):
        doc, ink, seal, iid, b1, (m1, m2), (o1, o2) = self._setup()
        revs = self.store.revisions(doc)
        p1 = json.loads(revs[0]["payload"])
        self.assertEqual(p1["material"], f"m{m1}")
        self.assertEqual(p1["source"], "s1")
        p2 = json.loads(revs[1]["payload"])
        self.assertEqual(p2["material"], f"m{m2}")
        self.assertEqual(p2["source"], "s1")  # 锐化件与原件同源
        # 裁决载荷携带各观察的来源归属
        o3 = observe_mat(self.store, doc, iid, m1, direction="seal_first",
                         reviewer="rB")
        adjudicate(self.store, iid, "rA", [o1, o2], "裁断：维持 ink_first")
        payload = json.loads(self.store.revisions(doc)[-1]["payload"])
        self.assertEqual(payload["observation_sources"][f"o{o1}"], "s1")
        self.assertEqual(payload["observation_sources"][f"o{o3}"], "s1")

    def test_signoff_diff_svg_recompute_carry_lineage(self):
        doc, ink, seal, iid, b1, (m1, m2), (o1, o2) = self._setup()
        a = inkseal.analyze(self.store, doc)
        # 复算 JSON：谱系、去重结果与规则同在
        self.assertEqual(a["lineage"]["requirements"]["min_independent_sources"], 1)
        self.assertEqual(len(a["lineage"]["sources"]), 1)
        self.assertIn("lineage", a["recompute"]["rules"])
        self.assertIn("duplicate_digest",
                      a["recompute"]["rules"]["blocking_defects"])
        # 签结快照携带谱系摘要与完整谱系
        v1, snap = inkseal.signoff(self.store, doc, "谱系签结")
        self.assertEqual(snap["lineage"]["sources"], 1)
        self.assertEqual(snap["lineage"]["materials"], 2)
        self.assertEqual(snap["lineage"]["batches"], 1)
        self.assertEqual(len(snap["analysis"]["lineage"]["sources"]), 1)
        self.assertEqual(inkseal.canon(snap["analysis"]), inkseal.canon(a))
        # SVG 携带谱系与规则摘要
        svg = inkseal.render_svg(a)
        self.assertIn("independent sources: 1", svg)
        self.assertIn("materials: 2", svg)
        # 新增素材后签结 v2：差异报告谱系变化
        m3, _ = material(self.store, doc, seed="photo-B")
        o3 = observe_mat(self.store, doc, iid, m3, reviewer="rA", strength=4)
        review(self.store, o3, "rA", "accept", "第二独立原件")
        v2, snap2 = inkseal.signoff(self.store, doc, "第二来源")
        d = inkseal.version_diff(snap, snap2)
        self.assertEqual(d["materials_added"], [f"m{m3}"])
        self.assertEqual(d["materials_removed"], [])
        self.assertEqual(d["sources_from"], 1)
        self.assertEqual(d["sources_to"], 2)
        self.assertTrue(d["material_changed"])

    def test_frozen_rules_replay_for_old_versions(self):
        doc, *_ = self._setup()
        v1, snap1 = inkseal.signoff(self.store, doc, "v1")
        frozen_digest = snap1["rules"]["digest"]
        frozen_analysis = inkseal.canon(snap1["analysis"])
        inkseal.RULES["edge_min_strength"] = 3
        try:
            v2, snap2 = inkseal.signoff(self.store, doc, "v2")
            self.assertNotEqual(snap2["rules"]["digest"], frozen_digest)
            # 旧版本仍按冻结旧规则还原
            row = self.store.get_version(v1)
            restored = json.loads(row["snapshot"])
            self.assertEqual(restored["rules"]["digest"], frozen_digest)
            self.assertEqual(restored["rules"]["body"]["edge_min_strength"], 4)
            self.assertEqual(inkseal.canon(restored["analysis"]), frozen_analysis)
        finally:
            inkseal.RULES["edge_min_strength"] = 4

    def test_version_diff_without_lineage_section(self):
        """冻结旧规则的历史快照没有 lineage 段：差异按空谱系处理。"""
        doc, ink, seal, iid, b1, (m1, m2), (o1, o2) = self._setup()
        _, snap_new = inkseal.signoff(self.store, doc, "新")
        legacy_snap = {
            "analysis": {k: v for k, v in snap_new["analysis"].items()
                         if k != "lineage"},
            "material": snap_new["material"],
            "rules": snap_new["rules"],
            "decisions": snap_new["decisions"],
        }
        d = inkseal.version_diff(legacy_snap, snap_new)
        self.assertEqual(d["sources_from"], 0)
        self.assertEqual(d["sources_to"], 1)
        self.assertEqual(d["materials_added"], [f"m{m1}", f"m{m2}"])


# --------------------------------------------------------------------------- #
# HTTP 端到端
# --------------------------------------------------------------------------- #

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from http.server import ThreadingHTTPServer as T
        cls.server = T(("127.0.0.1", 0), inkseal.Handler)
        cls.server.store = inkseal.Store(cls.db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.unlink(cls.db_path)

    def _req(self, method, path, body=None, accept=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            ctype = e.headers.get("Content-Type", "")
            status = e.code
        if accept is not None:
            self.assertEqual(status, accept, raw[:400])
        if "json" in ctype:
            return status, json.loads(raw)
        return status, raw.decode()

    def test_full_workflow_over_http(self):
        st, r = self._req("POST", "/api/documents", {
            "summary": "HTTP 端到端合同", "reviewers": REVIEWERS,
            "canvas": {"w": 800, "h": 1200}}, 201)
        doc = r["document"]
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "墨", "kind": "ink"}, 201)
        ink = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "印", "kind": "seal"}, 201)
        seal = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/intersections", {
            "layer_ids": [ink, seal], "coordinate": {"x": 120, "y": 240}}, 201)
        iid = r["intersection"]
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-wf-1"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z",
            "band": "660nm"}, 201)
        m1 = r["material"]
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-wf-2"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z"}, 201)
        m2 = r["material"]
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "seal_first", "strength": 5,
            "reviewer": "rA", "modality": "multispectral",
            "observed_at": OBS_AT,
            "conditions": {"band": "660nm"},
            "calibration": CAL_OK,
            "material": m1, "region": {"x": 5, "y": 5, "w": 20, "h": 20}}, 201)
        o1 = r["observation"]
        self._req("POST", f"/api/observations/{o1}/reviews",
                  {"reviewer": "rA", "decision": "accept",
                   "rationale": "660nm 印泥在下，墨膜覆盖"}, 201)
        # 第二名审查者独立标注（独立观察）
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "seal_first", "strength": 4,
            "reviewer": "rB", "modality": "microscopy",
            "observed_at": OBS_AT, "calibration": CAL_OK,
            "material": m2, "region": {"x": 8, "y": 8, "w": 20, "h": 20}}, 201)
        o2 = r["observation"]
        self._req("POST", f"/api/observations/{o2}/reviews",
                  {"reviewer": "rB", "decision": "accept",
                   "rationale": "显微下墨膜连续"}, 201)

        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])
        self.assertEqual(a["order"][0]["earlier"], seal)
        self.assertEqual(a["order"][0]["later"], ink)

        _, svg = self._req("GET", f"/api/documents/{doc}/svg", accept=200)
        self.assertTrue(svg.startswith("<svg"))

        _, v = self._req("POST", f"/api/documents/{doc}/signoff",
                         {"note": "结案 v1"}, 201)
        self.assertTrue(v["definitive"])
        vid = v["version"]
        _, snap = self._req("GET", f"/api/versions/{vid}")
        self.assertIn("snapshot", snap)
        _, rec = self._req("GET", f"/api/versions/{vid}/recompute")
        self.assertEqual(rec["recompute"]["rules_digest"],
                         inkseal.rules_digest())
        _, vsvg = self._req("GET", f"/api/versions/{vid}/svg")
        self.assertIn("<svg", vsvg)
        _, versions = self._req("GET", f"/api/documents/{doc}/versions")
        self.assertEqual(len(versions["versions"]), 1)

    def test_conflict_then_adjudicate_then_signoff(self):
        st, r = self._req("POST", "/api/documents", {
            "summary": "冲突案", "reviewers": REVIEWERS,
            "canvas": {"w": 500, "h": 500}}, 201)
        doc = r["document"]
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "墨", "kind": "ink"}, 201)
        ink = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "印", "kind": "seal"}, 201)
        seal = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/intersections",
                         {"layer_ids": [ink, seal],
                          "coordinate": {"x": 10, "y": 10}}, 201)
        iid = r["intersection"]
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-conflict-1"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z"}, 201)
        m1 = r["material"]
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-conflict-2"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z"}, 201)
        m2 = r["material"]
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "ink_first", "strength": 5,
            "reviewer": "rA", "modality": "microscopy",
            "observed_at": OBS_AT, "calibration": CAL_OK,
            "material": m1, "region": {"x": 1, "y": 1, "w": 5, "h": 5}}, 201)
        o1 = r["observation"]
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "seal_first", "strength": 5,
            "reviewer": "rB", "modality": "multispectral",
            "observed_at": OBS_AT, "calibration": CAL_OK,
            "material": m2, "region": {"x": 2, "y": 2, "w": 5, "h": 5}}, 201)
        o2 = r["observation"]
        self._req("POST", f"/api/observations/{o1}/reviews",
                  {"reviewer": "rA", "decision": "accept", "rationale": "甲"}, 201)
        self._req("POST", f"/api/observations/{o2}/reviews",
                  {"reviewer": "rB", "decision": "accept", "rationale": "乙"}, 201)
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertFalse(a["conclusion"]["definitive"])
        self._req("POST", f"/api/intersections/{iid}/adjudicate", {
            "arbiter": "rA", "accepted_observations": [o1],
            "rationale": "乙点位系擦抹反光，甲证据可采"}, 201)
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertTrue(a["conclusion"]["definitive"], a["defects"])
        _, v1 = self._req("POST", f"/api/documents/{doc}/signoff",
                          {"note": "v1"}, 201)
        _, v2 = self._req("POST", f"/api/documents/{doc}/signoff",
                          {"note": "v2"}, 201)
        _, diff = self._req(
            "GET", f"/api/versions/{v2['version']}/diff?against={v1['version']}")
        # 无新素材 -> 三摘要不变
        self.assertFalse(diff["material_changed"])
        self.assertFalse(diff["decisions_changed"])

    def test_errors_enveloped(self):
        st, r = self._req("GET", "/api/documents/d9999/analysis", accept=404)
        self.assertEqual(r["error"], "not_found")
        st, r = self._req("POST", "/api/documents", {"summary": "x"}, 400)
        self.assertEqual(r["error"], "missing_field")

    def test_reviewer_ids_validated_over_http(self):
        for bad in ([{"id": "rA"}, {"id": "rA"}],
                    [{"id": "rA"}, {"id": " rA\t"}],
                    [{"id": " "}, {"id": "rB"}],
                    [{"id": "rA"}, {"name": "缺id"}]):
            st, r = self._req("POST", "/api/documents",
                              {"summary": "x", "reviewers": bad}, 422)
            self.assertEqual(r["error"], "bad_reviewers")
        # 合法：去空白后不同的两人；id 归一化存储
        st, r = self._req("POST", "/api/documents", {
            "summary": "x",
            "reviewers": [{"id": " rA ", "name": "甲"},
                          {"id": "rB", "name": "乙"}]}, 201)
        doc = r["document"]
        _, d = self._req("GET", f"/api/documents/{doc}")
        self.assertEqual([x["id"] for x in d["reviewers"]], ["rA", "rB"])

    def test_blank_rationale_rejected_over_http(self):
        st, r = self._req("POST", "/api/documents", {
            "summary": "空白理由案", "reviewers": REVIEWERS,
            "canvas": {"w": 500, "h": 500}}, 201)
        doc = r["document"]
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "墨", "kind": "ink"}, 201)
        ink = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "印", "kind": "seal"}, 201)
        seal = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/intersections",
                         {"layer_ids": [ink, seal],
                          "coordinate": {"x": 10, "y": 10}}, 201)
        iid = r["intersection"]
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-blank"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z"}, 201)
        m1 = r["material"]
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "ink_first", "strength": 5,
            "reviewer": "rA", "modality": "microscopy",
            "observed_at": OBS_AT, "calibration": CAL_OK,
            "material": m1, "region": {"x": 1, "y": 1, "w": 5, "h": 5}}, 201)
        o1 = r["observation"]
        for blank in ("", "   ", " \t "):
            st, r = self._req("POST", f"/api/observations/{o1}/reviews",
                              {"reviewer": "rA", "decision": "accept",
                               "rationale": blank}, 422)
            self.assertEqual(r["error"], "blank_rationale")
        st, r = self._req("POST", f"/api/intersections/{iid}/adjudicate", {
            "arbiter": "rA", "accepted_observations": [o1],
            "rationale": "  "}, 422)
        self.assertEqual(r["error"], "blank_rationale")
        # 拒绝请求未写入：分析仍待审查，签结快照中 reviews / revisions 为空
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertIn("review_pending", {d["kind"] for d in a["defects"]})
        _, v = self._req("POST", f"/api/documents/{doc}/signoff",
                         {"note": "核查"}, 201)
        _, snap = self._req("GET", f"/api/versions/{v['version']}")
        self.assertEqual(snap["snapshot"]["decisions"]["reviews"], [])
        self.assertEqual(snap["snapshot"]["decisions"]["revisions"], [])

    def test_shortest_cycle_only_over_http(self):
        st, r = self._req("POST", "/api/documents", {
            "summary": "环案", "reviewers": REVIEWERS,
            "canvas": {"w": 500, "h": 500}}, 201)
        doc = r["document"]
        lids = {}
        for name, kind in (("墨一", "ink"), ("印一", "seal"),
                           ("墨二", "ink"), ("印二", "seal")):
            _, r = self._req("POST", f"/api/documents/{doc}/layers",
                             {"name": name, "kind": kind}, 201)
            lids[name] = r["layer"]
        specs = [
            ("墨一", "印一", "ink_first"),   # 短环边
            ("墨一", "印一", "seal_first"),  # 短环边
            ("墨一", "印二", "ink_first"),   # 绕行长环边
            ("墨二", "印二", "seal_first"),  # 绕行长环边
            ("墨二", "印一", "ink_first"),   # 绕行长环边
        ]
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-cyc"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z"}, 201)
        m1 = r["material"]
        short_obs = []
        for idx, (a_name, b_name, direction) in enumerate(specs):
            _, r = self._req("POST", f"/api/documents/{doc}/intersections", {
                "layer_ids": [lids[a_name], lids[b_name]],
                "coordinate": {"x": 10 * (idx + 1), "y": 10}}, 201)
            iid = r["intersection"]
            who = "rA" if idx % 2 == 0 else "rB"
            _, r = self._req("POST", f"/api/documents/{doc}/observations", {
                "intersection": iid, "direction": direction, "strength": 5,
                "reviewer": who, "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK,
                "material": m1,
                "region": {"x": idx, "y": 0, "w": 5, "h": 5}}, 201)
            oid = r["observation"]
            self._req("POST", f"/api/observations/{oid}/reviews",
                      {"reviewer": who, "decision": "accept",
                       "rationale": f"理由{idx}"}, 201)
            if idx < 2:
                short_obs.append(oid)
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertFalse(a["conclusion"]["definitive"])
        self.assertEqual(len(a["cycles"]), 1)
        self.assertEqual(a["cycles"][0]["nodes"], [lids["墨一"], lids["印一"]])
        self.assertEqual(a["cycles"][0]["observations"], sorted(short_obs))
        _, svg = self._req("GET", f"/api/documents/{doc}/svg")
        self.assertEqual(svg.count('fill="#fdecea"'), 2)
        _, v = self._req("POST", f"/api/documents/{doc}/signoff",
                         {"note": "v"}, 201)
        _, rec = self._req("GET", f"/api/versions/{v['version']}/recompute")
        self.assertEqual(rec["cycles"], a["cycles"])
        self.assertEqual(rec["defects"], a["defects"])

    def test_shortest_cycle_survives_cap_over_http(self):
        st, r = self._req("POST", "/api/documents", {
            "summary": "28层112边", "reviewers": REVIEWERS,
            "canvas": {"w": 1000, "h": 1400}}, 201)
        doc = r["document"]
        sizes = [3, 5, 5, 5, 3, 5]
        kinds = ["ink", "seal", "ink", "seal", "ink", "seal"]
        groups, kind_of = [], {}
        for gi, (size, kind) in enumerate(zip(sizes, kinds)):
            group = []
            for k in range(size):
                _, r = self._req("POST", f"/api/documents/{doc}/layers",
                                 {"name": f"g{gi}n{k}", "kind": kind}, 201)
                group.append(r["layer"])
                kind_of[r["layer"]] = kind
            groups.append(group)
        pair = []
        for name, kind in (("落款墨迹", "ink"), ("印章印文", "seal")):
            _, r = self._req("POST", f"/api/documents/{doc}/layers",
                             {"name": name, "kind": kind}, 201)
            pair.append(r["layer"])
            kind_of[r["layer"]] = kind
        l27, l28 = pair  # 本文档第 27、28 个图层（全局 id 依测试次序递增）
        edge_specs = []
        for i in range(6):
            for a in groups[i]:
                for b in groups[(i + 1) % 6]:
                    edge_specs.append((a, b))
        edge_specs += [(l27, l28), (l28, l27)]
        self.assertEqual(len(edge_specs), 112)
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-28layer"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z"}, 201)
        m1 = r["material"]
        short_obs = []
        for idx, (a, b) in enumerate(edge_specs):
            _, r = self._req("POST", f"/api/documents/{doc}/intersections",
                             {"layer_ids": [a, b],
                              "coordinate": {"x": 50, "y": 50}}, 201)
            iid = r["intersection"]
            direction = "ink_first" if kind_of[a] == "ink" else "seal_first"
            who = "rA" if idx % 2 == 0 else "rB"
            _, r = self._req("POST", f"/api/documents/{doc}/observations", {
                "intersection": iid, "direction": direction, "strength": 5,
                "reviewer": who, "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK,
                "material": m1,
                "region": {"x": idx % 100, "y": 0, "w": 5, "h": 5}}, 201)
            oid = r["observation"]
            self._req("POST", f"/api/observations/{oid}/reviews",
                      {"reviewer": who, "decision": "accept",
                       "rationale": f"边{idx}可采"}, 201)
            if (a, b) in ((l27, l28), (l28, l27)):
                short_obs.append(oid)
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertFalse(a["conclusion"]["definitive"])
        self.assertEqual([c["nodes"] for c in a["cycles"]], [[l27, l28]])
        self.assertEqual(a["cycles"][0]["observations"], sorted(short_obs))
        cyc = [d for d in a["defects"] if d["kind"] == "contradiction_cycle"]
        self.assertEqual(len(cyc), 1)
        self.assertEqual(a["conclusion"]["blocking_defect_count"], 1)
        _, svg = self._req("GET", f"/api/documents/{doc}/svg")
        self.assertEqual(svg.count('fill="#fdecea"'), 2)
        _, v = self._req("POST", f"/api/documents/{doc}/signoff",
                         {"note": "v"}, 201)
        _, rec = self._req("GET", f"/api/versions/{v['version']}/recompute")
        self.assertEqual(rec["cycles"], a["cycles"])
        self.assertEqual(rec["defects"], a["defects"])

    def test_tied_shortest_cycles_complete_over_http(self):
        st, r = self._req("POST", "/api/documents", {
            "summary": "5625并列环", "reviewers": REVIEWERS,
            "canvas": {"w": 1000, "h": 1400}}, 201)
        doc = r["document"]
        sizes = [3, 5, 5, 5, 3, 5]
        kinds = ["ink", "seal", "ink", "seal", "ink", "seal"]
        groups, kind_of = [], {}
        for gi, (size, kind) in enumerate(zip(sizes, kinds)):
            group = []
            for k in range(size):
                _, r = self._req("POST", f"/api/documents/{doc}/layers",
                                 {"name": f"g{gi}n{k}", "kind": kind}, 201)
                group.append(r["layer"])
                kind_of[r["layer"]] = kind
            groups.append(group)
        edge_specs = []
        for i in range(6):
            for a in groups[i]:
                for b in groups[(i + 1) % 6]:
                    edge_specs.append((a, b))
        self.assertEqual(len(edge_specs), 110)
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-5625"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z"}, 201)
        m1 = r["material"]
        for idx, (a, b) in enumerate(edge_specs):
            _, r = self._req("POST", f"/api/documents/{doc}/intersections",
                             {"layer_ids": [a, b],
                              "coordinate": {"x": 50, "y": 50}}, 201)
            iid = r["intersection"]
            direction = "ink_first" if kind_of[a] == "ink" else "seal_first"
            who = "rA" if idx % 2 == 0 else "rB"
            _, r = self._req("POST", f"/api/documents/{doc}/observations", {
                "intersection": iid, "direction": direction, "strength": 5,
                "reviewer": who, "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK,
                "material": m1,
                "region": {"x": idx % 100, "y": 0, "w": 5, "h": 5}}, 201)
            oid = r["observation"]
            self._req("POST", f"/api/observations/{oid}/reviews",
                      {"reviewer": who, "decision": "accept",
                       "rationale": f"边{idx}可采"}, 201)
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertFalse(a["conclusion"]["definitive"])
        # 5625 个并列最短环完整返回，一个不少
        self.assertEqual(len(a["cycles"]), 5625)
        self.assertTrue(all(len(c["nodes"]) == 6 for c in a["cycles"]))
        cyc = [d for d in a["defects"] if d["kind"] == "contradiction_cycle"]
        self.assertEqual(len(cyc), 5625)
        self.assertEqual(a["conclusion"]["blocking_defect_count"], 5625)
        _, svg = self._req("GET", f"/api/documents/{doc}/svg")
        self.assertEqual(svg.count('fill="#fdecea"'), 26)
        _, v = self._req("POST", f"/api/documents/{doc}/signoff",
                         {"note": "v"}, 201)
        _, rec = self._req("GET", f"/api/versions/{v['version']}/recompute")
        self.assertEqual(rec["cycles"], a["cycles"])
        self.assertEqual(rec["defects"], a["defects"])

    def test_lineage_workflow_over_http(self):
        st, r = self._req("POST", "/api/documents", {
            "summary": "谱系 HTTP 案", "reviewers": REVIEWERS,
            "canvas": {"w": 1000, "h": 1400},
            "requirements": {"min_independent_sources": 2,
                             "require_cross_modal": False}}, 201)
        doc = r["document"]
        _, d = self._req("GET", f"/api/documents/{doc}")
        self.assertEqual(d["requirements"]["min_independent_sources"], 2)
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "墨", "kind": "ink"}, 201)
        ink = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/layers",
                         {"name": "印", "kind": "seal"}, 201)
        seal = r["layer"]
        _, r = self._req("POST", f"/api/documents/{doc}/intersections", {
            "layer_ids": [ink, seal], "coordinate": {"x": 100, "y": 100}}, 201)
        i1 = r["intersection"]
        _, r = self._req("POST", f"/api/documents/{doc}/intersections", {
            "layer_ids": [ink, seal], "coordinate": {"x": 300, "y": 300}}, 201)
        i2 = r["intersection"]
        # 采集批次 + 一张照片 + 两块裁剪截图
        _, r = self._req("POST", f"/api/documents/{doc}/batches",
                         {"label": "首轮送检", "operator": "丁"}, 201)
        b1 = r["batch"]
        _, r = self._req("POST", f"/api/documents/{doc}/materials", {
            "kind": "original", "sha256": sha("http-photo"),
            "instrument": "MS-200", "acquired_at": "2026-09-01T09:00:00Z",
            "band": "visible", "batch": b1}, 201)
        photo = r["material"]
        self.assertIsNone(r["dangling_parent"])
        crops = []
        for x in (0, 200):
            _, r = self._req("POST", f"/api/documents/{doc}/materials", {
                "kind": "derived", "sha256": sha(f"http-crop-{x}"),
                "acquired_at": "2026-09-02T09:00:00Z", "parent": photo,
                "processing": {"crop": True},
                "crop": {"x": x, "y": 0, "w": 100, "h": 100}}, 201)
            crops.append(r["material"])
        # 两块截图各支撑一处观察
        for idx, (iid, mat) in enumerate(zip((i1, i2), crops)):
            who = "rA" if idx == 0 else "rB"
            _, r = self._req("POST", f"/api/documents/{doc}/observations", {
                "intersection": iid, "direction": "ink_first", "strength": 5,
                "reviewer": who, "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK,
                "material": mat,
                "region": {"x": 10, "y": 10, "w": 40, "h": 40}}, 201)
            self.assertEqual(r["material"], mat)
            self._req("POST", f"/api/observations/{r['observation']}/reviews",
                      {"reviewer": who, "decision": "accept",
                       "rationale": f"观察{idx}可采"}, 201)
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        # 同一照片的两块截图只算一个独立来源 -> 不满足 min_independent_sources=2
        self.assertEqual(a["edges"][0]["corroborated_by"], 1)
        self.assertFalse(a["conclusion"]["definitive"])
        self.assertIn("insufficient_sources",
                      {d["kind"] for d in a["defects"]})
        # 回归：省略 material 或 region 的观察被公开接口拒绝，门槛不可绕过
        base = {"intersection": i1, "direction": "ink_first", "strength": 5,
                "reviewer": "rA", "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK}
        for extra in ({},
                      {"region": {"x": 1, "y": 1, "w": 5, "h": 5}},
                      {"material": photo}):
            st, r = self._req("POST", f"/api/documents/{doc}/observations",
                              dict(base, **extra), 400)
            self.assertEqual(r["error"], "missing_field", extra)
        _, a = self._req("GET", f"/api/documents/{doc}/analysis")
        self.assertEqual(len(a["observations"]), 2)
        self.assertEqual(a["edges"][0]["corroborated_by"], 1)
        self.assertFalse(a["conclusion"]["definitive"])
        # 谱系一览端点
        _, lin = self._req("GET", f"/api/documents/{doc}/materials")
        self.assertEqual(len(lin["batches"]), 1)
        self.assertEqual(len(lin["materials"]), 3)
        self.assertEqual(len(lin["sources"]), 1)
        # 签结与复算携带谱系与规则
        _, v = self._req("POST", f"/api/documents/{doc}/signoff",
                         {"note": "谱系 v1"}, 201)
        _, snap = self._req("GET", f"/api/versions/{v['version']}")
        self.assertEqual(snap["snapshot"]["lineage"]["sources"], 1)
        _, rec = self._req("GET", f"/api/versions/{v['version']}/recompute")
        self.assertEqual(rec["lineage"]["sources"][0]["originals"], [photo])
        self.assertIn("lineage", rec["recompute"]["rules"])
        _, svg = self._req("GET", f"/api/documents/{doc}/svg")
        self.assertIn("independent sources: 1", svg)

    def test_health(self):
        _, r = self._req("GET", "/api/health")
        self.assertEqual(r["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
