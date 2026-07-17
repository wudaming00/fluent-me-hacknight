# demo 种子数据 — 空状态保险: 让每张图表/卡片墙今晚都有故事可讲。
# 页面会角标 "demo data" (诚实性台账 H9): 被评委看穿造假的代价远大于承认是种子。
# 跑法: .venv/bin/python server/seed_demo.py [--force]     (已有 cards.json 时默认不覆盖)
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
NOW = int(time.time())
D = 86400


def _card(pattern, label, typ, expl, hits, S, due_off, wrong, right,
          status="learning", pinned=False, D_=None, streak=0):
    due = NOW + due_off
    return {"pattern": pattern, "label": label, "type": typ, "explanation": expl,
            "examples": [{"wrong": wrong, "right": right, "ts": NOW - 2 * 86400}],
            "hits": hits, "clean_streak": streak, "S": S, "D": D_ or min(10, 4 + 0.5 * hits),
            "due_at": due, "last_seen": due - int(S * 86400), "status": status,
            "created": NOW - 8 * 86400, "pinned": pinned, "context": None}


def seed(force: bool = False):
    DATA.mkdir(parents=True, exist_ok=True)
    if (DATA / "cards.json").exists() and not force:
        print("data exists — use --force to overwrite")
        return

    cards = {c["pattern"]: c for c in [
        _card("tense-past-simple", "past simple tense", "tense", "past needs -ed/went",
              5, 1.0, -3600, "yesterday I meet my manager", "yesterday I met my manager"),          # 到期
        _card("prep-in-vs-on", "in vs on", "preposition", "on Monday, in July",
              4, 0.25, 0, "I have interview in Monday", "I have an interview on Monday", pinned=True),
        _card("third-person-s", "third person -s", "other", "she gives, not give",
              3, 3.0, 2 * D, "she give me advice", "she gives me advice"),
        _card("article-missing-the", "missing article", "article", "need a/the before noun",
              6, 1.0, D // 2, "I go to office early", "I go to the office early"),
        _card("wc-make-vs-do", "make vs do", "word-choice", "do research, make a plan",
              2, 7.0, 5 * D, "I made my homework", "I did my homework"),
        _card("cond-second", "second conditional", "other", "if + past, would",
              1, 0.25, 550, "if I have time I will practice", "if I had time I would practice"),
        _card("plural-uncountable", "uncountable nouns", "plural", "advice never takes -s",
              2, 3.0, D, "she gave me many advices", "she gave me a lot of advice"),
        _card("tense-present-perfect", "present perfect", "tense", "have + past participle",
              4, 22.0, 30 * D, "I already finish it", "I've already finished it",
              status="graduated", streak=3),
        _card("wc-borrow-vs-lend", "borrow vs lend", "word-choice", "borrow from, lend to",
              3, 25.0, 40 * D, "can you borrow me a pen", "can you lend me a pen",
              status="graduated", streak=4),
    ]}
    # cond-second: 刚学的卡, 强度条现场肉眼可见地衰减
    cards["cond-second"]["last_seen"] = NOW - 50

    profile = {
        "name": "Daming", "native_lang": "中文",
        "goals": [{"id": "g1", "kind": "interview", "label": "Stripe phone screen",
                   "date": (date.today() + timedelta(days=6)).isoformat(), "active": True}],
        "facts": [
            {"text": "preparing for job interviews at Stripe", "kind": "plan", "use": True, "ts": NOW - 5 * D, "src": "seed"},
            {"text": "has a friend at Google who offered a referral", "kind": "person", "use": True, "ts": NOW - 5 * D, "src": "seed"},
            {"text": "building a Dota 2 bot on weekends", "kind": "project", "use": True, "ts": NOW - 3 * D, "src": "seed"},
            {"text": "lives in the Sunset district", "kind": "preference", "use": False, "ts": NOW - 3 * D, "src": "seed"},
        ],
        "skills": {"grammar": 61.2, "vocab": 58.4, "fluency": 66.0, "pron": 55.1},
        "xp": 1240, "streak": 6, "last_day": time.strftime("%Y-%m-%d"), "turns_total": 132,
        "cx_ema": 2.8, "gentle_mode": False, "fix_voice": "user",
        "wishlist": ["ballpark figure", "circle back", "take-home"],
    }

    base = [(6, 58, 62, 8, 3, 0), (5, 60, 55, 7, 2, 0), (5, 63, 70, 9, 1, 1), (4, 61, 48, 6, 0, 1),
            (3, 66, 74, 8, 2, 1), (2, 68, 80, 10, 1, 2), (1, 71, 88, 9, 0, 1), (0, 74, 96, 9, 1, 2)]
    skills_walk = [(52.1, 51.0, 60.2, 50.3), (54.0, 52.4, 61.0, 51.2), (55.8, 53.9, 62.1, 52.0),
                   (57.1, 55.0, 63.0, 52.8), (58.4, 56.1, 64.0, 53.6), (59.5, 57.0, 64.8, 54.2),
                   (60.4, 57.8, 65.5, 54.7), (61.2, 58.4, 66.0, 55.1)]
    sessions = []
    for (days_ago, avg, xp, turns, created, adv), (g, v, f, p) in zip(base, skills_walk):
        ts = NOW - days_ago * D - 3 * 3600
        sessions.append({"ts": ts, "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
                         "mode": "free", "turns": turns, "avg": avg, "xp_gained": xp,
                         "cards_created": created, "cards_advanced": adv,
                         "new_patterns": [], "advanced_patterns": [],
                         "best_moment": "", "summary": "", "topics": [],
                         "skills": {"grammar": g, "vocab": v, "fluency": f, "pron": p},
                         "cx_ema": 2.0 + (7 - days_ago) * 0.1, "level": "B1"})

    episodes = [
        {"ts": NOW - 5 * D, "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 5 * D)),
         "summary": "Talked about the Stripe interview prep and the referral from the Google friend. Strong on vocabulary, past tense still slipping.",
         "topics": ["job hunt", "interviews"],
         "facts": [{"text": "preparing for job interviews at Stripe", "kind": "plan"},
                   {"text": "has a friend at Google who offered a referral", "kind": "person"}],
         "best_moment": "If I get this offer, everything changes."},
        {"ts": NOW - 3 * D, "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 3 * D)),
         "summary": "Weekend chat about the Dota 2 bot project; explained reinforcement learning in English for the first time.",
         "topics": ["side projects", "gaming"],
         "facts": [{"text": "building a Dota 2 bot on weekends", "kind": "project"}],
         "best_moment": "My bot finally won a lane against a human."},
        {"ts": NOW - 1 * D, "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 1 * D)),
         "summary": "Rehearsed small talk for coffee chats. Fluency noticeably better, fewer fillers.",
         "topics": ["small talk"], "facts": [],
         "best_moment": "I've been building something I actually believe in."},
    ]

    (DATA / "cards.json").write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "episodes.json").write_text(json.dumps(episodes, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "sessions.json").write_text(json.dumps(sessions, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"seeded: {len(cards)} cards, {len(sessions)} sessions, {len(episodes)} episodes")


if __name__ == "__main__":
    seed(force="--force" in sys.argv)
