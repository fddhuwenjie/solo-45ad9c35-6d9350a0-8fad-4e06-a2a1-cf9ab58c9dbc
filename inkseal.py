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
                    给出方向（ink_first / seal_first）与 1-5 的证据强度；
                    新观察必须同时指向采集素材与取证区域
采集批次 batch    : 一次送检 / 采集会话登记的一组素材
素材 material     : 原始照片 / 扫描件（original）或由其派生的处理件
                    （derived：锐化、伪彩、缩放、裁剪……），记录文件
                    SHA-256、仪器、采集时刻、波段、父素材、处理参数与裁剪范围
独立来源 source   : 沿父子链与相同 SHA-256 归并后的同源分量；
                    未绑定素材的历史观察各自构成一个 legacy 来源（冻结旧规则）

推断模型
--------
每条「采纳」的观察变成一条带证据强度的有向边 pred -> succ
（ink_first：ink -> seal；seal_first：seal -> ink）。
同一对图层身份的边跨交叉点合并取最强证据，强度 >= edge_min_strength 才成立；
边的 corroborated_by 按见证观察背后的独立来源（原件）计数，
同一张照片的裁剪 / 锐化 / 伪彩 / 缩放派生物只算一个来源。
在有向图上求可达偏序：可比对、不可比对、节点数最少的矛盾环。

下列任一情况存在，结果 definitive=false，并把缺陷定位回原始观察与素材路径：
坐标越界 / 图层错绑 / 校准失效 / 同点观察冲突 / 证据链断裂 /
审查未完成或有异议 / 推断成环 / 摘要重复 / 循环派生 / 裁剪重叠 /
时间倒置 / 校准错配 / 独立来源不足 / 跨模态要求未满足。
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
VALID_MATERIAL_KINDS = ("original", "derived")

# 文档级谱系要求的缺省值：单来源即可成立、不强制跨模态（与旧行为一致）。
DEFAULT_REQUIREMENTS = {"min_independent_sources": 1, "require_cross_modal": False}

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
    "lineage": {
        "source": "independent source = connected component of materials over "
                  "parent links and identical sha256, anchored at root originals",
        "unbound_observation": "an observation without material binding is its own "
                               "independent source (frozen legacy rule)",
        "corroborated_by": "count of distinct independent sources behind an "
                           "edge's witnesses",
        "duplicate_digest": "one sha256 shared by materials that are not in a "
                            "single derivation chain",
        "crop_overlap": "derived siblings of one parent with intersecting crop "
                        "rectangles, both used by effective observations",
        "time_inversion": "derived material acquired before its parent, or an "
                          "observation earlier than its material's acquisition",
        "calibration_mismatch": "observation calibration.instrument or "
                                "conditions.band disagrees with the bound "
                                "material's instrument/band",
    },
    "document_requirements": {
        "min_independent_sources": "each established edge needs at least N "
                                   "independent sources (default 1)",
        "require_cross_modal": "each established edge's witnesses must span at "
                               "least 2 modalities (default false)",
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
        "duplicate_digest",
        "derivation_cycle",
        "crop_overlap",
        "time_inversion",
        "calibration_mismatch",
        "insufficient_sources",
        "cross_modal_unmet",
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
    requirements   TEXT,            -- JSON: {min_independent_sources, require_cross_modal}
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
    material_id    INTEGER,         -- 绑定素材（可空：历史观察走冻结旧规则）
    region         TEXT,            -- JSON: 素材上的取证区域 {x,y,w,h}
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    label          TEXT NOT NULL,
    operator       TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    batch_id       INTEGER REFERENCES batches(id),
    kind           TEXT NOT NULL,   -- original | derived
    sha256         TEXT NOT NULL,   -- 文件摘要（64 位小写十六进制）
    instrument     TEXT,
    acquired_at    TEXT NOT NULL,   -- 采集时刻 ISO-8601 UTC
    band           TEXT,            -- 波段（如 visible / 660nm / UV365）
    parent_id      INTEGER,         -- 父素材；故意不加外键：悬空引用允许录入，
                                    -- 由分析阶段暴露 broken_chain
    processing     TEXT,            -- JSON: 处理参数（锐化 / 伪彩 / 缩放……）
    crop           TEXT,            -- JSON: 父素材坐标系中的裁剪范围 {x,y,w,h}
    note           TEXT,
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
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """既有库增补谱系列（新库由 SCHEMA 建全，此处为空操作）。"""
        obs_cols = {r["name"] for r in
                    self.execute("PRAGMA table_info(observations)")}
        if "material_id" not in obs_cols:
            self.execute("ALTER TABLE observations ADD COLUMN material_id INTEGER")
        if "region" not in obs_cols:
            self.execute("ALTER TABLE observations ADD COLUMN region TEXT")
        doc_cols = {r["name"] for r in
                    self.execute("PRAGMA table_info(documents)")}
        if "requirements" not in doc_cols:
            self.execute("ALTER TABLE documents ADD COLUMN requirements TEXT")

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

    def batches(self, doc_id):
        return self.execute(
            "SELECT * FROM batches WHERE document_id=? ORDER BY id", (doc_id,)
        ).fetchall()

    def materials(self, doc_id):
        return self.execute(
            "SELECT * FROM materials WHERE document_id=? ORDER BY id", (doc_id,)
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


def check_sha256(value):
    """文件摘要必须是 64 位十六进制（录入时归一为小写）。"""
    if not isinstance(value, str):
        return "sha256 must be a hex string"
    v = value.strip().lower()
    if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        return "sha256 must be 64 hex characters"
    return None


def check_region(region, what="region"):
    """取证区域 / 裁剪范围必须是非零面积的 {x,y,w,h}。"""
    if not isinstance(region, dict) or not all(k in region for k in ("x", "y", "w", "h")):
        return f"{what} must be {{x,y,w,h}}"
    x, y, w, h = (region[k] for k in ("x", "y", "w", "h"))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in (x, y, w, h)):
        return f"{what} x/y/w/h must be numbers"
    if x < 0 or y < 0:
        return f"{what} x/y must be >= 0"
    if w <= 0 or h <= 0:
        return f"{what} w/h must be > 0"
    return None


def rects_overlap(a, b):
    """两个 {x,y,w,h} 矩形是否存在正面积交集（仅贴边不算重叠）。"""
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


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


def document_requirements(doc):
    """文档锁定的谱系要求（缺省回退 DEFAULT_REQUIREMENTS）。"""
    req = dict(DEFAULT_REQUIREMENTS)
    stored = _j(doc, "requirements")
    if isinstance(stored, dict):
        for k in req:
            if k in stored:
                req[k] = stored[k]
    return req


def _is_ancestor(anc, node, parent_of):
    """anc 是否为 node 沿父链的祖先。"""
    seen = set()
    cur = parent_of.get(node)
    while cur is not None and cur not in seen:
        if cur == anc:
            return True
        seen.add(cur)
        cur = parent_of.get(cur)
    return False


def resolve_lineage(materials, observations):
    """沿父子链与相同摘要归并同源素材，划分独立来源。

    独立来源 = 父引用与相同 SHA-256 构成的连通分量，由根原件锚定；
    未绑定素材的观察各自构成一个 legacy 伪来源（冻结旧规则：按观察计数）。
    同时识别摘要重复、循环派生、悬空父引用（链路断裂）与素材级时间倒置。
    返回来源列表、映射与材料级缺陷；全部按确定顺序输出。
    """
    mat_by_id = {m["id"]: m for m in materials}
    parent_of = {}   # 仅存在的父引用；悬空引用单独登记
    dangling = {}
    for m in materials:
        pid = m["parent_id"]
        if pid is None:
            continue
        if pid in mat_by_id:
            parent_of[m["id"]] = pid
        else:
            dangling[m["id"]] = pid

    # 素材路径：从自身沿父链到根（或断裂 / 成环处为止）
    paths = {}
    for m in materials:
        chain, node, seen = [], m["id"], set()
        while node in mat_by_id and node not in seen:
            seen.add(node)
            chain.append(ref("m", node))
            node = parent_of.get(node)
        paths[m["id"]] = chain

    # 循环派生：沿父链回访到本路径上的节点即成环
    cycle_sets = set()
    for m in materials:
        seen, path, node = {}, [], m["id"]
        while node in parent_of and node not in seen:
            seen[node] = len(path)
            path.append(node)
            node = parent_of[node]
        if node in seen:
            cycle_sets.add(frozenset(path[seen[node]:]))
    cyclic = set().union(*cycle_sets) if cycle_sets else set()

    # 连通分量（父链 ∪ 同摘要）；并查集以小 id 为根保证确定性
    dsu = {m["id"]: m["id"] for m in materials}

    def find(x):
        while dsu[x] != x:
            dsu[x] = dsu[dsu[x]]
            x = dsu[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            dsu[max(ra, rb)] = min(ra, rb)

    for child, parent in parent_of.items():
        union(child, parent)
    by_digest = defaultdict(list)
    for m in materials:
        by_digest[m["sha256"]].append(m["id"])
    for ids in by_digest.values():
        for other in ids[1:]:
            union(ids[0], other)

    comps = defaultdict(list)
    for m in materials:
        comps[find(m["id"])].append(m["id"])
    ordered = sorted(comps.values(), key=lambda ids: min(ids))

    mat_to_source = {}
    sources = []
    broken_mats = set()
    for num, ids in enumerate(ordered, 1):
        members = sorted(ids)
        originals = [i for i in members if mat_by_id[i]["kind"] == "original"]
        broken = (any(i in dangling for i in members)
                  or any(i in cyclic for i in members)
                  or not originals)
        if broken:
            broken_mats.update(members)
        for i in members:
            mat_to_source[i] = num
        sources.append({
            "id": ref("s", num),
            "materials": [ref("m", i) for i in members],
            "originals": [ref("m", i) for i in originals],
            "sha256": sorted({mat_by_id[i]["sha256"] for i in members}),
            "observations": [],
            "cyclic": any(i in cyclic for i in members),
            "broken": broken,
            "legacy": False,
        })

    obs_to_source = {}
    for o in sorted(observations, key=lambda x: x["id"]):
        mid = o["material_id"]
        if mid is not None and mid in mat_to_source:
            num = mat_to_source[mid]
            obs_to_source[o["id"]] = num
            sources[num - 1]["observations"].append(ref("o", o["id"]))
        else:
            num = len(sources) + 1
            obs_to_source[o["id"]] = num
            sources.append({
                "id": ref("s", num), "materials": [], "originals": [],
                "sha256": [], "observations": [ref("o", o["id"])],
                "cyclic": False, "broken": False, "legacy": True,
            })

    defects = []
    # 摘要重复：同一 SHA-256 落在非单一派生链的素材之间（重复送审）
    for sha, ids in sorted(by_digest.items()):
        if len(ids) < 2:
            continue
        ids = sorted(ids)
        one_chain = all(
            _is_ancestor(a, b, parent_of) or _is_ancestor(b, a, parent_of)
            for x, a in enumerate(ids) for b in ids[x + 1:])
        if not one_chain:
            defects.append({
                "kind": "duplicate_digest",
                "message": (f"sha256 {sha[:16]}... shared by "
                            f"{', '.join(ref('m', i) for i in ids)} outside a "
                            "single derivation chain: duplicate submission, "
                            "counts as one source"),
                "location": {"materials": [ref("m", i) for i in ids],
                             "material_paths": [paths[i] for i in ids]}})
    # 循环派生：无法锚定根原件
    for cyc in sorted(cycle_sets, key=lambda c: min(c)):
        members = sorted(cyc)
        defects.append({
            "kind": "derivation_cycle",
            "message": (f"circular derivation among "
                        f"{', '.join(ref('m', i) for i in members)}: "
                        "no root original can be established"),
            "location": {"materials": [ref("m", i) for i in members],
                         "material_paths": [paths[i] for i in members]}})
    # 链路断裂：父引用悬空
    for mid, pid in sorted(dangling.items()):
        defects.append({
            "kind": "broken_chain",
            "message": (f"material m{mid} declares missing parent m{pid}: "
                        "lineage chain broken"),
            "location": {"materials": [ref("m", mid)],
                         "material_path": paths[mid]}})
    # 素材级时间倒置：派生物早于其父素材采集
    for child, parent in sorted(parent_of.items()):
        ca = mat_by_id[child]["acquired_at"]
        pa = mat_by_id[parent]["acquired_at"]
        if parse_iso(ca) < parse_iso(pa):
            defects.append({
                "kind": "time_inversion",
                "message": (f"derived material m{child} acquired at {ca} "
                            f"before its parent m{parent} acquired at {pa}"),
                "location": {"materials": [ref("m", child), ref("m", parent)],
                             "material_path": paths[child]}})

    return {
        "sources": sources,
        "mat_to_source": mat_to_source,
        "obs_to_source": obs_to_source,
        "broken_mats": broken_mats,
        "paths": paths,
        "defects": defects,
    }


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


def build_material(store, doc, layers, intersections, observations,
                   batches, materials):
    """被 material_digest 锁定的事实素材（不含审查决定）。"""
    return {
        "api": API_VERSION,
        "summary": doc["summary"],
        "examiner": doc["examiner"],
        "canvas": _j(doc, "canvas"),
        "requirements": _j(doc, "requirements"),
        "layers": sorted(
            ([l["id"], l["name"], l["kind"], l["description"]] for l in layers),
            key=lambda x: x[0],
        ),
        "intersections": sorted(
            ([i["id"], sorted(_j(i, "layer_ids")), _j(i, "coordinate"),
              _j(i, "canvas"), i["note"]] for i in intersections),
            key=lambda x: x[0],
        ),
        "batches": sorted(
            ([b["id"], b["label"], b["operator"], b["note"]] for b in batches),
            key=lambda x: x[0],
        ),
        "materials": sorted(
            ([m["id"], m["batch_id"], m["kind"], m["sha256"], m["instrument"],
              m["acquired_at"], m["band"], m["parent_id"], _j(m, "processing"),
              _j(m, "crop"), m["note"]] for m in materials),
            key=lambda x: x[0],
        ),
        "observations": sorted(
            ([o["id"], o["intersection_id"], o["direction"], o["strength"],
              o["reviewer"], o["modality"], o["observed_at"],
              _j(o, "conditions"), _j(o, "calibration"),
              o["material_id"], _j(o, "region")] for o in observations),
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
    batches = store.batches(doc_id)
    materials = store.materials(doc_id)
    requirements = document_requirements(doc)
    reviews = [r for rows in store.reviews_for([o["id"] for o in observations]).values()
               for r in rows]
    reviewer_ids = [r["id"] for r in _j(doc, "reviewers")]
    doc_canvas = _j(doc, "canvas")

    layer_by_id = {l["id"]: l for l in layers}
    ix_by_id = {i["id"]: i for i in intersections}
    mat_by_id = {m["id"]: m for m in materials}

    # -- 谱系归并：同源分量 = 独立来源；材料级缺陷先行登记 ------------------ #
    lineage = resolve_lineage(materials, observations)

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

    defects = list(lineage["defects"])  # 谱系材料级缺陷（已带素材路径定位）

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
            "material": ref("m", o["material_id"])
                        if o["material_id"] is not None else None,
            "region": _j(o, "region"),
            "source": ref("s", lineage["obs_to_source"][o["id"]]),
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
        # -- 谱系事实缺陷（仅绑定素材的观察；未绑定者走冻结旧规则） ---------- #
        mid = o["material_id"]
        if mid is not None and mid in mat_by_id:
            m = mat_by_id[mid]
            m_loc = {"observation": ref("o", o["id"]),
                     "intersection": ref("i", o["intersection_id"]),
                     "material": ref("m", mid),
                     "material_path": lineage["paths"][mid]}
            if mid in lineage["broken_mats"]:
                view["factual_defects"].append(
                    "broken_chain:material lineage broken")
            if parse_iso(o["observed_at"]) < parse_iso(m["acquired_at"]):
                view["factual_defects"].append("time_inversion")
                defects.append({
                    "kind": "time_inversion",
                    "message": (f"o{o['id']} observed at {o['observed_at']} "
                                f"before material m{mid} was acquired at "
                                f"{m['acquired_at']}"),
                    "location": dict(m_loc)})
            cal = _j(o, "calibration") or {}
            mismatches = []
            inst = cal.get("instrument")
            if inst and m["instrument"] and inst != m["instrument"]:
                mismatches.append(f"calibration instrument '{inst}' != material "
                                  f"instrument '{m['instrument']}'")
            band = (_j(o, "conditions") or {}).get("band")
            if band is not None and m["band"] and str(band) != str(m["band"]):
                mismatches.append(f"observation band '{band}' != material "
                                  f"band '{m['band']}'")
            if mismatches:
                view["factual_defects"].append("calibration_mismatch")
                defects.append({
                    "kind": "calibration_mismatch",
                    "message": f"o{o['id']} vs m{mid}: " + "; ".join(mismatches),
                    "location": dict(m_loc)})
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

    # -- 裁剪重叠：同一父素材的派生裁剪区域相交，且都被有效观察使用 -------- #
    # 同一张照片裁出的两个「交叉区域」若共享像素，相同像素被当成两处独立取证。
    active_mats = {o["material_id"] for o in observations
                   if o["id"] in active_ids and o["material_id"] is not None}
    kids_by_parent = defaultdict(list)
    for m in materials:
        if m["parent_id"] in mat_by_id and _j(m, "crop"):
            kids_by_parent[m["parent_id"]].append(m)
    for pid, kids in sorted(kids_by_parent.items()):
        in_use = [k for k in kids if k["id"] in active_mats]
        for x in range(len(in_use)):
            for y in range(x + 1, len(in_use)):
                a, b = in_use[x], in_use[y]
                if not rects_overlap(_j(a, "crop"), _j(b, "crop")):
                    continue
                obs_ab = sorted(ref("o", o["id"]) for o in observations
                                if o["material_id"] in (a["id"], b["id"]))
                defects.append({
                    "kind": "crop_overlap",
                    "message": (f"m{a['id']} and m{b['id']} crop overlapping "
                                f"regions of parent m{pid}: the same pixels "
                                "are presented as distinct forensic regions"),
                    "location": {
                        "materials": [ref("m", a["id"]), ref("m", b["id"])],
                        "parent": ref("m", pid),
                        "material_paths": [lineage["paths"][a["id"]],
                                           lineage["paths"][b["id"]]],
                        "observations": obs_ab}})

    # -- 同交叉点合并 + 冲突检测（仅对参与推断的有效观察） ------------------ #
    groups = defaultdict(list)
    for o in observations:
        if o["id"] in active_ids:
            groups[o["intersection_id"]].append(o)

    conflicted_obs = set()
    merged_edges = defaultdict(lambda: {"witnesses": [], "intersections": set(),
                                        "sources": set()})

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
            src = lineage["obs_to_source"].get(o["id"])
            slot["witnesses"].append({
                "observation": ref("o", o["id"]),
                "intersection": ref("i", ix_id),
                "reviewer": o["reviewer"],
                "strength": o["strength"],
                "modality": o["modality"],
                "material": ref("m", o["material_id"])
                            if o["material_id"] is not None else None,
                "source": ref("s", src) if src is not None else None,
            })
            slot["intersections"].add(ix_id)
            if src is not None:
                slot["sources"].add(src)

    # 强度 >= 阈值才成立为图边；否则构成“证据链断裂”：
    # 已采纳的观察不足以单独建立先后关系，且没有其他交叉点补强。
    edges = []
    rejected_edges = []
    for (pred, succ), slot in sorted(merged_edges.items(), key=lambda kv: kv[0]):
        strength = max(w["strength"] for w in slot["witnesses"])
        srcs = sorted(slot["sources"])
        edge = {
            "from": ref("l", pred),
            "to": ref("l", succ),
            "strength": strength,
            "corroborated_by": len(srcs),
            "sources": [ref("s", s) for s in srcs],
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

    # -- 文档级谱系要求：最少独立来源数 / 跨模态（仅约束已成立的边） -------- #
    for edge in edges:
        obs_refs = [w["observation"] for w in edge["witnesses"]]
        loc = {"intersections": edge["intersections"],
               "observations": obs_refs,
               "sources": edge["sources"]}
        if len(edge["sources"]) < requirements["min_independent_sources"]:
            defects.append({
                "kind": "insufficient_sources",
                "message": (f"{edge['from']} before {edge['to']} rests on "
                            f"{len(edge['sources'])} independent source(s) "
                            f"({', '.join(edge['sources'])}), below document "
                            f"min_independent_sources="
                            f"{requirements['min_independent_sources']}"),
                "location": dict(loc)})
        if requirements["require_cross_modal"]:
            mods = sorted({w["modality"] for w in edge["witnesses"]})
            if len(mods) < 2:
                defects.append({
                    "kind": "cross_modal_unmet",
                    "message": (f"{edge['from']} before {edge['to']} is "
                                f"supported only by modality {mods}; document "
                                "requires cross-modal evidence"),
                    "location": dict(loc)})

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

    material = build_material(store, doc, layers, intersections, observations,
                              batches, materials)
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
        "lineage": {
            "requirements": requirements,
            "batches": [{"id": ref("b", b["id"]), "label": b["label"],
                         "operator": b["operator"], "note": b["note"],
                         "created_at": b["created_at"]} for b in batches],
            "materials": [{
                "id": ref("m", m["id"]),
                "batch": ref("b", m["batch_id"]) if m["batch_id"] else None,
                "kind": m["kind"],
                "sha256": m["sha256"],
                "instrument": m["instrument"],
                "acquired_at": m["acquired_at"],
                "band": m["band"],
                "parent": ref("m", m["parent_id"]) if m["parent_id"] else None,
                "processing": _j(m, "processing"),
                "crop": _j(m, "crop"),
                "source": ref("s", lineage["mat_to_source"][m["id"]]),
                "path": lineage["paths"][m["id"]],
                "note": m["note"],
            } for m in materials],
            "sources": lineage["sources"],
        },
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
            "lineage": {
                "sources": len(analysis["lineage"]["sources"]),
                "materials": len(analysis["lineage"]["materials"]),
                "batches": len(analysis["lineage"]["batches"]),
                "requirements": analysis["lineage"]["requirements"],
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

    # 谱系：旧快照可能没有 lineage 段（冻结旧规则），按空谱系处理
    la = an.get("lineage") or {"materials": [], "sources": []}
    lb = bn.get("lineage") or {"materials": [], "sources": []}
    mats_a = {m["id"] for m in la["materials"]}
    mats_b = {m["id"] for m in lb["materials"]}

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
        "materials_added": sorted(mats_b - mats_a),
        "materials_removed": sorted(mats_a - mats_b),
        "sources_from": len(la["sources"]),
        "sources_to": len(lb["sources"]),
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
    lineage = analysis.get("lineage")
    if lineage:
        parts.append(
            f'<text x="12" y="{height - 30}" font-size="11" fill="#666">'
            f'independent sources: {len(lineage["sources"])} · '
            f'materials: {len(lineage["materials"])} · '
            f'batches: {len(lineage["batches"])} · '
            f'rules {analysis["recompute"]["rules_digest"][:12]}</text>')
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
    requirements = body.get("requirements")
    if requirements is not None:
        if not isinstance(requirements, dict):
            raise HttpError(400, "bad_requirements",
                            "requirements must be an object")
        mis = requirements.get("min_independent_sources", 1)
        rcm = requirements.get("require_cross_modal", False)
        if not isinstance(mis, int) or isinstance(mis, bool) or mis < 1:
            raise HttpError(422, "bad_requirements",
                            "min_independent_sources must be an integer >= 1")
        if not isinstance(rcm, bool):
            raise HttpError(422, "bad_requirements",
                            "require_cross_modal must be a boolean")
        requirements = {"min_independent_sources": mis,
                        "require_cross_modal": rcm}
    with store.lock:
        cur = store.execute(
            "INSERT INTO documents(summary,reviewers,examiner,canvas,requirements,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (summary, json.dumps(normalized, ensure_ascii=False),
             body.get("examiner"),
             json.dumps(canvas) if canvas else None,
             json.dumps(requirements, ensure_ascii=False)
             if requirements is not None else None, now_iso()))
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


def add_batch(store, doc_id, body):
    store.get_document(doc_id)
    label = require(body, "label", str)
    if not label.strip():
        raise HttpError(422, "bad_label", "batch label must be non-blank")
    with store.lock:
        cur = store.execute(
            "INSERT INTO batches(document_id,label,operator,note,created_at) "
            "VALUES(?,?,?,?,?)",
            (doc_id, label.strip(), body.get("operator"), body.get("note"),
             now_iso()))
        store.commit()
        return cur.lastrowid


def add_material(store, doc_id, body):
    """登记原始 / 派生素材。悬空父引用允许录入，由分析阶段暴露 broken_chain。"""
    store.get_document(doc_id)
    kind = require(body, "kind", str)
    if kind not in VALID_MATERIAL_KINDS:
        raise HttpError(422, "bad_kind",
                        f"kind must be one of {VALID_MATERIAL_KINDS}")
    sha = require(body, "sha256")
    msg = check_sha256(sha)
    if msg:
        raise HttpError(422, "bad_sha256", msg)
    sha_norm = sha.strip().lower()
    acquired_at = require(body, "acquired_at", str)
    try:
        parse_iso(acquired_at)
    except ValueError as exc:
        raise HttpError(422, "bad_timestamp", str(exc))
    batch_id = None
    if body.get("batch") is not None:
        batch_id = parse_ref(body["batch"], "b")
        row = store.execute(
            "SELECT id FROM batches WHERE id=? AND document_id=?",
            (batch_id, doc_id)).fetchone()
        if not row:
            raise HttpError(404, "not_found",
                            f"batch b{batch_id} not found in document d{doc_id}")
    parent_id = None
    dangling = None
    if kind == "derived":
        parent_ref = require(body, "parent", str)
        parent_id = parse_ref(parent_ref, "m")
        row = store.execute(
            "SELECT id FROM materials WHERE id=? AND document_id=?",
            (parent_id, doc_id)).fetchone()
        if not row:
            dangling = parent_id
    elif body.get("parent") is not None:
        raise HttpError(422, "bad_parent",
                        "original material must not declare a parent")
    processing = body.get("processing")
    if processing is not None and not isinstance(processing, dict):
        raise HttpError(422, "bad_processing", "processing must be an object")
    crop = body.get("crop")
    if crop is not None:
        msg = check_region(crop, "crop")
        if msg:
            raise HttpError(422, "bad_crop", msg)
    with store.lock:
        cur = store.execute(
            "INSERT INTO materials(document_id,batch_id,kind,sha256,instrument,"
            "acquired_at,band,parent_id,processing,crop,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, batch_id, kind, sha_norm, body.get("instrument"),
             acquired_at, body.get("band"), parent_id,
             json.dumps(processing, ensure_ascii=False)
             if processing is not None else None,
             json.dumps(crop, ensure_ascii=False) if crop is not None else None,
             body.get("note"), now_iso()))
        store.commit()
        return cur.lastrowid, dangling


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
    # 新观察必须同时指向素材与取证区域，任一缺失即拒绝；
    # 未绑定素材的观察只存在于既有数据库（谱系强制前的历史记录），
    # 由分析按冻结旧规则还原，公开接口不再接受。
    material_ref = require(body, "material", str)
    material_id = parse_ref(material_ref, "m")
    mrow = store.execute(
        "SELECT id FROM materials WHERE id=? AND document_id=?",
        (material_id, doc_id)).fetchone()
    if not mrow:
        raise HttpError(404, "not_found",
                        f"material m{material_id} not found in document "
                        f"d{doc_id}")
    region = require(body, "region", dict)
    msg = check_region(region)
    if msg:
        raise HttpError(422, "bad_region", msg)
    known_reviewers = {r["id"] for r in _j(doc, "reviewers")}
    external = reviewer not in known_reviewers
    with store.lock:
        cur = store.execute(
            "INSERT INTO observations(document_id,intersection_id,direction,strength,"
            "reviewer,modality,observed_at,conditions,calibration,material_id,"
            "region,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, ix_id, direction, strength, reviewer, modality, observed_at,
             json.dumps(body.get("conditions"), ensure_ascii=False)
             if body.get("conditions") is not None else None,
             json.dumps(cal, ensure_ascii=False) if cal is not None else None,
             material_id,
             json.dumps(region, ensure_ascii=False) if region is not None else None,
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
        # 谱系上下文随审查冻结：当时观察绑定的素材与独立来源
        lin = resolve_lineage(store.materials(obs["document_id"]),
                              store.observations(obs["document_id"]))
        src = lin["obs_to_source"].get(obs_id)
        seq = store.add_revision(
            obs["document_id"], "review", "observation", obs_id, reviewer, rationale,
            {"decision": decision, "review_id": cur.lastrowid,
             "material": ref("m", obs["material_id"])
                         if obs["material_id"] is not None else None,
             "source": ref("s", src) if src is not None else None})
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
        # 谱系上下文随裁决冻结：交叉点各观察当时的独立来源归属
        lin = resolve_lineage(store.materials(ix["document_id"]),
                              store.observations(ix["document_id"]))
        payload = {
            "intersection": ref("i", ix_id),
            "winning_direction": next(iter(directions), None),
            "accepted_observations": accepted_ids,
            "excluded_observations": excluded_ids,
            "observation_sources": {
                ref("o", oid): ref("s", lin["obs_to_source"][oid])
                for oid in row_by_id},
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
POST /api/documents                       建文档 {summary, reviewers:[2], canvas?,
                                          requirements?{min_independent_sources,
                                          require_cross_modal}}
GET  /api/documents/{d}                   文档状态 + 当前复算
POST /api/documents/{d}/layers            加图层 {name, kind:ink|seal|other}
POST /api/documents/{d}/intersections     加交叉点 {layer_ids, coordinate:{x,y}}
POST /api/documents/{d}/batches           登记采集批次 {label, operator?, note?}
POST /api/documents/{d}/materials         登记素材 {kind:original|derived, sha256,
                                          acquired_at, batch?, instrument?, band?,
                                          parent?(derived 必填), processing?, crop?}
GET  /api/documents/{d}/materials         谱系一览（批次 / 素材 / 独立来源）
POST /api/documents/{d}/observations      加观察 {intersection, direction,
                                              strength 1-5, reviewer, modality,
                                              observed_at, calibration{...},
                                              material, region:{x,y,w,h}}
                                              （material 与 region 缺一不可）
POST /api/observations/{o}/reviews        审查 {reviewer, decision, rationale}
POST /api/intersections/{i}/adjudicate    裁决 {arbiter, accepted_observations, rationale}
GET  /api/documents/{d}/analysis          确定性复算 JSON（含谱系与去重结果）
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
                    if method == "POST" and resource == "batches":
                        bid = add_batch(self.store, doc_id, self._body())
                        return self._send(201, {"batch": ref("b", bid)})
                    if method == "POST" and resource == "materials":
                        mid, dangling = add_material(
                            self.store, doc_id, self._body())
                        return self._send(201, {
                            "material": ref("m", mid),
                            "dangling_parent": ref("m", dangling)
                                               if dangling else None})
                    if method == "GET" and resource == "materials":
                        return self._send(
                            200, analyze(self.store, doc_id)["lineage"])
                    if method == "POST" and resource == "observations":
                        body = self._body()
                        oid, external = add_observation(
                            self.store, doc_id, body)
                        return self._send(201, {
                            "observation": ref("o", oid),
                            "material": body.get("material"),
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
            "requirements": document_requirements(doc),
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
