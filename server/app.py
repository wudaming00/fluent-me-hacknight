# fluent-me — hackathon: 有记忆的口语陪练, 用你自己的声音示范流利版
# run: cd ~/fluent-me/server && ~/claude-voice/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8901
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import brain
import memory
import scoring
import stt as stt_mod
import tts

BASE = Path(__file__).resolve().parent.parent
PAGES = Path(__file__).resolve().parent / "pages"
AUDIO = BASE / "data" / "audio"
AUDIO.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="fluent-me")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.mount("/audio", StaticFiles(directory=str(AUDIO)), name="audio")

store = memory.make_store()
SESSION = {"active": False, "convo": [], "turns": [], "xp_gained": 0, "cards_created": 0, "cards_advanced": 0}


def _to_wav(upload_bytes: bytes, tag: str) -> Path:
    raw = AUDIO / f"{tag}_raw"
    raw.write_bytes(upload_bytes)
    wav = AUDIO / f"{tag}.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-t", "45", "-ac", "1", "-ar", "16000", str(wav)],
                   check=True, capture_output=True)
    raw.unlink(missing_ok=True)
    return wav


def _page(name: str):
    return FileResponse(str(PAGES / name), media_type="text/html")


@app.get("/")
def index():
    return _page("index.html")


@app.get("/profile")
def profile_page():
    return _page("profile.html")


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
    import os
    return {
        "profile": store.profile, "level": memory.level_of(store.profile["skills"]),
        "enrolled_local": tts.USER_REF.exists(),
        "eleven_key": bool(os.environ.get("ELEVENLABS_API_KEY")),
        "eleven_cloned": bool(tts._eleven_voices().get("user")),
        "sauna_key": bool(os.environ.get("SAUNA_API_KEY")),
        "services": {"stt": up(8123), "tts_local": up(8124)},
        "session_active": SESSION["active"],
        "due_cards": [c["pattern"] for c in store.due_cards()],
        "cards_total": len([c for c in store.cards.values() if c["status"] == "learning"]),
    }


@app.post("/api/session/start")
def session_start():
    SESSION.update({"active": True, "convo": [], "turns": [], "xp_gained": 0,
                    "cards_created": 0, "cards_advanced": 0})
    briefing = store.briefing()
    g = brain.greeting(briefing)
    reply = g.get("reply", "Hey! Good to see you again — what are you building these days?")
    SESSION["convo"].append({"who": "tutor", "text": reply})
    ts = int(time.time() * 10) % 10_000_000
    out = AUDIO / f"greet_{ts}"
    engine = tts.say(reply, "tutor", out)
    return {"reply": reply, "audio": f"/audio/{tts.audio_path(out).name}", "engine": engine,
            "briefing": briefing}


@app.post("/api/turn")
async def turn(file: UploadFile = File(...)):
    if not SESSION["active"]:
        return JSONResponse({"error": "start a session first"}, status_code=400)
    t0 = time.time()
    ts = int(time.time() * 10) % 10_000_000
    wav = _to_wav(await file.read(), f"turn_{ts}")
    stt = stt_mod.transcribe(str(wav))
    heard = (stt.get("text") or "").strip()
    if not heard:
        return JSONResponse({"error": "no speech detected"}, status_code=400)

    briefing = store.briefing()   # 每轮取最新到期卡, 钓中一张后下一轮换下一张
    judge = brain.judge_turn(heard, SESSION["convo"], briefing)
    SESSION["convo"].append({"who": "learner", "text": heard})
    SESSION["convo"].append({"who": "tutor", "text": judge.get("reply", "")})

    # ---- 评分 ----
    n_words = len(heard.split())
    fl, fl_meta = scoring.fluency_score(stt, heard)
    pr, pr_meta = scoring.pron_score(stt, heard)
    turn_result = scoring.compose_turn(judge, fl, pr, n_words)

    # ---- 记忆写入 ----
    for err in judge.get("errors", []):
        existed = err.get("pattern") in store.cards
        store.record_error(err, heard)
        if not existed:
            SESSION["cards_created"] += 1
    for pattern, ok in (judge.get("elicited") or {}).items():
        if ok:
            store.record_elicited(pattern, True)
            SESSION["cards_advanced"] += 1
    for w in judge.get("wishlist", []) or []:
        if w not in store.profile["wishlist"]:
            store.profile["wishlist"].append(w)
    store.update_skills({k: v for k, v in turn_result["dims"].items()})
    store.add_xp(turn_result["xp"])
    SESSION["xp_gained"] += turn_result["xp"]
    SESSION["turns"].append({"heard": heard, "composite": turn_result["composite"]})
    store.save()

    # ---- 双路 TTS 并行: 导师回复(导师声) + 修正句(你的克隆声) ----
    reply = judge.get("reply", "")
    speak_fix = judge.get("recast") or judge.get("native")
    resp = {"heard": heard, "stt_engine": stt.get("engine", ""), "reply": reply,
            "recast": judge.get("recast"), "native": judge.get("native"),
            "errors": judge.get("errors", []),
            "scores": turn_result, "fl_meta": fl_meta, "pr_meta": pr_meta,
            "elicited": judge.get("elicited") or {},
            "level": memory.level_of(store.profile["skills"]),
            "skills": store.profile["skills"], "xp": store.profile["xp"],
            "hunting": [c["pattern"] for c in store.due_cards()][:3]}
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_reply = ex.submit(tts.say, reply, "tutor", AUDIO / f"reply_{ts}") if reply else None
        f_fix = ex.submit(tts.say, speak_fix, "user", AUDIO / f"fix_{ts}") if speak_fix else None
        if f_reply:
            resp["reply_engine"] = f_reply.result()
            resp["reply_audio"] = f"/audio/{tts.audio_path(AUDIO / f'reply_{ts}').name}"
        if f_fix:
            resp["fix_engine"] = f_fix.result()
            resp["fix_audio"] = f"/audio/{tts.audio_path(AUDIO / f'fix_{ts}').name}"
    resp["ms"] = int((time.time() - t0) * 1000)
    return resp


@app.post("/api/session/end")
def session_end():
    if not SESSION["active"]:
        return JSONResponse({"error": "no active session"}, status_code=400)
    SESSION["active"] = False
    report = {"turns": len(SESSION["turns"]), "xp_gained": SESSION["xp_gained"],
              "cards_created": SESSION["cards_created"], "cards_advanced": SESSION["cards_advanced"],
              "avg": None, "summary": "", "best_moment": ""}
    scored = [t["composite"] for t in SESSION["turns"] if t["composite"] is not None]
    if scored:
        report["avg"] = round(sum(scored) / len(scored))
    if SESSION["convo"]:
        try:
            s = brain.session_summary(SESSION["convo"])
            store.end_session(s)
            report["summary"] = s.get("summary", "")
            report["best_moment"] = s.get("best_moment", "")
        except Exception:
            pass
    return report


@app.get("/api/profile")
def api_profile():
    cards = sorted(store.cards.values(), key=lambda c: (c["status"] != "learning", -c["hits"]))
    return {"profile": store.profile, "level": memory.level_of(store.profile["skills"]),
            "cards": cards, "episodes": store.episodes[::-1][:10],
            "now": int(time.time())}


@app.post("/api/enroll")
async def enroll(file: UploadFile = File(...)):
    """录 15-25s 声纹 → 本地样本 (Higgs 直接可用; ElevenLabs 克隆再点一步)。"""
    wav24 = AUDIO / "enroll.wav"
    raw = AUDIO / "enroll_raw"
    raw.write_bytes(await file.read())
    subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-t", "25", "-ac", "1", "-ar", "24000", str(wav24)],
                   check=True, capture_output=True)
    raw.unlink(missing_ok=True)
    transcript = stt_mod.transcribe(str(wav24)).get("text", "")
    if not transcript:
        return JSONResponse({"error": "no speech detected"}, status_code=400)
    import shutil
    shutil.copy(wav24, tts.USER_REF)
    tts.USER_REF.with_suffix(".txt").write_text(transcript, encoding="utf-8")
    return {"transcript": transcript}


@app.post("/api/enroll/eleven")
def enroll_eleven():
    return tts.enroll_eleven()


@app.post("/api/dev/expire")
def dev_expire():
    """demo 用: 把所有在学卡片强制到期 (台上没法等 10 分钟 SRS 间隔)。"""
    n = 0
    for c in store.cards.values():
        if c["status"] == "learning":
            c["due_at"] = int(time.time()) - 1
            n += 1
    store.save()
    return {"expired": n}
