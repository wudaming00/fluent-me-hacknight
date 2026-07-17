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


def _say_mock(out: Path):
    """无任何 TTS 可用时的开发桩: 0.5s 提示音, UI 音频链路照常可测。"""
    import math
    import struct
    import wave
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * t / 16000)))
                               for t in range(8000)))


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
