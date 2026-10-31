"""鲸鱼娘表情包插件.

LLM 在回复末尾可选地附加隐藏标记 ``[表情:标签]``（标签取自 stickers/index.json）。
本插件：
  1. on_llm_response: 从回复文本中剥离标记，把标签暂存在 event extra 中；
  2. on_decorating_result: 再次兜底剥离所有 Plain 中的标记（防泄漏），
     并按「概率 + 每会话冷却」决定是否附带一张该标签的随机表情图（本地文件）。
  3. /表情作者 (/credits): 回复署名与许可说明。

环境变量（可选）：
  WHALE_STICKER_DIR       表情目录，默认 $ASTRBOT_ROOT/stickers
  WHALE_STICKER_PROB      有标记时实际发图概率，默认 0.35
  WHALE_STICKER_COOLDOWN  同一会话两次发图之间至少间隔的回复条数，默认 3
  WHALE_STICKER_COOLDOWN_SEC  同一会话两次发图之间至少间隔秒数，默认 60
"""

import json
from pathlib import Path
from .reply_plan import EXTRA_KEY as PLAN_KEY, build_reply_plan, detect_persistent_pref, detect_turn_override
from .expression_prefs import ExpressionPrefStore
import os
import random
import re
import time

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse

MARKER_RE = re.compile(r"\s*[\[【［(（]\s*表情\s*[:：]\s*([^\]】］)）\n]{1,12})\s*[\]】］)）]\s*")
EXTRA_KEY = "whale_sticker_tag"
EXCLUDE = {"preview.png"}  # contact sheet, never sent

# 常见近义词 -> 包内已有标签（按顺序尝试，命中第一个存在的）
ALIASES: dict[str, list[str]] = {
    "摸摸": ["摸头", "求摸头"], "摸摸头": ["摸头", "求摸头"], "rua": ["摸头", "求摸头"],
    "抱抱": ["撒娇", "喜欢", "求摸头"], "求抱抱": ["撒娇", "求摸头"], "贴贴": ["喜欢", "撒娇"],
    "笑": ["开心", "哈哈"], "大笑": ["哈哈", "笑死"], "笑哭": ["笑死", "哈哈"], "嘿嘿": ["偷笑", "坏笑"],
    "窃笑": ["偷笑"], "奸笑": ["坏笑", "偷笑"], "高兴": ["开心"], "快乐": ["开心"], "耶耶": ["耶"],
    "哭": ["哭哭", "难过"], "大哭": ["哭哭"], "呜呜": ["哭哭", "委屈"], "伤心": ["难过", "哭哭"],
    "惊讶": ["震惊"], "吃惊": ["震惊"], "惊": ["震惊"], "啊这": ["无语", "尴尬"], "汗": ["尴尬", "无语"],
    "翻白眼": ["翻白眼", "白眼"], "鄙视": ["白眼", "斜眼"], "嫌弃": ["白眼", "无语"],
    "愤怒": ["生气"], "气": ["生气"], "不爽": ["不爽", "生气"], "哼哼": ["哼", "傲娇"],
    "困": ["犯困"], "好困": ["犯困"], "睡": ["睡觉"], "晚安安": ["晚安"], "早": ["早安"], "早上好": ["早安"],
    "你好": ["打招呼"], "嗨": ["打招呼"], "在吗": ["打招呼"], "拜拜": ["打招呼"], "再见": ["打招呼"],
    "饿": ["饿了"], "好饿": ["饿了"], "干饭": ["干饭", "吃饭"], "白饭": ["吃饭"], "吃": ["吃饭"],
    "钱": ["没钱"], "穷": ["没钱"], "投喂": ["求投喂"],
    "赞": ["点赞"], "牛": ["点赞", "佩服"], "厉害": ["佩服", "崇拜"], "好棒": ["太棒了", "点赞"],
    "冲": ["冲鸭", "加油"], "加油鸭": ["加油"], "努力": ["加油"],
    "谢": ["谢谢"], "感谢": ["谢谢"], "多谢": ["谢谢"],
    "不行": ["拒绝"], "不要": ["拒绝"], "才不": ["拒绝", "傲娇"], "好": ["好的"], "行": ["好的"], "收到": ["好的"],
    "嘚瑟": ["嘚瑟", "得意"], "骄傲": ["骄傲", "得意"], "炫耀": ["得意", "嘚瑟"],
    "害臊": ["害羞"], "羞": ["害羞"], "脸红": ["害羞"], "爱": ["喜欢", "比心"], "喜欢你": ["喜欢", "比心"],
    "疑问": ["疑惑"], "问号": ["疑惑"], "啥": ["疑惑"], "想想": ["思考"],
    "累": ["累了", "心累"], "心累了": ["心累"], "躺平": ["摆烂"], "摸鱼中": ["摸鱼"], "忙碌": ["忙碌", "忙", "加班"], "下头": ["下头", "无语", "白眼"],
    "怕": ["害怕"], "吓": ["吓一跳", "害怕"], "晕了": ["晕"], "懵了": ["懵"],
    "期待": ["期待"], "兴奋": ["兴奋"], "等": ["等待"], "等你": ["等待"],
    "求夸": ["求夸奖"], "夸我": ["求夸奖"], "卖萌": ["卖萌"], "可爱": ["卖萌"],
    # reply_plan / common phrasing (map to existing tags when present)
    "唱歌": ["听歌"], "音乐": ["听歌"], "听音乐": ["听歌"], "生日": ["庆祝", "送礼"], "生日快乐": ["庆祝", "送礼"],
    "鼓掌": ["点赞", "太棒了"], "吃瓜": ["吃瓜", "好奇", "偷偷"], "惊喜": ["星星眼", "震惊"], "感动": ["心动", "喜欢"],
    "emo": ["emo", "难过", "不开心"], "破防": ["破防", "崩溃", "哭哭"], "社死": ["尴尬", "崩溃"], "无奈": ["苦笑", "无语"],
    "叹气": ["心累", "无语"], "摊手": ["佛系", "无语"], "安慰": ["摸头", "喜欢"], "道歉": ["心虚", "委屈"],
    "对不起": ["心虚", "委屈"], "晚上好": ["打招呼"], "下班": ["庆祝", "伸懒腰"], "上班": ["加班", "忙"],
    "学习": ["思考", "加油"], "游戏": ["调皮", "兴奋"], "睡不着": ["熬夜"], "失眠": ["熬夜"], "赞同": ["点赞", "好的"],
    "嗯嗯": ["好的", "乖巧"], "同意": ["好的", "点赞"], "气鼓鼓": ["生气", "哼"], "吃醋": ["哼", "不爽"],
    "想你": ["想你", "喜欢", "等待"], "思念": ["等待", "喜欢"], "喵": ["卖萌", "撒娇"], "大肥鱼": ["摸鱼"], "鲸鱼": ["摸鱼"],
    "白米饭": ["吃饭", "干饭"], "米饭": ["吃饭", "干饭"],
    "开黑": ["游戏", "开黑"], "野餐": ["野餐", "出门"], "画画": ["画画", "创作"],
    "等一下": ["等待"], "雨天": ["下雨", "雨天"], "修bug": ["调试", "修bug"], "debug": ["调试", "修bug"],
    # Douyin-comment-style moods (phrases only; art is original / open-licensed)
    "破大防": ["破防", "崩溃", "哭哭"], "破防了": ["破防", "崩溃"], "好破防": ["破防", "崩溃"],
    "在吃瓜": ["吃瓜", "好奇", "偷偷"], "瓜": ["吃瓜", "好奇"], "看戏": ["吃瓜", "偷偷"],
    "我emo了": ["emo", "难过", "不开心"], "上完班": ["累了", "心累"],
    "上头": ["兴奋", "期待"], "绝绝子": ["太棒了", "星星眼"], "yyds": ["太棒了", "崇拜"],
    "栓Q": ["谢谢", "无语"], "退退退": ["害怕", "拒绝"], "拿下": ["胜利", "得意"],
    "已读乱回": ["已读不回", "无语"], "已读": ["已读不回"], "尊嘟假嘟": ["疑惑", "怀疑"],
    "宝子": ["卖萌", "打招呼"], "家人们": ["打招呼"], "谁懂啊": ["破防", "崩溃", "委屈"],
    "好想你": ["想你", "喜欢", "等待"], "想暴富": ["没钱", "穷"], "干饭人": ["干饭", "吃饭"],
    "摸鱼人": ["摸鱼"], "摆烂了": ["摆烂"], "救命": ["崩溃", "害怕"], "啊啊啊": ["崩溃", "兴奋"],
    "无语住了": ["无语"], "白眼了": ["翻白眼", "白眼"], "翻个白眼": ["翻白眼", "白眼"],
    "加油加油": ["加油", "冲鸭"], "晚安啦": ["晚安"], "忙疯了": ["忙碌", "忙", "加班"],
    "害羞羞": ["害羞", "脸红"], "委屈巴巴": ["委屈", "哭哭"], "得意坏了": ["得意", "嘚瑟"],
}


def _norm(t: str) -> str:
    return re.sub(r"[\s~～!！?？。，,、.…·]+", "", t or "").strip().lower()


def _default_dir() -> str:
    root = os.environ.get("ASTRBOT_ROOT") or os.getcwd()
    return os.environ.get("WHALE_STICKER_DIR") or os.path.join(root, "stickers")


def strip_markers(text: str) -> tuple[str, list[str]]:
    tags = [m.strip() for m in MARKER_RE.findall(text or "")]
    cleaned = MARKER_RE.sub(" ", text or "").strip() if tags else (text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, tags


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.dir = _default_dir()
        self.prob = float(os.environ.get("WHALE_STICKER_PROB", "0.35"))
        self.cooldown_n = int(os.environ.get("WHALE_STICKER_COOLDOWN", "3"))
        self.cooldown_s = float(os.environ.get("WHALE_STICKER_COOLDOWN_SEC", "60"))
        self.recent_n = int(os.environ.get("WHALE_STICKER_NOREPEAT", "3"))
        self.by_tag: dict[str, list[str]] = {}
        self.tags_of: dict[str, list[str]] = {}
        # context-aware pick inside crowded tags (e.g. 开心 x15); WHALE_STICKER_CTX=0 disables
        self.ctx_pick = os.environ.get("WHALE_STICKER_CTX", "1") != "0"
        self._resolve_cache: dict[str, str | None] = {}
        self.credits = ""
        # session -> [replies since last sticker, last sticker ts, recent paths]
        self.state: dict[str, list] = {}
        _pref = os.environ.get("WHALE_EXPR_PREF") or os.path.join(
            os.environ.get("ASTRBOT_ROOT") or os.getcwd(), "data", "expression_prefs.json"
        )
        self.expr_prefs = ExpressionPrefStore(Path(_pref))
        self._load()

    def _load(self) -> None:
        try:
            with open(os.path.join(self.dir, "index.json"), encoding="utf-8") as f:
                idx = json.load(f)
            for s in idx.get("stickers", []):
                p = os.path.join(self.dir, s["file"])
                if os.path.basename(p) in EXCLUDE:
                    continue
                if os.path.isfile(p):
                    self.tags_of[p] = list(s.get("tags", []))
                    for t in s.get("tags", []):
                        self.by_tag.setdefault(t, []).append(p)
            logger.info(f"[whale_stickers] loaded {sum(len(v) for v in self.by_tag.values())} sticker-tag entries, {len(self.by_tag)} tags from {self.dir}")
        except Exception as e:
            logger.error(f"[whale_stickers] failed to load index.json: {e}")
        self.credits = self._build_credits()

    def resolve_tag(self, tag: str) -> str | None:
        """Map whatever the model wrote to an existing tag: exact -> alias -> substring -> char overlap."""
        raw = _norm(tag)
        if not raw:
            return None
        if raw in self._resolve_cache:
            return self._resolve_cache[raw]
        tags = self.by_tag
        hit: str | None = None
        if raw in tags:
            hit = raw
        if hit is None:
            hit = next((t for t in tags if t.lower() == raw), None)
        if hit is None:
            for a in ALIASES.get(raw, []):
                if a in tags:
                    hit = a
                    break
        if hit is None:
            # substring either way, prefer the tag with most stickers then shortest
            subs = [t for t in tags if t in raw or raw in t]
            if subs:
                hit = sorted(subs, key=lambda t: (-len(tags[t]), abs(len(t) - len(raw))))[0]
        if hit is None:
            # alias keys contained in the raw text (e.g. "想要抱抱" -> 抱抱)
            for k, vs in ALIASES.items():
                if len(k) >= 2 and k in raw:
                    hit = next((a for a in vs if a in tags), None)
                    if hit:
                        break
        if hit is None:
            # closest by shared characters (needs at least one shared char)
            best, score = None, 0.0
            rs = set(raw)
            for t in tags:
                common = len(rs & set(t))
                if common:
                    sc = common / max(len(rs | set(t)), 1)
                    if sc > score:
                        best, score = t, sc
            hit = best if score >= 0.34 else None
        self._resolve_cache[raw] = hit
        if hit != raw:
            logger.info(f"[whale_stickers] tag {tag!r} -> {hit!r}")
        return hit


    def ctx_score(self, path: str, main_tag: str | None, context: str) -> int:
        """How many of this sticker's *other* tags (or their aliases) show up in the reply/user text."""
        if not context:
            return 0
        ctx = _norm(context)
        sc = 0
        for t in self.tags_of.get(path, []):
            if t == main_tag or len(t) < 2:
                continue
            words = [t.lower()] + [k for k, vs in ALIASES.items() if t in vs and len(k) >= 2]
            if any(w in ctx for w in words):
                sc += 1
        return sc

    def pick(self, st: list, files: list[str], main_tag: str | None = None, context: str = "") -> str:
        """Random sticker avoiding the last ``recent_n`` sent in this session.

        If the reply/user text mentions another tag of some candidates (e.g. 开心 + 下雪),
        prefer those candidates; otherwise plain random as before."""
        if len(st) < 3 or not isinstance(st[2], list):
            # migrate old last-path str -> list
            old = st[2] if len(st) > 2 and isinstance(st[2], str) else None
            st[2:] = [[old] if old else []]
        hist: list[str] = st[2]
        win = min(self.recent_n, max(len(files) - 1, 0))
        recent = set(hist[-win:]) if win else set()
        pool = [f for f in files if f not in recent] or files
        if self.ctx_pick and context and len(pool) > 1:
            scored = [(self.ctx_score(f, main_tag, context), f) for f in pool]
            best = max(sc for sc, _ in scored)
            if best > 0:
                pool = [f for sc, f in scored if sc == best]
        path = random.choice(pool)
        hist.append(path)
        del hist[:-max(self.recent_n, 1) * 4]
        return path

    def _build_credits(self) -> str:
        srcs = []
        try:
            with open(os.path.join(self.dir, "CREDITS.md"), encoding="utf-8") as f:
                md = f.read()
            for m in re.finditer(r"^### (\S+).*?\n(?:.*\n)*?- 作者：(.+)", md, re.M):
                who, url = m.group(2).strip(), m.group(1).strip()
                who = re.sub(r"^作者[:：]", "", who)
                srcs.append(f"· {who}" if url in who else f"· {who}（{url}）")
        except Exception:
            pass
        head = (
            "鲸鱼娘表情包署名\n"
            "角色原作：上善无形（OC「溟月」）；DeepSeek 女仆装二设：ZipZipPipe。\n"
            "整套表情按 CC BY-NC-SA 4.0 使用：须署名、仅限非商业、改编作品相同方式共享。"
            "非 DeepSeek 官方素材，与深度求索公司无关。\n素材来源：\n"
        )
        tail = "\n许可：https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans\n权利人如需移除请联系机器人维护者。"
        return head + "\n".join(srcs) + tail

    # 1) strip marker from the raw LLM response (keeps stored history clean too)
    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        try:
            text = resp.completion_text or ""
            cleaned, tags = strip_markers(text)
            if tags:
                resp.completion_text = cleaned
                event.set_extra(EXTRA_KEY, tags[-1])
            # Build shared reply_plan once (stickers + TTS consume it)
            try:
                user_text = event.get_message_str() or event.message_str or ""
                sid = f"{event.get_platform_name()}:{event.get_sender_id()}"
                pref = self.expr_prefs.get_mode(sid) if getattr(self, "expr_prefs", None) else None
                pers = detect_persistent_pref(user_text)
                if pers and self.expr_prefs:
                    self.expr_prefs.set_mode(sid, pers)
                    pref = pers
                turn = detect_turn_override(user_text)
                plan = build_reply_plan(
                    reply_text=resp.completion_text or cleaned or text,
                    user_text=user_text,
                    preference_mode=turn or pref,
                    sticker_tag=(tags[-1] if tags else event.get_extra(EXTRA_KEY)),
                )
                event.set_extra(PLAN_KEY, plan.to_dict())
            except Exception as _pe:
                logger.debug(f"[whale_stickers] reply_plan: {_pe}")
        except Exception as e:
            logger.error(f"[whale_stickers] on_llm_response: {e}")

    # 2) final safety strip + maybe attach sticker
    @filter.on_decorating_result()
    async def on_decorate(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if result is None or not result.chain:
            return
        found: list[str] = []
        new_chain = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                cleaned, tags = strip_markers(comp.text)
                found += tags
                if tags:
                    if not cleaned.strip():
                        continue
                    comp = Plain(cleaned)
            new_chain.append(comp)
        result.chain = new_chain
        tag = event.get_extra(EXTRA_KEY) or (found[-1] if found else None)
        plan = event.get_extra(PLAN_KEY) or {}
        if plan.get("sticker_tag"):
            tag = plan.get("sticker_tag")
        if plan.get("mode") in ("text", "voice", "text_voice") and not plan.get("sticker_tag"):
            # plan says no sticker this turn
            tag = None
        if not result.is_llm_result():
            return
        sid = event.unified_msg_origin
        st = self.state.setdefault(sid, [999, 0.0, []])
        st[0] += 1
        if not tag:
            return
        resolved = self.resolve_tag(tag)
        files = self.by_tag.get(resolved) if resolved else None
        if not files:
            logger.info(f"[whale_stickers] unknown tag {tag!r}, ignored")
            return
        if any(isinstance(c, Image) for c in result.chain):
            return
        if st[0] < self.cooldown_n or time.time() - st[1] < self.cooldown_s:
            logger.info(f"[whale_stickers] tag={tag} skipped (cooldown)")
            return
        if random.random() >= self.prob:
            logger.info(f"[whale_stickers] tag={tag} skipped (prob)")
            return
        tag = resolved
        ctx_text = " ".join(c.text for c in result.chain if isinstance(c, Plain))
        try:
            ctx_text += " " + (event.get_message_str() or "")
        except Exception:
            pass
        path = self.pick(st, files, tag, ctx_text)
        result.chain.append(Image.fromFileSystem(path))
        st[0], st[1] = 0, time.time()
        logger.info(f"[whale_stickers] tag={tag} attached {os.path.basename(path)}")

    @filter.command("表情作者", alias={"credits", "表情署名"})
    async def credits_cmd(self, event: AstrMessageEvent):
        """查看鲸鱼娘表情包的作者署名与许可"""
        yield event.plain_result(self.credits)
