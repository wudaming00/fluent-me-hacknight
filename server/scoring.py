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
    """ASR 置信度 → pron 代理分。两套标定 (分布不同): whisper avg_logprob vs Scribe v2 词级 logprob。
    仍是 proxy, 界面照旧标注; Scribe 路径额外产出低置信词表 (词级发音标注)。"""
    words = [w for w in text.split() if w]
    if len(words) < MIN_WORDS:
        return None, {"note": "sample too short"}
    lp = stt.get("avg_logprob")
    if lp is None:
        return None, {"note": "no confidence signal"}
    if stt.get("conf_source") == "scribe":
        # Scribe v2 词级 logprob: 0 最自信。经验带 [-0.02, -0.5] → [100, 0] 线性
        conf = max(0.0, min(1.0, 1.0 - (abs(lp) - 0.02) / 0.48))
        unclear = [w["text"] for w in stt.get("words", [])
                   if w.get("logprob") is not None and w["logprob"] < -0.35][:6]
        return round(100 * conf), {"avg_logprob": round(lp, 3), "proxy": True,
                                   "unclear_words": unclear}
    conf = max(0.0, min(1.0, (lp + 1.0) / 0.9))   # whisper: [-1,-0.1] → [0,1], 沿用 mirror 标定
    return round(100 * conf), {"avg_logprob": round(lp, 3), "proxy": True}


def _tokens(text: str) -> list:
    return [w for w in re.sub(r"[^a-z0-9' ]", " ", text.lower()).split() if w]


def token_overlap(a: str, b: str) -> float:
    """无序 token 重叠率 |A∩B| / min(|A|,|B|) — echo 形式核对用 (recast 被包含即算说到位)。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    from collections import Counter
    inter = sum((Counter(ta) & Counter(tb)).values())
    return inter / min(len(ta), len(tb))


def is_parrot(heard: str, recast: str) -> bool:
    """防鹦鹉: 学员本轮是否只是复读上一条 recast。分母用学员句长 —
    把纠正自然嵌进更长的句子 (恰恰是想奖励的行为) 不算复读。"""
    th, tr = _tokens(heard), _tokens(recast)
    if not th or not tr:
        return False
    from collections import Counter
    inter = sum((Counter(th) & Counter(tr)).values())
    return inter / len(th) >= 0.75 and len(th) <= len(tr) + 3


def echo_compare(echo_stt: dict, echo_text: str, recast: str, orig: dict) -> dict:
    """echo 跟读 vs 目标句 + vs 首次尝试。纯本地信号, 无 LLM。
    结果只报方向 + 粗 delta (措辞: rough single-sentence signal, 不装精度)。"""
    match = token_overlap(echo_text, recast)
    passed = match >= 0.8
    out = {"match": round(match, 2), "passed": passed, "heard": echo_text}
    fl, fl_meta = fluency_score(echo_stt, echo_text)
    pr, pr_meta = pron_score(echo_stt, echo_text)
    if fl is not None and orig.get("fl") is not None:
        out["fluency_delta"] = fl - orig["fl"]
    if pr is not None and orig.get("pr") is not None:
        out["pron_delta"] = pr - orig["pr"]
    if fl_meta.get("fillers") is not None and orig.get("fillers") is not None:
        out["fillers_delta"] = fl_meta["fillers"] - orig["fillers"]
    return out


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
