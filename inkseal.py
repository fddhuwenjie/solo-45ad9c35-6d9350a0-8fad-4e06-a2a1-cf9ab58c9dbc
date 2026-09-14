#!/usr/bin/env python3
"""InkSeal — 朱墨时序（书写 / 盖章先后）鉴识服务。

只依赖 Python 标准库：
  * http.server 暴露 HTTP/JSON 端点
  * json        解析载荷
  * sqlite3     持久化文书、交叉点、观察、审查与修订

术语
----
图层对象 layer   : 一页面上的一个对象（墨水笔画 ink / 印泥印章 seal / other）
交叉点 intersection: 墨水对象与印章对象在纸面上的同一坐标位置
观察 observation  : 一名标注者在交叉点上的一次显微 / 多光谱观察，
                    给出方向（ink_first / seal_first）与 1-5 的证据强度

推断模型
--------
每条「采纳」的观察变成一条带证据强度的有向边 pred -> succ
（ink_first：ink -> seal；seal_first：seal -> ink）。
同一对图层身份的边跨交叉点合并取最强证据，强度 >= edge_min_strength 才成立。
在有向图上求可达偏序：可比对、不可比对、节点数最少的矛盾环。

下列任一情况存在，结果 definitive=false，并把缺陷定位回原始观察：
坐标越界 / 图层错绑 / 校准失效 / 同点观察冲突 / 证据链断裂 /
审查未完成或有异议 / 推断成环。
"""

import argparse
import hashlib
import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

API_VERSION = "inkseal/1"

STRENGTH_SCALE = (1, 2, 3, 4, 5)
EDGE_MIN_STRENGTH = 4
VALID_DIRECTIONS = ("ink_first", "seal_first")
VALID_LAYER_KINDS = ("ink", "seal", "other")
VALID_REVIEW_DECISIONS = ("accept", "exclude")
VALID_MODALITIES = ("microscopy", "multispectral", "other")
DECISION_REVISIONS = ("review", "adjudicate")

# 签结时被冻结的规则集。任何改动都会改变 rules_digest，旧版本仍可复算。
RULES = {
    "api": API_VERSION,
    "strength_scale": list(STRENGTH_SCALE),
    "edge_min_strength": EDGE_MIN_STRENGTH,
    "directions": {
        "ink_first": {"predecessor": "ink", "successor": "seal"},
        "seal_first": {"predecessor": "seal", "successor": "ink"},
    },
    "observation_requirements": {
        "intersection": True,
        "direction": list(VALID_DIRECTIONS),
        "strength": list(STRENGTH_SCALE),
        "reviewer": True,
        "modality": list(VALID_MODALITIES),
        "observed_at": "ISO-8601 UTC",
    },
    "calibration": {
        "requires": ["instrument", "valid_until"],
        "valid_until": "ISO-8601 UTC, must be >= observed_at",
    },
    "coordinate": {"requires": ["x", "y"], "bounds": "0 <= x/y <= canvas.w/h"},
    "intersection_layers": "exactly one ink layer and one seal layer, both bound",
    "conflict_policy": "opposite directions among effective observations at the same intersection",
    "review_policy": {
        "reviewers": 2,
        "decisions": list(VALID_REVIEW_DECISIONS),
        "rationale_required": True,
        "unanimous_among_submitted": True,
        "dispute_resolution": "adjudication",
    },
    "blocking_defects": [
        "out_of_bounds",
        "layer_misbind",
        "calibration_invalid",
        "same_point_conflict",
        "broken_chain",
        "review_pending",
        "review_dispute",
        "contradiction_cycle",
    ],
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    """容忍 'Z' 后缀的 ISO-8601 解析。"""
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canon(obj):
    """确定性 JSON 序列化（用于摘要 / 复算）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj):
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def rules_digest():
    return digest(RULES)


class HttpError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def parse_ref(ref, prefix):
    """'o7' -> 7。"""
    if not isinstance(ref, str) or not ref.startswith(prefix):
        raise HttpError(422, "bad_reference", f"expected id like {prefix}<n>", {"got": ref})
    try:
        return int(ref[len(prefix):])
    except ValueError:
        raise HttpError(422, "bad_reference", f"expected id like {prefix}<n>", {"got": ref})


def ref(prefix, ident):
    return f"{prefix}{ident}"


# --------------------------------------------------------------------------- #
# 存储
# -------------------------------------------------------------------: #

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    summary        TEXT NOT NULL,
    reviewers      TEXT NOT NULL,   -- JSON: [{id,name}]，两名审查者
    examiner       TEXT,            -- 采集 / 送检说明
    canvas         TEXT,            -- JSON: {w,h}
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS layers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    description    TEXT
);
CREATE TABLE IF NOT EXISTS intersections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    layer_ids      TEXT NOT NULL,   -- JSON: [layer-id,...]
    coordinate     TEXT NOT NULL,   -- JSON: {x,y}
    canvas         TEXT,            -- JSON: {w,h}，缺省回退文档画布
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    intersection_id INTEGER NOT NULL REFERENCES intersections(id),
    direction      TEXT NOT NULL,
    strength       INTEGER NOT NULL,
    reviewer       TEXT NOT NULL,
    modality       TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    conditions     TEXT,            -- JSON: 采集条件
    calibration    TEXT,            -- JSON: 仪器校准 {instrument,valid_until,details}
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    reviewer       TEXT NOT NULL,
    decision       TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(observation_id, reviewer)
);
CREATE TABLE IF NOT EXISTS revisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    seq            INTEGER NOT NULL,
    kind           TEXT NOT NULL,   -- review | adjudicate
    target_type    TEXT NOT NULL,   -- observation | intersection
    target_id      INTEGER NOT NULL,
    actor          TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    payload        TEXT,            -- JSON 快照
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id       INTEGER NOT NULL REFERENCES documents(id),
    note              TEXT,
    definitive        INTEGER NOT NULL,
    material_digest   TEXT NOT NULL,
    rules_digest      TEXT NOT NULL,
    decisions_digest  TEXT NOT NULL,
    snapshot          TEXT NOT NULL, -- JSON 全量冻结快照
    created_at        TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql, args=()):
        return self.conn.execute(sql, args)

    def commit(self):
        self.conn.commit()

    # -- 基础加载 ---------------------------------------------------------- #

    def get_document(self, doc_id):
        row = self.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HttpError(404, "not_found", f"document d{doc_id} not found")
        return row

    def layers(self, doc_id):
        return self.execute(
            "SELECT * FROM layers WHERE document_id=? ORDER BY id", (doc_id,)
        ).fetchall()

    def intersections(self, doc_id):
        return self.execute(
            "SELECT * FROM intersections WHERE document_id=? ORDER BY id", (doc_id,)
        ).fetchall()

    def observations(self, doc_id):
        return self.execute(
            "SELECT * FROM observations WHERE document_id=? ORDER BY id", (doc_id,)
        ).fetchall()

    def reviews_for(self, observation_ids):
        if not observation_ids:
            return {}
        marks = ",".join("?" * len(observation_ids))
        rows = self.execute(
            f"SELECT * FROM reviews WHERE observation_id IN ({marks}) ORDER BY id",
            tuple(observation_ids),
        ).fetchall()
        out = defaultdict(list)
        for r in rows:
            out[r["observation_id"]].append(r)
        return out

    def revisions(self, doc_id):
        return self.execute(
            "SELECT * FROM revisions WHERE document_id=? ORDER BY seq, id", (doc_id,)
        ).fetchall()

    def next_revision_seq(self, doc_id):
        row = self.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM revisions WHERE document_id=?",
            (doc_id,),
        ).fetchone()
        return row["s"]

    def add_revision(self, doc_id, kind, target_type, target_id, actor, rationale, payload):
        seq = self.next_revision_seq(doc_id)
        self.execute(
            "INSERT INTO revisions(document_id,seq,kind,target_type,target_id,actor,"
            "rationale,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (doc_id, seq, kind, target_type, target_id, actor, rationale,
             json.dumps(payload, ensure_ascii=False), now_iso()),
        )
        return seq

    def latest_version(self, doc_id):
        return self.execute(
            "SELECT * FROM versions WHERE document_id=? ORDER BY id DESC LIMIT 1", (doc_id,)
        ).fetchone()

    def get_version(self, version_id):
        row = self.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if not row:
            raise HttpError(404, "not_found", f"version v{version_id} not found")
        return row


# --------------------------------------------------------------------------- #
# 校验（采集阶段的事实校验；缺陷在分析阶段暴露，不阻断录入）
# --------------------------------------------------------------------------- #

def require(body, key, types=None):
    if key not in body:
        raise HttpError(400, "missing_field", f"field '{key}' is required", {"field": key})
    if types and not isinstance(body[key], types):
        raise HttpError(400, "bad_field", f"field '{key}' has wrong type", {"field": key})
    return body[key]


def check_coordinate(coord, canvas):
    """返回越界缺陷消息；正常返回 None。"""
    if not isinstance(coord, dict) or not all(k in coord for k in ("x", "y")):
        return "coordinate must be {x,y}"
    x, y = coord["x"], coord["y"]
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return "coordinate x/y must be numbers"
    if not canvas or not all(k in canvas for k in ("w", "h")):
        return "canvas {w,h} is required to bound the coordinate"
    w, h = canvas["w"], canvas["h"]
    if x < 0 or y < 0 or x > w or y > h:
        return f"coordinate ({x},{y}) outside canvas ({w}x{h})"
    return None


def check_calibration(cal, observed_at):
    """校准失效：缺仪器、缺有效期，或有效期早于观察时刻。"""
    if not isinstance(cal, dict):
        return "calibration object is required"
    instrument = cal.get("instrument")
    valid_until = cal.get("valid_until")
    if not instrument:
        return "calibration.instrument is required"
    if not valid_until:
        return "calibration.valid_until is required"
    try:
        if parse_iso(valid_until) < parse_iso(observed_at):
            return f"calibration expired at {valid_until}, before observation {observed_at}"
    except ValueError as exc:
        return f"unparsable calibration timestamp: {exc}"
    return None


# --------------------------------------------------------------------------- #
# 图算法
# --------------------------------------------------------------------------- #

def tarjan_scc(nodes, edges):
    """迭代版 Tarjan，返回 SCC 列表（每 SCC 为节点列表）。"""
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
    index = {}
    low = {}
    stack = []
    on_stack = set()
    counter = [0]
    result = []

    def strong(v):
        work = [(v, iter(adj[v]))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, it = work[-1]
            try:
                w = next(it)
            except StopIteration:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        x = stack.pop()
                        on_stack.discard(x)
                        comp.append(x)
                        if x == node:
                            break
                    result.append(comp)
                continue
            if w not in index:
                index[w] = low[w] = counter[0]
                counter[0] += 1
                stack.append(w)
                on_stack.add(w)
                work.append((w, iter(adj[w])))
            elif w in on_stack:
                low[node] = min(low[node], index[w])

    for n in nodes:
        if n not in index:
            strong(n)
    return result


def cycle_girth(nodes, adj):
    """有向图的最短环长；无环返回 None。自环不计（本模型无自环）。

    从每个节点 BFS，首次回到起点的边给出过该点的最短环；
    已找到的更短环会截枝。O(n·(n+e))，图很小。
    """
    best = None
    for s in sorted(nodes):
        dist = {s: 0}
        queue = [s]
        while queue:
            node = queue.pop(0)
            d = dist[node] + 1
            if best is not None and d >= best:
                break  # 队列按距离非减，之后不可能更短
            for w in adj[node]:
                if w == s:
                    if d >= 2:
                        best = d if best is None else min(best, d)
                elif w not in dist:
                    dist[w] = d
                    queue.append(w)
    return best


def enumerate_simple_cycles(nodes, edges):
    """枚举全部最短简单环（长度 = 全图最短环长），完整集合。

    先 BFS 求最短环长 L，再只枚举长度恰为 L 的环：高分支组件的
    长环不会挤占名额、让别处的更短环被遗漏；并列最短环不设上限，
    一个不少地完整返回。每个环只从“环内最小 id”出发、只访问
    更大 id 的节点，恰好枚举一次，无需事后去重。
    返回节点序列环（首节点 = 环内最小 id），按 (长度, 节点序列)
    排序，顺序确定。
    """
    adj = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
    girth = cycle_girth(nodes, adj)
    if girth is None:
        return []
    found = []

    def simple_dfs(start):
        # 用元组传递路径，避免回溯时可变状态出错；邻接按 id 排序保证确定性。
        stack = [(start, (start,), frozenset((start,)))]
        while stack:
            node, path, visited = stack.pop()
            for w in sorted(adj[node]):
                if w == start and len(path) == girth:
                    found.append(list(path))
                elif w not in visited and w > start and len(path) < girth:
                    stack.append((w, path + (w,), visited | {w}))

    for s in sorted(nodes):
        simple_dfs(s)

    found.sort(key=lambda r: (len(r), [str(x) for x in r]))
    return found


def shortest_cycles(rings):
    """只保留节点数最少的环。

    同一矛盾可能同时引出短环与绕行长环；定位以最短环为准，
    长环只是同一矛盾的间接转述。输入已由 enumerate_simple_cycles
    按 (长度, 节点序列) 排序，并列最短的多个环全部保留且顺序确定。
    """
    if not rings:
        return []
    shortest = min(len(r) for r in rings)
    return [r for r in rings if len(r) == shortest]


def reachability(nodes, edge_list):
    reach = {a: {a} for a in nodes}
    for a, b in edge_list:
        reach[a].add(b)
    changed = True
    while changed:
        changed = False
        for a in nodes:
            added = set()
            for b in list(reach[a]):
                added |= reach[b]
            if not added <= reach[a]:
                reach[a] |= added
                changed = True
    return reach


def bfs_path(adj, src, dst):
    if src == dst:
        return [src]
    seen = {src: None}
    queue = [src]
    while queue:
        node = queue.pop(0)
        for w in adj.get(node, ()):  # noqa: B023 - 纯只读
            if w in seen:
                continue
            seen[w] = node
            if w == dst:
                path = [w]
                while seen[path[-1]] is not None:
                    path.append(seen[path[-1]])
                return list(reversed(path))
            queue.append(w)
    return None


# --------------------------------------------------------------------------- #
# 核心分析
# --------------------------------------------------------------------------- #

def _j(row, key):
    value = row[key]
    return json.loads(value) if value is not None else None


def effective_review_state(obs_id, reviews_by_obs, forced):
    """返回 (state, reasons)。

    forced: observation_id -> ('accept'|'exclude', revision, adjudication dict)
    state: accepted | excluded | pending | disputed
    """
    if obs_id in forced:
        verdict, revision, _adj = forced[obs_id]
        return ("accepted" if verdict == "accept" else "excluded",
                [f"adjudication r{revision['seq']} by {revision['actor']}: "
                 f"{revision['rationale']}"])
    reviews = reviews_by_obs.get(obs_id, [])
    if not reviews:
        return "pending", ["no review recorded"]
    decisions = {r["decision"] for r in reviews}
    if len(decisions) == 1:
        decision = decisions.pop()
        who = ", ".join(f"{r['reviewer']}={r['decision']}({r['rationale']})" for r in reviews)
        return ("accepted" if decision == "accept" else "excluded"), [who]
    return "disputed", [
        f"{r['reviewer']}={r['decision']}({r['rationale']})" for r in reviews
    ]


def build_material(store, doc, layers, intersections, observations):
    """被 material_digest 锁定的事实素材（不含审查决定）。"""
    return {
        "api": API_VERSION,
        "summary": doc["summary"],
        "examiner": doc["examiner"],
        "canvas": _j(doc, "canvas"),
        "layers": sorted(
            ([l["id"], l["name"], l["kind"], l["description"]] for l in layers),
            key=lambda x: x[0],
        ),
        "intersections": sorted(
            ([i["id"], sorted(_j(i, "layer_ids")), _j(i, "coordinate"),
              _j(i, "canvas"), i["note"]] for i in intersections),
            key=lambda x: x[0],
        ),
        "observations": sorted(
            ([o["id"], o["intersection_id"], o["direction"], o["strength"],
              o["reviewer"], o["modality"], o["observed_at"],
              _j(o, "conditions"), _j(o, "calibration")] for o in observations),
            key=lambda x: x[0],
        ),
    }


def build_decisions(reviews, revisions):
    return {
        "reviews": sorted(
            ([r["observation_id"], r["reviewer"], r["decision"], r["rationale"]]
             for r in reviews),
            key=lambda x: (x[0], x[1]),
        ),
        "revisions": sorted(
            ([r["seq"], r["kind"], r["target_type"], r["target_id"], r["actor"],
              r["rationale"], json.loads(r["payload"]) if r["payload"] else None]
             for r in revisions),
            key=lambda x: x[0],
        ),
    }


def analyze(store, doc_id):
    """对文档当前状态做确定性复算，返回完整分析 JSON（可直接作为响应 / 快照）。"""
    doc = store.get_document(doc_id)
    layers = store.layers(doc_id)
    intersections = store.intersections(doc_id)
    observations = store.observations(doc_id)
    revisions = store.revisions(doc_id)
    reviews = [r for rows in store.reviews_for([o["id"] for o in observations]).values()
               for r in rows]
    reviewer_ids = [r["id"] for r in _j(doc, "reviewers")]
    doc_canvas = _j(doc, "canvas")

    layer_by_id = {l["id"]: l for l in layers}
    ix_by_id = {i["id"]: i for i in intersections}

    # -- 裁决派生的强制结论（同一观察以最后一次裁决为准） -------------------- #
    forced = {}
    adjudications = [r for r in revisions if r["kind"] == "adjudicate"]
    for rev in adjudications:
        payload = json.loads(rev["payload"])
        for oid in payload["accepted_observations"]:
            forced[oid] = ("accept", rev, payload)
        for oid in payload["excluded_observations"]:
            forced[oid] = ("exclude", rev, payload)

    reviews_by_obs = store.reviews_for([o["id"] for o in observations])

    defects = []

    def add_defect(kind, message, obs_id=None, ix_id=None):
        loc = {}
        if obs_id is not None:
            loc["observation"] = ref("o", obs_id)
        if ix_id is not None:
            loc["intersection"] = ref("i", ix_id)
        defects.append({"kind": kind, "message": message, "location": loc})

    # -- 交叉点结构缺陷（越界 / 错绑） ------------------------------------- #
    ix_structural_bad = {}
    for ix in intersections:
        bound = _j(ix, "layer_ids")
        coord = _j(ix, "coordinate")
        canvas = _j(ix, "canvas") or doc_canvas
        problems = []
        if not isinstance(bound, list) or len(bound) < 2:
            problems.append("intersection must bind at least two layers")
            bound = bound if isinstance(bound, list) else []
        unknown = [b for b in bound if b not in layer_by_id]
        msg_bounds = check_coordinate(coord, canvas)
        kinds = defaultdict(list)
        for b in bound:
            if b in layer_by_id:
                kinds[layer_by_id[b]["kind"]].append(b)
        bind_problems = []
        if not unknown:
            if len(kinds["ink"]) != 1 or len(kinds["seal"]) != 1:
                bind_problems.append(
                    f"intersection must bind exactly one ink and one seal layer "
                    f"(found ink={[f'l{x}' for x in kinds['ink']]}, "
                    f"seal={[f'l{x}' for x in kinds['seal']]})")
        if unknown or bind_problems:
            all_bind = [f"layer(s) not bound to document: "
                        + ", ".join(f"l{u}" for u in unknown)] + bind_problems
            add_defect("layer_misbind", "; ".join(p for p in all_bind if p),
                       ix_id=ix["id"])
        if msg_bounds:
            add_defect("out_of_bounds", msg_bounds, ix_id=ix["id"])
        problems = ([f"unbound layer l{u}" for u in unknown] + bind_problems
                    + ([msg_bounds] if msg_bounds else []))
        if problems:
            ix_structural_bad[ix["id"]] = problems

    # -- 逐观察：审查状态 + 事实缺陷 --------------------------------------- #
    obs_view = []
    active_ids = set()
    for o in observations:
        state, reasons = effective_review_state(o["id"], reviews_by_obs, forced)
        view = {
            "id": ref("o", o["id"]),
            "intersection": ref("i", o["intersection_id"]),
            "direction": o["direction"],
            "strength": o["strength"],
            "reviewer": o["reviewer"],
            "modality": o["modality"],
            "observed_at": o["observed_at"],
            "conditions": _j(o, "conditions"),
            "calibration": _j(o, "calibration"),
            "state": state,
            "state_reasons": reasons,
            "factual_defects": [],
        }
        ix = ix_by_id.get(o["intersection_id"])
        if ix is None:
            view["factual_defects"].append("broken_chain:dangling intersection")
            add_defect("broken_chain",
                       f"o{o['id']} references missing intersection "
                       f"i{o['intersection_id']}", obs_id=o["id"])
        else:
            if ix["id"] in ix_structural_bad:
                view["factual_defects"].append("broken_chain:intersection structurally invalid")
                add_defect(
                    "broken_chain",
                    f"o{o['id']} hangs off structurally invalid intersection "
                    f"i{ix['id']} ({'; '.join(ix_structural_bad[ix['id']])})",
                    obs_id=o["id"], ix_id=ix["id"])
            msg_cal = check_calibration(_j(o, "calibration"), o["observed_at"])
            if msg_cal:
                view["factual_defects"].append("calibration_invalid")
                add_defect("calibration_invalid", f"o{o['id']}: {msg_cal}",
                           obs_id=o["id"], ix_id=ix["id"])
        if state == "pending":
            add_defect("review_pending",
                       f"o{o['id']} has no review yet", obs_id=o["id"],
                       ix_id=o["intersection_id"])
        elif state == "disputed":
            add_defect("review_dispute",
                       f"o{o['id']} reviewers disagree: {'; '.join(reasons)}",
                       obs_id=o["id"], ix_id=o["intersection_id"])
        if state == "accepted" and not view["factual_defects"]:
            active_ids.add(o["id"])
        obs_view.append(view)

    # -- 同交叉点合并 + 冲突检测（仅对参与推断的有效观察） ------------------ #
    groups = defaultdict(list)
    for o in observations:
        if o["id"] in active_ids:
            groups[o["intersection_id"]].append(o)

    conflicted_obs = set()
    merged_edges = defaultdict(lambda: {"witnesses": [], "intersections": set()})

    for ix_id, obs_list in groups.items():
        dirs = {o["direction"] for o in obs_list}
        if len(dirs) > 1:
            conflicted_obs.update(o["id"] for o in obs_list)
            detail = "; ".join(
                f"o{o['id']}={o['direction']} by {o['reviewer']} "
                f"(strength {o['strength']})" for o in obs_list)
            # 单条缺陷：消息列明各方向，位置覆盖全部原始观察与交叉点
            defects.append({
                "kind": "same_point_conflict",
                "message": f"contrary ordering evidence at i{ix_id}: {detail}",
                "location": {
                    "intersection": ref("i", ix_id),
                    "observations": [ref("o", o["id"]) for o in obs_list],
                },
            })

    # -- 边聚合：按图层对象身份跨交叉点合并 -------------------------------- #
    for ix_id, obs_list in groups.items():
        ix = ix_by_id[ix_id]
        bound = _j(ix, "layer_ids")
        ink = next(b for b in bound if layer_by_id[b]["kind"] == "ink")
        seal = next(b for b in bound if layer_by_id[b]["kind"] == "seal")
        for o in obs_list:
            if o["id"] in conflicted_obs:
                continue
            if o["direction"] == "ink_first":
                pred, succ = ink, seal
            else:
                pred, succ = seal, ink
            slot = merged_edges[(pred, succ)]
            slot["witnesses"].append({
                "observation": ref("o", o["id"]),
                "intersection": ref("i", ix_id),
                "reviewer": o["reviewer"],
                "strength": o["strength"],
                "modality": o["modality"],
            })
            slot["intersections"].add(ix_id)

    # 强度 >= 阈值才成立为图边；否则构成“证据链断裂”：
    # 已采纳的观察不足以单独建立先后关系，且没有其他交叉点补强。
    edges = []
    rejected_edges = []
    for (pred, succ), slot in sorted(merged_edges.items(), key=lambda kv: kv[0]):
        strength = max(w["strength"] for w in slot["witnesses"])
        edge = {
            "from": ref("l", pred),
            "to": ref("l", succ),
            "strength": strength,
            "corroborated_by": len(slot["witnesses"]),
            "intersections": sorted(ref("i", i) for i in slot["intersections"]),
            "witnesses": sorted(slot["witnesses"], key=lambda w: w["observation"]),
            "established": strength >= EDGE_MIN_STRENGTH,
        }
        if edge["established"]:
            edges.append(edge)
        else:
            rejected_edges.append(edge)
            obs_refs = [w["observation"] for w in edge["witnesses"]]
            defects.append({
                "kind": "broken_chain",
                "message": (
                    f"accepted evidence {', '.join(obs_refs)} for "
                    f"{edge['from']} before {edge['to']} has max strength "
                    f"{strength} below edge_min_strength={EDGE_MIN_STRENGTH} "
                    "and no corroboration: chain link does not establish"),
                "location": {
                    "intersections": edge["intersections"],
                    "observations": obs_refs,
                },
            })

    # -- 图：SCC / 环 / 可达 ---------------------------------------------- #
    node_ids = sorted(layer_by_id.keys())
    edge_pairs = [(parse_ref(e["from"], "l"), parse_ref(e["to"], "l")) for e in edges]
    edge_index = {(a, b): e for (a, b), e in zip(edge_pairs, edges)}

    sccs = tarjan_scc(node_ids, edge_pairs)
    cyclic_components = [c for c in sccs if len(c) > 1 or any(
        b == c[0] for a, b in edge_pairs if a == c[0])]
    # 自环也视为环（本模型一般无自环）
    cyclic_nodes = {n for comp in cyclic_components for n in comp}

    rings = enumerate_simple_cycles(node_ids, edge_pairs)
    rings = shortest_cycles(rings)

    cycles_out = []
    cycle_obs = set()
    for ring in rings:
        witness_obs = []
        ring_edges = []
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            ring_edges.append([ref("l", a), ref("l", b)])
            for w in edge_index[(a, b)]["witnesses"]:
                witness_obs.append(w["observation"])
                cycle_obs.add(parse_ref(w["observation"], "o"))
        cycles_out.append({
            "nodes": [ref("l", n) for n in ring],
            "edges": ring_edges,
            "observations": sorted(set(witness_obs)),
        })
        add_defect("contradiction_cycle",
                   f"ordering cycle through {', '.join(ref('l', n) for n in ring)} "
                   f"based on {', '.join(sorted(set(witness_obs)))}",
                   ix_id=None)

    reach = reachability(node_ids, edge_pairs)
    adj = defaultdict(list)
    for a, b in edge_pairs:
        adj[a].append(b)

    # 去掉“回边”（去掉后 SCC 即瓦解的边）后的可达性：
    # 一对节点若不靠回边仍可到达，顺序就不受矛盾环污染。
    cycle_edge_pairs = set()
    for ring in rings:
        for i in range(len(ring)):
            cycle_edge_pairs.add((ring[i], ring[(i + 1) % len(ring)]))
    acyclic_pairs = [(a, b) for a, b in edge_pairs if (a, b) not in cycle_edge_pairs]
    reach_sound = reachability(node_ids, acyclic_pairs)

    # -- 偏序输出 ---------------------------------------------------------- #
    def witnesses_on_path(path):
        obs = set()
        for i in range(len(path) - 1):
            for w in edge_index[(path[i], path[i + 1])]["witnesses"]:
                obs.add(w["observation"])
        return obs

    order = []
    incomparable = []
    tainted_pairs = []
    for i in range(len(node_ids)):
        for j in range(i + 1, len(node_ids)):
            a, b = node_ids[i], node_ids[j]
            ab = b in reach[a]
            ba = a in reach[b]
            if ab and ba:
                # 同一强连通（矛盾环）分量
                tainted_pairs.append([ref("l", a), ref("l", b)])
                continue
            if not ab and not ba:
                incomparable.append([ref("l", a), ref("l", b)])
                continue
            if ab:
                earlier, later = a, b
            else:
                earlier, later = b, a
            path = bfs_path(adj, earlier, later)
            witnesses = sorted(witnesses_on_path(path))
            pair = {
                "earlier": ref("l", earlier),
                "later": ref("l", later),
                "observations": witnesses,
                "via_edges": [[ref("l", path[k]), ref("l", path[k + 1])]
                              for k in range(len(path) - 1)],
            }
            if later not in reach_sound[earlier]:
                pair["tainted_by_cycle"] = True
                tainted_pairs.append([pair["earlier"], pair["later"]])
            order.append(pair)

    # 直接边（非经由更长路径）；环内边不标 direct
    direct = []
    for e, (a, b) in zip(edges, edge_pairs):
        if a in cyclic_nodes or b in cyclic_nodes:
            continue
        alt_adj = defaultdict(list)
        for x, y in edge_pairs:
            if (x, y) != (a, b):
                alt_adj[x].append(y)
        if bfs_path(alt_adj, a, b) is None:
            direct.append([ref("l", a), ref("l", b)])

    # -- 结论 -------------------------------------------------------------- #
    # 所有登记的缺陷都阻断确定结论；每条缺陷都已带原始观察 / 交叉点定位。
    definitive = not defects

    def layer_ref(lid):
        l = layer_by_id[lid]
        return {"id": ref("l", lid), "name": l["name"], "kind": l["kind"]}

    material = build_material(store, doc, layers, intersections, observations)
    decisions = build_decisions(reviews, revisions)
    mdig = digest(material)
    ddig = digest(decisions)
    rdig = rules_digest()

    return {
        "api": API_VERSION,
        "document": ref("d", doc_id),
        "summary": doc["summary"],
        "reviewers": _j(doc, "reviewers"),
        "conclusion": {
            "definitive": definitive,
            "status": "determined" if definitive else "inconclusive",
            "statement": None
            if not definitive
            else "; ".join(f"{p['earlier']} before {p['later']}" for p in order)
                 or "no comparable object pairs",
            "blocking_defect_count": len(defects),
        },
        "order": order,
        "incomparable_pairs": incomparable,
        "tainted_pairs": tainted_pairs,
        "cycles": cycles_out,
        "edges": edges,
        "rejected_edges": rejected_edges,
        "direct_edges": direct,
        "layers": [layer_ref(lid) for lid in node_ids],
        "intersections": [
            {
                "id": ref("i", ix["id"]),
                "layers": [ref("l", b) for b in _j(ix, "layer_ids")],
                "coordinate": _j(ix, "coordinate"),
                "canvas": _j(ix, "canvas") or doc_canvas,
                "note": ix["note"],
                "structurally_valid": ix["id"] not in ix_structural_bad,
            }
            for ix in intersections
        ],
        "observations": obs_view,
        "defects": defects,
        "recompute": {
            "material_digest": mdig,
            "rules_digest": rdig,
            "decisions_digest": ddig,
            "rules": RULES,
            "digest": digest([mdig, rdig, ddig]),
        },
    }


# --------------------------------------------------------------------------- #
# 签结 / 差异 / SVG
# --------------------------------------------------------------------------- #

def signoff(store, doc_id, note):
    with store.lock:
        analysis = analyze(store, doc_id)
        doc = store.get_document(doc_id)
        revisions = store.revisions(doc_id)
        reviews = [r for rows in store.reviews_for(
            [o["id"] for o in store.observations(doc_id)]).values() for r in rows]
        snapshot = {
            "api": API_VERSION,
            "signed_at": now_iso(),
            "note": note,
            "analysis": analysis,
            "material": {
                "digest": analysis["recompute"]["material_digest"],
            },
            "rules": {
                "digest": analysis["recompute"]["rules_digest"],
                "body": RULES,
            },
            "decisions": {
                "digest": analysis["recompute"]["decisions_digest"],
                "reviews": build_decisions(reviews, revisions)["reviews"],
                "revisions": build_decisions(reviews, revisions)["revisions"],
            },
        }
        cur = store.execute(
            "INSERT INTO versions(document_id,note,definitive,material_digest,"
            "rules_digest,decisions_digest,snapshot,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, note, 1 if analysis["conclusion"]["definitive"] else 0,
             analysis["recompute"]["material_digest"],
             analysis["recompute"]["rules_digest"],
             analysis["recompute"]["decisions_digest"],
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now_iso()),
        )
        store.commit()
        version_id = cur.lastrowid
        return version_id, snapshot


def version_diff(snap_a, snap_b):
    """两个签结版本的差异。"""
    an, bn = snap_a["analysis"], snap_b["analysis"]

    def obs_index(analysis):
        return {o["id"]: o for o in analysis["observations"]}

    def ix_index(analysis):
        return {i["id"]: i for i in analysis["intersections"]}

    oa, ob = obs_index(an), obs_index(bn)
    added_o = sorted(set(ob) - set(oa))
    removed_o = sorted(set(oa) - set(ob))
    changed_o = []
    for key in sorted(set(oa) & set(ob)):
        changes = {}
        for field in ("direction", "strength", "state"):
            if oa[key][field] != ob[key][field]:
                changes[field] = {"from": oa[key][field], "to": ob[key][field]}
        if changes:
            changed_o.append({"observation": key, "changes": changes})

    ia, ib = ix_index(an), ix_index(bn)
    added_i = sorted(set(ib) - set(ia))
    removed_i = sorted(set(ia) - set(ib))

    ea = {(e["from"], e["to"]): e for e in an["edges"]}
    eb = {(e["from"], e["to"]): e for e in bn["edges"]}
    edges_added = sorted(set(eb) - set(ea))
    edges_removed = sorted(set(ea) - set(eb))

    def pairset(analysis):
        return {(p["earlier"], p["later"]) for p in analysis["order"]}

    pa, pb = pairset(an), pairset(bn)

    revs_a = {tuple(r[:2]) for r in snap_a["decisions"]["revisions"]}
    revs_b = snap_b["decisions"]["revisions"]
    revisions_added = [r for r in revs_b if tuple(r[:2]) not in revs_a]

    return {
        "api": API_VERSION,
        "from": {"digest": snap_a["material"]["digest"]},
        "to": {"digest": snap_b["material"]["digest"]},
        "material_changed": snap_a["material"]["digest"] != snap_b["material"]["digest"],
        "rules_changed": snap_a["rules"]["digest"] != snap_b["rules"]["digest"],
        "decisions_changed": snap_a["decisions"]["digest"] != snap_b["decisions"]["digest"],
        "intersections_added": added_i,
        "intersections_removed": removed_i,
        "observations_added": added_o,
        "observations_removed": removed_o,
        "observations_changed": changed_o,
        "edges_added": [{"from": a, "to": b} for a, b in edges_added],
        "edges_removed": [{"from": a, "to": b} for a, b in edges_removed],
        "order_added": [{"earlier": a, "later": b} for a, b in sorted(pb - pa)],
        "order_removed": [{"earlier": a, "later": b} for a, b in sorted(pa - pb)],
        "cycles_from": len(an["cycles"]),
        "cycles_to": len(bn["cycles"]),
        "definitive_from": an["conclusion"]["definitive"],
        "definitive_to": bn["conclusion"]["definitive"],
        "revisions_added": [
            {"seq": r[0], "kind": r[1], "actor": r[4], "rationale": r[5]}
            for r in revisions_added
        ],
    }


def render_svg(analysis):
    """把顺序图渲染成 SVG（DAG 分层布局；环内节点标红）。"""
    layers = {l["id"]: l for l in analysis["layers"]}
    ids = list(layers)

    succ = defaultdict(list)
    pred = defaultdict(list)
    for e in analysis["edges"]:
        succ[e["from"]].append(e["to"])
        pred[e["to"]].append(e["from"])

    cyclic = {n for c in analysis["cycles"] for n in c["nodes"]}

    # 最长路径分层（忽略回边造成的层级修正，环节点单独着色）
    rank = {}

    def compute_rank(node, seen=None):
        if node in rank:
            return rank[node]
        seen = seen or set()
        if node in seen:
            return 0
        seen.add(node)
        up = [compute_rank(p, seen) for p in pred.get(node, []) if p not in cyclic]
        rank[node] = 1 + max([-1] + up)
        return rank[node]

    for n in ids:
        compute_rank(n)

    rows = defaultdict(list)
    for n in ids:
        rows[rank.get(n, 0)].append(n)
    max_rank = max(rows, default=0)

    box_w, box_h = 150, 54
    x_gap, y_gap = 60, 40
    positions = {}
    for r in range(max_rank + 1):
        row = sorted(rows[r])
        for k, n in enumerate(row):
            positions[n] = (40 + k * (box_w + x_gap), 40 + r * (box_h + y_gap))

    width = max((x + box_w + 40 for x, _ in positions.values()), default=240)
    height = 80 + (max_rank + 1) * (box_h + y_gap)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif">',
        '<defs><marker id="arrow" markerWidth="10" markerHeight="10" '
        'refX="9" refY="3" orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L0,6 L9,3 z" fill="#444"/></marker></defs>',
    ]
    for e in analysis["edges"]:
        (x1, y1) = positions[e["from"]]
        (x2, y2) = positions[e["to"]]
        color = "#c0392b" if e["from"] in cyclic or e["to"] in cyclic else "#444"
        dash = ' stroke-dasharray="6,4"' if e["strength"] < 5 else ""
        parts.append(
            f'<line x1="{x1 + box_w}" y1="{y1 + box_h / 2:.0f}" '
            f'x2="{x2}" y2="{y2 + box_h / 2:.0f}" stroke="{color}" '
            f'stroke-width="2" marker-end="url(#arrow)"{dash}/>'
        )
        midx, midy = (x1 + box_w + x2) / 2, (y1 + y2) / 2 + 14
        parts.append(
            f'<text x="{midx:.0f}" y="{midy:.0f}" text-anchor="middle" '
            f'font-size="11" fill="{color}">s{e["strength"]} '
            f'×{e["corroborated_by"]}</text>'
        )
    for n, (x, y) in positions.items():
        info = layers[n]
        fill = "#fdecea" if n in cyclic else "#eef4fb"
        stroke = "#c0392b" if n in cyclic else "#2c6fbb"
        parts.append(
            f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="8" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        parts.append(
            f'<text x="{x + box_w / 2:.0f}" y="{y + 22}" text-anchor="middle" '
            f'font-size="13" font-weight="bold">{info["name"]} ({n})</text>')
        parts.append(
            f'<text x="{x + box_w / 2:.0f}" y="{y + 40}" text-anchor="middle" '
            f'font-size="11" fill="#555">{info["kind"]}</text>')
    status = ("DETERMINED" if analysis["conclusion"]["definitive"]
              else f'INCONCLUSIVE — {analysis["conclusion"]["blocking_defect_count"]} '
                   "blocking defect(s)")
    parts.append(
        f'<text x="12" y="{height - 12}" font-size="12" '
        f'fill="{"#1e7d32" if analysis["conclusion"]["definitive"] else "#c0392b"}">'
        f'{status}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 写入服务（被 HTTP 层调用，也可直接被测试调用）
# --------------------------------------------------------------------------- #

def normalize_reviewer_ids(reviewers):
    """每个审查者 id 去首尾空白；非对象 / 非字符串 id 记为 None。"""
    ids = []
    for r in reviewers:
        rid = r.get("id") if isinstance(r, dict) else None
        ids.append(rid.strip() if isinstance(rid, str) else None)
    return ids


def create_document(store, body):
    summary = require(body, "summary", str)
    reviewers = require(body, "reviewers", list)
    # 同一人不得充当两名审查者：id 去空白后都必须非空且互不相同
    ids = normalize_reviewer_ids(reviewers)
    if len(ids) != 2 or any(not rid for rid in ids) or ids[0] == ids[1]:
        raise HttpError(422, "bad_reviewers",
                        "exactly two reviewers with distinct non-blank ids "
                        "are required")
    normalized = [dict(r, id=rid) for r, rid in zip(reviewers, ids)]
    canvas = body.get("canvas")
    if canvas is not None and (not all(k in canvas for k in ("w", "h"))):
        raise HttpError(400, "bad_canvas", "canvas must be {w,h}")
    with store.lock:
        cur = store.execute(
            "INSERT INTO documents(summary,reviewers,examiner,canvas,created_at) "
            "VALUES(?,?,?,?,?)",
            (summary, json.dumps(normalized, ensure_ascii=False),
             body.get("examiner"),
             json.dumps(canvas) if canvas else None, now_iso()))
        store.commit()
        return cur.lastrowid


def add_layer(store, doc_id, body):
    store.get_document(doc_id)
    name = require(body, "name", str)
    kind = require(body, "kind", str)
    if kind not in VALID_LAYER_KINDS:
        raise HttpError(422, "bad_kind", f"kind must be one of {VALID_LAYER_KINDS}")
    with store.lock:
        cur = store.execute(
            "INSERT INTO layers(document_id,name,kind,description) VALUES(?,?,?,?)",
            (doc_id, name, kind, body.get("description")))
        store.commit()
        return cur.lastrowid


def add_intersection(store, doc_id, body):
    doc = store.get_document(doc_id)
    layer_refs = require(body, "layer_ids", list)
    coord = require(body, "coordinate", dict)
    ids = [parse_ref(x, "l") for x in layer_refs]
    known = {l["id"] for l in store.layers(doc_id)}
    unknown = [i for i in ids if i not in known]
    # 未知图层允许录入（缺陷在分析时暴露），但必须是数字引用，已由 parse_ref 保证。
    canvas = body.get("canvas") or _j(doc, "canvas")
    with store.lock:
        cur = store.execute(
            "INSERT INTO intersections(document_id,layer_ids,coordinate,canvas,note,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (doc_id, json.dumps(ids), json.dumps(coord, ensure_ascii=False),
             json.dumps(canvas) if canvas else None, body.get("note"), now_iso()))
        store.commit()
        return cur.lastrowid, unknown


def add_observation(store, doc_id, body):
    doc = store.get_document(doc_id)
    ix_ref = require(body, "intersection", str)
    direction = require(body, "direction", str)
    strength = require(body, "strength", int)
    reviewer = require(body, "reviewer", str)
    modality = require(body, "modality", str)
    observed_at = require(body, "observed_at", str)
    ix_id = parse_ref(ix_ref, "i")
    if direction not in VALID_DIRECTIONS:
        raise HttpError(422, "bad_direction", f"direction must be {VALID_DIRECTIONS}")
    if strength not in STRENGTH_SCALE:
        raise HttpError(422, "bad_strength",
                        f"strength must be one of {STRENGTH_SCALE}")
    if modality not in VALID_MODALITIES:
        raise HttpError(422, "bad_modality", f"modality must be {VALID_MODALITIES}")
    try:
        parse_iso(observed_at)
    except ValueError as exc:
        raise HttpError(422, "bad_timestamp", str(exc))
    ix = store.execute("SELECT id FROM intersections WHERE id=? AND document_id=?",
                       (ix_id, doc_id)).fetchone()
    if not ix:
        raise HttpError(404, "not_found",
                        f"intersection {ix_ref} not found in document d{doc_id}")
    cal = body.get("calibration")
    if cal is not None and cal.get("valid_until"):
        try:
            parse_iso(cal["valid_until"])
        except ValueError as exc:
            raise HttpError(422, "bad_timestamp", str(exc))
    known_reviewers = {r["id"] for r in _j(doc, "reviewers")}
    external = reviewer not in known_reviewers
    with store.lock:
        cur = store.execute(
            "INSERT INTO observations(document_id,intersection_id,direction,strength,"
            "reviewer,modality,observed_at,conditions,calibration,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (doc_id, ix_id, direction, strength, reviewer, modality, observed_at,
             json.dumps(body.get("conditions"), ensure_ascii=False)
             if body.get("conditions") is not None else None,
             json.dumps(cal, ensure_ascii=False) if cal is not None else None,
             now_iso()))
        store.commit()
        return cur.lastrowid, external


def require_nonblank_rationale(body):
    """采纳 / 排除 / 裁决都必须给出非空白理由；拒绝时不得写入任何数据。"""
    rationale = require(body, "rationale", str)
    if not rationale.strip():
        raise HttpError(422, "blank_rationale",
                        "rationale must be non-blank after trimming whitespace")
    return rationale


def add_review(store, obs_id, body):
    reviewer = require(body, "reviewer", str)
    decision = require(body, "decision", str)
    rationale = require_nonblank_rationale(body)
    if decision not in VALID_REVIEW_DECISIONS:
        raise HttpError(422, "bad_decision",
                        f"decision must be {VALID_REVIEW_DECISIONS}")
    obs = store.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs:
        raise HttpError(404, "not_found", f"observation o{obs_id} not found")
    doc = store.get_document(obs["document_id"])
    known = {r["id"] for r in _j(doc, "reviewers")}
    if reviewer not in known:
        raise HttpError(422, "unknown_reviewer",
                        "reviewer must be one of the two designated reviewers")
    with store.lock:
        exists = store.execute(
            "SELECT id FROM reviews WHERE observation_id=? AND reviewer=?",
            (obs_id, reviewer)).fetchone()
        if exists:
            raise HttpError(409, "duplicate_review",
                            f"reviewer {reviewer} already reviewed o{obs_id}; "
                            "adjudication supersedes reviews")
        cur = store.execute(
            "INSERT INTO reviews(observation_id,reviewer,decision,rationale,created_at)"
            " VALUES(?,?,?,?,?)",
            (obs_id, reviewer, decision, rationale, now_iso()))
        seq = store.add_revision(
            obs["document_id"], "review", "observation", obs_id, reviewer, rationale,
            {"decision": decision, "review_id": cur.lastrowid})
        store.commit()
        return seq


def adjudicate(store, ix_id, body):
    arbiter = require(body, "arbiter", str)
    rationale = require_nonblank_rationale(body)
    accepted_refs = require(body, "accepted_observations", list)
    ix = store.execute("SELECT * FROM intersections WHERE id=?", (ix_id,)).fetchone()
    if not ix:
        raise HttpError(404, "not_found", f"intersection i{ix_id} not found")
    doc = store.get_document(ix["document_id"])
    known = {r["id"] for r in _j(doc, "reviewers")}
    if arbiter not in known:
        raise HttpError(422, "unknown_arbiter",
                        "arbiter must be one of the two designated reviewers")
    accepted_ids = [parse_ref(x, "o") for x in accepted_refs]
    rows = store.execute(
        "SELECT * FROM observations WHERE intersection_id=? ORDER BY id",
        (ix_id,)).fetchall()
    row_by_id = {r["id"]: r for r in rows}
    for oid in accepted_ids:
        if oid not in row_by_id:
            raise HttpError(422, "bad_observation",
                            f"o{oid} is not at intersection i{ix_id}")
    directions = {row_by_id[oid]["direction"] for oid in accepted_ids}
    if len(directions) > 1:
        raise HttpError(422, "inconsistent_award",
                        "all accepted observations must share one direction")
    excluded_ids = [oid for oid in row_by_id if oid not in accepted_ids]
    with store.lock:
        payload = {
            "intersection": ref("i", ix_id),
            "winning_direction": next(iter(directions), None),
            "accepted_observations": accepted_ids,
            "excluded_observations": excluded_ids,
        }
        seq = store.add_revision(
            ix["document_id"], "adjudicate", "intersection", ix_id,
            arbiter, rationale, payload)
        store.commit()
        return seq, accepted_ids, excluded_ids


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

INDEX_HTML = """<!doctype html><meta charset=utf-8>
<title>InkSeal 朱墨时序鉴识</title>
<body style="font-family:sans-serif;margin:2rem">
<h1>InkSeal</h1><p>朱墨时序（书写 / 盖章先后）鉴识服务 · 仅 Python 标准库</p>
<h2>端点</h2>
<pre>
POST /api/documents                       建文档 {summary, reviewers:[2], canvas?}
GET  /api/documents/{d}                   文档状态 + 当前复算
POST /api/documents/{d}/layers            加图层 {name, kind:ink|seal|other}
POST /api/documents/{d}/intersections     加交叉点 {layer_ids, coordinate:{x,y}}
POST /api/documents/{d}/observations      加观察 {intersection, direction,
                                              strength 1-5, reviewer, modality,
                                              observed_at, calibration{...}}
POST /api/observations/{o}/reviews        审查 {reviewer, decision, rationale}
POST /api/intersections/{i}/adjudicate    裁决 {arbiter, accepted_observations, rationale}
GET  /api/documents/{d}/analysis          确定性复算 JSON
GET  /api/documents/{d}/svg               当前顺序图 SVG
POST /api/documents/{d}/signoff           签结 {note?}
GET  /api/documents/{d}/versions          版本列表
GET  /api/versions/{v}                    签结快照
GET  /api/versions/{v}/recompute          快照中的复算 JSON
GET  /api/versions/{v}/svg                快照顺序图 SVG
GET  /api/versions/{v}/diff?against={v2}  版本差异
GET  /api/health
</pre></body>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "InkSeal/1.0"

    def log_message(self, fmt, *args):  # 安静日志
        return

    @property
    def store(self):
        return self.server.store

    def _send(self, status, payload, content_type="application/json"):
        if content_type == "application/json":
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise HttpError(400, "bad_json", f"invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise HttpError(400, "bad_payload", "JSON body must be an object")
        return data

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/" and method == "GET":
                return self._send(200, INDEX_HTML, "text/html")
            if path == "/api/health":
                return self._send(200, {"status": "ok", "api": API_VERSION,
                                       "rules_digest": rules_digest()})
            if method == "POST" and path == "/api/documents":
                doc_id = create_document(self.store, self._body())
                return self._send(201, {"document": ref("d", doc_id)})

            parts = [p for p in path.split("/") if p]
            # 归一：/api/documents/d{id}/... 与 /api/d{id}/... 等价
            if len(parts) >= 3 and parts[:2] == ["api", "documents"]:
                parts = ["api"] + parts[2:]

            # ---- 文档域 ------------------------------------------------ #
            if len(parts) >= 2 and parts[0] == "api" and parts[1].startswith("d"):
                doc_id = parse_ref(parts[1], "d")
                if method == "GET" and len(parts) == 2:
                    return self._document_get(doc_id)
                if len(parts) == 3:
                    resource = parts[2]
                    if method == "POST" and resource == "layers":
                        lid = add_layer(self.store, doc_id, self._body())
                        return self._send(201, {"layer": ref("l", lid)})
                    if method == "POST" and resource == "intersections":
                        iid, unknown = add_intersection(
                            self.store, doc_id, self._body())
                        return self._send(201, {
                            "intersection": ref("i", iid),
                            "unknown_layers": [ref("l", u) for u in unknown]})
                    if method == "POST" and resource == "observations":
                        oid, external = add_observation(
                            self.store, doc_id, self._body())
                        return self._send(201, {
                            "observation": ref("o", oid),
                            "external_reviewer": external,
                            "state": "pending" if external else "pending_review"})
                    if method == "POST" and resource == "signoff":
                        body = self._body()
                        vid, snap = signoff(self.store, doc_id, body.get("note"))
                        return self._send(201, {
                            "version": ref("v", vid),
                            "definitive": snap["analysis"]["conclusion"]
                            ["definitive"]})
                    if method == "GET" and resource == "analysis":
                        return self._send(200, analyze(self.store, doc_id))
                    if method == "GET" and resource == "svg":
                        analysis = analyze(self.store, doc_id)
                        return self._send(200, render_svg(analysis),
                                          "image/svg+xml")
                    if method == "GET" and resource == "versions":
                        rows = self.store.execute(
                            "SELECT id,note,definitive,material_digest,rules_digest,"
                            "decisions_digest,created_at FROM versions "
                            "WHERE document_id=? ORDER BY id", (doc_id,)).fetchall()
                        return self._send(200, {"versions": [
                            {"version": ref("v", r["id"]), "note": r["note"],
                             "definitive": bool(r["definitive"]),
                             "material_digest": r["material_digest"],
                             "rules_digest": r["rules_digest"],
                             "decisions_digest": r["decisions_digest"],
                             "signed_at": r["created_at"]} for r in rows]})

            # ---- 审查 -------------------------------------------------- #
            if (method == "POST" and len(parts) == 4
                    and parts[:2] == ["api", "observations"]
                    and parts[3] == "reviews"):
                oid = parse_ref(parts[2], "o")
                seq = add_review(self.store, oid, self._body())
                return self._send(201, {"revision_seq": seq})

            # ---- 裁决 -------------------------------------------------- #
            if (method == "POST" and len(parts) == 4
                    and parts[:2] == ["api", "intersections"]
                    and parts[3] == "adjudicate"):
                iid = parse_ref(parts[2], "i")
                seq, accepted, excluded = adjudicate(
                    self.store, iid, self._body())
                return self._send(201, {
                    "revision_seq": seq,
                    "accepted_observations": [ref("o", x) for x in accepted],
                    "excluded_observations": [ref("o", x) for x in excluded]})

            # ---- 版本 -------------------------------------------------- #
            if (method == "GET" and len(parts) == 3
                    and parts[:2] == ["api", "versions"]):
                vid = parse_ref(parts[2], "v")
                row = self.store.get_version(vid)
                snap = json.loads(row["snapshot"])
                return self._send(200, {
                    "version": ref("v", vid),
                    "signed_at": row["created_at"],
                    "note": row["note"],
                    "definitive": bool(row["definitive"]),
                    "material_digest": row["material_digest"],
                    "rules_digest": row["rules_digest"],
                    "decisions_digest": row["decisions_digest"],
                    "snapshot": snap})
            if (method == "GET" and len(parts) == 4
                    and parts[:2] == ["api", "versions"]):
                vid = parse_ref(parts[2], "v")
                row = self.store.get_version(vid)
                snap = json.loads(row["snapshot"])
                resource = parts[3]
                if resource == "recompute":
                    return self._send(200, snap["analysis"])
                if resource == "svg":
                    return self._send(200, render_svg(snap["analysis"]),
                                      "image/svg+xml")
                if resource == "diff":
                    against = query.get("against", [None])[0]
                    if not against:
                        raise HttpError(400, "missing_query",
                                        "?against=v<n> is required")
                    other_id = parse_ref(against, "v")
                    other = json.loads(
                        self.store.get_version(other_id)["snapshot"])
                    return self._send(200, version_diff(other, snap))

            raise HttpError(404, "no_route", f"no route for {method} {path}")
        except HttpError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message,
                                    "details": exc.details})

    def _document_get(self, doc_id):
        doc = self.store.get_document(doc_id)
        analysis = analyze(self.store, doc_id)
        return self._send(200, {
            "document": ref("d", doc_id),
            "summary": doc["summary"],
            "reviewers": _j(doc, "reviewers"),
            "examiner": doc["examiner"],
            "canvas": _j(doc, "canvas"),
            "created_at": doc["created_at"],
            "analysis": analysis,
        })


def serve(db_path, host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = Store(db_path)
    print(f"InkSeal listening on http://{host}:{port} (db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="InkSeal 朱墨时序鉴识服务")
    parser.add_argument("--db", default="inkseal.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    serve(args.db, args.host, args.port)


if __name__ == "__main__":
    main()
