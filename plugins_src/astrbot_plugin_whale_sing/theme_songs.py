"""Theme short-song templates + lyric syllable checks (offline-testable)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Approximate Chinese syllable = character (engine may differ; we also allow
# digits/letters as 1 syllable each after normalization).
_SKIP = set(" ，,。.!！?？、；;：:""''\"（）()【】[]…—-~\t\n")


@dataclass
class SongTemplate:
    template_id: str
    theme: str
    mood: str
    bpm: int
    transpose: int
    chords: str
    base_lyrics: str
    notes_lines: list[str]
    syllables_per_line: list[int]
    path: str = ""

    def note_syllable_budget(self) -> list[int]:
        """Non-rest, non-tie note counts per notes line (singable slots)."""
        if self.syllables_per_line:
            return list(self.syllables_per_line)
        out = []
        for line in self.notes_lines:
            n = 0
            for tok in line.replace("notes:", "").split():
                tok = tok.strip()
                if not tok or tok.upper().startswith("R"):
                    continue
                if tok.startswith("~"):
                    continue
                n += 1
            out.append(n)
        return out


THEME_ALIASES = {
    "晚安": "wanan",
    "睡觉": "wanan",
    "好梦": "wanan",
    "加油": "jiayou",
    "鼓励": "jiayou",
    "打气": "jiayou",
    "庆祝": "qingzhu",
    "恭喜": "qingzhu",
    "生日快乐": "qingzhu",
    "通过": "qingzhu",
    "俏皮": "qiaopi",
    "调皮": "qiaopi",
    "摸鱼": "qiaopi",
    "开心": "qingzhu",
}


def count_syllables(text: str) -> int:
    t = normalize_lyrics_piece(text)
    return sum(1 for ch in t if ch not in _SKIP and not ch.isspace())


def normalize_lyrics_piece(text: str) -> str:
    """Normalize names/digits/english for singing length checks."""
    t = text or ""
    # Digits → keep as single tokens length 1 each already
    # English words → keep letters as 1 syllable each (engine dependent)
    t = re.sub(r"\s+", "", t)
    return t


def split_lyrics_to_lines(lyrics: str, budgets: list[int]) -> list[str] | None:
    chars = [ch for ch in normalize_lyrics_piece(lyrics) if ch not in _SKIP]
    need = sum(budgets)
    if len(chars) != need:
        return None
    lines = []
    i = 0
    for b in budgets:
        lines.append("".join(chars[i : i + b]))
        i += b
    return lines


def load_templates(directory: Path) -> dict[str, SongTemplate]:
    directory = Path(directory)
    out: dict[str, SongTemplate] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.txt")):
        tpl = parse_template_file(path)
        if tpl:
            out[tpl.template_id] = tpl
    return out


def parse_template_file(path: Path) -> SongTemplate | None:
    text = Path(path).read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    bpm = 90
    transpose = 5
    chords = ""
    lyrics = ""
    notes_lines: list[str] = []
    syllables: list[int] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            # # theme:晚安 id:wanan mood:comforting bpm:78
            for m in re.finditer(r"(\w+):([^\s]+)", line):
                meta[m.group(1)] = m.group(2)
            if "syllables_per_line" in line:
                nums = re.findall(r"\d+", line.split("syllables_per_line", 1)[-1])
                syllables = [int(x) for x in nums]
            continue
        if line.startswith("bpm:"):
            bpm = int(re.search(r"\d+", line).group())
        elif line.startswith("transpose:"):
            transpose = int(re.search(r"-?\d+", line).group())
        elif line.startswith("chords:"):
            chords = line.split(":", 1)[1].strip()
        elif line.startswith("lyrics:"):
            lyrics = line.split(":", 1)[1].strip()
        elif line.startswith("notes:"):
            notes_lines.append(line)
    tid = meta.get("id") or path.stem
    theme = meta.get("theme") or tid
    mood = meta.get("mood") or "calm"
    if meta.get("bpm"):
        bpm = int(meta["bpm"])
    return SongTemplate(
        template_id=tid,
        theme=theme,
        mood=mood,
        bpm=bpm,
        transpose=transpose,
        chords=chords,
        base_lyrics=lyrics,
        notes_lines=notes_lines,
        syllables_per_line=syllables,
        path=str(path),
    )


def select_template(
    templates: dict[str, SongTemplate],
    theme: str,
    mood: str | None = None,
) -> SongTemplate | None:
    if not templates:
        return None
    key = THEME_ALIASES.get((theme or "").strip(), None)
    if key and key in templates:
        return templates[key]
    # direct id
    if theme in templates:
        return templates[theme]
    # fuzzy by theme field
    for t in templates.values():
        if theme and theme in t.theme:
            return t
    if mood:
        for t in templates.values():
            if t.mood == mood:
                return t
    # default wanan
    return templates.get("wanan") or next(iter(templates.values()))


def fit_lyrics(
    template: SongTemplate,
    theme: str,
    recipient: str = "",
    *,
    llm_lyrics: str | None = None,
    max_fixes: int = 2,
) -> tuple[str, str]:
    """Return (lyrics_joined, reason). Falls back to base lyrics + dedication."""
    budgets = template.note_syllable_budget()
    need = sum(budgets)
    candidates: list[str] = []
    if llm_lyrics:
        candidates.append(llm_lyrics)
    # Deterministic short lyrics without LLM
    name = normalize_name(recipient)
    if template.template_id == "wanan":
        candidates.append(
            f"月光轻轻拍浪花，小鲸鱼眨着眼眶，愿你{name or '今晚'}好梦长，醒来还有白米饭"
        )
    elif template.template_id == "jiayou":
        candidates.append(
            f"深呼吸抬起头，困难都被吓跑，小鲸鱼给{name or '你'}鼓，加油你最棒哦"
        )
    elif template.template_id == "qingzhu":
        candidates.append(
            f"锣鼓敲得响亮，星星都在鼓掌，今天值得开饭庆祝，{name or '鲸鱼'}跳圈表扬"
        )
    else:
        candidates.append(
            f"哼哼藏起尾巴，假装没有偷懒，白米饭叫我去，摸鱼也要可爱"
        )
    candidates.append(template.base_lyrics)

    last_reason = "fallback"
    for attempt, cand in enumerate(candidates):
        # Try auto-trim/pad twice
        for fix in range(max_fixes + 1):
            lines = split_lyrics_to_lines(cand, budgets)
            if lines:
                return "，".join(lines), f"ok:{attempt}:{fix}"
            # adjust length
            chars = [ch for ch in normalize_lyrics_piece(cand) if ch not in _SKIP]
            if len(chars) > need:
                cand = "".join(chars[:need])
                last_reason = "trimmed"
            elif len(chars) < need:
                pad = template.base_lyrics
                pad_chars = [ch for ch in normalize_lyrics_piece(pad) if ch not in _SKIP]
                cand = "".join((chars + pad_chars)[:need])
                last_reason = "padded"
            else:
                last_reason = f"mismatch:{len(chars)}!={need}"
    # Absolute fallback: base lyrics forced to budget
    chars = [ch for ch in normalize_lyrics_piece(template.base_lyrics) if ch not in _SKIP]
    while len(chars) < need:
        chars += chars[: max(1, need - len(chars))]
    lines = []
    i = 0
    for b in budgets:
        lines.append("".join(chars[i : i + b]))
        i += b
    return "，".join(lines), "base_forced"


def normalize_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    # Keep 2–3 hanzi ideally; longer names truncate for melody
    n = re.sub(r"\s+", "", n)
    if re.fullmatch(r"\d+", n):
        # read digits as-is; length = digit count
        return n[:4]
    if re.fullmatch(r"[A-Za-z]+", n):
        return n[:6]
    return n[:3]


def parse_theme_request(text: str) -> dict[str, Any] | None:
    """Extract theme/recipient/mood/revision from NL without requiring jianpu."""
    t = (text or "").strip()
    t = re.sub(r"^[/／]?唱(?:歌|一首|个)?\s*", "", t)
    if not t:
        return None
    # Explicit jianpu path left to existing parser
    if "|" in t or t.lower().startswith("notes:") or t in ("demo",):
        return None

    revision = None
    if re.search(r"(快一点|慢一点|加点速|放慢)", t):
        revision = "tempo"
    elif re.search(r"(加上|加入).{0,4}(名字|我的名字)|带上我的名字", t):
        revision = "add_name"
    elif re.search(r"(换|改).{0,4}(开心|俏皮|温柔|加油|庆祝|晚安)", t):
        revision = "change_mood"

    recipient = ""
    m = re.search(r"(?:给|帮|为)(?P<name>[\w\u4e00-\u9fff]{1,8})(?:唱|来)", t)
    if m:
        recipient = m.group("name")
        if recipient in ("我", "咱", "本小姐"):
            recipient = ""

    theme = ""
    for k in THEME_ALIASES:
        if k in t:
            theme = k
            break
    if not theme and revision:
        theme = ""  # revision uses session theme
    if not theme and not revision:
        if re.search(r"(唱|歌|曲)", t):
            theme = "俏皮"  # soft default
        else:
            return None

    mood = None
    if "开心" in t or "高兴" in t:
        mood = "happy"
    elif "温柔" in t or "安静" in t:
        mood = "comforting"
    elif "俏皮" in t or "调皮" in t:
        mood = "playful"

    length = "short"
    return {
        "theme": theme,
        "recipient": recipient,
        "mood": mood,
        "length": length,
        "revision_of": revision,
        "raw": t,
    }


def template_to_pipeline_kwargs(template: SongTemplate, lyrics: str, *, bpm: int | None = None) -> dict[str, Any]:
    # Prefer song_file when path exists so sing.py reads full chart
    if template.path and Path(template.path).exists():
        # Write a temp-less approach: pass lyrics+notes
        notes = " ".join(
            ln.replace("notes:", "").strip() for ln in template.notes_lines
        )
        return {
            "lyrics": lyrics.replace("，", ""),
            "notes": notes,
            "bpm": int(bpm if bpm is not None else template.bpm),
            "transpose": int(template.transpose),
        }
    return {
        "lyrics": lyrics.replace("，", ""),
        "jianpu": "1 2 3 5 3 1--",  # should not happen if templates loaded
        "bpm": int(bpm if bpm is not None else template.bpm),
    }
