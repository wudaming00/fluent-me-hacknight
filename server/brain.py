# Claude 判卷层 — 每轮一次调用同时产出: 戏内回复 + 纠错 + 评分 + pattern 归一 + 钓卡判定
# (+ 模式增量: scenario 的 beats / interview 的 STAR+polished / presentation 的 clarity+polished)
# 单调用是延迟预算的关键: haiku 一次 2-4s, 拆开现场就冷场。
#
# 传输三级降级: ANTHROPIC_API_KEY 直连 → claude CLI (带 --strict-mcp-config, 免 MCP 启动税)
#              → FLUENTME_MOCK=1 假判卷 (无 key 全链路可开发)
# 加固: judge_safe = 25s 超时 + reply 正则抢救 + canned 兜底 (评委面前判卷挂了对话也不断)
import json
import os
import random
import re
import subprocess
import urllib.request

MODEL = "claude-haiku-4-5-20251001"          # API 直连默认: 快 (2-5s)
CLI_MODEL = os.environ.get("FLUENTME_JUDGE_MODEL", "opus")   # CLI 走订阅 flat: 用 Opus 换质量
TIMEOUT = 25


def tier() -> str:
    if os.environ.get("FLUENTME_MOCK"):
        return "mock"
    return "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"


def judge_timeout(base: int = 25) -> int:
    """CLI(Windows interop) 实测大 prompt 40s+, 给足水位; 判卷挂了有 canned 兜底不至于死。"""
    return base + 65 if tier() == "cli" else base

CANNED = ["Hm, I want to make sure I got that — could you say it once more, with a bit more detail?",
          "Interesting — tell me a bit more about that?",
          "Right — and how did that feel?"]

_P_ZH = "L1 is Mandarin: watch articles (a/the), third-person -s, tense marking, plurals, in/on/at."
_P_ES = "L1 is Spanish: watch he/she mix-ups, false friends, missing do-support."
_P_JA = "L1 is Japanese: watch articles, plurals, r/l in speech, dropped subjects."
_P_KO = "L1 is Korean: watch articles, verb tense agreement, prepositions."
_P_FR = "L1 is French: watch false friends, adjective order, -ing vs to-infinitive."
# 键 = O1 界面选项原文 + 常用缩写 (两边都收, 免得再对不上)
L1_PRIORS = {"中文": _P_ZH, "zh": _P_ZH,
             "Español": _P_ES, "es": _P_ES,
             "日本語": _P_JA, "ja": _P_JA,
             "한국어": _P_KO, "ko": _P_KO,
             "Français": _P_FR, "fr": _P_FR}


# ---------- 传输 ----------
def _api_text(prompt: str, timeout: int) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": MODEL, "max_tokens": 1500,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return "".join(b.get("text", "") for b in json.loads(r.read())["content"])


def _cli_text(prompt: str, timeout: int) -> str:
    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    # prompt 必须放在 --mcp-config 之前: 该 flag 会把后续位置参数吞成配置文件路径
    r = subprocess.run(["claude", "-p", prompt, "--model", CLI_MODEL,
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                        "--output-format", "text"],
                       capture_output=True, text=True, timeout=timeout, env=env)
    return r.stdout.strip()


def _raw_text(prompt: str, timeout: int) -> str:
    if os.environ.get("FLUENTME_MOCK"):
        return json.dumps(_mock_json(prompt))
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _api_text(prompt, timeout)
    return _cli_text(prompt, timeout)


def _claude_json(prompt: str, timeout: int = TIMEOUT) -> dict:
    txt = _raw_text(prompt, timeout)
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


def judge_safe(prompt: str, timeout: int = TIMEOUT) -> tuple[dict, bool]:
    """(judge_dict, degraded)。挂了也要给出一句戏内回复, 对话不断 — 故障变韧性展示。"""
    txt = ""
    try:
        txt = _raw_text(prompt, timeout)
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1]), False
    except Exception:
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', txt or "")
        reply = json.loads(f'"{m.group(1)}"') if m else random.choice(CANNED)
        return {"reply": reply, "errors": [], "scores": {}, "complexity": 1,
                "elicited": {}, "recast": None, "native": None}, True


# ---------- briefing 块 ----------
def _briefing_block(briefing: dict) -> str:
    lines = []
    if briefing.get("name"):
        lines.append(f'Learner name: {briefing["name"]}.')
    prior = L1_PRIORS.get(briefing.get("native_lang", ""), "")
    if prior:
        lines.append(prior)
    cards = briefing.get("due_cards", [])
    if cards:
        lines.append("REVIEW TARGETS (weave into conversation naturally — engineer questions "
                     "whose natural answer requires the pattern; NEVER quiz or name the pattern aloud):")
        for c in cards:
            lines.append(f'- {c["pattern"]}: {c["label"]} | they previously said "{c["example_wrong"]}" '
                         f'→ should be "{c["example_right"]}" | failed {c["hits"]}x')
    if briefing.get("facts"):
        lines.append("WHAT YOU KNOW ABOUT THEM (their life — reference casually like an old friend):")
        for t in briefing["facts"]:
            lines.append(f"- {t}")
    eps = briefing.get("recent_episodes", [])
    if eps:
        lines.append("RECENT SESSIONS:")
        for e in eps:
            facts = "; ".join(e.get("facts", [])[:4])
            lines.append(f'- {e["date"]}: {e.get("summary", "")} | {facts}')
    if briefing.get("wishlist"):
        lines.append("WORDS THEY WANTED TO LEARN: " + ", ".join(briefing["wishlist"]))
    lines.append(f'Their level estimate: {briefing.get("level", "B1")} — pitch your language one notch above.')
    cx = briefing.get("cx_ema", 2.0)
    lines.append(f"Their recent sentence ambition averages {cx:.1f}/5 — ask questions that invite "
                 f"structures one notch above (target {min(5, round(cx) + 1)}).")
    if briefing.get("plateau"):
        lines.append("PLATEAU DETECTED: accuracy high but ambition flat. Push: at least one question "
                     "demanding a hypothetical/conditional or defending an opinion.")
    return "\n".join(lines)


# ---------- 模式增量块 ----------
def _scene_block(scene: dict, mode: str, cursor: int, phase: str) -> str:
    if mode == "scenario" or (mode == "presentation" and phase == "qa"):
        beats = "\n".join(f'{b["i"]}. {b["goal"]} [{"done" if b.get("done") else "pending"}]'
                          for b in scene.get("beats", []))
        return f"""=== SCENE (roleplay in progress) ===
You are Kai playing: {scene.get("kai_role", "a friendly local")}. Setting: {scene.get("setting", "")}.
Learner plays: {scene.get("learner_role", "themselves")}. Their objective: {scene.get("objective", "")}.
BEATS — steer the conversation through these in order, never announcing them:
{beats}
Stay fully in character in "reply". Corrections happen ONLY via recast/errors — never break
character to lecture. Review targets above still apply: detect "elicited" passively, and prefer
beat questions that also bait a due pattern.
ADD to the JSON: "beats_done": [<beat indices completed by this exchange>],
"scene_state": "ongoing" | "wrapup" (all beats done or scene stalling → deliver a natural
scene-closing line as your character and set "wrapup")."""
    if mode == "interview":
        qs = scene.get("questions", [])
        q = qs[min(cursor, len(qs) - 1)] if qs else {"q": "Tell me about yourself.", "kind": "opener", "wants": None}
        return f"""=== MOCK INTERVIEW ===
You are {scene.get("interviewer", "the interviewer")}, interviewing for {scene.get("role", "the role")} at {scene.get("company", "the company")}.
Current question ({cursor + 1}/{len(qs) or 1}): "{q["q"]}" (kind={q.get("kind")}, wants={q.get("wants")}).
Also grade the answer AS AN INTERVIEW ANSWER — this is coaching feedback, not a hiring assessment:
ADD to the JSON:
"structure": {{"s":bool|null,"t":bool|null,"a":bool|null,"r":bool|null,"score":0-100,"note":"<≤10 words>"}}
  (STAR ticks only when wants=STAR; otherwise judge logic/clarity and set s/t/a/r to null),
"polished": rewrite THEIR OWN answer as a strong ≤55-word spoken answer — keep their facts
  exactly, NEVER invent experience or numbers; null if they spoke <20 words,
"next_move": "followup" (ask ONE probing follow-up — max one per question) | "advance"
  (ask the next planned question in reply) | "wrap" (that was the last question).
reply = your next interviewer line, fully in character. Set "native" to null.
Do not steer topics toward review targets (the interview owns the topics); still report "elicited" passively."""
    if mode == "presentation":
        secs = scene.get("sections", [])
        s = secs[min(cursor, len(secs) - 1)] if secs else {"title": "the talk", "notes": ""}
        return f"""=== PRESENTATION COACH ===
They just delivered section {cursor + 1}/{len(secs) or 1}: "{s["title"]}". Their planned notes: {s.get("notes", "")[:400]}
This is not a conversation. ADD to the JSON:
"clarity": 0-100 (was the section's point clear, ordered, landed?), "clarity_note": "<≤12 words>",
"polished": a tightened ≤60-word version of what they said — their content, sharper phrasing.
reply = ONE coach line: what worked + the single biggest fix. No question. Set "native" to null."""
    return ""


# ---------- 主判卷 ----------
def judge_prompt(transcript: str, convo: list, briefing: dict, mode: str = "free",
                 scene: dict | None = None, cursor: int = 0, phase: str = "main",
                 drill_q: str | None = None) -> str:
    hist = "\n".join(f'{t["who"].upper()}: {t["text"]}' for t in convo[-8:]) or "(session just started)"
    targets = [c["pattern"] for c in briefing.get("due_cards", [])]
    mode_block = _scene_block(scene, mode, cursor, phase) if scene else ""
    drill_block = (f'\nWARM-UP MODE: reply with ≤1 short encouraging sentence, then ask exactly '
                   f'this next question: "{drill_q}"') if drill_q else ""
    return f"""You are Kai — a sharp, warm English conversation tutor. You correct like a friend, not a textbook.

{_briefing_block(briefing)}

{mode_block}

Conversation so far:
{hist}

Learner's new line (ASR transcript, may have noise — don't punish obvious ASR artifacts): "{transcript}"

Do ALL of the following in ONE json:
1. reply: your next in-scene line. 1-2 spoken sentences. Stay on the learner's topic{"" if mode != "free" else ", usually end with a question"}.
   If a review target hasn't been elicited yet, steer so the NEXT answer naturally requires it.{drill_block}
2. Grade the line: grammar 0-100 (error count/severity vs sentence length), vocab 0-100 (word choice + naturalness).
3. List real errors only (max 3, most important first). Two kinds count: (a) grammar mistakes, and
   (b) HABITS — grammatical but unnatural phrasing a native wouldn't use (L1-transfer calques like
   "very like", "how to say", overformal textbook phrasing, awkward collocations). Mark habits with
   type "habit". Canonicalize each into a reusable kebab-case pattern key matching the same mistake
   in other sentences (e.g. "tense-past-simple", "article-missing-the", "wc-make-vs-do",
   "habit-very-like", "habit-textbook-greeting"). Reuse keys from REVIEW TARGETS when same pattern.
4. complexity 1-5: ambition of attempted structure (1 = "Yes I like it", 5 = conditionals/relative clauses/nuance).
5. recast: minimal-edit corrected version (null if already natural). native: how a relaxed native speaker
   would say it (null if recast already is, or when the mode says so).
6. elicited: for each review target {targets} — ONLY if this line actually attempted that pattern,
   true (used correctly) or false (failed again). Omit patterns not attempted.
7. wishlist: if they asked how to say something, list those items.

Return ONLY JSON (reply first):
{{"reply": "<str>", "scores": {{"grammar": int, "vocab": int}}, "complexity": int,
"errors": [{{"span": "<their words>", "type": "tense|article|preposition|word-choice|plural|word-order|collocation|habit|other",
"severity": "minor|major|blocking", "correction": "<fixed>", "explanation": "<≤8 words>",
"pattern": "<kebab-key>", "pattern_label": "<short label>"}}],
"recast": "<str|null>", "native": "<str|null>", "elicited": {{"<pattern>": true|false}}, "wishlist": ["<str>"]}}"""


def greeting(briefing: dict, mode: str = "free", scene: dict | None = None,
             drill_targets: list | None = None) -> dict:
    scene_block = ""
    if scene:
        scene_block = (f'\nYou are OPENING a roleplay scene IN CHARACTER as {scene.get("kai_role") or scene.get("interviewer", "")}. '
                       f'Setting: {scene.get("setting", scene.get("company", ""))}. '
                       f'Open the scene naturally (greet + first beat/question). Never mention it is practice.')
    drill_block = ""
    if drill_targets:
        items = "\n".join(f'- {c["pattern"]}: {c["label"]} (they said "{c["example_wrong"]}" → "{c["example_right"]}")'
                          for c in drill_targets)
        drill_block = f"""
Also produce DRILL: for these review targets, one rapid-fire natural question each (≤12 words,
never naming the pattern — e.g. "So what did you get up to last weekend?" hunts past tense):
{items}
Add to JSON: "drill": [{{"pattern": "<key>", "question": "<str>"}}]. Your reply asks the FIRST drill question."""
    prompt = f"""You are Kai — a warm English conversation tutor greeting a returning learner by voice.

{_briefing_block(briefing)}
{scene_block}{drill_block}

Write your opening line: greet them{' by name' if briefing.get('name') else ''}, reference something specific you
remember (if any), and open with a question that naturally invites the FIRST review target pattern (never name it).
If you know nothing yet, warmly ask what they're building these days. 1-2 spoken sentences.
Return ONLY JSON: {{"reply": "<str>"{', "drill": [...]' if drill_targets else ''}}}"""
    return _claude_json(prompt)


def session_summary(convo: list, mode: str = "free") -> dict:
    hist = "\n".join(f'{t["who"].upper()}: {t["text"]}' for t in convo)
    prompt = f"""Distill this tutoring session into memory. Conversation:
{hist}

Return ONLY JSON:
{{"summary": "<2 sentences: what you talked about + how they did>",
"topics": ["<str>"],
"facts": [{{"text": "<personal fact the learner revealed — projects, plans, people, preferences. Specific, reusable>",
"kind": "job|person|project|plan|preference|other"}}],
"best_moment": "<their single best sentence verbatim, or empty>",
"sendoff": "<1 warm spoken sentence: name their best moment + a hook to come back — like a friend expecting news>"}}"""
    return _claude_json(prompt)


def mock_verdict(rows: str, kind: str) -> dict:
    """面试/演讲收尾裁决 (1 call)。kind ∈ interview|presentation。"""
    what = ("mock interview" if kind == "interview" else "presentation rehearsal")
    prompt = f"""You are an English-speaking coach. Here are the per-{'question' if kind == 'interview' else 'section'} results of a {what}:
{rows}

Return ONLY JSON — coaching feedback, never a hiring assessment:
{{"coach_verdict": "<2 sentences, warm but concrete>",
"top_fixes": ["<imperative fix ≤10 words>", "<second fix>"]}}"""
    return _claude_json(prompt)


# ---------- mock (无 key 全链路开发桩) ----------
def _mock_json(prompt: str) -> dict:
    if '"drill"' in prompt and "DRILL" in prompt:
        return {"reply": "Good to see you! Quick one to warm up — what did you get up to last weekend?",
                "drill": [{"pattern": "tense-past-simple", "question": "What did you do last weekend?"},
                          {"pattern": "article-missing-the", "question": "How was the commute today?"}]}
    if '"suggestions"' in prompt:
        return {"suggestions": [
            {"title": "Coffee chat with a Google recruiter", "hook": "because you told Kai about your friend at Google",
             "kai_role": "Sarah, a friendly Google recruiter", "setting": "a cafe near campus",
             "learner_role": "yourself — an engineer exploring roles",
             "objective": "introduce your background and ask two good questions",
             "beats": ["small talk about your mutual friend", "describe your last project",
                       "ask about the team", "wrap up politely"],
             "target_patterns": ["tense-past-simple"], "target_vocab": ["referral"],
             "seeded_from": [], "wildcard": False},
            {"title": "Explaining your hackathon demo", "hook": "because you're building fluent-me",
             "kai_role": "a curious judge", "setting": "demo booth", "learner_role": "the builder",
             "objective": "pitch the project in plain words", "beats": ["what it does", "why memory matters", "the voice trick"],
             "target_patterns": [], "target_vocab": ["spaced repetition"], "seeded_from": [], "wildcard": False},
            {"title": "Ordering at a ramen place", "hook": "wildcard", "kai_role": "the chef",
             "setting": "a tiny ramen bar", "learner_role": "a hungry customer",
             "objective": "order and make small talk", "beats": ["order", "ask a question", "compliment"],
             "target_patterns": [], "target_vocab": [], "seeded_from": [], "wildcard": True},
            {"title": "Calling your landlord about a leak", "hook": "wildcard practical",
             "kai_role": "the landlord", "setting": "phone call", "learner_role": "tenant",
             "objective": "describe the problem and agree next steps", "beats": ["describe", "negotiate", "confirm"],
             "target_patterns": [], "target_vocab": [], "seeded_from": [], "wildcard": True}]}
    if '"questions"' in prompt and "interview" in prompt.lower():
        return {"company": "Google", "role": "Solutions Engineer",
                "interviewer": "Alex, a senior engineer — friendly but probing",
                "questions": [
                    {"i": 0, "q": "Tell me a bit about yourself.", "kind": "opener", "wants": None},
                    {"i": 1, "q": "Tell me about a time you debugged something under pressure.", "kind": "behavioral", "wants": "STAR"},
                    {"i": 2, "q": "How would you explain an API rate limit to a frustrated customer?", "kind": "role", "wants": "clarity"},
                    {"i": 3, "q": "Why this team?", "kind": "motivation", "wants": None}],
                "target_vocab": ["stakeholder", "trade-off"]}
    if '"sections"' in prompt:
        return {"title": "Demo pitch", "audience": "hackathon judges, technical, short attention",
                "sections": [{"i": 0, "title": "Hook", "notes": "why memory matters", "target_sec": 30},
                             {"i": 1, "title": "Live demo", "notes": "own-voice correction", "target_sec": 60},
                             {"i": 2, "title": "Close", "notes": "the ask", "target_sec": 30}],
                "qa": {"n": 2, "stance": "skeptical but fair"}}
    if '"coach_verdict"' in prompt:
        return {"coach_verdict": "Solid content and honest energy; your answers land better when you finish with the result.",
                "top_fixes": ["Land the Result — say the number.", "Cut the fillers in openings."]}
    if '"summary"' in prompt and '"sendoff"' in prompt:
        return {"summary": "Mock session: chatted about the hackathon; solid effort on past tense.",
                "topics": ["hackathon"], "facts": [{"text": "is building a hackathon project", "kind": "project"}],
                "best_moment": "I went there yesterday with my friend.",
                "sendoff": "That hackathon story was your best English tonight — come back and tell me how the demo went."}
    if '"scores"' in prompt:
        out = {"reply": "Nice! What did you two end up doing there?",
               "scores": {"grammar": 78, "vocab": 72}, "complexity": 3,
               "errors": [{"span": "I go there yesterday", "type": "tense", "severity": "major",
                           "correction": "I went there yesterday", "explanation": "past needs went",
                           "pattern": "tense-past-simple", "pattern_label": "past simple tense"}],
               "recast": "I went there yesterday with my friend.",
               "native": "Yeah, I swung by yesterday with a friend.",
               "elicited": {}, "wishlist": []}
        if "MOCK INTERVIEW" in prompt:
            out.update({"structure": {"s": True, "t": True, "a": True, "r": False, "score": 65,
                                      "note": "missing the result"},
                        "polished": "I led the migration of our billing service, cut latency forty percent, and shipped it two weeks early.",
                        "next_move": "advance", "native": None})
        if "PRESENTATION COACH" in prompt:
            out.update({"clarity": 78, "clarity_note": "clear point, weak landing",
                        "polished": "Duolingo teaches everyone the same course. Kai remembers you — your mistakes, your life — and builds tonight's practice from them.",
                        "reply": "Strong open; land the last sentence slower and it doubles in weight.", "native": None})
        if "SCENE (roleplay" in prompt:
            out.update({"beats_done": [0], "scene_state": "ongoing"})
        return out
    return {"reply": "Hey, good to see you again! What are you building these days?"}
