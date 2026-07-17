# Fluent Me — a tutor that remembers you, in your own voice

Built for **ElevenLabs x Sauna Hack Night** (2026-07-16, SF). The prompt was "AI with
memory and voice" — this is the literal answer: a spoken-English tutor with real memory.

**The idea.** Hold a button and chat with Kai. Every mistake becomes a spaced-repetition
memory card — and instead of flashcards, Kai *engineers the conversation* so the natural answer
to his next question forces you to face that exact pattern again, right before you'd forget it.
Corrections are spoken back **in your own cloned voice**: you literally hear the fluent version
of yourself, already existing. Then the ECHO loop asks you to try it on — and scores you against
*yourself five seconds ago*, the only comparison that can't hurt.

**Why it's different.**
- *Conversational spaced repetition* — FSRS-inspired scheduling (per-card stability + live
  retrievability decay), hidden inside small talk. Never a flashcard. Duolingo drills everyone
  the same; Kai remembers *you* and sets traps.
- *Your life is the curriculum* — identity facts Kai learns (or you toggle on /me) generate
  personalized roleplay scenes: "coffee chat with a Google recruiter — because you told Kai
  about your friend there." Plus full **Interview prep** (STAR feedback + the killer moment:
  *your own answer, polished, in your own voice*) and **Presentation rehearsal** (per-section
  timing, clarity coaching, skeptical-audience Q&A).
- *Honest scoring* — grammar/vocab by an LLM judge; fluency from pace/pauses/fillers;
  pronunciation is an **ASR-confidence proxy and labeled as such** (Scribe v2 word-level
  logprobs — it even flags which words came out fuzzy).
- *Anti-grinding XP* — XP = score × sentence-ambition(1-5). Attempting conditionals beats
  repeating "Yes, I like it." Echoing Kai's correction back? Detected, zero SRS credit —
  only genuine retrieval in later conversation levels a card up.

**Stack.** ElevenLabs end-to-end for voice (Scribe v2 STT in — word timestamps + confidence,
flash v2.5 TTS + instant voice clone out), Claude haiku judging in ONE call per turn
(NDJSON-streamed: transcript on screen in ~1.5s, scores follow), Sauna integration via its
documented extension point (**we expose an MCP server** with the learner's memory + mirror it
in Sauna's dual-memory workspace-markdown format). FastAPI + vanilla JS, zero build, zero CDN.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # + ffmpeg on PATH
cp .env.example .env                       # put ELEVENLABS_API_KEY / ANTHROPIC_API_KEY here
cd server && ../.venv/bin/uvicorn app:app --port 8901                # → http://localhost:8901
```

First run walks you through onboarding: name/goals → 60s voice enrollment (fun script) →
**the payoff: you hear yourself, fluent, within seconds.** Then talk.

No keys at all? `FLUENTME_MOCK=1` runs the entire product with stub STT/judge/TTS — every
screen and flow works for development.

### Judge tiers (auto-selected)
`ANTHROPIC_API_KEY` direct API (primary) → `claude` CLI (`--strict-mcp-config`, no MCP boot
tax) → `FLUENTME_MOCK`. A hung judge degrades to a canned in-scene reply + honest badge —
conversation never dies on stage.

### Sauna (no public memory API — the integration direction is inverted)
Sauna consumes MCP servers you bring. So:
```bash
.venv/bin/python server/sauna_mcp.py            # MCP (streamable-http) on :8902/mcp
cloudflared tunnel --url http://localhost:8902  # public URL → Sauna → connect MCP server
```
Tools: `get_learner_profile · get_due_cards · get_recent_sessions · get_progress_summary ·
add_identity_fact` (that last one writes back — tell Sauna a fact, Kai uses it next session).
`data/sauna_export/` also mirrors all memory in Sauna's documented workspace format
(ABOUT.md + memory/*.md + sessions/*.md). Badge says "local · Sauna-ready" — never fakes a sync.

## 现场 runbook (venue)

```bash
# 开门前
.venv/bin/python server/seed_demo.py --force   # 满血种子数据 (页面自带 "demo data" 角标)
cp -r data data.bak                            # 一键还原: rm -rf data && cp -r data.bak data
export ELEVENLABS_API_KEY=...  ANTHROPIC_API_KEY=...
# 安静房间录声纹 (/, onboarding O2) → 升级 Starter 或现场促销 key 后:
curl -X POST :8901/api/enroll/eleven           # IVC 克隆 (free tier 会 402, 界面有诚实降级)
# 预生成: 打开 Scenario tab 一次 (建议缓存) + Interview intake 一次
curl -X POST :8901/api/dev/expire              # 上台前: 全部卡片到期 + 强度条掉到红色
```

Demo 弧线 (≈4 分钟): 录声纹→立刻听到流利的自己 → Free talk 犯错→一条 focus 纠错 + 自己声音的
修正 → ECHO 跟读→"vs 你自己"的进步 → Scenario: 从你的真实生活生成的场景 → /progress: 每张卡的
遗忘曲线实时衰减 → /me: "这就是 Sauna 式记忆, 我控制它" + MCP server 现场连 Sauna。

### 已知降级路径 (全部诚实标注)
- 无 IVC 权限 (free tier) → 修正句用 Kai 声 + 徽章 "your twin is offline"
- 判卷挂 → canned 回复 + "judge offline" 徽章, 对话继续, 该轮不计分不落卡
- Scribe 挂 → 本地 whisper (:8123, 家里 GPU 机) → 都挂才报错
- 断网 → FLUENTME_MOCK=1 全流程照跑 (演 UI, 不演智能)

## Architecture

```
browser (vanilla JS, no CDN) ── NDJSON stream ──▶ FastAPI :8901
  /          practice: onboarding · 5 modes · echo loop · live hunt bars
  /progress  trends · streak calendar · CEFR gauge · card wall (live R decay) · trophies
  /me        identity facts (per-fact toggle) · goals · voice · Sauna panel
        ├─ stt.py     Scribe v2 (word logprobs → pron proxy + fuzzy-word flags) / whisper / mock
        ├─ brain.py   1 haiku call/turn: reply+errors+scores+pattern-keys+elicit check
        │             (+scenario beats / interview STAR+polished / presentation clarity)
        ├─ scenes.py  scene engine: personalized suggestions · interview/present intake · reports
        ├─ scoring.py 4 dims (honest signal split) · echo comparison · anti-parrot overlap
        ├─ memory.py  FSRS-lite SRS (S/D, R=1/(1+t/9S)) · facts/goals · sessions history · XP/Lv
        └─ sauna.py   workspace-markdown export ─┐
           sauna_mcp.py MCP server :8902 ◀───────┴── Sauna plugs in here
```

Design doc: [DESIGN.md](DESIGN.md) — the adversarial-review rulings and the honesty ledger
(what we deliberately do NOT claim: no "best method" claims, no fake sync badges, pron is a
proxy, CEFR is an estimate, FSRS constants are hand-picked defaults, seeded data is labeled).
