# fluent-me 的 MCP server — Sauna 官方扩展点 ("Wire in any MCP server")
#
# 跑法:  .venv/bin/python server/sauna_mcp.py          # streamable-http :8902/mcp
# 公网:  cloudflared tunnel --url http://localhost:8902  (Sauna 是云端产品, 本地要隧道)
# 依赖:  pip install mcp   (requirements.txt 已列; 未安装时本文件不影响主应用)
#
# 暴露的工具 = "Kai 的记忆, 可被你的 Sauna 查询":
#   get_learner_profile / get_due_cards / get_recent_sessions / get_progress_summary
#   add_identity_fact (Sauna → fluent-me 反向写入: 在 Sauna 里告诉它一个 fact, Kai 下场就知道)
import json
import time

from mcp.server.fastmcp import FastMCP

import memory as memmod

mcp = FastMCP("fluent-me", host="0.0.0.0", port=8902)


def _store():
    return memmod.make_store()   # 每次调用重读磁盘 — 主应用是唯一写者, 这里保持只读新鲜


@mcp.tool()
def get_learner_profile() -> str:
    """The learner's spoken-English profile: level estimate, skills, XP, streak, identity facts, goals."""
    s = _store()
    p = s.profile
    return json.dumps({
        "name": p.get("name"), "level_estimate": memmod.level_of(p["skills"]),
        "note": "level is estimated from conversation scoring, not a certified test",
        "skills": p["skills"], "xp": p["xp"], "streak_days": p["streak"],
        "facts": [f["text"] for f in p.get("facts", []) if f.get("use", True)],
        "goals": [g for g in p.get("goals", []) if g.get("active")],
        "wishlist": p.get("wishlist", []),
    }, ensure_ascii=False)


@mcp.tool()
def get_due_cards() -> str:
    """Mistake patterns currently due for review (spaced repetition), weakest memory first."""
    s = _store()
    now = int(time.time())
    out = [{"pattern": c["pattern"], "label": c["label"], "missed": c["hits"],
            "memory_strength_pct": round(memmod.retrievability(c, now) * 100),
            "example": c["examples"][-1] if c["examples"] else None}
           for c in s.due_cards(limit=10)]
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def get_recent_sessions(n: int = 5) -> str:
    """Summaries of the learner's recent practice sessions (episodic memory)."""
    s = _store()
    return json.dumps(s.episodes[-n:], ensure_ascii=False)


@mcp.tool()
def get_progress_summary() -> str:
    """Progress over time: per-session averages, XP, cards created/advanced."""
    s = _store()
    return json.dumps({"sessions": s.sessions[-14:],
                       "cards": {"learning": sum(1 for c in s.cards.values() if c["status"] == "learning"),
                                 "graduated": sum(1 for c in s.cards.values() if c["status"] == "graduated")}},
                      ensure_ascii=False)


@mcp.tool()
def add_identity_fact(text: str, kind: str = "other") -> str:
    """Teach Kai something about the learner (job, person, project, plan, preference). Kai will use it next session."""
    s = _store()
    s.merge_facts([{"text": text, "kind": kind}], src="sauna")
    s.save()
    return f"Saved. Kai now knows: {text}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
