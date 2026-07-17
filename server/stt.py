# STT 适配层 — 有 ELEVENLABS_API_KEY 时用 Scribe v2, 否则本地 faster-whisper
#
#   Scribe v2 (2026): 正文逐字(保留 um/uh) + 词级时间戳 + **每词 logprob 置信度**
#     → fluency: 语速 + 词间停顿 + 填充词 三信号
#     → pron:   词级置信度直接来自 Scribe, 不再需要 whisper 并行跑 (省一路延迟)
#       还能标出具体哪个词说得含糊 (word-level pron heatmap)
#   whisper (:8123) 只在无 key / Scribe 失败时兜底。
import json
import os
import subprocess
import urllib.request
import uuid


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
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\nscribe_v2\r\n").encode()
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


def _from_whisper(w: dict, engine: str) -> dict:
    return {"text": (w.get("text") or "").strip(), "speech_dur": w.get("speech_dur", 0.0),
            "avg_logprob": w.get("avg_logprob"), "conf_source": "whisper",
            "words": [], "engine": engine}


_MOCK_LINES = ["I go to the office yesterday and meet my manager.",
               "If I will have more time, I would practice every day.",
               "She give me many good advices about the interview."]
_mock_i = 0


def transcribe(wav_path: str) -> dict:
    """统一返回: {text, speech_dur, avg_logprob?, conf_source, pause_ratio?, words[], engine}
    words: [{text,start,end,logprob}] — Scribe v2 时非空, 供词级发音标注。"""
    if os.environ.get("FLUENTME_MOCK"):
        global _mock_i
        text = _MOCK_LINES[_mock_i % len(_MOCK_LINES)]; _mock_i += 1
        # 合成词级时间轴 (含一个长停顿 + 一个填充词 + 一个低置信词), 节奏条 UI 无 key 可开发
        toks = text.replace(".", "").split()
        toks.insert(max(1, len(toks) // 2), "um")
        words, t = [], 0.3
        for i, w in enumerate(toks):
            dur = 0.18 + 0.04 * (len(w) % 4)
            lp = -0.55 if i == len(toks) - 2 else (-0.3 if w == "um" else -0.06 - 0.02 * (i % 3))
            words.append({"text": w, "start": round(t, 2), "end": round(t + dur, 2), "logprob": lp})
            t += dur + (0.75 if i == len(toks) // 3 else 0.06)   # 一处 0.75s 停顿
        dur_total = words[-1]["end"] - words[0]["start"]
        gaps = [words[i + 1]["start"] - words[i]["end"] for i in range(len(words) - 1)]
        return {"text": " ".join(toks), "speech_dur": round(dur_total, 2), "avg_logprob": -0.18,
                "conf_source": "scribe",
                "pause_ratio": round(sum(g for g in gaps if g > 0.5) / dur_total, 3),
                "words": words, "engine": "mock"}
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return _from_whisper(_whisper(wav_path), "whisper · local")

    s = _scribe(wav_path)
    if not s.get("text"):          # Scribe 失败 → whisper 兜底
        return _from_whisper(_whisper(wav_path), "whisper · fallback")

    words = [x for x in s.get("words", []) if x.get("type") == "word"]
    dur = (words[-1]["end"] - words[0]["start"]) if len(words) >= 2 else 0.0
    # 词间停顿: >0.5s 的间隙时长占比
    gaps = [words[i + 1]["start"] - words[i]["end"] for i in range(len(words) - 1)]
    pause_ratio = (sum(g for g in gaps if g > 0.5) / dur) if dur else None
    # 每词 logprob (0 最自信, 负得越多越含糊); 缺失的词跳过
    lps = [w["logprob"] for w in words if w.get("logprob") is not None]
    avg_lp = (sum(lps) / len(lps)) if lps else None
    return {"text": s["text"].strip(), "speech_dur": dur,
            "avg_logprob": avg_lp, "conf_source": "scribe",
            "pause_ratio": pause_ratio,
            "words": [{"text": w.get("text", ""), "start": w.get("start"), "end": w.get("end"),
                       "logprob": w.get("logprob")} for w in words],
            "engine": "scribe-v2 · elevenlabs"}
