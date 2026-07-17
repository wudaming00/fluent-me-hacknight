# fluent-me v2 — Design (synthesized 2026-07-16)

> 综合 5 轴并行设计 + 对抗评审 16 条裁决。构建者是 Claude(Opus)，所以评审按"人类6小时"
> 做的纯时间性 CUT 大多被推翻；其**正确性裁决**(C1-C16)和**诚实性修正**(H1-H10)全部采纳。
> 设计原语：turn 循环永远只有 1 次 haiku 调用；额外 LLM 调用只发生在 setup(建议/intake) 和会话结束。

## 0. 产品论题与 demo 弧线

**论题**：语言学习的两大缺失是"记得你的老师"和"够得着的模仿目标"。fluent-me 用三层记忆
(错题SRS/画像/情景) 解决前者，用你自己的克隆声说流利版解决后者 (speech research: 自己声音的
母语口音版是异常有效的模仿目标——我们用 IVC 克隆近似它, 措辞见 H1)。

**Demo 弧线** (评审定稿，全部环节 <60s)：
录声纹 → 立刻听到"流利的自己" (O3 payoff) → 对话, 犯错 → 一条 focus 纠错 + 自己声音的修正
→ ECHO 跟读, 看到 vs 自己的进步 → 场景练习: Kai 用**你的生活**生成的场景 → Progress 页:
每张错题卡的遗忘曲线实时衰减 → /me 页: "这就是 Sauna 要存的记忆, 我控制它"。

## 1. 关键裁决 (含对评审的推翻)

| # | 决定 | 依据 |
|---|---|---|
| R1 | **FSRS-lite 上** (推翻评审 CUT) | 用户点名遗忘曲线; 本机 data/ 为空, 零迁移风险 |
| R2 | Echo 学分 = 只完成学习步 (S<1→1, 即 10min→1d "fast-track ⚡"); S≥1 的卡 echo 零学分, due 顺延 4h。晋级必须靠对话中真实钓出 | C2 折中: 模仿≠检索, 但保留可见 payoff |
| R3 | 热身 drill 独立成 "Quick review" 会话类型; 默认 Start 走开场宽限(前2轮不亮分/不弹纠错, 后台照常记) | 解 C7 矛盾 |
| R4 | 身份 facts 单一存储: `profile["facts"] = [{text, kind, use, ts, src}]`; briefing 只放 use:true | C5 |
| R5 | 三模式全建 (scenario/interview/presentation); presentation 的 Q&A 复用 scenario delta | 用户点名 |
| R6 | 传输 = NDJSON StreamingResponse 单 POST; 老单 JSON 路径删除 | C4, lat 设计 |
| R7 | 判卷加固: timeout 25s (presentation 40s) + canned fallback + reply 正则抢救; API-key 直连为主, CLI 加 `--strict-mcp-config` 兜底 | C13 |
| R8 | Sauna = 诚实集成: workspace-markdown 导出 (他们文档的记忆格式) + **MCP server** (他们文档的官方扩展点, 用户已确认) ; 徽章只写 "local · Sauna-ready" 除非真连上 | H3 |
| R9 | 到期排序 `(-pinned, R升序, -hits)`; briefing 织入上限 3 张; 每卡每场最多钓 2 次 | C3+srs |
| R10 | 会话历史单一存储 `data/sessions.json` (含 skills 快照/cx_ema/卡片数); plateau 检测和全部图表都读它 | 合并 srs.D + ui.1.1 |
| R11 | 防鹦鹉: 学员本轮与上一条 recast 的 token 重叠 ≥0.6 → 该轮 elicited 不给 SRS 学分 (纯 Python, 不加判卷字段) | C10 精简版 |
| R12 | 判卷 JSON: reply 放第一个 key; errors ≤3; scene 字段仅在对应模式注入 | C10 |
| R13 | 克隆不可用 (free tier / 断网) → fix 音频用导师声 + 徽章 "Kai's voice — your twin is offline" | 现实: 当前 key 无 IVC |
| R14 | 老 /profile 302 → /progress; profile.html 删除 | C8 |

## 2. 架构

```
浏览器 (vanilla JS, 无CDN)
  index.html    onboarding(O1身份→O2录声→O3 payoff) + 4模式练习 + streaming reader + echo
  progress.html 趋势/日历/CEFR/会话史/卡片墙(实时R衰减)/奖杯架
  me.html       facts开关/goals/wishlist/voice/episodes + Sauna 面板
        │ NDJSON stream + JSON REST
FastAPI :8901 (app.py)
  ├─ stt.py     Scribe v2 (词级 logprob → pron proxy + unclear_words) / whisper 兜底 / mock
  ├─ brain.py   1-call 判卷 (模式感知) · judge_safe · greeting(+drill) · summary(+sendoff) · 场景prompts
  ├─ scenes.py  场景引擎: 建议生成(缓存) · interview/present intake · beat 追踪 · 模式报告
  ├─ scoring.py 4维: grammar/vocab←Claude, fluency/pron←ASR信号 · echo 对比
  ├─ memory.py  FSRS-lite SRS · facts/goals · sessions.json · XP等级 · plateau · briefing
  └─ sauna.py   workspace-markdown 导出 + (sauna_mcp.py) MCP server :8902
```

### 数据文件 (data/, gitignored)
`profile.json` (skills, xp, streak, name, native_lang, goals[], facts[], wishlist, gentle_mode,
fix_voice, cx_ema, turns_total) · `cards.json` (FSRS 字段见 §3) · `episodes.json` ·
`sessions.json` (历史快照, §6) · `scenes.json` (计划+历史) · `scene_suggestions.json` (缓存) ·
`voice/` · `sauna_export/` (markdown 镜像)

### 端点总表
| 端点 | 说明 |
|---|---|
| GET / /progress /me | 页面; /profile→302 /progress |
| GET /api/state | + name, onboarded, goal_suggestion, due_summary {n, weakest_R} |
| POST /api/setup | O1: {name, native_lang, goals[]} |
| POST /api/enroll | -t 90 (修 bug); 返回 transcript + 词数 |
| POST /api/enroll/eleven | IVC; free-tier 报错时返回 {error, reason:"tier"} 界面诚实提示 |
| POST /api/voice/payoff | O3: 模板句 (无LLM) → 克隆声 TTS; 无克隆→ 501 + 提示 |
| POST /api/session/start | {mode:"free"\|"review", scene_id?} 统一 _start_session(); review 模式 greeting 带 drill 问题 |
| POST /api/turn | **NDJSON 流**: received→stt→judge→tts×N→done / error{recoverable} |
| POST /api/echo | {recast, orig:{wps,fillers,pron}} + 音频 → STT + token对齐 + delta (无LLM) |
| POST /api/session/end | log_session + 模式分支报告 + sendoff |
| GET /api/scenes/suggest?regen= | 4 张个性化场景卡 (1 haiku, 缓存) |
| POST /api/scene/start | {suggestion_idx?\|scene?} → in-character 开场 |
| POST /api/scene/interview/intake | {jd_text?\|fact_ids?, role_hint?} → 面试计划 |
| POST /api/scene/present/intake | {outline_text, target_minutes} → 分节计划 |
| POST /api/scene/next | interview 跳题 / presentation 下一节 |
| GET /api/progress | sessions[], days{}, skills_now, level_progress, 计数 |
| GET /api/cards | 全卡 (含 S, D, last_seen, pinned) — R 由前端实时算 |
| POST /api/card/action | archive/unarchive/reset/prioritize |
| GET /api/me · POST /api/me/fact · /api/me/goal · /api/me/wishlist | 身份管理 |
| POST /api/voice/test | 试听克隆声 |
| POST /api/sauna/export | 手动触发 markdown 导出; 返回文件清单 |
| POST /api/dev/expire · /api/dev/seed | demo 工具 |

### NDJSON wire (lat 设计定稿)
每行一个 JSON, 必有 `stage` + `t`(ms)。顺序: received → stt{heard, engine} →
judge{reply, recast, native, errors, scores, fl_meta, pr_meta, elicited, level, xp, lv,
hunting[{pattern,R,due_in}], scene?, echo_offer?, degraded} → tts{kind, audio, engine}×0-2 →
done{ms}。error{at, recoverable}: 不可恢复即终止流。
客户端: fetch + ReadableStream, TextDecoder(stream:true), \n 分帧带 carry buffer, 每行独立
try/catch, 60s watchdog AbortController。音频: 单队列顺序播, PTT 按下 = barge-in 全停。
**内存写入恰好一次** (judge 事件前), 之后的失败只降级展示、不伤数据。

## 3. SRS v2 — FSRS-lite (srs 设计定稿)

每卡: `S`(稳定性, 天) `D`(难度 1-10) `last_seen` `due_at` `clean_streak` `hits` `status` `pinned` `context{scene_id,mode}?`
```
R(t) = 1 / (1 + t/(9·S))          t = 距 last_seen 的天数; R(S)=0.90
到期 = last_seen + S·86400 (即 90% 保持率处安排复习)
新卡:  S=0.25, D=5, due=now+600 (10分钟)
成功(真实钓出): S<1 → S=1;  否则 S ×= clamp((1+2.2·(1.5−R))·(11−D)/6, 1.3, 3.5)
                D −= 0.3 (≥1);  streak++;  S≥21 且 streak≥3 → graduated
失误(软降级):   S = max(0.25, 0.30·S);  D += 1 (≤10);  streak=0;  due=now+600 (本场修复窗)
Echo 成功:     S<1 → S=1 (⚡fast-track, 唯一学分);  否则仅 due=max(due, now+4h)
```
诚实措辞 (H4/H5): "FSRS-inspired defaults, not a fitted model"; 强度条 = 调度可视化。
`/api/dev/expire`: due=now−1 且 last_seen 回拨到 R≈0.5, 让衰减肉眼可见。

## 4. 评分与改进迭代

- 4 维不变; **unclear_words** (Scribe 词级 logprob < −0.35) 作词级发音标注 chips
- **Focus 纠错剂量**: 服务端选 1 条 (到期卡匹配 > severity > 首条), 其余静默落卡;
  UI: 1 个 errchip + "+N noted quietly"
- **Echo 闭环**: 出现 recast 且 (卡到期或 hits≥2 或 blocking) 且 complexity≥2 且
  距上次 echo ≥3 轮 且 非宽限期 → judge 事件带 echo_offer; PTT 变 ECHO 态; 一次机会无重试;
  结果只报方向+粗delta ("faster, fewer fillers than your first try — rough single-sentence signal")
- **XP** = composite/10 × complexity + 5/卡晋级 + 20/毕业; **Lv** = floor((xp/100)^(1/1.6));
  CEFR 与 Lv 两种货币分开 (attainment vs effort), CEFR 永远标 estimate
- **i+1**: cx_ema(α=0.3) 进 briefing "邀请高一档结构"; **plateau**: 近3场 avg 波动≤3 且
  cx_ema 增幅<0.2 且 avg≥75 → briefing 注入推一把指令 + UI 紫色徽章

## 5. 三模式 (modes 设计定稿, 共享场景引擎)

Scene doc / interview plan / presentation plan 的 JSON 形状按 modes 设计原文 (§1b, 2a, 3a)。
判卷 delta 只在对应模式注入; 全模式共享同一评分+记忆管线。
- **scenario**: beats 3-6, 由 use:true facts + 到期卡 + wishlist 生成; "beats_done"/"scene_state" 两字段
- **interview**: structure(STAR)/polished/next_move; **polished 走 f_fix 克隆声道 = killer moment**;
  delivery 全部本地算 (wpm/fillers/时长); 报告措辞 "coaching feedback, not a hiring assessment" (H7)
- **presentation**: 分节计时本地算; clarity + polished ≤60词; 结束后 phase=qa 复用 scenario delta
  (kai_role="skeptical audience member"); 二次排练输出 vs_last_run delta

## 6. 三页面

- **/** Practice: onboarding 三态 → 模式页签 (Free talk · Quick review · Scenario · Interview ·
  Presentation) → 会话。宽限期 / focus 纠错 / echo / beat 条 / 分数面板默认折叠。
- **/progress**: tiles(xp/streak/turns/cards/CEFR gauge) · composite 线图+XP 条 (共x轴双图,
  不用双轴) · 4 技能 sparkline 小倍图 (blue/violet 永不同图, 调色板校验结论) · GitHub 点日历 ·
  会话史展开行 · **卡片墙** (R 实时衰减条: ≥.6 aqua "strong" / .3-.6 yellow "fading" /
  <.3 red "almost gone"; 措辞 "estimated") · 奖杯架。种子数据页面角标 "demo data" (H9)。
- **/me**: facts 开关 (◉/○, off = Kai 永不提) · goals(带日期→Practice 页模式建议 chip) ·
  wishlist · voice 面板 (状态/试听/重录) · episodes 时间线 · **Sauna 面板**: 导出的 workspace
  markdown 预览 + MCP server 状态与接入说明; 徽章 "● local · Sauna-ready"。

sessions.json 记录: {ts, date, mode, turns, avg, xp_gained, cards_created/advanced,
new/advanced_patterns[], best_moment, summary, topics, skills快照, cx_ema, level}

## 7. Sauna 集成 (研究结论: 无公开 API; 方向反转 — Sauna 消费你的 MCP)

1. **sauna.py**: 每次 save/end_session 把记忆镜像成 Sauna 文档约定的 workspace 结构:
   `data/sauna_export/ABOUT.md` (学习者画像) + `memory/mistake-cards.md` + `memory/identity.md`
   + `sessions/YYYY-MM-DD-HHMM.md` (情景记忆)。这是他们 /learn/memory 描述的 dual-memory
   (durable curated + episodic) 的直接映射 — 评委话术现成。
2. **sauna_mcp.py**: FastMCP (streamable-http :8902) tools: `get_learner_profile` ·
   `get_due_cards` · `get_recent_sessions` · `add_identity_fact` · `get_progress_summary`。
   Sauna "Wire in any MCP server" 即插。公网接入用 `cloudflared tunnel` (README 写明)。
3. 现场若发布 API → SaunaStore._push 填上 (钩子保留)。

## 8. 诚实性台账 (评审 H1-H10, 全采纳)

自声学习说"unusually effective imitation target, approximated with a clone" 不说 best ·
echo delta 报方向不报精度 · Sauna 徽章不假装已同步 · "FSRS-inspired, not fitted" ·
强度条=调度可视化 · 延迟数字只引用当天实测 · 面试反馈永不说 hire signal · pron=ASR proxy
到处标注 · 种子数据标 "demo data" · CEFR 永远 "estimate, not a certified test"

## 9. 现场风险 (评审风险表, 全采纳)

判卷挂 → API key 直连 + 25s timeout + canned 降级徽章 (把故障变成韧性展示) ·
断网 → 手机热点 + 预生成 (场景建议/payoff mp3/一轮完整音频) + 30s 录屏棺材角 ·
autoplay → 单队列 + 去 autoplay 属性 + 上台浏览器实测 · 空状态 → seed_demo.py + data.bak ·
克隆差/无权限 → 提前安静房间录 + 预渲染最佳 take + R13 降级。**当前 free tier 无 IVC —
需 Starter 升级或现场促销 key。**
