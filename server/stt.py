# STT 适配层 — 有 ELEVENLABS_API_KEY 时用 Scribe, 否则本地 faster-whisper
#
#   两家信号各有所长, 有 key 时并行双跑合并 (不加墙钟时间):
#     Scribe  → 正文更逐字(保留 um/uh, 填充词检测才准) + 词级时间戳
#               (→ fluency 升级: 语速 + 词间停顿 + 填充词 三信号)
#     whisper → avg_logprob 置信度 (pron 代理分唯一来源, Scribe 不给置信度)
#   evenlabs key 没到手前, whisper 单跑, 一切照旧。
import json
import os
import subprocess
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor


def _whisper(wav_path: str) -> dict:
    r = subprocess.run(["curl", "-s", "-X", "POST", "http://localhost:8123/v1/audio/transcriptions",
                        "-F", f"file=@{wav_path}", "-F", "model=whisper-1"],
                       capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def _scribe(wav_path: str) -> dict:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        return {}
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\nscribe_v1\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode()
    with open(wav_path, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request("https://api.elevenlabs.io/v1/speech-to-text", data=body,
                                 headers={"xi-api-key": key,
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def transcribe(wav_path: str) -> dict:
    """统一返回: {text, speech_dur, avg_logprob?, words?, pauses?, engine}"""
    if not os.environ.get("ELEVENLABS_API_KEY"):
        w = _whisper(wav_path)
        return {"text": (w.get("text") or "").strip(), "speech_dur": w.get("speech_dur", 0.0),
                "avg_logprob": w.get("avg_logprob"), "engine": "whisper · local"}

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_s, f_w = ex.submit(_scribe, wav_path), ex.submit(_whisper, wav_path)
        s, w = f_s.result(), f_w.result()
    if not s.get("text"):    # Scribe 失败 → whisper 兜底
        return {"text": (w.get("text") or "").strip(), "speech_dur": w.get("speech_dur", 0.0),
                "avg_logprob": w.get("avg_logprob"), "engine": "whisper · fallback"}

    words = [x for x in s.get("words", []) if x.get("type") == "word"]
    dur = (words[-1]["end"] - words[0]["start"]) if len(words) >= 2 else w.get("speech_dur", 0.0)
    # 词间停顿: >0.5s 的间隙占比 (Scribe 时间戳独有, whisper 拿不到)
    gaps = [words[i + 1]["start"] - words[i]["end"] for i in range(len(words) - 1)]
    pause_ratio = (sum(g for g in gaps if g > 0.5) / dur) if dur else None
    return {"text": s["text"].strip(), "speech_dur": dur,
            "avg_logprob": w.get("avg_logprob"),        # pron 代理仍取自 whisper 并行跑
            "pause_ratio": pause_ratio, "engine": "scribe-v1 · elevenlabs (+whisper conf)"}
