# TTS 适配层 — 现场唯一要换的引擎面
#
#   voice="user"  → 学员自己的克隆声 (recast/native 用它说: "听到流利的自己" 是全场 hook)
#   voice="tutor" → 导师声 Kai
#
#   引擎优先级: ElevenLabs (有 ELEVENLABS_API_KEY 即启用, 现场拿 credits 后 export 就切) →
#              本地 Higgs v2 (:8124, 今晚测试用, 不依赖任何 key)
#   克隆: enroll_eleven() 把已有的 mirror_user.wav 直接喂 ElevenLabs IVC,
#         不用重录 —— 声纹样本今天下午刚录过, 还热着
import json
import os
import subprocess
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
(DATA / "voice").mkdir(parents=True, exist_ok=True)
# 声纹样本: 优先项目内 data/voice/ (笔记本可携带); 兜底家里 GPU 机上 higgs 的注册样本
_LOCAL_REF = DATA / "voice" / "mirror_user.wav"
_LEGACY_REF = Path("/home/carwaii/higgs-audio/examples/voice_prompts/mirror_user.wav")
USER_REF = _LOCAL_REF if _LOCAL_REF.exists() or not _LEGACY_REF.exists() else _LEGACY_REF
NEUTRAL_SCENE = "The speaker speaks clear, fluent, natural English."
ELEVEN_TUTOR_DEFAULT = "SAz9YHcvj6GT2YYXdXww"  # River — relaxed/neutral, free tier 可用 (library voices 要付费)


def _eleven_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "")


def _eleven_voices() -> dict:
    f = DATA / "eleven_voices.json"
    return json.loads(f.read_text()) if f.exists() else {}


def enroll_eleven() -> dict:
    """把本地声纹样本注册成 ElevenLabs IVC 克隆声。现场第 0 步, 一次即可。"""
    key = _eleven_key()
    if not key:
        return {"error": "ELEVENLABS_API_KEY not set"}
    if not USER_REF.exists():
        return {"error": "no local voice sample — record on /enroll first"}
    import mimetypes
    import uuid
    boundary = uuid.uuid4().hex
    body = b""
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\nfluent-me-user\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"ref.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode()
    body += USER_REF.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request("https://api.elevenlabs.io/v1/voices/add", data=body, method="POST",
                                 headers={"xi-api-key": key,
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        vid = json.loads(r.read()).get("voice_id", "")
    voices = _eleven_voices()
    voices["user"] = vid
    (DATA / "eleven_voices.json").write_text(json.dumps(voices))
    return {"voice_id": vid}


def _say_eleven(text: str, voice: str, out: Path) -> bool:
    key = _eleven_key()
    if not key:
        return False
    voices = _eleven_voices()
    vid = voices.get("user") if voice == "user" else os.environ.get("ELEVEN_TUTOR_VOICE", ELEVEN_TUTOR_DEFAULT)
    if not vid:
        return False
    model = os.environ.get("ELEVEN_TTS_MODEL", "eleven_flash_v2_5")  # turbo 已弃用; flash ~75ms
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": model}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out.write_bytes(r.read())
        return out.stat().st_size > 2000
    except Exception:
        return False


def _say_local(text: str, voice: str, out: Path):
    ref = "mirror_user" if (voice == "user" and USER_REF.exists()) else ("belinda" if voice == "tutor" else "chef")
    subprocess.run(["curl", "-s", "-X", "POST", "http://localhost:8124/v1/audio/speech",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps({"input": text, "voice": ref, "scene": NEUTRAL_SCENE}),
                    "-o", str(out)], check=True, timeout=300)


POOL = DATA / "voice" / "pool"
POOL.mkdir(parents=True, exist_ok=True)
REBUILD_AT = (3, 6, 10)     # 样本池到这些数量时重建克隆 — 越练越像你


def ensure_clone_from(wav_path: str, min_dur: float = 1.5) -> str | None:
    """零 warm-up 克隆: 练习语音直接入样本池。无克隆 → 立即用它建;
    有克隆 → 攒到 REBUILD_AT 档位就重建 (删旧建新, 换 voice_id)。
    返回 "created" | "improved" | None。失败静默 (克隆是增强, 不能挡练习)。"""
    key = _eleven_key()
    if not key or os.environ.get("FLUENTME_MOCK"):
        return None
    try:
        import shutil
        import wave as wavmod
        with wavmod.open(wav_path, "rb") as w:
            dur = w.getnframes() / w.getframerate()
        if dur < min_dur:
            return None
        n = len(list(POOL.glob("*.wav")))
        shutil.copy(wav_path, POOL / f"s{int(os.path.getmtime(wav_path) * 10) % 10**9}_{n}.wav")
        if not USER_REF.exists():
            shutil.copy(wav_path, USER_REF)          # 本地 Higgs 兜底也有样本
        voices = _eleven_voices()
        have = bool(voices.get("user"))
        n += 1
        if have and n not in REBUILD_AT:
            return None
        samples = sorted(POOL.glob("*.wav"), key=lambda p: p.stat().st_mtime)[-10:]
        if have:                                      # 重建: 删旧 (slot 有限)
            try:
                req = urllib.request.Request(
                    f"https://api.elevenlabs.io/v1/voices/{voices['user']}",
                    method="DELETE", headers={"xi-api-key": key})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception:
                pass
        boundary = __import__("uuid").uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\nfluent-me-user\r\n").encode()
        for p in samples:
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{p.name}\"\r\n"
                     f"Content-Type: audio/wav\r\n\r\n").encode() + p.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request("https://api.elevenlabs.io/v1/voices/add", data=body, method="POST",
                                     headers={"xi-api-key": key,
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            vid = json.loads(r.read()).get("voice_id", "")
        if vid:
            voices["user"] = vid
            (DATA / "eleven_voices.json").write_text(json.dumps(voices))
            return "improved" if have else "created"
    except Exception:
        pass
    return None


def _say_mock(out: Path):
    """无任何 TTS 可用时的开发桩: 0.5s 提示音, UI 音频链路照常可测。"""
    import math
    import struct
    import wave
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * t / 16000)))
                               for t in range(8000)))


def _synth_words(text: str) -> list:
    """mock/兜底: 按词长合成时间轴 (UI 无 key 可开发)。"""
    words, t = [], 0.15
    for w in text.replace(".", "").replace(",", "").split():
        dur = 0.16 + 0.05 * min(len(w), 8) / 2
        words.append({"text": w, "start": round(t, 2), "end": round(t + dur, 2), "logprob": -0.05})
        t += dur + 0.07
    return words


def say_with_timing(text: str, voice: str, out: Path) -> tuple[str, list]:
    """→ (engine, words) — 打磨循环的参考音频: ElevenLabs with-timestamps 端点
    返回字符级时间戳, 聚合成词级 → 参考图谱不花额外调用。失败退回普通 say, words=[]。"""
    if os.environ.get("FLUENTME_MOCK"):
        _say_mock(out.with_suffix(".wav"))
        return "mock", _synth_words(text)
    key = _eleven_key()
    voices = _eleven_voices()
    vid = voices.get("user") if voice == "user" else os.environ.get("ELEVEN_TUTOR_VOICE", ELEVEN_TUTOR_DEFAULT)
    if key and vid:
        try:
            import base64
            model = os.environ.get("ELEVEN_TTS_MODEL", "eleven_flash_v2_5")
            req = urllib.request.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{vid}/with-timestamps?output_format=mp3_44100_128",
                data=json.dumps({"text": text, "model_id": model}).encode(),
                headers={"xi-api-key": key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            mp3 = out.with_suffix(".mp3")
            mp3.write_bytes(base64.b64decode(d["audio_base64"]))
            al = d.get("alignment") or {}
            chars = al.get("characters", [])
            t0s = al.get("character_start_times_seconds", [])
            t1s = al.get("character_end_times_seconds", [])
            words, cur = [], None
            for c, a, b in zip(chars, t0s, t1s):
                if c.strip():
                    if cur is None:
                        cur = {"text": c, "start": a, "end": b, "logprob": None}
                    else:
                        cur["text"] += c
                        cur["end"] = b
                else:
                    if cur:
                        words.append(cur)
                        cur = None
            if cur:
                words.append(cur)
            if mp3.stat().st_size > 2000:
                out.with_suffix(".wav").unlink(missing_ok=True)
                return "elevenlabs · " + model.replace("eleven_", "").replace("_", "-"), words
        except Exception:
            pass
    return say(text, voice, out), []


def say(text: str, voice: str, out: Path) -> str:
    """返回引擎标签, 界面 badge 直接显示。out 后缀由引擎决定, 调用方用返回的真实路径。"""
    if os.environ.get("FLUENTME_MOCK"):
        _say_mock(out.with_suffix(".wav"))
        return "mock"
    mp3 = out.with_suffix(".mp3")
    if _say_eleven(text, voice, mp3):
        out.unlink(missing_ok=True)
        return "elevenlabs · " + os.environ.get("ELEVEN_TTS_MODEL", "eleven_flash_v2_5").replace("eleven_", "").replace("_", "-")
    _say_local(text, voice, out.with_suffix(".wav"))
    mp3.unlink(missing_ok=True)
    return "higgs-v2 · local"


def audio_path(out: Path) -> Path:
    """say() 之后拿真实产物路径 (mp3 或 wav)。"""
    mp3 = out.with_suffix(".mp3")
    return mp3 if mp3.exists() else out.with_suffix(".wav")
