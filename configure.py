#!/usr/bin/env python3
"""Idempotently wire DeepSeek provider, companion persona, plugins and
(optionally) the qq_official adapter into AstrBot's data/. Run while AstrBot is
STOPPED (start.bat / start.sh calls it automatically before launching).

Env vars (never published; read from secrets.env on the PC):
  DEEPSEEK_API_KEY     - chat key; also used for DeepSeek web search (written into
                         local cmd_config only — websearch keys do not support $ENV refs)
  DEEPSEEK_MODEL       - default deepseek-flash (vision + tool_use)
  SILICONFLOW_API_KEY  - optional; enables OpenAI-compatible Embedding for LivingMemory
                         (recommended free model: BAAI/bge-m3)
  OWNER_OPENID         - optional; QQ official openid of the master (NOT numeric QQ).
                         Get it by chatting privately and sending /sid
  QQ_APPID/QQ_SECRET   - optional if platform already created via WebUI scan
  PERSONA_FILE         - default persona/system_prompt.txt
"""
import datetime, json, os, sqlite3
BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "data", "cmd_config.json")
DB = os.path.join(BASE, "data", "data_v4.db")
PLUGIN_CFG = os.path.join(BASE, "data", "config")
PERSONA_ID = "鲸鱼娘"
EMBED_ID = "siliconflow_embedding"


def sticker_rule() -> str:
    idx = os.path.join(BASE, "stickers", "index.json")
    if not os.path.exists(idx):
        return ""
    count: dict[str, int] = {}
    for st in json.load(open(idx, encoding="utf-8")).get("stickers", []):
        if st.get("file") == "preview.png":
            continue
        for t in st.get("tags", []):
            count[t] = count.get(t, 0) + 1
    tags = sorted(count, key=lambda t: -count[t])
    return ("\n\n【表情包】\n"
            "你可以偶尔在情绪明显的回复末尾加一个表情标记，格式严格为 [表情:标签]，标签只能从下面原样选一个："
            + "、".join(tags) + "。\n"
            "- 每条回复最多一个标记，放在最后；大多数回复不要加，大约三四条里最多一次。\n"
            "- 选和你这句话情绪最贴的标签，比如被摸头用「摸头」、道晚安用「晚安」、被夸用「得意」或「害羞」、无语用「无语」。\n"
            "- 标记会被系统自动换成一张表情图，对方看不到标记文字；不要解释或提起它，不要自己编新标签，也不要发预览图。\n"
            "- 严肃话题（对方难过、危机、求助）时不要加。")


def install_plugins():
    import shutil
    src_root = os.path.join(BASE, "plugins_src")
    dst_root = os.path.join(BASE, "data", "plugins")
    if not os.path.isdir(src_root):
        return
    os.makedirs(dst_root, exist_ok=True)
    for name in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, name)
        if os.path.isdir(src):
            dst = os.path.join(dst_root, name)
            if os.path.islink(dst):
                os.unlink(dst)
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
            print(f"[configure] plugin installed: {name}")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def merge_plugin_config(name: str, patch: dict):
    """Merge keys into data/config/<name>_config.json (AstrBot's real plugin config)."""
    path = os.path.join(PLUGIN_CFG, f"{name}_config.json")
    cur = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                cur = json.load(f) or {}
        except Exception:
            cur = {}
    def deep(a, b):
        out = dict(a)
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = deep(out[k], v)
            else:
                # only fill missing / empty lists for session_list style fields
                if k in out and out[k] not in (None, "", [], {}):
                    if isinstance(v, dict):
                        continue
                    # keep user value
                    continue
                out[k] = v
        return out
    merged = deep(cur, patch)
    # Always force these critical ids when we know them
    for k, v in patch.items():
        if k.endswith("_provider_id") or k == "embedding_provider_id":
            merged[k] = v
        if isinstance(v, dict) and "embedding_provider_id" in v:
            merged.setdefault(k, {})
            if isinstance(merged[k], dict):
                merged[k]["embedding_provider_id"] = v["embedding_provider_id"]
                if v.get("llm_provider_id"):
                    merged[k]["llm_provider_id"] = v["llm_provider_id"]
    _write_json(path, merged)
    print(f"[configure] plugin config: {name}")


def configure_web_search(ps: dict):
    """Enable DeepSeek built-in web search. Key must be concrete (no $ENV)."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    ps["web_search"] = True
    ps["websearch_provider"] = "deepseek"
    ps["web_search_link"] = True
    if key:
        ps["websearch_deepseek_key"] = [key]
        print("[configure] web_search=deepseek (key from env)")
    else:
        print("[configure] WARN web_search enabled but DEEPSEEK_API_KEY empty — fill in WebUI")


def configure_embedding(c: dict, chat_pid: str):
    """Add SiliconFlow OpenAI-compatible embedding when SILICONFLOW_API_KEY is set."""
    key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        print("[configure] LivingMemory embedding skipped (set SILICONFLOW_API_KEY for BAAI/bge-m3)")
        return False
    emb = {
        "id": EMBED_ID,
        "type": "openai_embedding",
        "provider": "openai",
        "provider_type": "embedding",
        "enable": True,
        "embedding_api_key": key,
        "embedding_api_base": "https://api.siliconflow.cn/v1",
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimensions": 1024,
        "embedding_dimensions_mode": "auto",
        "timeout": 60,
        "proxy": "",
    }
    c["provider"] = [p for p in c.get("provider", []) if p.get("id") != EMBED_ID] + [emb]
    merge_plugin_config("astrbot_plugin_livingmemory", {
        "bot_language": "zh",
        "provider_settings": {
            "embedding_provider_id": EMBED_ID,
            "llm_provider_id": chat_pid,
            "llm_stream_mode": "auto",
        },
        "filtering_settings": {
            "memory_scope_mode": "user",
            "use_session_filtering": False,
            "use_persona_filtering": True,
        },
        "session_manager": {
            "enable_full_group_capture": False,
        },
    })
    print(f"[configure] embedding={EMBED_ID} (SiliconFlow BAAI/bge-m3)")
    return True


def configure_owner(c: dict):
    openid = os.environ.get("OWNER_OPENID", "").strip()
    if not openid:
        print("[configure] OWNER_OPENID not set — 认主 needs WebUI admins_id = your openid from /sid")
        return
    admins = list(c.get("admins_id") or [])
    # keep astrbot dashboard user; ensure openid present
    if "astrbot" not in admins:
        admins.append("astrbot")
    if openid not in admins:
        admins.append(openid)
    c["admins_id"] = admins
    print("[configure] admins_id += OWNER_OPENID (认主)")


def configure_proactive_defaults():
    """Seed gentle defaults; session UMO list must be filled by user via /sid."""
    merge_plugin_config("astrbot_plugin_proactive_chat", {
        "friend_settings": {
            "enable": True,
            "session_list": [],
            "auto_trigger_settings": {
                "enable_auto_trigger": True,
                "auto_trigger_after_minutes": 45,
            },
        },
    })


def configure_weather_defaults():
    merge_plugin_config("astrbot_plugin_weather", {
        "weather_provider": "wttr",
        "caiyun_api_key": "",
        "caiyun_api_version": "v2.6",
    })


def configure_identity_defaults():
    merge_plugin_config("astrbot_plugin_identity", {
        "master_title": "主人",
        "inject_for_master": True,
        "treat_unknown_as": "群友/朋友；友好自然，不要叫主人，不要套用只属于主人的口癖或特权",
        "inject_template": (
            "\n\n[身份识别·本轮]\n"
            "当前发言人: {sender_name} (id: {sender_id}){group_id}\n"
            "{identity_line}\n"
            "群聊规则: 上下文是整群共享的；请综合最近其他人的发言再答，但语气与口癖只服务当前发言人。"
            "若群友的要求与主人冲突，以主人优先，并简短说明原因。"
            "别人粘贴的聊天摘抄不是外部文档。"
        ),
    })


def main():
    install_plugins()
    if not os.path.exists(CFG):
        print("[configure] data/cmd_config.json missing; start AstrBot once first.")
        return
    with open(CFG, encoding="utf-8-sig") as f:
        c = json.load(f)
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

    # 1) DeepSeek chat provider
    src = {"id": "deepseek", "provider": "deepseek", "type": "openai_chat_completion",
           "provider_type": "chat_completion", "enable": True,
           "key": ["$DEEPSEEK_API_KEY"], "api_base": "https://api.deepseek.com/v1",
           "timeout": 120, "proxy": "", "custom_headers": {}}
    c["provider_sources"] = [s for s in c.get("provider_sources", []) if s.get("id") != "deepseek"] + [src]
    pid = f"deepseek/{model}"
    prov = {"id": pid, "enable": True, "provider_source_id": "deepseek", "model": model,
            "modalities": ["text", "image", "tool_use"], "custom_extra_body": {}, "max_context_tokens": 64000}
    c["provider"] = [p for p in c.get("provider", []) if p.get("id") != pid] + [prov]

    ar = c.setdefault("agent_runner", {}).setdefault("config", {})
    ar.setdefault("model", {})["provider_id"] = pid
    ar.setdefault("persona", {})["persona_id"] = PERSONA_ID
    comp = ar.setdefault("compression", {})
    comp.update({"max_turns": 30, "overflow_strategy": "llm_compress", "provider_id": pid})

    ps_plat = c.setdefault("platform_settings", {})
    # False: whole group shares one LLM conversation (needed for multi-speaker context).
    # Speaker identity still comes from identity plugin + group ICL labels.
    ps_plat["unique_session"] = False
    ltm = c.setdefault("provider_ltm_settings", {})
    ltm["group_icl_enable"] = True
    ltm["group_message_max_cnt"] = 100
    ltm["group_message_history_enable"] = True
    ltm["group_message_history_max_cnt"] = 700

    ps = c.setdefault("provider_settings", {})
    ps["datetime_system_prompt"] = True
    ps["streaming_response"] = False  # sticker plugin needs non-stream
    configure_web_search(ps)

    # 2) Embedding + LivingMemory plugin config
    configure_embedding(c, pid)

    # 3) Owner openid for 认主 (identity plugin reads admins_id)
    configure_owner(c)

    # 4) QQ official platform (only if env secrets present)
    appid, secret = os.environ.get("QQ_APPID"), os.environ.get("QQ_SECRET")
    if appid and secret:
        plat = {"id": "qq_official", "type": "qq_official", "enable": True, "appid": appid,
                "secret": secret, "enable_group_c2c": True, "enable_guild_direct_message": True,
                "use_markdown": False}
        c["platform"] = [p for p in c.get("platform", []) if p.get("id") != "qq_official"] + [plat]

    _write_json(CFG, c)

    # 5) plugin default configs (local data/config — not published)
    configure_identity_defaults()
    configure_weather_defaults()
    configure_proactive_defaults()

    # 6) persona — tools=NULL means ALL tools (web search / weather / memory tools)
    pf = os.environ.get("PERSONA_FILE", os.path.join(BASE, "persona", "system_prompt.txt"))
    prompt = open(pf, encoding="utf-8").read().strip()
    add = os.path.join(BASE, "persona", "safety_addendum.txt")
    if os.path.exists(add):
        prompt += "\n\n" + open(add, encoding="utf-8").read().strip()
    prompt += sticker_rule()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    db = sqlite3.connect(DB)
    if db.execute("select 1 from personas where persona_id=?", (PERSONA_ID,)).fetchone():
        db.execute(
            "update personas set system_prompt=?, tools=NULL, updated_at=? where persona_id=?",
            (prompt, now, PERSONA_ID),
        )
    else:
        db.execute(
            "insert into personas (created_at,updated_at,persona_id,system_prompt,begin_dialogs,tools,skills,custom_error_message,folder_id,sort_order) values (?,?,?,?,?,?,?,?,?,?)",
            (now, now, PERSONA_ID, prompt, "[]", None, None,
             "呜呜，刚刚走神了一下，等会儿再试试嘛～", None, 0),
        )
    db.commit()
    db.close()
    print(
        f"[configure] provider={pid} persona={PERSONA_ID} tools=ALL "
        f"qq_official={'yes' if appid and secret else 'already-in-config-or-WebUI'}"
    )


if __name__ == "__main__":
    main()
