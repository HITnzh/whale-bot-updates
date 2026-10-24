# astrbot_plugin_identity

身份识别 / 认主认友插件。给 Bot 每条消息注入发送者身份（主人 / 已认识的朋友 / 陌生人），避免乱认主；并提供让主人通过自然语言或命令登记新身份的能力。

---

## 工作原理

1. **`on_llm_request` 钩子**：每次 LLM 请求前，根据发送者 QQ 查表，在 `system_prompt` 末尾追加身份段：
   - 在 `admins_id` 里 → **主人**
   - 在 `registry.json` 里 → **已认识的人**（带称呼/关系/备注）
   - 都不在 → **陌生人**

2. **LLM 工具**：
   - `register_known_person(user_id, title, relation, note)` — 登记（仅主人）
   - `forget_known_person(user_id)` — 忘记（仅主人）
   - `lookup_known_person(name)` — 按称呼/名字片段查找已登记的人，返回 QQ；任何人可触发

3. **存储**：`data/plugin_data/astrbot_plugin_identity/registry.json`

4. **主人来源**：直接读 AstrBot 主配置 `admins_id`，不维护单独的主人列表。

---

## 使用方式

### 一、自然语言（推荐）

必须包含 **唤醒词**（AngelHeart 的 `alias` 配的"亚托莉/ATRI/AI/助手"等），让 AngelHeart 进入"被呼唤"状态触发 LLM。

```
亚托莉 @某人 这位是吴老师
亚托莉 @某人 他叫小明，是我同事
亚托莉 @某人 这是我哥，叫大成
```

Bot 会：
1. 调用 `register_known_person` 存档
2. 用 Atri 口吻自然回复（如"好的我记住了喵～"）

**忘记某人**：
```
亚托莉 @某人 你不认识他了 / 忘掉他吧
```

**询问 ta 是谁**（已 @ 时，注入的 system prompt 里已带身份信息，LLM 直接答）：
```
亚托莉 @某人 这是谁？
亚托莉 @某人 你认识 ta 吗？
```

**按名字反查**（不 @，让 Bot 自己找到对应的人并 @ 出来）：
```
亚托莉，吴老师是谁？
亚托莉，牢菲在哪？
亚托莉，你认识吴老师吗？
```
Bot 会调 `lookup_known_person` 在注册表里搜，拿到 QQ 后用 `[CQ:at,qq=xxx]` @ 出来并自然介绍。例如：
> [@吴老师] 就是吴老师呀，主人之前介绍过的老师～

### 二、手动命令（兜底，LLM 没正确调用工具时用）

| 命令 | 说明 |
|---|---|
| `/认识 <QQ号> <称呼> [关系] [备注...]` | 强制登记 |
| `/忘记 <QQ号>` | 删除记录 |
| `/认识列表` | 列出本平台所有已登记的人 |
| `/我是谁` | 任何人可发，自查身份 |

别名：
- `/认识` = `/记住身份` = `/认友`
- `/忘记` = `/忘掉身份` = `/取消认识`
- `/认识列表` = `/身份列表`

示例：
```
/认识 XXXX 吴老师 老师 上次群活动认识的
/忘记 XXXX
/认识列表
```

---

## 配置项（WebUI / `_conf_schema.json`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `master_title` | `主人` | 注入提示里对管理员的称呼 |
| `inject_for_master` | `true` | 是否对主人那条消息也注入身份段 |
| `treat_unknown_as` | `陌生人，你之前不认识 ta` | 陌生人在 prompt 中的描述 |
| `inject_template` | 见 schema | 注入到 system_prompt 末尾的模板，占位符：`{sender_id}` `{sender_name}` `{group_id}` `{identity_line}` |

---

## 常见问题

**Q：Bot 没反应 / 没调工具？**
- 检查消息里有没有唤醒词。AngelHeart 默认只在被 `@` 或检测到 `alias` 关键词时才触发 LLM。
- 看日志有没有 `[identity] LLM 登记` 字样，没有就是 LLM 没调工具（提示词不够强，可在配置里加强 `inject_template`）。

**Q：title 被存成了 QQ 群昵称而不是介绍的称呼？**
- LLM 偶尔会偷懒。重新发一次更明确的介绍（"亚托莉 @某人 ta 叫吴老师"），或用 `/认识` 手动覆盖。

**Q：怎么改主人？**
- 编辑 `data/cmd_config.json` 的 `admins_id` 数组，重启生效。

**Q：删除某个登记？**
- 自然语言："亚托莉 @某人 忘掉 ta"
- 命令：`/忘记 <QQ号>`
- 或直接编辑 `data/plugin_data/astrbot_plugin_identity/registry.json`

---

## 数据结构

`registry.json` 示例：
```json
{
  "aiocqhttp:XXXXX": {
    "title": "吴老师",
    "relation": "老师",
    "note": "",
    "updated_at": XXXXX
  }
}
```

Key 格式：`<平台名>:<QQ号>`，同一 QQ 在不同平台是独立记录。

---

## 与其他插件的协作

- **angel_heart**：负责唤醒触发；本插件依赖它把消息送进 LLM 流程
- **angel_memory**：负责长期记忆；本插件只存"身份标签"，记忆细节交给 angel_memory
- **antipromptinjector**：本插件在 priority=500 注入，反注入插件在 priority=999 兜底，互不冲突
