# 评分机制 — 四维评分, 信号来源刻意分两路:
#
#   grammar / vocab   ← Claude 判卷 (0-100 + 结构化 errors, errors 同时喂错题卡)
#   fluency / pron    ← whisper 本地信号, 零额外延迟:
#       fluency = 语速贴合(words/speech_dur 对母语带 1.8~3.4 wps) × 填充词惩罚
#       pron    = whisper avg_logprob 置信映射 —— 诚实标注: 这是 ASR 代理分,
#                 不是音素级对齐 (界面上写 "proxy", 评委问就大方承认, 别装)
#
#   防噪声:
#     < 4 词的短句只记 grammar/vocab, fluency/pron 记 None 不进画像 EMA
#     (否则学员刷 "Yes." 就能刷高流利度)
#   防刷分 (XP 才是给用户看的进度货币):
#     XP = 综合分/10 × complexity(1-5, Claude 判句子野心)
#     → 说难句子哪怕错更多, 成长比说安全短句快; 这是拉学习曲线的核心杠杆
import re

WEIGHTS = {"grammar": 0.30, "vocab": 0.25, "fluency": 0.25, "pron": 0.20}
MIN_WORDS = 4
FILLERS = re.compile(r"\b(um+|uh+|erm+|hmm+|like|you know|i mean)\b", re.I)


def fluency_score(stt: dict, text: str):
    """语速 + 填充词 (+ 词间停顿, 仅 Scribe 时间戳可用时)。信号缺就放弃打分而不是瞎给。"""
    words = [w for w in re.sub(r"[^a-zA-Z0-9' ]", " ", text).split() if w]
    n = len(words)
    if n < MIN_WORDS:
        return None, {"note": "sample too short"}
    dur = stt.get("speech_dur", 0.0)
    if not dur:
        return None, {"note": "no duration signal"}
    wps = n / dur
    # 1.8~3.4 wps 视作自然带, 带内满分, 每偏 1.0 wps 扣 45
    pace = max(0.0, 1.0 - max(0.0, abs(wps - 2.6) - 0.8) * 0.45)
    n_fill = len(FILLERS.findall(text))
    fill_ratio = n_fill / n
    fill = max(0.0, 1.0 - fill_ratio * 4.0)     # 每 25% 填充词扣光
    meta = {"wps": round(wps, 2), "fillers": n_fill}
    if stt.get("pause_ratio") is not None:      # Scribe: >0.5s 停顿时长占比, 20% 停顿扣光该项
        pause = max(0.0, 1.0 - stt["pause_ratio"] * 5.0)
        score = round(100 * (0.45 * pace + 0.30 * pause + 0.25 * fill))
        meta["pause_ratio"] = round(stt["pause_ratio"], 3)
    else:
        score = round(100 * (0.65 * pace + 0.35 * fill))
    return score, meta


def pron_score(stt: dict, text: str):
    words = [w for w in text.split() if w]
    if len(words) < MIN_WORDS:
        return None, {"note": "sample too short"}
    lp = stt.get("avg_logprob")
    if lp is None:
        return None, {"note": "no confidence signal"}
    conf = max(0.0, min(1.0, (lp + 1.0) / 0.9))   # [-1,-0.1] → [0,1], 沿用 mirror 标定
    return round(100 * conf), {"avg_logprob": round(lp, 3), "proxy": True}


def compose_turn(judge: dict, fl, pr, n_words: int) -> dict:
    """合成本轮综合分 + XP。缺维度按剩余权重归一, 不硬编。"""
    dims = {"grammar": judge.get("scores", {}).get("grammar"),
            "vocab": judge.get("scores", {}).get("vocab"),
            "fluency": fl, "pron": pr}
    avail = {k: v for k, v in dims.items() if v is not None}
    if not avail:
        return {"dims": dims, "composite": None, "xp": 0}
    wsum = sum(WEIGHTS[k] for k in avail)
    composite = round(sum(WEIGHTS[k] * v for k, v in avail.items()) / wsum)
    complexity = max(1, min(5, int(judge.get("complexity", 1))))
    xp = round(composite / 10 * complexity) if n_words >= MIN_WORDS else 0
    return {"dims": dims, "composite": composite, "xp": xp, "complexity": complexity}
