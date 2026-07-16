# 记忆机制 — 三层设计, 也是 Sauna 集成的落点
#
#   L1 错题卡 (error cards): 每个语法/用词错误由 Claude 归一成 pattern key
#      (如 "tense-past-simple"), 同 pattern 去重合卡。卡片走 Leitner SRS:
#      犯错 → box 归零、10 分钟后到期; 在对话中被"钓"出来且用对 → box+1,
#      间隔 10min → 1d → 3d → 7d → 21d; box4 连续 3 次干净 → 毕业退役。
#      到期卡片进开场 briefing, 由导师在对话里自然设局引出 (对话式间隔重复,
#      不是闪卡 —— 这是和 Duolingo 的差异点)。
#
#   L2 学习者画像 (profile): 四维技能 EMA(α=0.25, 单句好运不跳级)、等级估计、
#      XP、连续天数、想学的词 (wishlist)。
#
#   L3 情景记忆 (episodes): 每次会话蒸馏成 摘要+话题+学习者透露的个人事实。
#      下次开场导师会主动提起 ("上次你说 Dota bot 要打比赛, 后来赢了吗?")
#      —— demo 的第二记忆点, 也是 Sauna "有记忆的 AI" 的直给场景。
#
# SaunaStore 继承 LocalStore: 本地 JSON 永远是 source of truth + 兜底
# (场馆断网 demo 不能死), Sauna 连上后只是多一路镜像推送/拉取。
import json
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(parents=True, exist_ok=True)

SRS_STEPS = [600, 86400, 3 * 86400, 7 * 86400, 21 * 86400]  # box0..4 的复习间隔(秒)
GRADUATE_STREAK = 3          # box4 之后连续用对几次算毕业
BRIEFING_CARDS = 5           # 每场开场最多带几张到期卡
SKILL_ALPHA = 0.25           # 技能 EMA 步长

DEFAULT_PROFILE = {
    "skills": {"grammar": 50.0, "vocab": 50.0, "fluency": 50.0, "pron": 50.0},
    "xp": 0, "streak": 0, "last_day": "", "turns_total": 0,
    "wishlist": [],          # 用户问过 "how do I say X" 的词
}


def _load(p: Path, default):
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(json.dumps(default))


class LocalStore:
    def __init__(self):
        self.f_cards = DATA / "cards.json"
        self.f_profile = DATA / "profile.json"
        self.f_episodes = DATA / "episodes.json"
        self.cards: dict = _load(self.f_cards, {})
        self.profile: dict = _load(self.f_profile, DEFAULT_PROFILE)
        self.episodes: list = _load(self.f_episodes, [])

    # ---------- 持久化 ----------
    def save(self):
        self.f_cards.write_text(json.dumps(self.cards, ensure_ascii=False, indent=1), encoding="utf-8")
        self.f_profile.write_text(json.dumps(self.profile, ensure_ascii=False, indent=1), encoding="utf-8")
        self.f_episodes.write_text(json.dumps(self.episodes[-50:], ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- L1 错题卡 ----------
    def record_error(self, err: dict, sentence: str):
        """Claude 判出的一个错误 → 落卡。同 pattern 合并, SRS 归零重修。"""
        key = err.get("pattern") or f"other-{int(time.time())}"
        now = int(time.time())
        card = self.cards.get(key) or {
            "pattern": key, "label": err.get("pattern_label") or key,
            "type": err.get("type", "other"), "explanation": err.get("explanation", ""),
            "examples": [], "hits": 0, "clean_streak": 0,
            "box": 0, "due_at": 0, "status": "learning", "created": now,
        }
        card["hits"] += 1
        card["clean_streak"] = 0
        card["box"] = 0
        card["due_at"] = now + SRS_STEPS[0]
        card["last_seen"] = now
        card["status"] = "learning"
        ex = {"wrong": err.get("span", sentence)[:120], "right": err.get("correction", "")[:120], "ts": now}
        card["examples"] = (card["examples"] + [ex])[-5:]
        self.cards[key] = card

    def record_elicited(self, pattern: str, success: bool):
        """导师设局引出的 pattern: 用对 → SRS 晋级; 用错走 record_error 路径(判卷层负责)。"""
        card = self.cards.get(pattern)
        if not card or not success:
            return
        now = int(time.time())
        card["box"] = min(card["box"] + 1, len(SRS_STEPS) - 1)
        card["clean_streak"] += 1
        card["due_at"] = now + SRS_STEPS[card["box"]]
        card["last_seen"] = now
        if card["box"] == len(SRS_STEPS) - 1 and card["clean_streak"] >= GRADUATE_STREAK:
            card["status"] = "graduated"

    def due_cards(self) -> list:
        """到期优先, 同为到期按 hits 降序 (最顽固的错优先钓)。"""
        now = int(time.time())
        active = [c for c in self.cards.values() if c["status"] == "learning"]
        due = sorted([c for c in active if c["due_at"] <= now],
                     key=lambda c: (-c["hits"], c["due_at"]))
        return due[:BRIEFING_CARDS]

    # ---------- L2 画像 ----------
    def update_skills(self, dims: dict):
        """dims: {"grammar": 82, ...} 只更新本轮实际有值的维度。EMA 抗单句波动。"""
        for k, v in dims.items():
            if v is None:
                continue
            old = self.profile["skills"].get(k, 50.0)
            self.profile["skills"][k] = round(old + SKILL_ALPHA * (v - old), 1)
        self.profile["turns_total"] += 1

    def add_xp(self, xp: int):
        self.profile["xp"] += int(xp)
        today = time.strftime("%Y-%m-%d")
        if self.profile["last_day"] != today:
            import datetime
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            self.profile["streak"] = self.profile["streak"] + 1 if self.profile["last_day"] == yesterday else 1
            self.profile["last_day"] = today

    # ---------- L3 情景记忆 ----------
    def end_session(self, summary: dict):
        """summary 来自 Claude 蒸馏: {summary, topics[], facts[], best_moment}"""
        summary["ts"] = int(time.time())
        summary["date"] = time.strftime("%Y-%m-%d %H:%M")
        self.episodes.append(summary)
        self.save()

    # ---------- 开场 briefing: 记忆 → 导师的作战简报 ----------
    def briefing(self) -> dict:
        return {
            "due_cards": [{"pattern": c["pattern"], "label": c["label"],
                           "explanation": c["explanation"],
                           "example_wrong": c["examples"][-1]["wrong"] if c["examples"] else "",
                           "example_right": c["examples"][-1]["right"] if c["examples"] else "",
                           "hits": c["hits"]} for c in self.due_cards()],
            "skills": self.profile["skills"],
            "level": level_of(self.profile["skills"]),
            "recent_episodes": [{"date": e["date"], "summary": e.get("summary", ""),
                                 "facts": e.get("facts", [])} for e in self.episodes[-2:]],
            "wishlist": self.profile["wishlist"][-5:],
        }


def level_of(skills: dict) -> str:
    """CEFR 风格等级估计 (标注 estimate, 不装权威)。"""
    s = sum(skills.values()) / max(len(skills), 1)
    for band, name in [(88, "C2"), (75, "C1"), (60, "B2"), (45, "B1"), (30, "A2")]:
        if s >= band:
            return name
    return "A1"


class SaunaStore(LocalStore):
    """Sauna 镜像层 — 现场看完 17:00 的 API demo 后填 _push/_pull 两个函数即可。
    设计约定: 本地写永远先落盘成功, Sauna 推送失败只打日志不阻塞 (断网不死)。
    预期映射: 错题卡+画像 → Sauna 结构化记忆; episodes → Sauna 对话记忆。"""

    def __init__(self, api_key: str):
        super().__init__()
        self.key = api_key

    def _push(self, kind: str, payload: dict):
        # TODO(现场): POST 到 Sauna memory API, kind ∈ {card, profile, episode}
        pass

    def save(self):
        super().save()
        try:
            self._push("profile", self.profile)
        except Exception:
            pass

    def end_session(self, summary: dict):
        super().end_session(summary)
        try:
            self._push("episode", summary)
        except Exception:
            pass


def make_store():
    import os
    key = os.environ.get("SAUNA_API_KEY", "")
    return SaunaStore(key) if key else LocalStore()
