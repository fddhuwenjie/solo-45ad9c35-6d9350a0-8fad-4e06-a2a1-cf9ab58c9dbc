#!/usr/bin/env python3
"""InkSeal 测试套件 —— 仅使用标准库 unittest。"""

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
    oid, _ = inkseal.add_observation(store, doc, {
        "intersection": f"i{iid}", "direction": direction,
        "strength": strength, "reviewer": reviewer,
        "modality": modality, "observed_at": observed_at,
        "conditions": {"lighting": "同轴反射光"},
        "calibration": CAL_OK if calibration is None else calibration})
    return oid


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


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def tearDown(self):
        close_store(self.store)


# --------------------------------------------------------------------------- #
# 纯算法
# --------------------------------------------------------------------------- #

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
        rings, truncated = inkseal.enumerate_simple_cycles(
            nodes, [(1, 2), (2, 3), (3, 1)])
        self.assertFalse(truncated)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0], [1, 2, 3])

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
        with self.assertRaises(inkseal.HttpError) as cm:
            inkseal.add_observation(self.store, doc, {
                "intersection": f"i{iid}", "direction": "sideways",
                "strength": 5, "reviewer": "rA", "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK})
        self.assertEqual(cm.exception.status, 422)

    def test_bad_strength(self):
        doc = new_doc(self.store)
        ink, seal, _ = layers(self.store, doc)
        iid = intersection(self.store, doc, ink, seal)
        with self.assertRaises(inkseal.HttpError):
            inkseal.add_observation(self.store, doc, {
                "intersection": f"i{iid}", "direction": "ink_first",
                "strength": 9, "reviewer": "rA", "modality": "microscopy",
                "observed_at": OBS_AT, "calibration": CAL_OK})

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
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "seal_first", "strength": 5,
            "reviewer": "rA", "modality": "multispectral",
            "observed_at": OBS_AT,
            "conditions": {"band": "660nm"},
            "calibration": CAL_OK}, 201)
        o1 = r["observation"]
        self._req("POST", f"/api/observations/{o1}/reviews",
                  {"reviewer": "rA", "decision": "accept",
                   "rationale": "660nm 印泥在下，墨膜覆盖"}, 201)
        # 第二名审查者独立标注（独立观察）
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "seal_first", "strength": 4,
            "reviewer": "rB", "modality": "microscopy",
            "observed_at": OBS_AT, "calibration": CAL_OK}, 201)
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
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "ink_first", "strength": 5,
            "reviewer": "rA", "modality": "microscopy",
            "observed_at": OBS_AT, "calibration": CAL_OK}, 201)
        o1 = r["observation"]
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "seal_first", "strength": 5,
            "reviewer": "rB", "modality": "multispectral",
            "observed_at": OBS_AT, "calibration": CAL_OK}, 201)
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
        _, r = self._req("POST", f"/api/documents/{doc}/observations", {
            "intersection": iid, "direction": "ink_first", "strength": 5,
            "reviewer": "rA", "modality": "microscopy",
            "observed_at": OBS_AT, "calibration": CAL_OK}, 201)
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
                "observed_at": OBS_AT, "calibration": CAL_OK}, 201)
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

    def test_health(self):
        _, r = self._req("GET", "/api/health")
        self.assertEqual(r["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
