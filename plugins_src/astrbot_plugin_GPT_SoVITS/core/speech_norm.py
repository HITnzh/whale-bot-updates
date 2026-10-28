#!/usr/bin/env python3
"""鲸鱼娘「念稿前整理」: 把聊天文本改成 TTS(GPT-SoVITS / edge-tts) 好念的样子。

解决的问题：日期/时间/百分比/温度/小数/区间/电话号码/网址/波浪号/重复标点
被 TTS 念歪或卡顿。纯标准库，可单独用，也可在 strip_for_speech 之后调用。

用法：python3 speech_norm.py "明天10/28 15:30开会，降温到-3°C，概率80%~90%"
"""
import re, sys

D = "零一二三四五六七八九"
U = ["", "十", "百", "千"]

def digits(s):  # 逐位读：3060 -> 三零六零
    return "".join(D[int(c)] for c in s if c.isdigit())

def _4(n):  # 0..9999
    if n == 0: return ""
    s, zero, out = str(n), False, ""
    L = len(s)
    for i, c in enumerate(s):
        k = int(c); p = L - 1 - i
        if k == 0: zero = True; continue
        if zero and out: out += "零"
        zero = False
        out += D[k] + U[p]
    if out.startswith("一十"): out = out[1:]
    return out

def num(n):  # 整数 -> 中文
    n = int(n)
    if n == 0: return "零"
    neg = n < 0; n = abs(n)
    parts, units, i = [], ["", "万", "亿"], 0
    while n and i < 3:
        parts.append((n % 10000, units[i])); n //= 10000; i += 1
    out, need0 = "", False
    for v, u in reversed(parts):
        if v == 0: need0 = bool(out); continue
        if out and (need0 or v < 1000): out += "零"
        seg = _4(v)
        if out and seg.startswith("十"): seg = "一" + seg
        out += seg + u; need0 = False
    return ("负" if neg else "") + out

def dec(s):  # "3.14" -> 三点一四
    s = s.replace(",", "")
    neg = s.startswith("-"); s = s.lstrip("-+")
    a, _, b = s.partition(".")
    r = num(a or 0) + ("点" + digits(b) if b else "")
    return ("负" if neg else "") + r

def two(n):  # 时间里的分钟：05 -> 零五, 30 -> 三十
    n = int(n)
    return "" if n == 0 else ("零" + D[n] if n < 10 else num(n))

RULES = [
    (re.compile(r"https?://\S+|www\.\S+"), lambda m: "链接"),
    # 2026-10-27 / 2026/10/27 / 2026年10月27日
    (re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?"),
     lambda m: f"{digits(m[1])}年{num(m[2])}月{num(m[3])}日"),
    # 10/28 (月/日, 合法范围才转)
    (re.compile(r"(?<![\d/])(1[0-2]|0?[1-9])/([12]\d|3[01]|0?[1-9])(?![\d/])"),
     lambda m: f"{num(m[1])}月{num(m[2])}日"),
    # 15:30 / 7:05
    (re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)"),
     lambda m: f"{num(m[1])}点半" if m[2] == "30" else f"{num(m[1])}点" + (two(m[2]) + ("分" if int(m[2]) < 10 or int(m[2]) % 10 else "") if int(m[2]) else "")),
    # 区间 80%~90% / 3-5个 / 1~2
    (re.compile(r"(-?\d+(?:\.\d+)?)(%?)\s*[~～\-—到]\s*(-?\d+(?:\.\d+)?)%"),
     lambda m: f"百分之{dec(m[1])}到百分之{dec(m[3])}"),
    (re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[~～—]\s*(\d+(?:\.\d+)?)"),
     lambda m: f"{dec(m[1])}到{dec(m[2])}"),
    (re.compile(r"(-?\d+(?:\.\d+)?)\s*%"), lambda m: "百分之" + dec(m[1])),
    (re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°C|℃|度C)"), lambda m: dec(m[1]) + "摄氏度"),
    (re.compile(r"(-?\d+(?:\.\d+)?)\s*°"), lambda m: dec(m[1]) + "度"),
    # 手机号/QQ号/型号等长数字逐位读（5位以上，或 RTX 3060 这类型号）
    (re.compile(r"(?<![\d.])(1[3-9]\d{9})(?![\d.])"), lambda m: digits(m[1])),
    (re.compile(r"((?:QQ|qq|号码|号|编号|ID|id|尾号|验证码)[是为:：]?\s*)(\d{4,})"), lambda m: m[1] + digits(m[2])),
    (re.compile(r"(?<=[A-Za-z] )(\d{3,4})(?!\d)"), lambda m: digits(m[1])),
    (re.compile(r"(?<![\d.])(-?\d{1,3}(?:,\d{3})+|-?\d+\.\d+|-?\d+)(?![\d.])"), lambda m: dec(m[1])),
    # 语气符号：~ 变成拖音逗号，重复标点压缩
    (re.compile(r"[~～]+"), lambda m: "，"),
    (re.compile(r"([！!？?。，,…])\1+"), lambda m: m[1]),
    (re.compile(r"\.{3,}|…+"), lambda m: "…"),
]

def normalize(text: str) -> str:
    t = text or ""
    for rx, fn in RULES:
        t = rx.sub(fn, t)
    t = re.sub(r"([零一二三四五六七八九十百千万点])\s*-\s*(?=[零一二三四五六七八九十])", r"\1到", t)
    t = re.sub(r"，\s*([。！？!?])", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip(" ，")
    return t

if __name__ == "__main__":
    print(normalize(" ".join(sys.argv[1:]) or sys.stdin.read()))
