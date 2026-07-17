# 场景引擎 — 三模式共享: scenario(个性化角色扮演) / interview(模拟面试) / presentation(演讲排练)
#
#   个性化来源: profile.facts (use:true) + 到期 SRS 卡 + wishlist → Claude 生成 4 张场景卡
#   (3 张来自真实生活 + 1 张 wildcard)。缓存 keyed by hash(facts+due), 上台前预生成。
#   设计原语: setup/intake 各 1 次 LLM 调用; 会话内 turn 循环仍然只有 1 次判卷调用。
import hashlib
import json
import time
from pathlib import Path

import brain
import memory as memmod

DATA = Path(__file__).resolve().parent.parent / "data"
F_SUGGEST = DATA / "scene_suggestions.json"
F_SCENES = DATA / "scenes.json"


def _load(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _save(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- scenario 建议 ----------
def suggest(store, regen: bool = False) -> dict:
    facts = store.active_facts()
    due = store.due_cards(limit=5)
    key = hashlib.sha1(json.dumps([[f["text"] for f in facts],
                                   [c["pattern"] for c in due]]).encode()).hexdigest()[:12]
    cache = _load(F_SUGGEST, {})
    if not regen and cache.get("key") == key and cache.get("suggestions"):
        return {"suggestions": cache["suggestions"], "cached": True}

    fact_lines = "\n".join(f'- ({f.get("kind", "other")}) {f["text"]}' for f in facts) or "- (nothing yet)"
    due_lines = "\n".join(f'- {c["pattern"]}: {c["label"]} ("{c["examples"][-1]["wrong"]}" → "{c["examples"][-1]["right"]}")'
                          if c["examples"] else f'- {c["pattern"]}: {c["label"]}' for c in due) or "- none due"
    wl = ", ".join(store.profile["wishlist"][-5:]) or "none"
    prompt = f"""You design roleplay scenes for a spoken-English learner (level {memmod.level_of(store.profile['skills'])}).
WHAT YOU KNOW ABOUT THEM:
{fact_lines}
GRAMMAR PATTERNS DUE FOR REVIEW:
{due_lines}
WORDS THEY WANT: {wl}

Create 4 scenes: #1-3 built directly on their real life facts (cite which in "hook"), #4 a wildcard —
fun, unexpected, still level-appropriate. Each scene should make at least one due pattern
UNAVOIDABLE in natural replies (when any are due). Return ONLY JSON:
{{"suggestions": [{{"title": "<str>", "hook": "because you <str>", "kai_role": "<str>", "setting": "<str>",
"learner_role": "<str>", "objective": "<str>", "beats": ["<3-6 goal strings>"],
"target_patterns": ["<only from the due list>"], "target_vocab": ["<str>"], "wildcard": bool}}]}}"""
    out = brain._claude_json(prompt, timeout=40)
    suggestions = out.get("suggestions", [])[:4]
    _save(F_SUGGEST, {"key": key, "ts": int(time.time()), "suggestions": suggestions})
    return {"suggestions": suggestions, "cached": False}


def build_scene(stub: dict, mode: str = "scenario") -> dict:
    """建议卡 → 正式 scene doc (beats 字符串 → 对象)。"""
    beats = [{"i": i, "goal": g if isinstance(g, str) else g.get("goal", ""), "done": False}
             for i, g in enumerate(stub.get("beats", []))]
    return {"id": f"sc_{int(time.time())}", "mode": mode,
            "title": stub.get("title", "Scene"), "hook": stub.get("hook", ""),
            "kai_role": stub.get("kai_role", "a friendly local"),
            "setting": stub.get("setting", ""), "learner_role": stub.get("learner_role", "yourself"),
            "objective": stub.get("objective", ""), "beats": beats,
            "target_patterns": stub.get("target_patterns", []),
            "target_vocab": stub.get("target_vocab", []),
            "wildcard": bool(stub.get("wildcard"))}


# ---------- interview intake ----------
def interview_intake(store, jd_text: str = "", fact_ids: list | None = None, role_hint: str = "") -> dict:
    level = memmod.level_of(store.profile["skills"])
    if jd_text.strip():
        src = f"JOB DESCRIPTION (pasted):\n{jd_text[:3000]}"
    else:
        facts = store.active_facts()
        chosen = [f["text"] for f in facts if not fact_ids or f["text"] in fact_ids] or [f["text"] for f in facts]
        src = "WHAT WE KNOW ABOUT THE CANDIDATE:\n" + "\n".join(f"- {t}" for t in chosen[:8])
        if role_hint:
            src += f"\nTarget role hint: {role_hint}"
    prompt = f"""You are prepping a mock interview for a {level} English learner.
{src}

Write 4-6 interview questions: a mix of opener, behavioral (marked wants="STAR"), role-specific,
and motivation. Pick a realistic company/role from the material. Include 3-6 words from the
material worth teaching as target_vocab. Return ONLY JSON:
{{"company": "<str>", "role": "<str>", "interviewer": "<name, a senior engineer on the team — friendly but probing>",
"questions": [{{"i": 0, "q": "<str>", "kind": "opener|behavioral|role|motivation", "wants": "STAR"|"clarity"|null}}],
"target_vocab": ["<str>"]}}"""
    plan = brain._claude_json(prompt, timeout=40)
    plan.update({"id": f"iv_{int(time.time())}", "mode": "interview"})
    for w in plan.get("target_vocab", []):
        if w not in store.profile["wishlist"]:
            store.profile["wishlist"].append(w)
    _persist_plan(plan)
    return plan


# ---------- presentation intake ----------
def present_intake(outline_text: str, target_minutes: float = 3) -> dict:
    prompt = f"""Split this talk outline into sections and budget time. Total target: {target_minutes} minutes.
OUTLINE (verbatim from the speaker):
{outline_text[:3000]}

Return ONLY JSON:
{{"title": "<str>", "audience": "<inferred audience, e.g. 'hackathon judges, technical, short attention'>",
"sections": [{{"i": 0, "title": "<str>", "notes": "<their bullet text for this section, verbatim>", "target_sec": int}}],
"qa": {{"n": 2, "stance": "skeptical but fair"}}}}"""
    plan = brain._claude_json(prompt, timeout=40)
    plan.update({"id": f"pr_{int(time.time())}", "mode": "presentation"})
    _persist_plan(plan)
    return plan


def _persist_plan(plan: dict):
    all_ = _load(F_SCENES, {"plans": [], "runs": []})
    all_["plans"] = [p for p in all_["plans"] if p.get("id") != plan["id"]] + [plan]
    all_["plans"] = all_["plans"][-20:]
    _save(F_SCENES, all_)


def get_plan(plan_id: str) -> dict | None:
    for p in _load(F_SCENES, {"plans": []})["plans"]:
        if p.get("id") == plan_id:
            return p
    return None


def log_run(run: dict):
    all_ = _load(F_SCENES, {"plans": [], "runs": []})
    all_["runs"] = (all_.get("runs", []) + [run])[-50:]
    _save(F_SCENES, all_)


def last_run(plan_id: str) -> dict | None:
    runs = [r for r in _load(F_SCENES, {"runs": []}).get("runs", []) if r.get("plan_id") == plan_id]
    return runs[-1] if runs else None


# ---------- turn 后处理: beat / cursor 推进 ----------
def apply_turn(session: dict, judge: dict):
    """把判卷的模式字段应用到 SESSION 里的 scene 状态。返回给前端的 scene 增量。"""
    mode, scene = session.get("mode", "free"), session.get("scene")
    if not scene:
        return None
    out = {"mode": mode, "cursor": session.get("cursor", 0), "phase": session.get("phase", "main")}
    if mode == "scenario" or (mode == "presentation" and session.get("phase") == "qa"):
        for i in judge.get("beats_done", []) or []:
            for b in scene.get("beats", []):
                if b["i"] == i:
                    b["done"] = True
        out["beats"] = scene.get("beats", [])
        out["scene_state"] = judge.get("scene_state", "ongoing")
    elif mode == "interview":
        out["structure"] = judge.get("structure")
        out["polished"] = judge.get("polished")
        nm = judge.get("next_move", "advance")
        out["next_move"] = nm
        if nm == "advance":
            session["cursor"] = min(session.get("cursor", 0) + 1, len(scene.get("questions", [])) - 1)
        out["cursor"] = session["cursor"]
        out["n"] = len(scene.get("questions", []))
    elif mode == "presentation":
        out["clarity"] = judge.get("clarity")
        out["clarity_note"] = judge.get("clarity_note")
        out["polished"] = judge.get("polished")
        out["cursor"] = session.get("cursor", 0)
        out["n"] = len(scene.get("sections", []))
    return out


# ---------- 会话结束: 模式分支报告 ----------
def end_report(session: dict) -> dict:
    mode, scene, log = session.get("mode", "free"), session.get("scene"), session.get("scene_log", [])
    if mode == "interview" and scene:
        rows = []
        for e in log:
            st = e.get("structure") or {}
            rows.append({"q": e.get("q", ""), "kind": e.get("kind", ""),
                         "lang": e.get("composite"), "structure": st.get("score"),
                         "star": {k: st.get(k) for k in "star"} if st else None,
                         "delivery": e.get("delivery"), "polished_audio": e.get("polished_audio")})
        rows_txt = "\n".join(f'Q{i+1} [{r["kind"]}] "{r["q"]}" — lang {r["lang"]}, structure {r["structure"]}, '
                             f'delivery {json.dumps(r["delivery"])}' for i, r in enumerate(rows))
        try:
            verdict = brain.mock_verdict(rows_txt, "interview")
        except Exception:
            verdict = {"coach_verdict": "", "top_fixes": []}
        langs = [r["lang"] for r in rows if r["lang"] is not None]
        sts = [r["structure"] for r in rows if r["structure"] is not None]
        rep = {"mode": "interview", "company": scene.get("company", ""), "role": scene.get("role", ""),
               "per_question": rows,
               "overall": {"language": round(sum(langs) / len(langs)) if langs else None,
                           "structure": round(sum(sts) / len(sts)) if sts else None},
               **verdict}
        log_run({"plan_id": scene.get("id"), "ts": int(time.time()), "mode": "interview",
                 "overall": rep["overall"]})
        return rep
    if mode == "presentation" and scene:
        secs = []
        for e in log:
            if e.get("phase") == "qa":
                continue
            secs.append({"title": e.get("title", ""), "target_sec": e.get("target_sec"),
                         "actual_sec": e.get("actual_sec"), "wpm": e.get("wpm"),
                         "fillers": e.get("fillers"), "clarity": e.get("clarity"),
                         "polished_audio": e.get("polished_audio")})
        qa = [e for e in log if e.get("phase") == "qa"]
        rows_txt = "\n".join(f'S{i+1} "{s["title"]}" — {s["actual_sec"]}s/{s["target_sec"]}s, '
                             f'{s["wpm"]}wpm, clarity {s["clarity"]}' for i, s in enumerate(secs))
        try:
            verdict = brain.mock_verdict(rows_txt, "presentation")
        except Exception:
            verdict = {"coach_verdict": "", "top_fixes": []}
        total = {"actual_sec": sum(s["actual_sec"] or 0 for s in secs),
                 "target_sec": sum(s["target_sec"] or 0 for s in secs)}
        prev = last_run(scene.get("id"))
        rep = {"mode": "presentation", "title": scene.get("title", ""), "sections": secs,
               "total": total,
               "qa": {"asked": len(qa),
                      "avg": round(sum(e.get("composite") or 0 for e in qa) / len(qa)) if qa else None},
               **verdict}
        this_run = {"plan_id": scene.get("id"), "ts": int(time.time()), "mode": "presentation",
                    "total_sec": total["actual_sec"],
                    "fillers": sum(s.get("fillers") or 0 for s in secs)}
        if prev:
            rep["vs_last_run"] = {"total_sec": this_run["total_sec"] - prev.get("total_sec", 0),
                                  "fillers": this_run["fillers"] - prev.get("fillers", 0)}
        log_run(this_run)
        return rep
    if mode == "scenario" and scene:
        done = sum(1 for b in scene.get("beats", []) if b.get("done"))
        rep = {"mode": "scenario", "title": scene.get("title", ""),
               "beats": f'{done}/{len(scene.get("beats", []))}',
               "outcome": "completed" if done == len(scene.get("beats", [])) else "partial"}
        log_run({"plan_id": scene.get("id"), "ts": int(time.time()), "mode": "scenario",
                 "outcome": rep["outcome"]})
        return rep
    return {"mode": mode}
