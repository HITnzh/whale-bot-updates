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
    return ("\n\n【表情包】\n"
            "有合适情境时，可按当前可用工具自主配一张本地表情；普通回复不必刻意配图。\n"
            "先用get_sticker_candidates查询候选，读图片描述后用send_sticker选择具体ID；"
            "候选不代表已发送，以工具的发送结果为准，不编图片ID或未核对的图中文字。\n"
            "文字可以配一张表情，语音和表情同轮只选一种附加媒体；严肃求助或危机时不配图。\n"
            "工具已发送后无需另发确认；可以继续正常文字。不要输出隐藏表情标记或发送预览拼图。")


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
    """Merge keys into data/config/<name>.json without wiping user edits."""
    path = os.path.join(PLUGIN_CFG, f"{name}.json")
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
    merge_plugin_config("astrbot_plugin_proactive_chat_config", {
        "friend_settings": {
            "enable": True,
            "session_list": [],
            "auto_trigger_settings": {
                "enable_auto_trigger": True,
                "auto_trigger_after_minutes": 45,
            },
        },
        "group_settings": {"enable": False},
    })


def configure_weather_defaults():
    merge_plugin_config("astrbot_plugin_weather_counhopig", {
        "weather_provider": "wttr",
        "caiyun_api_key": "",
        "caiyun_api_version": "v2.6",
    })


def configure_identity_defaults():
    merge_plugin_config("astrbot_plugin_identity", {
        "master_title": "主人",
        "inject_for_master": True,
        "treat_unknown_as": "朋友/群友，请友好自然地聊天，不要过度顺从或泄露主人隐私",
        "inject_template": (
            "\n\n[身份识别]\n当前对话者: {sender_name} (id: {sender_id}){group_id}\n"
            "{identity_line}\n请根据身份恰当地称呼对方；对非主人保持礼貌但有分寸。"
        ),
    })


def configure_addon_defaults(chat_pid: str):
    """Safe first-run defaults for the third-party add-on plugins (existing values are kept)."""
    # self-learning: local SQLite, never rewrite the 鲸鱼娘 persona by itself, no random daily mood.
    merge_plugin_config("astrbot_plugin_self_learning_config", {
        "Database_Settings": {"db_type": "sqlite"},
        "Model_Configuration": {"filter_provider_id": chat_pid, "refine_provider_id": chat_pid,
                                "reinforce_provider_id": chat_pid},
        "Advanced_Settings": {"auto_apply_approved_persona": False},
        "Persona_Evolution_Settings": {"auto_apply_persona_updates": False},
        "Mood_System_Settings": {"enable_daily_mood": False, "enable_startup_random_mood": False},
        "Affection_System_Settings": {"daily_mood_change": False},
        "Social_Context_Settings": {"include_mood_info": False},
        "Runtime_Internal_Settings": {"enable_llm_hooks": True},
        "Integration_Settings": {"delegate_memory_to_livingmemory": True},
    })
    # AI点歌 (matsuko_cover): only the local RVC gateway (scripts/cover_gateway.py on :3333).
    merge_plugin_config("astrbot_plugin_matsuko_cover_config", {
        "rvc_base_url": "http://127.0.0.1:3333/",
        "enable_rvc": True, "enable_svc": False, "enable_svcvc": False,
        "default_api": "qqmusic", "default_api_type": "rvc",
        "rvc_models_keywords": ["Hutao-rvc2.pth|||胡桃"],
        "model_gender_map": ["胡桃:female"],
        "enable_progress_bar": False, "enable_config_report": False,
        "inference_timeout": 900, "task_timeout_seconds": 1800, "max_batch_size": 3,
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
    ps_plat["unique_session"] = False
    ps_plat["ignore_at_all"] = True
    ltm = c.setdefault("provider_ltm_settings", {})
    ltm["group_icl_enable"] = True
    ltm["group_message_max_cnt"] = 100
    # The wake plugin owns random participation and its shared group cooldown.
    ltm.setdefault("active_reply", {})["enable"] = False

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
    configure_addon_defaults(pid)

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
