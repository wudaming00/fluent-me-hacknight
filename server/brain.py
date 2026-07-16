# Claude 判卷层 — 每轮一次调用同时产出: 导师戏内回复 + 纠错 + 评分 + pattern 归一 + 钓卡判定
# (合并成单调用是延迟预算的关键: haiku 一次 ~2-4s, 拆三次调用现场 demo 就冷场了)
import json
import subprocess

MODEL = "claude-haiku-4-5-20251001"


def _claude_json(prompt: str, timeout: int = 180) -> dict:
    r = subprocess.run(["claude", "-p", "--model", MODEL, prompt, "--output-format", "text"],
                       capture_output=True, text=True, timeout=timeout)
    txt = r.stdout.strip()
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


def _briefing_block(briefing: dict) -> str:
    cards = briefing.get("due_cards", [])
    lines = []
    if cards:
        lines.append("REVIEW TARGETS (weave these into conversation naturally — engineer questions "
                     "whose natural answer requires the pattern; NEVER quiz or name the pattern aloud):")
        for c in cards:
            lines.append(f'- {c["pattern"]}: {c["label"]} | they previously said "{c["example_wrong"]}" '
                         f'→ should be "{c["example_right"]}" | failed {c["hits"]}x')
    eps = briefing.get("recent_episodes", [])
    if eps:
        lines.append("WHAT YOU REMEMBER ABOUT THEM (reference casually like an old friend would):")
        for e in eps:
            facts = "; ".join(e.get("facts", [])[:4])
            lines.append(f'- {e["date"]}: {e.get("summary", "")} | facts: {facts}')
    if briefing.get("wishlist"):
        lines.append("WORDS THEY WANTED TO LEARN: " + ", ".join(briefing["wishlist"]))
    lines.append(f'Their level estimate: {briefing.get("level", "B1")} — pitch your language one notch above.')
    return "\n".join(lines)


def judge_turn(transcript: str, convo: list, briefing: dict) -> dict:
    hist = "\n".join(f'{t["who"].upper()}: {t["text"]}' for t in convo[-8:]) or "(session just started)"
    targets = [c["pattern"] for c in briefing.get("due_cards", [])]
    prompt = f"""You are Kai — a sharp, warm English conversation tutor. You correct like a friend, not a textbook.

{_briefing_block(briefing)}

Conversation so far:
{hist}

Learner's new line (whisper transcript, may have ASR noise — don't punish obvious ASR artifacts): "{transcript}"

Do ALL of the following in ONE json:
1. Grade the line: grammar 0-100 (error count/severity vs sentence length), vocab 0-100 (word choice + naturalness).
2. List real errors only (max 4). Canonicalize each into a reusable kebab-case pattern key that would match the
   same mistake made in a different sentence (e.g. "tense-past-simple", "article-missing-the", "wc-make-vs-do",
   "prep-in-vs-on", "third-person-s"). Reuse an existing key from REVIEW TARGETS when it's the same pattern.
3. complexity 1-5: how ambitious was the attempted structure (1 = "Yes I like it", 5 = conditionals/relative clauses/nuance).
4. recast: minimal-edit corrected version (null if already natural). native: how a relaxed native speaker would
   phrase the same idea (null if recast already is).
5. elicited: for each review target pattern {targets} — ONLY if this line actually attempted that pattern,
   report true (used correctly) or false (failed again). Omit patterns not attempted.
6. reply: your next in-scene line. 1-2 spoken sentences. Stay on the learner's topic, usually end with a question.
   If a review target hasn't been elicited yet, steer the topic so the NEXT answer naturally requires it.
7. wishlist: if the learner asked how to say something ("how do I say...", said a Chinese word), list those items.

Return ONLY JSON:
{{"scores": {{"grammar": int, "vocab": int}}, "complexity": int,
"errors": [{{"span": "<their words>", "type": "tense|article|preposition|word-choice|plural|word-order|collocation|other",
"severity": "minor|major|blocking", "correction": "<fixed>", "explanation": "<≤8 words>",
"pattern": "<kebab-key>", "pattern_label": "<short label>"}}],
"recast": "<str|null>", "native": "<str|null>",
"elicited": {{"<pattern>": true|false}},
"reply": "<str>", "wishlist": ["<str>"]}}"""
    return _claude_json(prompt)


def greeting(briefing: dict) -> dict:
    prompt = f"""You are Kai — a warm English conversation tutor greeting a returning learner by voice.

{_briefing_block(briefing)}

Write your opening line: greet them, reference something specific you remember about them (if any),
and open with a question that naturally invites the FIRST review target pattern (never name the pattern).
If you know nothing yet, just warmly ask what they're building these days. 1-2 spoken sentences.
Return ONLY JSON: {{"reply": "<str>"}}"""
    return _claude_json(prompt)


def session_summary(convo: list) -> dict:
    hist = "\n".join(f'{t["who"].upper()}: {t["text"]}' for t in convo)
    prompt = f"""Distill this tutoring session into memory. Conversation:
{hist}

Return ONLY JSON:
{{"summary": "<2 sentences: what you talked about + how they did>",
"topics": ["<str>"],
"facts": ["<personal facts the learner revealed — projects, plans, people, preferences. Specific, reusable next session>"],
"best_moment": "<their single best sentence verbatim, or empty>"}}"""
    return _claude_json(prompt)
