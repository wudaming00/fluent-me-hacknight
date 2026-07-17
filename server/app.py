# fluent-me — 有记忆的口语陪练, 用你自己的声音示范流利版
# run: cd server && ../.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8901
#
# turn 管线 = NDJSON 流式响应: received → stt → judge → tts×N → done
#   转写 ~1.5s 先上屏, 评分/纠错后到, 音频就绪即播 — 感知延迟贴零。
#   内存写入恰好一次 (judge 事件之前); 之后的失败只降级展示, 不伤数据。
import json
import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent.parent

# .env 自动加载 (ELEVENLABS_API_KEY / ANTHROPIC_API_KEY / FLUENTME_MOCK ...), 不覆盖已有环境
_envf = BASE / ".env"
if _envf.exists():
    for line in _envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import brain          # noqa: E402
import memory         # noqa: E402
import sauna          # noqa: E402
import scenes         # noqa: E402
import scoring        # noqa: E402
import stt as stt_mod # noqa: E402
import tts            # noqa: E402

PAGES = Path(__file__).resolve().parent / "pages"
AUDIO = BASE / "data" / "audio"
AUDIO.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="fluent-me")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.mount("/audio", StaticFiles(directory=str(AUDIO)), name="audio")

store = memory.make_store()
EX = ThreadPoolExecutor(max_workers=4)          # 模块级复用, TTS 双路并行

FRESH_SESSION = {"active": False, "mode": "free", "scene": None, "cursor": 0, "phase": "main",
                 "convo": [], "turns": [], "xp_gained": 0, "cards_created": 0, "cards_advanced": 0,
                 "new_patterns": [], "advanced_patterns": [], "elicit_attempts": {},
                 "drill": [], "drill_i": 0, "turn_i": 0, "last_echo_turn": -9, "last_recast": "",
                 "scene_log": []}
SESSION = json.loads(json.dumps(FRESH_SESSION))

ECHO_MIN_GAP = 3      # 距上次 echo 至少 3 轮
GRACE_TURNS = 2       # 开场宽限: 前 2 轮不弹纠错不亮分 (后台照常记)


def _to_wav(upload_bytes: bytes, tag: str, max_sec: int = 45) -> Path:
    raw = AUDIO / f"{tag}_raw"
    raw.write_bytes(upload_bytes)
    wav = AUDIO / f"{tag}.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(raw),
                    "-t", str(max_sec), "-ac", "1", "-ar", "16000", str(wav)],
                   check=True, capture_output=True)
    raw.unlink(missing_ok=True)
    return wav


def _page(name: str):
    # no-store: 今晚频繁热更, 浏览器缓存旧 UI 是 demo 隐形杀手
    return FileResponse(str(PAGES / name), media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    return _page("index.html")


@app.get("/progress")
def progress_page():
    return _page("progress.html")


@app.get("/me")
def me_page():
    return _page("me.html")


@app.get("/profile")
def profile_redirect():
    return RedirectResponse("/progress")


@app.get("/demo")
def demo_page():
    return _page("demo.html")


# ============================================================ state / setup
@app.get("/api/state")
def state():
    def up(port):
        s = socket.socket(); s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port)); return True
        except OSError:
            return False
        finally:
            s.close()
    p = store.profile
    now = int(time.time())
    due = store.due_cards(limit=10)
    return {
        "profile": {k: p[k] for k in ("name", "native_lang", "xp", "streak", "turns_total",
                                      "skills", "gentle_mode", "fix_voice", "wishlist")},
        "level": memory.level_of(p["skills"]),
        "lv": memory.xp_level(p["xp"]),
        "onboarded": bool(p.get("name")),
        "enrolled_local": tts.USER_REF.exists(),
        "eleven_key": bool(os.environ.get("ELEVENLABS_API_KEY")),
        "eleven_cloned": bool(tts._eleven_voices().get("user")),
        "mock": bool(os.environ.get("FLUENTME_MOCK")),
        "judge": "api" if os.environ.get("ANTHROPIC_API_KEY") else
                 ("mock" if os.environ.get("FLUENTME_MOCK") else "cli"),
        "services": {"stt_local": up(8123), "tts_local": up(8124), "sauna_mcp": up(8902)},
        "session_active": SESSION["active"], "session_mode": SESSION["mode"],
        "due_summary": {"n": len(due),
                        "weakest_R": round(min((memory.retrievability(c, now) for c in due), default=1.0), 2)},
        "cards_total": sum(1 for c in store.cards.values() if c["status"] == "learning"),
        "goal_suggestion": store.goal_suggestion(),
    }


@app.post("/api/setup")
async def setup(payload: dict):
    """O1: {name, native_lang, goals: [kind,...]} — goals 无日期先落 label 空的 goal。"""
    store.profile["name"] = str(payload.get("name", ""))[:40]
    store.profile["native_lang"] = str(payload.get("native_lang", ""))[:20]
    for kind in payload.get("goals", [])[:2]:
        store.upsert_goal({"kind": kind, "label": kind.title(), "active": True})
    store.save()
    return {"ok": True}


# ============================================================ 声纹 / 克隆 / payoff
@app.post("/api/enroll")
async def enroll(file: UploadFile = File(...)):
    """录 45-90s 声纹。旧版 -t 25 截断 bug 已修 (评审 C12)。"""
    raw = AUDIO / "enroll_raw"
    raw.write_bytes(await file.read())
    wav24 = AUDIO / "enroll.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(raw),
                    "-t", "90", "-ac", "1", "-ar", "24000", str(wav24)],
                   check=True, capture_output=True)
    raw.unlink(missing_ok=True)
    r = stt_mod.transcribe(str(wav24))
    transcript = r.get("text", "")
    if not transcript:
        return JSONResponse({"error": "no speech detected"}, status_code=400)
    import shutil
    shutil.copy(wav24, tts.USER_REF)
    tts.USER_REF.with_suffix(".txt").write_text(transcript, encoding="utf-8")
    return {"transcript": transcript, "dur": r.get("speech_dur", 0.0),
            "words": len(transcript.split())}


@app.post("/api/enroll/eleven")
def enroll_eleven():
    try:
        r = tts.enroll_eleven()
        if r.get("error"):                      # 无 key / 无样本: 也要走错误状态码, 前端 !r.ok 才能兜住
            return JSONResponse({**r, "reason": "setup"}, status_code=400)
        return r
    except Exception as e:
        msg = str(e)
        # free tier IVC 是 402 payment_required — 界面要给诚实的 "需要 Starter" 提示
        tier = any(m in msg.lower() for m in ("402", "payment", "paid_plan", "401", "403"))
        return JSONResponse({"error": msg[:200], "reason": "tier" if tier else "api"}, status_code=502)


PAYOFF = {
    "interview": "Hi, I'm {name}. Tell me about yourself? Sure — I'd love to. I've been waiting to say that smoothly for a long time.",
    "presentation": "Good morning everyone — I'm {name}, and today I'm going to show you something I'm genuinely proud of.",
    "default": "Hey, I'm {name}. Ask me anything — small talk doesn't scare me anymore.",
}


@app.post("/api/voice/payoff")
def voice_payoff():
    """O3: 注册后 8 秒内的第一声 "流利的自己"。模板句, 零 LLM 延迟。"""
    p = store.profile
    kinds = [g["kind"] for g in p.get("goals", [])]
    key = "interview" if "interview" in kinds else ("presentation" if "presentation" in kinds else "default")
    text = PAYOFF[key].format(name=p.get("name") or "there")
    out = AUDIO / f"payoff_{int(time.time())}"
    try:
        engine = tts.say(text, "user", out)
    except Exception:
        return JSONResponse({"error": "no voice engine available — set ELEVENLABS_API_KEY or run local TTS",
                             "text": text}, status_code=502)
    return {"text": text, "audio": f"/audio/{tts.audio_path(out).name}", "engine": engine,
            "cloned": engine.startswith("elevenlabs") and bool(tts._eleven_voices().get("user"))
                      or engine.startswith("higgs")}


@app.post("/api/voice/test")
async def voice_test(payload: dict | None = None):
    text = (payload or {}).get("text") or "This is my voice — fluent, clear, and confident. Pretty cool, right?"
    out = AUDIO / f"vtest_{int(time.time())}"
    try:
        engine = tts.say(text, "user", out)
    except Exception:
        return JSONResponse({"error": "no voice engine available"}, status_code=502)
    return {"audio": f"/audio/{tts.audio_path(out).name}", "engine": engine}


# ============================================================ 会话
def _start_session(mode: str, scene: dict | None = None):
    global SESSION
    SESSION = json.loads(json.dumps(FRESH_SESSION))
    SESSION.update({"active": True, "mode": mode, "scene": scene})
    briefing = store.briefing()
    drill_targets = None
    if mode == "review":
        due = store.due_cards(limit=memory.DRILL_MAX)
        drill_targets = briefing["due_cards"][:memory.DRILL_MAX] if due else None
    try:
        g = brain.greeting(briefing, mode=mode, scene=scene, drill_targets=drill_targets)
    except Exception:
        g = {"reply": "Hey! Good to see you again — what are you building these days?"}
    SESSION["drill"] = g.get("drill", []) if mode == "review" else []
    SESSION["drill_i"] = 0
    reply = g.get("reply", "Hey! Good to see you — what's new?")
    SESSION["convo"].append({"who": "tutor", "text": reply})
    ts = int(time.time() * 10) % 10_000_000
    out = AUDIO / f"greet_{ts}"
    try:
        engine = tts.say(reply, "tutor", out)
        audio = f"/audio/{tts.audio_path(out).name}"
    except Exception:
        engine, audio = "none", None
    return {"reply": reply, "audio": audio, "engine": engine, "briefing": briefing,
            "mode": mode, "scene": scene, "drill_n": len(SESSION["drill"]),
            "grace_turns": 0 if mode == "review" else GRACE_TURNS}


@app.post("/api/session/start")
async def session_start(payload: dict | None = None):
    mode = (payload or {}).get("mode", "free")
    if mode not in ("free", "review"):
        return JSONResponse({"error": "use /api/scene/start for scene modes"}, status_code=400)
    return _start_session(mode)


@app.get("/api/scenes/suggest")
def scenes_suggest(regen: int = 0):
    try:
        return scenes.suggest(store, regen=bool(regen))
    except Exception as e:
        return JSONResponse({"error": f"suggestion generation failed: {e}"}, status_code=502)


@app.post("/api/scene/start")
async def scene_start(payload: dict):
    """{suggestion_idx} | {plan_id} | {scene: <stub>}"""
    scene = None
    if "suggestion_idx" in payload:
        sugg = scenes.suggest(store).get("suggestions", [])
        idx = int(payload["suggestion_idx"])
        if idx >= len(sugg):
            return JSONResponse({"error": "no such suggestion"}, status_code=400)
        scene = scenes.build_scene(sugg[idx], "scenario")
    elif "plan_id" in payload:
        scene = scenes.get_plan(payload["plan_id"])
        if not scene:
            return JSONResponse({"error": "plan not found"}, status_code=404)
    elif "scene" in payload:
        scene = scenes.build_scene(payload["scene"], payload.get("mode", "scenario"))
    if not scene:
        return JSONResponse({"error": "nothing to start"}, status_code=400)
    return _start_session(scene.get("mode", "scenario"), scene)


@app.post("/api/scene/interview/intake")
async def interview_intake(payload: dict):
    try:
        return scenes.interview_intake(store, jd_text=payload.get("jd_text", ""),
                                       fact_ids=payload.get("fact_ids"),
                                       role_hint=payload.get("role_hint", ""))
    except Exception as e:
        return JSONResponse({"error": f"intake failed: {e}"}, status_code=502)


@app.post("/api/scene/present/intake")
async def present_intake(payload: dict):
    try:
        return scenes.present_intake(payload.get("outline_text", ""),
                                     float(payload.get("target_minutes", 3)))
    except Exception as e:
        return JSONResponse({"error": f"intake failed: {e}"}, status_code=502)


@app.post("/api/scene/next")
def scene_next():
    """interview 跳题 / presentation 手动下一节。"""
    scene = SESSION.get("scene")
    if not (SESSION["active"] and scene):
        return JSONResponse({"error": "no scene session"}, status_code=400)
    if SESSION["mode"] == "interview":
        n = len(scene.get("questions", []))
        SESSION["cursor"] = min(SESSION["cursor"] + 1, n - 1)
        q = scene["questions"][SESSION["cursor"]]
        reply = q["q"]
    elif SESSION["mode"] == "presentation":
        n = len(scene.get("sections", []))
        if SESSION["cursor"] + 1 >= n:
            _enter_qa()
            reply = "That's the full run — now I've got a few questions for you."
        else:
            SESSION["cursor"] += 1
            reply = f'Next up: "{scene["sections"][SESSION["cursor"]]["title"]}" — go when ready.'
    else:
        return JSONResponse({"error": "not applicable"}, status_code=400)
    SESSION["convo"].append({"who": "tutor", "text": reply})
    ts = int(time.time() * 10) % 10_000_000
    out = AUDIO / f"next_{ts}"
    try:
        engine = tts.say(reply, "tutor", out)
        audio = f"/audio/{tts.audio_path(out).name}"
    except Exception:
        engine, audio = "none", None
    return {"reply": reply, "audio": audio, "engine": engine,
            "cursor": SESSION["cursor"], "phase": SESSION["phase"]}


def _enter_qa():
    """presentation 讲完 → Kai 变怀疑派听众, 复用 scenario delta。"""
    scene = SESSION["scene"]
    n = int((scene.get("qa") or {}).get("n", 2))
    SESSION["phase"] = "qa"
    scene["kai_role"] = "a skeptical but fair audience member who just watched the talk"
    scene["setting"] = "Q&A right after the presentation"
    scene["learner_role"] = "the presenter"
    scene["objective"] = "answer audience questions clearly without getting defensive"
    scene["beats"] = [{"i": i, "goal": f"audience question {i + 1} answered", "done": False} for i in range(n)]


# ============================================================ TURN — NDJSON 流
@app.post("/api/turn")
async def turn(file: UploadFile = File(...)):
    if not SESSION["active"]:
        return JSONResponse({"error": "start a session first"}, status_code=400)
    body = await file.read()
    ts = int(time.time() * 10) % 10_000_000
    max_sec = 180 if SESSION["mode"] == "presentation" else 45

    def gen():
        t0 = time.time()

        def ev(stage, **kw):
            kw.update(stage=stage, t=int((time.time() - t0) * 1000))
            return json.dumps(kw, ensure_ascii=False) + "\n"

        yield ev("received")
        # ---- STT ----
        try:
            wav = _to_wav(body, f"turn_{ts}", max_sec)
            stt = stt_mod.transcribe(str(wav))
        except Exception as e:
            yield ev("error", at="stt", error=f"audio failed: {e}", recoverable=False)
            return
        heard = (stt.get("text") or "").strip()
        if not heard:
            yield ev("error", at="stt", error="no speech detected — hold while you speak", recoverable=False)
            return
        # words: [{text,start,end,logprob}] — 前端节奏条 (rhythm strip) 的原料, 判卷没回来就能先画
        yield ev("stt", heard=heard, stt_engine=stt.get("engine", ""),
                 words=stt.get("words", []), speech_dur=stt.get("speech_dur", 0.0),
                 pause_ratio=stt.get("pause_ratio"))

        # ---- 判卷 (25s 加固; presentation 输入长, 40s) ----
        SESSION["turn_i"] += 1
        turn_i = SESSION["turn_i"]
        exhausted = {p for p, n in SESSION["elicit_attempts"].items() if n >= 2}
        briefing = store.briefing(exclude=exhausted)
        for c in briefing["due_cards"]:
            SESSION["elicit_attempts"][c["pattern"]] = SESSION["elicit_attempts"].get(c["pattern"], 0) + 1
        drill_q = None
        if SESSION["mode"] == "review" and SESSION["drill_i"] + 1 < len(SESSION["drill"]):
            drill_q = SESSION["drill"][SESSION["drill_i"] + 1]["question"]
        prompt = brain.judge_prompt(heard, SESSION["convo"], briefing,
                                    mode=SESSION["mode"], scene=SESSION.get("scene"),
                                    cursor=SESSION["cursor"], phase=SESSION["phase"], drill_q=drill_q)
        judge, degraded = brain.judge_safe(
            prompt, timeout=brain.judge_timeout(40 if SESSION["mode"] == "presentation" else 25))
        if drill_q is not None:
            SESSION["drill_i"] += 1
        SESSION["convo"].append({"who": "learner", "text": heard})
        SESSION["convo"].append({"who": "tutor", "text": judge.get("reply", "")})

        # ---- 评分 ----
        n_words = len(heard.split())
        fl, fl_meta = scoring.fluency_score(stt, heard)
        pr, pr_meta = scoring.pron_score(stt, heard)
        turn_result = scoring.compose_turn(judge, fl, pr, n_words)

        # ---- 防鹦鹉: 本轮 ≈ 上一条 recast 的复读 → elicited 不给 SRS 学分 ----
        parrot = bool(SESSION["last_recast"]) and scoring.is_parrot(heard, SESSION["last_recast"])

        # ---- 记忆写入 (恰好一次; degraded 时跳过) ----
        focus_pattern = None
        if not degraded:
            due_set = {c["pattern"] for c in briefing["due_cards"]}
            errors = judge.get("errors", [])[:3]
            sev_rank = {"blocking": 0, "major": 1, "minor": 2}
            if errors:
                focus = sorted(errors, key=lambda e: (e.get("pattern") not in due_set,
                                                      sev_rank.get(e.get("severity"), 3)))[0]
                focus_pattern = focus.get("pattern")
            ctx = {"scene_id": SESSION["scene"]["id"], "mode": SESSION["mode"]} if SESSION.get("scene") else None
            for err in errors:
                existed = err.get("pattern") in store.cards
                store.record_error(err, heard, context=ctx)
                if not existed:
                    SESSION["cards_created"] += 1
                    SESSION["new_patterns"].append(err.get("pattern"))
            bonus = 0
            for pattern, ok in (judge.get("elicited") or {}).items():
                if ok and not parrot:
                    res = store.record_elicited(pattern, True)
                    if res:
                        SESSION["cards_advanced"] += 1
                        SESSION["advanced_patterns"].append(pattern)
                        bonus += memory.XP_GRADUATE if res == "graduated" else memory.XP_CARD_ADVANCE
            for w in judge.get("wishlist", []) or []:
                if w not in store.profile["wishlist"]:
                    store.profile["wishlist"].append(w)
            store.update_skills(turn_result["dims"])
            store.update_cx(turn_result.get("complexity", 1))
            store.add_xp(turn_result["xp"] + bonus)
            SESSION["xp_gained"] += turn_result["xp"] + bonus
            SESSION["turns"].append({"heard": heard, "composite": turn_result["composite"]})
            store.save()

        # ---- 场景状态推进 ----
        scene_out = scenes.apply_turn(SESSION, judge)
        log_entry = None
        if SESSION["mode"] == "interview" and SESSION.get("scene"):
            qs = SESSION["scene"].get("questions", [])
            qi = max(0, SESSION["cursor"] - (1 if (scene_out or {}).get("next_move") == "advance" else 0))
            q = qs[min(qi, len(qs) - 1)] if qs else {}
            log_entry = {"q": q.get("q", ""), "kind": q.get("kind", ""),
                         "composite": turn_result["composite"],
                         "structure": judge.get("structure"),
                         "delivery": {"wpm": round(fl_meta.get("wps", 0) * 60) if fl_meta.get("wps") else None,
                                      "fillers": fl_meta.get("fillers"),
                                      "answer_sec": round(stt.get("speech_dur", 0))},
                         "polished": judge.get("polished")}
            if (scene_out or {}).get("next_move") == "wrap":
                scene_out["scene_state"] = "wrapup"
        elif SESSION["mode"] == "presentation" and SESSION.get("scene"):
            if SESSION["phase"] == "qa":
                log_entry = {"phase": "qa", "composite": turn_result["composite"]}
                if scene_out is not None and all(b.get("done") for b in SESSION["scene"].get("beats", [])):
                    scene_out["scene_state"] = "wrapup"
            else:
                secs = SESSION["scene"].get("sections", [])
                s = secs[min(SESSION["cursor"], len(secs) - 1)] if secs else {}
                actual = round(stt.get("speech_dur", 0))
                log_entry = {"phase": "main", "title": s.get("title", ""),
                             "target_sec": s.get("target_sec"), "actual_sec": actual,
                             "wpm": round(fl_meta.get("wps", 0) * 60) if fl_meta.get("wps") else None,
                             "fillers": fl_meta.get("fillers"), "clarity": judge.get("clarity"),
                             "composite": turn_result["composite"], "polished": judge.get("polished")}
                if scene_out is not None:
                    scene_out["timing"] = {"actual_sec": actual, "target_sec": s.get("target_sec"),
                                           "delta": (actual - s["target_sec"]) if s.get("target_sec") else None,
                                           "wpm": log_entry["wpm"]}
                # 自动推进; 讲完最后一节 → Q&A
                if SESSION["cursor"] + 1 >= len(secs):
                    _enter_qa()
                    scene_out["qa_starting"] = True
                else:
                    SESSION["cursor"] += 1
                scene_out["cursor"] = SESSION["cursor"]
        if log_entry:
            SESSION["scene_log"].append(log_entry)

        # 打磨循环由前端在 fix 音频到达时自动开启 (openPolish), 服务端不再做 echo 邀约门控
        recast = judge.get("recast")
        grace = SESSION["mode"] == "free" and turn_i <= GRACE_TURNS   # 宽限只藏分数/错误标签; 自声修正照播
        SESSION["last_recast"] = recast or judge.get("native") or ""

        yield ev("judge", reply=judge.get("reply", ""), recast=recast, native=judge.get("native"),
                 errors=judge.get("errors", [])[:3], focus_pattern=focus_pattern,
                 scores=turn_result, fl_meta=fl_meta, pr_meta=pr_meta,
                 elicited={} if parrot else (judge.get("elicited") or {}), parrot=parrot,
                 level=memory.level_of(store.profile["skills"]), skills=store.profile["skills"],
                 xp=store.profile["xp"], lv=memory.xp_level(store.profile["xp"]),
                 scene=scene_out, grace=grace, degraded=degraded)

        # ---- 双路 TTS 并行, 完成即推 ----
        reply = judge.get("reply", "")
        # 修正声道: interview/presentation 用 polished (killer moment), 其他用 recast/native;
        # 句子本来就对 → 用原句合成克隆参考 ("你的句子, 母语节奏") — 每轮都有可打磨的对象
        if SESSION["mode"] in ("interview", "presentation"):
            fix_text = judge.get("polished")
        else:
            fix_text = recast or judge.get("native") or (heard if n_words >= 4 and not degraded else None)
        fix_voice = store.profile.get("fix_voice", "user")
        jobs = {}
        if reply:
            jobs[EX.submit(tts.say, reply, "tutor", AUDIO / f"reply_{ts}")] = ("reply", AUDIO / f"reply_{ts}")
        # 自声修正是产品本体: 宽限期也播; 只有温柔模式才静音。
        # 打磨循环的参考: with-timestamps 合成 → 参考图谱 (词级时间轴) 零额外调用
        if fix_text and not store.profile.get("gentle_mode"):
            jobs[EX.submit(tts.say_with_timing, fix_text, fix_voice, AUDIO / f"fix_{ts}")] = ("fix", AUDIO / f"fix_{ts}")
        try:
            for f in as_completed(jobs, timeout=40):
                kind, out = jobs[f]
                try:
                    ref_words = []
                    if kind == "fix":
                        engine, ref_words = f.result()
                    else:
                        engine = f.result()
                    url = f"/audio/{tts.audio_path(out).name}"
                    if kind == "fix" and log_entry is not None:
                        log_entry["polished_audio"] = url
                    if kind == "fix":       # /demo 页对比框的"最近一对": 原声 vs 流利版
                        (BASE / "data" / "demo_latest.json").write_text(json.dumps({
                            "heard": heard, "fix_text": fix_text,
                            "orig_audio": f"/audio/turn_{ts}.wav", "fix_audio": url,
                            "own_voice": fix_voice == "user", "engine": engine,
                            "words": stt.get("words", []), "ts": int(time.time())}, ensure_ascii=False))
                    yield ev("tts", kind=kind, audio=url, engine=engine,
                             own_voice=(kind == "fix" and fix_voice == "user"),
                             ref_text=fix_text if kind == "fix" else None,
                             ref_words=ref_words if kind == "fix" else None)
                except Exception as e:
                    yield ev("error", at=f"tts-{kind}", error=str(e)[:150], recoverable=True)
        except Exception:
            yield ev("error", at="tts", error="tts timed out", recoverable=True)
        yield ev("done", ms=int((time.time() - t0) * 1000))

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ============================================================ 打磨循环 (说→纠→反复模仿)
@app.post("/api/polish/attempt")
async def polish_attempt(file: UploadFile = File(...), ref_text: str = Form(...),
                         ref_words: str = Form("[]"), pattern: str = Form(""),
                         attempt: int = Form(1)):
    """一次模仿尝试: STT → 与克隆声参考做 match/pron/rhythm/smooth 四维对比。
    无 LLM, ~1-2s 回。第一次达标且带 pattern → SRS 学习步 fast-track (仅一次)。"""
    ts = int(time.time() * 10) % 10_000_000
    try:
        wav = _to_wav(await file.read(), f"att_{ts}")
        stt = stt_mod.transcribe(str(wav))
    except Exception as e:
        return JSONResponse({"error": f"audio failed: {e}"}, status_code=400)
    heard = (stt.get("text") or "").strip()
    if not heard:
        return JSONResponse({"error": "no speech detected — hold while you speak"}, status_code=400)
    try:
        rw = json.loads(ref_words)
    except json.JSONDecodeError:
        rw = []
    result = scoring.polish_compare(stt, heard, ref_text, rw)
    result["audio"] = f"/audio/att_{ts}.wav"
    result["fast_tracked"] = False
    if result["passed"] and pattern and attempt == 1:
        result["fast_tracked"] = store.echo_pass(pattern)
        store.save()
    return result


# ============================================================ 会话结束
@app.post("/api/session/end")
def session_end():
    if not SESSION["active"]:
        return JSONResponse({"error": "no active session"}, status_code=400)
    SESSION["active"] = False
    report = {"mode": SESSION["mode"], "turns": len(SESSION["turns"]), "xp_gained": SESSION["xp_gained"],
              "cards_created": SESSION["cards_created"], "cards_advanced": SESSION["cards_advanced"],
              "avg": None, "summary": "", "best_moment": "", "sendoff": None, "sendoff_audio": None}
    scored = [t["composite"] for t in SESSION["turns"] if t["composite"] is not None]
    if scored:
        report["avg"] = round(sum(scored) / len(scored))
    if SESSION.get("scene"):
        report["scene_report"] = scenes.end_report(SESSION)
    if SESSION["convo"] and SESSION["turns"]:    # 0 轮会话没有可蒸馏的东西
        try:
            s = brain.session_summary(SESSION["convo"], mode=SESSION["mode"])
            store.end_session(s)
            report["summary"] = s.get("summary", "")
            report["best_moment"] = s.get("best_moment", "")
            report["sendoff"] = s.get("sendoff", "")
            if report["sendoff"]:
                ts = int(time.time() * 10) % 10_000_000
                out = AUDIO / f"sendoff_{ts}"
                try:
                    tts.say(report["sendoff"], "tutor", out)
                    report["sendoff_audio"] = f"/audio/{tts.audio_path(out).name}"
                except Exception:
                    pass
        except Exception:
            pass
    if report["turns"] > 0:      # 误点 Start→End 的空会话不进历史, 不污染趋势图
        store.log_session({"mode": SESSION["mode"], "turns": report["turns"], "avg": report["avg"],
                           "xp_gained": report["xp_gained"], "cards_created": report["cards_created"],
                           "cards_advanced": report["cards_advanced"],
                           "new_patterns": SESSION["new_patterns"],
                           "advanced_patterns": SESSION["advanced_patterns"],
                           "best_moment": report["best_moment"], "summary": report["summary"],
                           "topics": []})
    try:
        sauna.export_all(store)
    except Exception:
        pass
    return report


# ============================================================ progress / cards / me
@app.get("/api/progress")
def api_progress():
    days = {}
    for s in store.sessions:
        d = time.strftime("%Y-%m-%d", time.localtime(s.get("ts", 0)))
        agg = days.setdefault(d, {"xp": 0, "turns": 0})
        agg["xp"] += s.get("xp_gained", 0)
        agg["turns"] += s.get("turns", 0)
    cards = list(store.cards.values())
    return {"sessions": store.sessions, "days": days,
            "skills_now": store.profile["skills"],
            "level": memory.level_of(store.profile["skills"]),
            "level_progress": memory.level_progress(store.profile["skills"]),
            "xp_total": store.profile["xp"], "lv": memory.xp_level(store.profile["xp"]),
            "streak": store.profile["streak"], "turns_total": store.profile["turns_total"],
            "cx_ema": store.profile.get("cx_ema"), "plateau": store.plateau(),
            "cards": {"learning": sum(1 for c in cards if c["status"] == "learning"),
                      "due": sum(1 for c in cards if c["status"] == "learning" and c["due_at"] <= time.time()),
                      "graduated": sum(1 for c in cards if c["status"] == "graduated"),
                      "archived": sum(1 for c in cards if c["status"] == "archived")}}


@app.get("/api/cards")
def api_cards():
    return {"now": int(time.time()), "factor": memory.FACTOR,
            "cards": sorted(store.cards.values(), key=lambda c: (c["status"] != "learning", c["due_at"]))}


@app.post("/api/card/action")
async def card_action(payload: dict):
    c = store.card_action(payload.get("pattern", ""), payload.get("action", ""))
    if not c:
        return JSONResponse({"error": "no such card"}, status_code=404)
    return c


@app.get("/api/me")
def api_me():
    p = store.profile
    return {"name": p.get("name"), "native_lang": p.get("native_lang"),
            "facts": p.get("facts", []), "goals": p.get("goals", []),
            "wishlist": p.get("wishlist", []),
            "gentle_mode": p.get("gentle_mode"), "fix_voice": p.get("fix_voice"),
            "voice": {"enrolled_local": tts.USER_REF.exists(),
                      "eleven_key": bool(os.environ.get("ELEVENLABS_API_KEY")),
                      "eleven_cloned": bool(tts._eleven_voices().get("user")),
                      "voice_id": tts._eleven_voices().get("user", "")},
            "episodes": store.episodes[::-1][:10],
            "xp": p["xp"], "streak": p["streak"], "level": memory.level_of(p["skills"]),
            "sauna": sauna.status()}


@app.post("/api/me/fact")
async def me_fact(payload: dict):
    if payload.get("add"):
        store.merge_facts([{"text": payload["add"], "kind": payload.get("kind", "other")}], src="manual")
        store.save()
    elif payload.get("delete"):
        store.set_fact(payload.get("text", ""), delete=True)
    else:
        store.set_fact(payload.get("text", ""), use=bool(payload.get("use", True)))
    return {"facts": store.profile["facts"]}


@app.post("/api/me/goal")
async def me_goal(payload: dict):
    store.upsert_goal(payload)
    return {"goals": store.profile["goals"]}


@app.post("/api/me/wishlist")
async def me_wishlist(payload: dict):
    wl = store.profile["wishlist"]
    if payload.get("add") and payload["add"] not in wl:
        wl.append(payload["add"])
    if payload.get("remove") in wl:
        wl.remove(payload["remove"])
    store.save()
    return {"wishlist": wl}


@app.post("/api/me/prefs")
async def me_prefs(payload: dict):
    for k in ("gentle_mode", "fix_voice"):
        if k in payload:
            store.profile[k] = payload[k]
    store.save()
    return {"ok": True}


@app.get("/api/demo")
def api_demo():
    """demo 页数据: 最近一对 原声/流利版 + 错题例句表 + 实测计时。"""
    latest = None
    f = BASE / "data" / "demo_latest.json"
    if f.exists():
        try:
            latest = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    rows = []
    for c in sorted(store.cards.values(), key=lambda c: -c.get("last_seen", 0)):
        for ex in c.get("examples", [])[-1:]:
            rows.append({"ts": ex.get("ts", c.get("last_seen")), "wrong": ex["wrong"],
                         "right": ex["right"], "pattern": c["pattern"], "type": c.get("type", "")})
    return {"latest": latest, "rows": rows[:14],
            "cards": {"learning": sum(1 for c in store.cards.values() if c["status"] == "learning"),
                      "graduated": sum(1 for c in store.cards.values() if c["status"] == "graduated")},
            "profile": {"xp": store.profile["xp"], "turns": store.profile["turns_total"],
                        "level": memory.level_of(store.profile["skills"])}}


# ============================================================ Sauna / dev
@app.post("/api/sauna/export")
def sauna_export():
    return sauna.export_all(store)


@app.post("/api/dev/expire")
def dev_expire():
    """demo 用: 全部在学卡强制到期, 且 last_seen 回拨到 R≈0.5 — 衰减肉眼可见。"""
    n = 0
    now = int(time.time())
    for c in store.cards.values():
        if c["status"] == "learning":
            c["due_at"] = now - 1
            c["last_seen"] = now - int(9 * c.get("S", 0.25) * 86400)
            n += 1
    store.save()
    return {"expired": n}


@app.post("/api/dev/seed")
def dev_seed():
    import seed_demo
    global store
    seed_demo.seed(force=True)
    store = memory.make_store()
    sauna.export_all(store)
    return {"ok": True, "cards": len(store.cards), "sessions": len(store.sessions)}
