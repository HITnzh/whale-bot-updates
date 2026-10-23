#!/usr/bin/env python3
"""Idempotently wire DeepSeek provider, companion persona, memory settings and
(optionally) the qq_official adapter into AstrBot's data/. Run while AstrBot is STOPPED
(start.sh calls it automatically before launching).

Env vars:
  DEEPSEEK_API_KEY   - NOT written to disk; provider key is stored as the literal
                       reference "$DEEPSEEK_API_KEY", resolved by AstrBot at load time.
  DEEPSEEK_MODEL     - default deepseek-flash (vision)
  QQ_APPID/QQ_SECRET - if both set, a qq_official (WebSocket) platform is added/updated.
                       (AstrBot has no env-ref for platform secrets, so the secret is
                       written to data/cmd_config.json, which is chmod 600.)
  PERSONA_FILE       - default persona/system_prompt.txt (+ persona/safety_addendum.txt appended)
"""
import datetime, json, os, sqlite3, sys
BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "data", "cmd_config.json")
DB = os.path.join(BASE, "data", "data_v4.db")
PERSONA_ID = "鲸鱼娘"

def sticker_rule() -> str:
    """Persona addendum listing the exact sticker tags (from stickers/index.json)."""
    idx = os.path.join(BASE, "stickers", "index.json")
    if not os.path.exists(idx):
        return ""
    count: dict[str, int] = {}
    for st in json.load(open(idx, encoding="utf-8")).get("stickers", []):
        if st.get("file") == "preview.png":
            continue
        for t in st.get("tags", []):
            count[t] = count.get(t, 0) + 1
    tags = sorted(count, key=lambda t: -count[t])  # most common moods first
    return ("\n\n【表情包】\n"
            "你可以偶尔在情绪明显的回复末尾加一个表情标记，格式严格为 [表情:标签]，标签只能从下面原样选一个："
            + "、".join(tags) + "。\n"
            "- 每条回复最多一个标记，放在最后；大多数回复不要加，大约三四条里最多一次。\n"
            "- 选和你这句话情绪最贴的标签，比如被摸头用「摸头」、道晚安用「晚安」、被夸用「得意」或「害羞」、无语用「无语」。\n"
            "- 标记会被系统自动换成一张表情图，对方看不到标记文字；不要解释或提起它，不要自己编新标签，也不要发预览图。\n"
            "- 严肃话题（对方难过、危机、求助）时不要加。")


def install_plugins():
    """Copy plugins_src/* into data/plugins/ (idempotent; survives re-runs)."""
    import shutil
    src_root = os.path.join(BASE, "plugins_src")
    dst_root = os.path.join(BASE, "data", "plugins")
    if not os.path.isdir(src_root):
        return
    os.makedirs(dst_root, exist_ok=True)
    for name in os.listdir(src_root):
        src = os.path.join(src_root, name)
        if os.path.isdir(src):
            dst = os.path.join(dst_root, name)
            if os.path.islink(dst):
                os.unlink(dst)
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            print(f"[configure] plugin installed: {name}")


def main():
    install_plugins()
    if not os.path.exists(CFG):
        print("[configure] data/cmd_config.json missing; start AstrBot once first."); return
    with open(CFG, encoding="utf-8-sig") as f:
        c = json.load(f)
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

    # 1) DeepSeek provider source + model provider
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
    # 2) memory: keep last 30 turns verbatim per session; on overflow, LLM-summarise older turns
    comp = ar.setdefault("compression", {})
    comp.update({"max_turns": 30, "overflow_strategy": "llm_compress", "provider_id": pid})

    ps = c.setdefault("platform_settings", {})
    ps["unique_session"] = True          # in groups, each member gets their own conversation memory
    ltm = c.setdefault("provider_ltm_settings", {})
    ltm["group_icl_enable"] = True        # bot is aware of recent group chatter for context
    ltm["group_message_max_cnt"] = 100
    c.setdefault("provider_settings", {})["datetime_system_prompt"] = True
    # sticker plugin hooks (marker stripping / image attach) only run on non-streamed results
    c["provider_settings"]["streaming_response"] = False

    # 3) QQ official platform (WebSocket mode, outbound only, no public IP needed)
    appid, secret = os.environ.get("QQ_APPID"), os.environ.get("QQ_SECRET")
    if appid and secret:
        plat = {"id": "qq_official", "type": "qq_official", "enable": True, "appid": appid,
                "secret": secret, "enable_group_c2c": True, "enable_guild_direct_message": True,
                "use_markdown": False}
        c["platform"] = [p for p in c.get("platform", []) if p.get("id") != "qq_official"] + [plat]

    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)          # no-op-ish on Windows
    except OSError:
        pass
    os.replace(tmp, CFG)

    # 4) persona (stored in sqlite personas table); tools=[] => no tool access
    pf = os.environ.get("PERSONA_FILE", os.path.join(BASE, "persona", "system_prompt.txt"))
    prompt = open(pf, encoding="utf-8").read().strip()
    add = os.path.join(BASE, "persona", "safety_addendum.txt")
    if os.path.exists(add):
        prompt += "\n\n" + open(add, encoding="utf-8").read().strip()
    prompt += sticker_rule()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    db = sqlite3.connect(DB)
    if db.execute("select 1 from personas where persona_id=?", (PERSONA_ID,)).fetchone():
        db.execute("update personas set system_prompt=?, tools='[]', updated_at=? where persona_id=?", (prompt, now, PERSONA_ID))
    else:
        db.execute("insert into personas (created_at,updated_at,persona_id,system_prompt,begin_dialogs,tools,skills,custom_error_message,folder_id,sort_order) values (?,?,?,?,?,?,?,?,?,?)",
                   (now, now, PERSONA_ID, prompt, "[]", "[]", "null", "呜呜，鲸鱼娘刚刚走神了一下，等会儿再试试嘛～", None, 0))
    db.commit(); db.close()
    print(f"[configure] provider={pid} persona={PERSONA_ID} qq_official={'yes' if appid and secret else 'not configured (set QQ_APPID/QQ_SECRET or use WebUI scan-to-create)'}")

if __name__ == "__main__":
    main()
