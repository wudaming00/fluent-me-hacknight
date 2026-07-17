# 记忆机制 — 四层: L1 错题卡(FSRS-lite SRS) / L2 画像 / L3 情景记忆 / L4 身份 facts
#
#   L1 错题卡: 错误 → Claude 归一 pattern key → 同类合卡。FSRS-lite 调度:
#      每卡 S(稳定性,天) + D(难度1-10), 保持率 R(t)=1/(1+t/(9S)) → R(S)=0.90,
#      到期即 R 衰减到 90% 处 ("在你快忘时设伏")。
#      成功(对话中真实钓出): S 乘性增长, 越接近遗忘增益越大 (desirable difficulty);
#      失误: 软降级保留 30% 稳定性 (relearning savings), 10 分钟修复窗;
#      echo 跟读成功: 只完成学习步 S<1→1 (模仿≠检索, 晋级必须真实钓出)。
#      措辞注意: "FSRS-inspired defaults, not a fitted model" — 常数是手选的。
#
#   L2 画像: 四维技能 EMA + CEFR estimate + XP/Lv 双货币 + cx_ema(句子野心) + streak
#   L3 情景记忆 episodes: 每场蒸馏 摘要+facts; 开场 Kai 主动提起
#   L4 身份 facts: [{text, kind, use, ts, src}] — use=false 的 Kai 永不使用
#      (me 页开关直通这里; 也是 Sauna workspace-memory 的映射源)
#
#   sessions.json: 每场快照 — 图表/plateau 检测唯一数据源
import json
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(parents=True, exist_ok=True)

# ---- FSRS-lite 常数 (手选, 非拟合) ----
FACTOR = 9.0            # R(S)=0.90 的标定
GROW = 2.2              # 增益系数: 越接近遗忘 (R低) 增益越大
G_MIN, G_MAX = 1.3, 3.5
LAPSE_KEEP = 0.30       # 失误保留 30% 稳定性
S_MIN, S_NEW = 0.25, 0.25
S_GRAD, GRAD_STREAK = 21.0, 3
D_INIT = 5.0
ECHO_DELAY = 4 * 3600   # echo 后至少 4h 才能真实钓出拿学分
REPAIR_DELAY = 600      # 失误后 10 分钟修复窗
BRIEF_WEAVE = 3         # 每场对话织入的到期卡上限
DRILL_MIN_DUE, DRILL_MAX = 3, 4
SKILL_ALPHA = 0.25
CX_ALPHA = 0.3
XP_CARD_ADVANCE, XP_GRADUATE = 5, 20
CEFR_BANDS = [(0, "A1"), (30, "A2"), (45, "B1"), (60, "B2"), (75, "C1"), (88, "C2")]

DEFAULT_PROFILE = {
    "name": "", "native_lang": "", "goals": [], "facts": [],
    "skills": {"grammar": 50.0, "vocab": 50.0, "fluency": 50.0, "pron": 50.0},
    "xp": 0, "streak": 0, "last_day": "", "turns_total": 0,
    "cx_ema": 2.0, "gentle_mode": False, "fix_voice": "user",
    "wishlist": [],
}


def _load(p: Path, default):
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(json.dumps(default))


def retrievability(card: dict, now: float | None = None) -> float:
    """R(t) = 1/(1 + t/(9S)) — 保持率估计, 前端强度条同款公式实时重算。"""
    t = ((now or time.time()) - card.get("last_seen", card.get("created", 0))) / 86400
    return 1.0 / (1.0 + max(0.0, t) / (FACTOR * card.get("S", S_NEW)))


class LocalStore:
    def __init__(self):
        self.f_cards = DATA / "cards.json"
        self.f_profile = DATA / "profile.json"
        self.f_episodes = DATA / "episodes.json"
        self.f_sessions = DATA / "sessions.json"
        self.cards: dict = _load(self.f_cards, {})
        self.profile: dict = _load(self.f_profile, DEFAULT_PROFILE)
        for k, v in DEFAULT_PROFILE.items():        # 旧 profile 补新字段
            self.profile.setdefault(k, json.loads(json.dumps(v)))
        self.episodes: list = _load(self.f_episodes, [])
        self.sessions: list = _load(self.f_sessions, [])
        self._migrate_cards()

    def _migrate_cards(self):
        """老 Leitner 卡 → FSRS 字段 (幂等; 本机 data/ 为空时是 no-op)。"""
        steps = [0.25, 1.0, 3.0, 7.0, 21.0]
        for c in self.cards.values():
            if "S" not in c:
                c["S"] = steps[min(c.get("box", 0), 4)]
                c["D"] = min(10.0, 4.0 + 0.5 * c.get("hits", 1))
                c.setdefault("last_seen", c.get("created", int(time.time())))
            c.setdefault("pinned", False)
            c.setdefault("context", None)

    # ---------- 持久化 ----------
    def save(self):
        self.f_cards.write_text(json.dumps(self.cards, ensure_ascii=False, indent=1), encoding="utf-8")
        self.f_profile.write_text(json.dumps(self.profile, ensure_ascii=False, indent=1), encoding="utf-8")
        self.f_episodes.write_text(json.dumps(self.episodes[-50:], ensure_ascii=False, indent=1), encoding="utf-8")
        self.f_sessions.write_text(json.dumps(self.sessions[-200:], ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- L1 错题卡 ----------
    def record_error(self, err: dict, sentence: str, context: dict | None = None):
        """判出的一个错误 → 落卡。已有卡走软降级 (保 30% S), 新卡 10 分钟后到期。"""
        key = err.get("pattern") or f"other-{int(time.time())}"
        now = int(time.time())
        card = self.cards.get(key)
        if card is None:
            card = {"pattern": key, "label": err.get("pattern_label") or key,
                    "type": err.get("type", "other"), "explanation": err.get("explanation", ""),
                    "examples": [], "hits": 0, "clean_streak": 0,
                    "S": S_NEW, "D": D_INIT, "due_at": now + REPAIR_DELAY,
                    "last_seen": now, "status": "learning", "created": now,
                    "pinned": False, "context": context}
        else:
            card["S"] = max(S_MIN, LAPSE_KEEP * card["S"])       # 软降级
            card["D"] = min(10.0, card.get("D", D_INIT) + 1.0)
            card["due_at"] = now + REPAIR_DELAY
            card["last_seen"] = now
            card["status"] = "learning"
        card["hits"] += 1
        card["clean_streak"] = 0
        ex = {"wrong": err.get("span", sentence)[:120], "right": err.get("correction", "")[:120], "ts": now}
        card["examples"] = (card["examples"] + [ex])[-5:]
        self.cards[key] = card

    def record_elicited(self, pattern: str, success: bool) -> str | None:
        """对话中真实钓出且用对 → FSRS 晋级。返回 'advanced'|'graduated'|None (XP 加成用)。"""
        card = self.cards.get(pattern)
        if not card or not success or card["status"] != "learning":
            return None
        now = int(time.time())
        r = retrievability(card, now)
        if card["S"] < 1.0:
            card["S"] = 1.0                                       # 学习步毕业: 10min → 1d
        else:
            mult = (1 + GROW * (1.5 - r)) * (11 - card.get("D", D_INIT)) / 6
            card["S"] = card["S"] * min(G_MAX, max(G_MIN, mult))
        card["D"] = max(1.0, card.get("D", D_INIT) - 0.3)
        card["clean_streak"] += 1
        card["last_seen"] = now
        card["due_at"] = now + int(card["S"] * 86400)
        card["pinned"] = False
        if card["S"] >= S_GRAD and card["clean_streak"] >= GRAD_STREAK:
            card["status"] = "graduated"
            return "graduated"
        return "advanced"

    def echo_pass(self, pattern: str) -> bool:
        """echo 跟读成功: 只完成学习步 (⚡fast-track); S≥1 的卡零学分只顺延。模仿≠检索。"""
        card = self.cards.get(pattern)
        if not card or card["status"] != "learning":
            return False
        now = int(time.time())
        if card["S"] < 1.0:
            card["S"] = 1.0
            card["due_at"] = now + 86400
            card["last_seen"] = now
            return True
        card["due_at"] = max(card["due_at"], now + ECHO_DELAY)
        return False

    def due_cards(self, exclude: set | None = None, limit: int = BRIEF_WEAVE) -> list:
        """到期卡: pinned 优先, 再按 R 升序 (最接近遗忘的先救), 同 R 按 hits。"""
        now = int(time.time())
        ex = exclude or set()
        due = [c for c in self.cards.values()
               if c["status"] == "learning" and c["due_at"] <= now and c["pattern"] not in ex]
        due.sort(key=lambda c: (-int(c.get("pinned", False)), retrievability(c, now), -c["hits"]))
        return due[:limit]

    def card_action(self, pattern: str, action: str) -> dict | None:
        c = self.cards.get(pattern)
        if not c:
            return None
        now = int(time.time())
        if action == "archive":
            c["status"] = "archived"
        elif action == "unarchive":
            c["status"] = "learning"
        elif action == "reset":
            c.update({"S": S_NEW, "D": D_INIT, "clean_streak": 0,
                      "due_at": now + REPAIR_DELAY, "last_seen": now, "status": "learning"})
        elif action == "prioritize":
            c["pinned"] = True
            c["due_at"] = now
            c["status"] = "learning"
        self.save()
        return c

    # ---------- L2 画像 ----------
    def update_skills(self, dims: dict):
        for k, v in dims.items():
            if v is None:
                continue
            old = self.profile["skills"].get(k, 50.0)
            self.profile["skills"][k] = round(old + SKILL_ALPHA * (v - old), 1)
        self.profile["turns_total"] += 1

    def update_cx(self, complexity: int):
        self.profile["cx_ema"] = round(self.profile.get("cx_ema", 2.0)
                                       + CX_ALPHA * (complexity - self.profile.get("cx_ema", 2.0)), 2)

    def add_xp(self, xp: int):
        self.profile["xp"] += int(xp)
        today = time.strftime("%Y-%m-%d")
        if self.profile["last_day"] != today:
            import datetime
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            self.profile["streak"] = self.profile["streak"] + 1 if self.profile["last_day"] == yesterday else 1
            self.profile["last_day"] = today

    # ---------- L4 身份 facts / goals / wishlist ----------
    def merge_facts(self, facts: list, src: str = ""):
        """会话蒸馏 facts 合入; 接受 'str' 或 {text, kind}。lowercase 去重, 上限 40。"""
        have = {f["text"].lower() for f in self.profile["facts"]}
        now = int(time.time())
        for f in facts or []:
            text = f.get("text", "") if isinstance(f, dict) else str(f)
            kind = f.get("kind", "other") if isinstance(f, dict) else "other"
            text = text.strip()
            if text and text.lower() not in have:
                self.profile["facts"].append({"text": text, "kind": kind, "use": True,
                                              "ts": now, "src": src or time.strftime("%Y-%m-%d")})
                have.add(text.lower())
        self.profile["facts"] = self.profile["facts"][-40:]

    def set_fact(self, text: str, use: bool | None = None, delete: bool = False):
        fl = self.profile["facts"]
        if delete:
            self.profile["facts"] = [f for f in fl if f["text"] != text]
        else:
            for f in fl:
                if f["text"] == text and use is not None:
                    f["use"] = use
        self.save()

    def active_facts(self) -> list:
        return [f for f in self.profile["facts"] if f.get("use", True)]

    def upsert_goal(self, goal: dict):
        gid = goal.get("id") or f"g{int(time.time())}"
        goals = [g for g in self.profile["goals"] if g["id"] != gid]
        if not goal.get("delete"):
            goals.append({"id": gid, "kind": goal.get("kind", "custom"),
                          "label": goal.get("label", ""), "date": goal.get("date", ""),
                          "active": goal.get("active", True)})
        self.profile["goals"] = goals
        self.save()

    def goal_suggestion(self) -> dict | None:
        """最近的未来 active goal → Practice 页模式建议 chip。"""
        import datetime
        today = datetime.date.today()
        best = None
        for g in self.profile["goals"]:
            if not g.get("active") or not g.get("date"):
                continue
            try:
                d = datetime.date.fromisoformat(g["date"])
            except ValueError:
                continue
            if d >= today and (best is None or d < best[0]):
                best = (d, g)
        if not best:
            return None
        d, g = best
        days = (d - today).days
        mode = g["kind"] if g["kind"] in ("interview", "presentation") else "scenario"
        when = "today" if days == 0 else f"in {days} day{'s' if days > 1 else ''}"
        return {"text": f"{g['kind'].title()} “{g['label']}” {when} — practice {mode.title()} mode?",
                "mode": mode, "goal_id": g["id"], "days": days}

    # ---------- L3 情景记忆 + 会话历史 ----------
    def end_session(self, summary: dict):
        summary["ts"] = int(time.time())
        summary["date"] = time.strftime("%Y-%m-%d %H:%M")
        self.episodes.append(summary)
        self.merge_facts(summary.get("facts", []), src=time.strftime("%Y-%m-%d"))
        self.save()

    def log_session(self, record: dict):
        """每场快照 → sessions.json (图表 + plateau 检测唯一数据源)。"""
        record.setdefault("ts", int(time.time()))
        record.setdefault("date", time.strftime("%Y-%m-%d %H:%M"))
        record["skills"] = dict(self.profile["skills"])
        record["cx_ema"] = self.profile.get("cx_ema", 2.0)
        record["level"] = level_of(self.profile["skills"])
        self.sessions.append(record)
        self.save()

    def plateau(self) -> bool:
        """近3场: avg 波动≤3 且 cx_ema 增幅<0.2 且 avg≥75 → 舒适区平台期。"""
        recent = [s for s in self.sessions[-3:] if s.get("avg") is not None]
        if len(recent) < 3:
            return False
        avgs = [s["avg"] for s in recent]
        cxs = [s.get("cx_ema", 2.0) for s in recent]
        return (max(avgs) - min(avgs) <= 3 and (cxs[-1] - cxs[0]) < 0.2 and
                sum(avgs) / 3 >= 75)

    # ---------- 开场 briefing: 记忆 → 导师作战简报 ----------
    def briefing(self, exclude: set | None = None) -> dict:
        cards = self.due_cards(exclude=exclude)
        b = {
            "due_cards": [{"pattern": c["pattern"], "label": c["label"],
                           "explanation": c["explanation"],
                           "example_wrong": c["examples"][-1]["wrong"] if c["examples"] else "",
                           "example_right": c["examples"][-1]["right"] if c["examples"] else "",
                           "hits": c["hits"], "R": round(retrievability(c), 2)} for c in cards],
            "skills": self.profile["skills"],
            "level": level_of(self.profile["skills"]),
            "cx_ema": self.profile.get("cx_ema", 2.0),
            "plateau": self.plateau(),
            "name": self.profile.get("name", ""),
            "native_lang": self.profile.get("native_lang", ""),
            "facts": [f["text"] for f in self.active_facts()][-8:],
            "recent_episodes": [{"date": e["date"], "summary": e.get("summary", ""),
                                 "facts": [x.get("text", x) if isinstance(x, dict) else x
                                           for x in e.get("facts", [])]} for e in self.episodes[-2:]],
            "wishlist": self.profile["wishlist"][-5:],
        }
        return b


def level_of(skills: dict) -> str:
    """CEFR 风格等级 estimate (界面永远标注 estimate, 不装权威)。"""
    s = sum(skills.values()) / max(len(skills), 1)
    name = "A1"
    for band, n in CEFR_BANDS:
        if s >= band:
            name = n
    return name


def level_progress(skills: dict) -> dict:
    s = sum(skills.values()) / max(len(skills), 1)
    bands = CEFR_BANDS + [(100, "C2+")]
    for i in range(len(bands) - 1):
        if bands[i][0] <= s < bands[i + 1][0]:
            floor, ceiling = bands[i][0], bands[i + 1][0]
            return {"band": bands[i][1], "next": bands[i + 1][1], "score": round(s, 1),
                    "floor": floor, "ceiling": ceiling,
                    "pct": round((s - floor) / (ceiling - floor) * 100)}
    return {"band": "C2", "next": "C2", "score": round(s, 1), "floor": 88, "ceiling": 100, "pct": 100}


def xp_level(xp: int) -> dict:
    """Lv = floor((xp/100)^(1/1.6)) — effort 货币, 与 CEFR (attainment) 分开。"""
    lv = int((max(xp, 0) / 100) ** (1 / 1.6))
    nxt = round(100 * (lv + 1) ** 1.6)
    cur = round(100 * lv ** 1.6)
    return {"lv": lv, "next_at": nxt, "floor": cur,
            "pct": round((xp - cur) / max(nxt - cur, 1) * 100)}


def make_store():
    return LocalStore()
