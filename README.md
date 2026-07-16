# Fluent Me — a tutor that remembers you, in your own voice

Built for **ElevenLabs x Sauna Hack Night** (2026-07-16, SF). The prompt was "AI with
memory and voice" — this is the literal answer: a spoken-English tutor with real memory.

**The idea.** Hold a button and chat with Kai. Every mistake you make becomes a spaced-repetition
memory card — and instead of showing you flashcards, Kai *engineers the conversation* so the natural
answer to his next question forces you to face that exact pattern again. Answer it right and the card
levels up (10min → 1d → 3d → 7d → 21d). Corrections are spoken back **in your own cloned voice**:
you literally hear the fluent version of yourself, already existing.

**Why it's different.**
- *Conversational spaced repetition* — SRS hidden inside small talk, never a flashcard. Duolingo
  drills you; Kai remembers you and sets traps.
- *Episodic memory* — Kai recalls what you talked about last time ("did your friend at Google get
  back to you?") and greets you like a friend who was paying attention.
- *Honest scoring* — 4 dimensions from two signal paths: grammar/vocab graded by an LLM judge,
  fluency/pronunciation computed from the ASR signal itself (pronunciation is labeled as an
  ASR-confidence *proxy*, because that's what it is).
- *Anti-grinding XP* — XP = score × sentence-ambition(1-5). Attempting hard structures beats
  repeating "Yes, I like it."

**Stack.** ElevenLabs end-to-end for voice (Scribe STT in, TTS + instant voice clone out), Claude for
judging/coaching, Sauna for persistent memory (local JSON as offline fallback). FastAPI + vanilla JS.

## Quickstart (laptop / venue mode)

```bash
pip install -r requirements.txt          # plus: ffmpeg on PATH, `claude` CLI logged in
export ELEVENLABS_API_KEY=...            # enables Scribe STT + TTS automatically
cd server && uvicorn app:app --port 8901 # → http://localhost:8901
```

Then in order: ① record 15s on `/api/enroll` (or drop an existing sample at
`data/voice/mirror_user.wav`) → ② `curl -X POST :8901/api/enroll/eleven` to clone the voice →
③ Start session and talk. Without the key it falls back to local whisper (:8123) + Higgs TTS
(:8124) if you run them — that's dev mode on the home GPU box.

---

以下是中文 runbook（现场操作手册）。

## 跑起来（家里 GPU 机 dev 模式）

```bash
cd ~/fluent-me/server && ~/claude-voice/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8901
# → http://localhost:8901        依赖: 本地 whisper :8123 + Higgs TTS :8124 (boson-demo 同一套)
```

## 架构（现场只换两个适配面）

```
浏览器 PTT ──▶ /api/turn
  ├─ STT: stt.py 适配器 —— 有 key: Scribe(正文+词级时间戳) ∥ whisper(只取置信度) 并行双跑;
  │        无 key: whisper 单跑。export key 即自动切换, 零代码改动
  ├─ 判卷: brain.py → claude haiku 单次调用 (回复+纠错+评分+pattern归一+钓卡判定)
  ├─ 评分: scoring.py (grammar/vocab←Claude, fluency/pron←STT信号)
  ├─ 记忆: memory.py 三层 (错题卡SRS / 画像EMA / 情景记忆)   ◀── Sauna 接这里
  └─ TTS: tts.py 双路并行 (导师声 + 你的克隆声)              ◀── ElevenLabs 接这里
```

讲给评委的点: **声进声出全链路 ElevenLabs**(Scribe 进, TTS 出), whisper 只在后台补
Scribe 不提供的置信度信号(pron 代理分), 且与 Scribe 并行不加延迟。

### 评分机制
- **grammar / vocab**: Claude 判卷 0-100 + 结构化 errors（错误同时喂记忆层）
- **fluency**: 语速 (1.8~3.4 wps 自然带) × 填充词惩罚；接上 Scribe 后自动升级三信号
  （+词间停顿占比，>0.5s 间隙；且 Scribe 转写更逐字，um/uh 不会像 whisper 那样被洗掉）
- **pron**: whisper avg_logprob 置信映射 —— UI 标注 "ASR proxy"，评委问就大方承认不是音素级
- 防噪声：<4 词的句子 fluency/pron 不计入画像；技能走 EMA(α=0.25) 单句好运不跳级
- **XP = 综合分/10 × complexity(1-5)**：说难句子成长更快，防"刷 Yes."

### 记忆机制（Sauna 展示位）
- **L1 错题卡**：错误 → Claude 归一 pattern key（如 `tense-past-simple`）→ 同类合卡。
  Leitner SRS：犯错归零 10min 后到期；被钓出且用对 → 10min→1d→3d→7d→21d；box4 连对 3 次毕业
- **L2 画像**：四维 EMA + CEFR 等级估计 + XP/streak/wishlist
- **L3 情景记忆**：每场会话蒸馏 摘要+个人事实。下次开场 Kai 主动提起
- **闭环**（差异化核心）：到期卡进开场 briefing → Kai 设计出"自然回答必然用到该 pattern"
  的问题 → 判卷层检测是否用对 → SRS 晋级。全程不点破，不出闪卡

## 昨晚已端到端验证 ✅

1. 造错句音频投喂 → 3 个错误全抓到并归一（tense/prep/third-person-s），评分合理
2. 结束会话 → episodes 蒸馏出 facts（"preparing for job interviews", "friend at Google"）
3. **Day-2 效果**：新会话开场 = "great to see you again! when you met your friend from
   Google at the library, what did you two talk about?" —— 同时命中情景记忆 + 钓过去时卡
4. 用对过去时回答 → 两张卡 box 0→1（间隔跳 1 天），没练到的卡继续挂着被钓

## 现场 2 小时排期

| 时间 | 事 |
|---|---|
| 0:00-0:20 | 拿 ElevenLabs credits → `export ELEVENLABS_API_KEY=...`（TTS 和 Scribe STT 同 key 自动切换）→ `curl -X POST :8901/api/enroll/eleven`（用已有声纹样本克隆，不用重录）→ 选导师声换 `ELEVEN_TUTOR_VOICE`；验证 turn 响应里 `stt_engine` 显示 scribe |
| 0:20-0:50 | 看完 Sauna demo 后填 `memory.py` 里 `SaunaStore._push/_pull`（本地 JSON 永远兜底，断网不死）|
| 0:50-1:30 | 真麦克风联调 + 按实际延迟修 UI 提示；`claude -p hi` 预热 CLI |
| 1:30-2:00 | 排练 demo 剧本 ×2；`data/` 里留一份排练好的记忆状态备份 |

## Demo 剧本（3 分钟）

1. （30s）痛点：Duolingo 不认识你。演示页面，报家门："这是我今天下午录的 15 秒声纹"
2. （60s）现场说两句带错的英语 → 评分面板 + 错题卡生成 + **修正句用我自己的声音放出来**（hook #1）
3. （30s）`curl -X POST :8901/api/dev/expire` 假装第二天，End session → Start session
4. （60s）**Kai 记得我上次说的事，并且开口第一句就在钓我的旧错误**（hook #2，指着 "secretly hunting" 面板讲）→ 答对 → 卡片当场晋级
5. 收尾：评分诚实（pron 标 proxy）、记忆结构（Memory 页）、Sauna 集成点

## 已知坑

- 延迟：本地 TTS 一轮 50-70s；换 ElevenLabs turbo 后预计 ~15-20s（大头变成 haiku 判卷 ~12s + STT ~4s）。开场前跑一次 `claude -p hi` 预热
- whisper 会"好心"纠正部分口误（meeted→meet），发音错误会被 ASR 洗掉一部分 —— pron 是 proxy 的另一个原因，讲评分时主动说
- `/api/dev/expire` 是 demo 后门，赛后删
- ElevenLabs IVC 免费档有克隆数量限制，voice_id 存 `data/eleven_voices.json` 只克隆一次
