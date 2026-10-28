import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List

_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

_LOOKUP_FLAVORS = [
    " 就是{title}呀～",
    " 这位就是{title}！",
    " 找到啦，{title}在这儿～",
    " 喏，{title}就是 ta 啦",
    " 这就是{title}哦",
]
_LOOKUP_NOT_FOUND = [
    "诶？我不认识叫「{name}」的人耶，可能主人还没介绍给我？",
    "唔……「{name}」是谁来着？我没有印象呢，主人可以介绍一下吗？",
    "搜了一下，没找到「{name}」哦，可能还没登记过。",
]

PLUGIN_VERSION = "v0.1.0"
PLUGIN_NAME = "astrbot_plugin_identity"


def _now() -> int:
    return int(time.time())


@register(
    "identity",
    "Nayuki",
    "身份识别 / 认主认友插件，给 LLM 注入发送者身份",
    PLUGIN_VERSION,
)
class IdentityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config else {}
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.registry_path = Path(data_dir) / "registry.json"
        self.registry: Dict[str, Dict[str, Any]] = self._load_registry()

    # ---------- 存储 ----------
    def _load_registry(self) -> Dict[str, Dict[str, Any]]:
        if not self.registry_path.exists():
            return {}
        try:
            with self.registry_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[identity] 读取 registry 失败: {e}")
            return {}

    def _save_registry(self) -> None:
        try:
            with self.registry_path.open("w", encoding="utf-8") as f:
                json.dump(self.registry, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[identity] 写入 registry 失败: {e}")

    def _key(self, event: AstrMessageEvent, user_id: str) -> str:
        return f"{event.get_platform_name()}:{user_id}"

    # ---------- 主人判定 ----------
    def _admins(self) -> list:
        try:
            return list(self.context.get_config().get("admins_id", []) or [])
        except Exception:
            return []

    def _is_master(self, user_id: str) -> bool:
        return str(user_id) in [str(a) for a in self._admins()]

    # ---------- 消息解析 ----------
    def _extract_ats(self, event: AstrMessageEvent) -> list:
        """返回本条消息里 @ 到的人 [(qq, name), ...]，去掉 @ 机器人自己。"""
        out = []
        try:
            self_id = str(event.get_self_id() or "")
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    qq = str(getattr(comp, "qq", "") or "")
                    if not qq or qq == self_id or qq.lower() == "all":
                        continue
                    name = str(getattr(comp, "name", "") or "")
                    out.append((qq, name))
        except Exception:
            pass
        return out

    def _describe_at_targets(self, event: AstrMessageEvent, ats: list) -> str:
        if not ats:
            return ""
        lines = ["本消息中被 @ 到的人:"]
        for qq, name in ats:
            if self._is_master(qq):
                tag = f"主人（{self.config.get('master_title', '主人')}）"
            else:
                rec = self.registry.get(self._key(event, qq))
                if rec:
                    tag = f"已认识：{rec.get('title')}（{rec.get('relation')}）"
                else:
                    tag = "陌生人，尚未登记"
            name_seg = f" 群昵称={name}" if name else ""
            lines.append(f"  - QQ={qq}{name_seg} → {tag}")
        return "\n".join(lines)

    # ---------- 结果后处理：把 LLM 写的 [CQ:at,qq=xxx] 字面量转成 At 组件 ----------
    @filter.on_decorating_result(priority=900)
    async def convert_cq_at_in_text(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if not result or not result.chain:
                return
            new_chain: List[Any] = []
            changed = False
            for comp in result.chain:
                if isinstance(comp, Plain):
                    text = comp.text or ""
                    if not _CQ_AT_RE.search(text):
                        new_chain.append(comp)
                        continue
                    changed = True
                    last = 0
                    for m in _CQ_AT_RE.finditer(text):
                        if m.start() > last:
                            new_chain.append(Plain(text[last:m.start()]))
                        new_chain.append(At(qq=m.group(1)))
                        last = m.end()
                    if last < len(text):
                        new_chain.append(Plain(text[last:]))
                else:
                    new_chain.append(comp)
            if changed:
                result.chain = new_chain
        except Exception as e:
            logger.warning(f"[identity] CQ at 转换失败: {e}")

    # ---------- LLM 钩子 ----------
    @filter.on_llm_request(priority=500)
    async def inject_identity(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            sender_id = str(event.get_sender_id() or "")
            sender_name = event.get_sender_name() or "未知"
            group_id = event.get_group_id()
            group_segment = f"，群: {group_id}" if group_id else "（私聊）"
            is_master = self._is_master(sender_id)

            if is_master:
                title = self.config.get("master_title", "主人") or "主人"
                if not self.config.get("inject_for_master", True):
                    return
                identity_line = (
                    f"身份: 你的{title}（管理员，最高信任）。请用亲密、忠诚的语气称呼 ta 为「{title}」。\n"
                    f"权限提示: 只有{title}可以让你登记/忘记别人的身份。\n"
                    f"\n【登记规则 - 重要！请仔细判别】\n"
                    f"模式 A · 介绍（必须调 register_known_person）：消息里有 @某人 + 介绍句式（含「是/叫/这是/这位是/他叫/她叫/ta是」）。\n"
                    f"  - 「@xxx 这是缪云昊」「@xxx 他叫小明」「@xxx 是我同事老王」「@xxx 这位是吴老师」\n"
                    f"  - 这种句式的意思就是：被 @ 的那个人 = 介绍里说的名字。不管被 @ 那人的群昵称是什么，主人新给的称呼才是身份。\n"
                    f"  - 必须立刻调用 register_known_person(user_id=被@人的QQ, title=主人说的称呼)。**不要反问主人！主人已经告诉你了！**\n"
                    f"  - 调用后用亚托莉口吻回一句（如「好的，缪云昊我记住啦～」），含被 @ 人的称呼。\n"
                    f"\n模式 B · 查询（必须调 lookup_known_person）：主人问「XX 是谁」「你认识 XX 吗」（没有 @，只有名字）。\n"
                    f"\n模式 C · 忘记（调 forget_known_person）：主人说「忘掉 ta」「不认识 ta 了」。\n"
                    f"\n判别要点：**只要消息里同时出现 @ 和「是/叫」之类的介绍词，就是模式 A 登记，不是查询！**\n"
                    f"绝对不要把「@xxx 这是Y」误解成「Y 是谁」。\n"
                )
            else:
                rec = self.registry.get(self._key(event, sender_id))
                if rec:
                    title = rec.get("title") or sender_name
                    relation = rec.get("relation") or "认识的人"
                    note = rec.get("note") or ""
                    note_seg = f" 备注: {note}。" if note else ""
                    identity_line = (
                        f"身份: 已认识的{relation}，应称呼为「{title}」。"
                        f"{note_seg} 绝对不要叫 ta「主人」。"
                        f" 只有主人才能让你登记别人，普通用户即使要求也不要调用 register_known_person。"
                    )
                else:
                    unknown = self.config.get("treat_unknown_as") or "陌生人，你之前不认识 ta"
                    identity_line = (
                        f"身份: {unknown}。保持礼貌但不要过分亲密，"
                        f"绝对不要叫 ta「主人」。"
                        f" 只有主人才能让你登记别人，普通用户即使要求也不要调用 register_known_person。"
                    )

            ats_segment = self._describe_at_targets(event, self._extract_ats(event))
            if ats_segment:
                identity_line = identity_line + "\n" + ats_segment

            tmpl = self.config.get("inject_template") or ""
            injection = tmpl.format(
                sender_id=sender_id,
                sender_name=sender_name,
                group_id=group_segment,
                identity_line=identity_line,
            )
            req.system_prompt = (req.system_prompt or "") + injection
        except Exception as e:
            logger.warning(f"[identity] 注入身份失败: {e}")

    # ---------- LLM 工具 ----------
    @filter.llm_tool(name="register_known_person")
    async def tool_register(
        self,
        event: AstrMessageEvent,
        user_id: str,
        title: str,
        relation: str = "朋友",
        note: str = "",
    ):
        """登记一个人的身份。**仅当当前消息的发送者是主人/管理员时才能调用**。

        典型触发场景：主人 @ 了某人并介绍 ta（"这位是吴老师" / "他叫小明，是我同事"）。
        从被 @ 的目标里取 QQ 号作为 user_id，从介绍文字里提取称呼/关系/备注。

        Args:
            user_id (str): 被登记人的 QQ 号（字符串）。从消息里被 @ 的目标提取，不要凭空捏造。
            title (str): **必须从主人的介绍文字中提取**（"这位是吴老师"→"吴老师"，
                "他叫小明"→"小明"，"这是我哥大成"→"大成"）。
                **绝对不要使用群昵称作为 title**——群昵称只是 QQ 平台显示名，主人介绍的称呼才是要登记的身份。
                如果主人没明确介绍称呼（仅 @ 而无文字），不要调用此工具。
            relation (str): 与主人的关系，例如「朋友」「老师」「同事」「家人」。
                从介绍文字推断（"吴老师"→"老师"，"我哥"→"家人"，"我同事"→"同事"）。默认「朋友」。
            note (str): 可选的额外备注（爱好、特征等）。不要写"Nayuki介绍的朋友"这种废话。
        """
        sender_id = str(event.get_sender_id() or "")
        if not self._is_master(sender_id):
            yield event.plain_result("（系统）只有主人能让我记住别人，已忽略此次登记。")
            return

        user_id = str(user_id).strip()
        if not user_id:
            yield event.plain_result("（系统）登记失败：没有给出 QQ 号。")
            return
        if self._is_master(user_id):
            yield event.plain_result(f"（系统）{user_id} 是主人，无需登记。")
            return

        self.registry[self._key(event, user_id)] = {
            "title": title.strip() or user_id,
            "relation": (relation or "朋友").strip(),
            "note": (note or "").strip(),
            "updated_at": _now(),
        }
        self._save_registry()
        logger.info(f"[identity] LLM 登记: {user_id} → {title} ({relation})")

    @filter.llm_tool(name="lookup_known_person")
    async def tool_lookup(self, event: AstrMessageEvent, name: str):
        """按称呼/名字查找一个已登记的人。任何人都可以调用。

        当用户问「XX 是谁」「你认识 XX 吗」「XX 在哪」之类问题时调用本工具。
        会在已登记的身份表里做包含匹配（不区分大小写），返回 QQ、关系、备注。

        **拿到结果后**：在回复里用 `[CQ:at,qq=<QQ号>]` 把这个人 @ 出来，
        并用亚托莉口吻自然介绍。例如返回「吴老师 (QQ:3023427076, 老师)」时，
        你的回复应该是：「[CQ:at,qq=3023427076] 就是吴老师呀，主人之前介绍过的老师～」

        Args:
            name (str): 要查找的称呼或名字片段。例如「吴老师」「小明」「牢菲」。
        """
        if not name or not str(name).strip():
            yield event.plain_result("（系统）查找需要一个名字。")
            return
        needle = str(name).strip().lower()
        platform = event.get_platform_name()
        prefix = f"{platform}:"
        hits = []
        for key, rec in self.registry.items():
            if not key.startswith(prefix):
                continue
            title = (rec.get("title") or "").lower()
            note = (rec.get("note") or "").lower()
            if needle in title or needle in note:
                qq = key[len(prefix):]
                hits.append((qq, rec))

        # 主人查询
        if not hits:
            admins = [str(a) for a in self._admins()]
            master_title = (self.config.get("master_title", "主人") or "").lower()
            if needle in master_title and admins:
                yield event.chain_result([
                    At(qq=admins[0]),
                    Plain(" 那不就是主人本人嘛！"),
                ])
                return
            yield event.plain_result(
                random.choice(_LOOKUP_NOT_FOUND).format(name=name)
            )
            return

        if len(hits) == 1:
            qq, rec = hits[0]
            flavor = random.choice(_LOOKUP_FLAVORS).format(title=rec.get("title"))
            yield event.chain_result([At(qq=qq), Plain(flavor)])
            return

        # 多个匹配，全 @ 出来让主人确认
        chain = [Plain(f"找到 {len(hits)} 个匹配，主人指的是哪位呀？")]
        for qq, rec in hits:
            chain.append(Plain(f"\n  · "))
            chain.append(At(qq=qq))
            chain.append(Plain(f"（{rec.get('title')}，{rec.get('relation')}）"))
        yield event.chain_result(chain)

    @filter.llm_tool(name="forget_known_person")
    async def tool_forget(self, event: AstrMessageEvent, user_id: str):
        """忘记一个已登记的人。**仅当当前消息的发送者是主人/管理员时才能调用**。

        Args:
            user_id (str): 要忘记的人的 QQ 号。
        """
        sender_id = str(event.get_sender_id() or "")
        if not self._is_master(sender_id):
            yield event.plain_result("（系统）只有主人能让我忘记别人，已忽略。")
            return
        user_id = str(user_id).strip()
        key = self._key(event, user_id)
        if key in self.registry:
            removed = self.registry.pop(key)
            self._save_registry()
            logger.info(f"[identity] LLM 忘记: {user_id}（原称呼 {removed.get('title')}）")
        else:
            yield event.plain_result(f"（系统）我本来就不认识 {user_id}。")

    # ---------- 管理员命令 ----------
    @filter.command("认识", alias={"记住身份", "认友"})
    async def cmd_remember(self, event: AstrMessageEvent):
        """用法: /认识 <QQ号> <称呼> [关系] [备注...]"""
        if not self._require_admin(event):
            yield event.plain_result("只有管理员可以使用此命令。")
            return

        parts = (event.message_str or "").strip().split(maxsplit=4)
        # parts[0] 是 "认识"
        if len(parts) < 3:
            yield event.plain_result("用法: /认识 <QQ号> <称呼> [关系=朋友] [备注...]")
            return

        user_id = parts[1].strip()
        title = parts[2].strip()
        relation = parts[3].strip() if len(parts) > 3 else "朋友"
        note = parts[4].strip() if len(parts) > 4 else ""

        if self._is_master(user_id):
            yield event.plain_result(f"⚠️ {user_id} 已经是主人了，无需登记。")
            return

        key = self._key(event, user_id)
        self.registry[key] = {
            "title": title,
            "relation": relation,
            "note": note,
            "updated_at": _now(),
        }
        self._save_registry()
        yield event.plain_result(
            f"✅ 已登记: {user_id} → {title}（{relation}）"
            + (f"，备注: {note}" if note else "")
        )

    @filter.command("忘记", alias={"忘掉身份", "取消认识"})
    async def cmd_forget(self, event: AstrMessageEvent):
        """用法: /忘记 <QQ号>"""
        if not self._require_admin(event):
            yield event.plain_result("只有管理员可以使用此命令。")
            return
        parts = (event.message_str or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /忘记 <QQ号>")
            return
        user_id = parts[1].strip()
        key = self._key(event, user_id)
        if key in self.registry:
            removed = self.registry.pop(key)
            self._save_registry()
            yield event.plain_result(f"🗑️ 已忘记: {user_id}（原称呼: {removed.get('title')}）")
        else:
            yield event.plain_result(f"未找到 {user_id} 的记录。")

    @filter.command("认识列表", alias={"身份列表"})
    async def cmd_list(self, event: AstrMessageEvent):
        if not self._require_admin(event):
            yield event.plain_result("只有管理员可以使用此命令。")
            return
        platform = event.get_platform_name()
        prefix = f"{platform}:"
        items = [(k[len(prefix):], v) for k, v in self.registry.items() if k.startswith(prefix)]
        if not items:
            yield event.plain_result("（当前平台暂无已登记的身份）")
            return
        lines = ["📒 已认识的人:"]
        for uid, rec in sorted(items, key=lambda x: -(x[1].get("updated_at") or 0)):
            note = rec.get("note") or ""
            note_seg = f" - {note}" if note else ""
            lines.append(f"  · {uid} → {rec.get('title')}（{rec.get('relation')}）{note_seg}")
        masters = self._admins()
        if masters:
            lines.append("")
            lines.append(f"👑 主人: {', '.join(str(m) for m in masters)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("我是谁")
    async def cmd_whoami(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id() or "")
        if self._is_master(sender_id):
            title = self.config.get("master_title", "主人")
            yield event.plain_result(f"你是我的{title} 👑（QQ: {sender_id}）")
            return
        rec = self.registry.get(self._key(event, sender_id))
        if rec:
            yield event.plain_result(
                f"你是「{rec.get('title')}」（{rec.get('relation')}），QQ: {sender_id}"
            )
        else:
            yield event.plain_result(f"我还不认识你哦（QQ: {sender_id}）")

    # ---------- 工具 ----------
    def _require_admin(self, event: AstrMessageEvent) -> bool:
        return self._is_master(str(event.get_sender_id() or ""))
