# 鲸鱼娘聊天语音

当前用户操作统一见根目录 [使用说明.txt](../../使用说明.txt) 第6节。本插件基于 [Zhalslar/astrbot_plugin_GPT_SoVITS](https://github.com/Zhalslar/astrbot_plugin_GPT_SoVITS)，沿用仓库中的 GPL 许可。

- 主模型通过现有 `gsv_tts` 工具自主决定是否发语音，不额外调用语音方式分类模型。
- `auto.mode=model` 为自主选择，`probability` 保留概率转换，`off` 关闭自动与工具语音。手动朗读命令保留。
- 个人偏好按会话和发送者隔离；群内自主语音冷却按整个会话计时。明确文字要求、代码、链接、表格和长回答使用文字。
- 同轮语音工具去重，合成失败回退文字。工具成功结果携带 `spoken_text`，随工具历史进入上下文；不停止事件来清理重复回复。
- 主模型流式输出时使用文字，避免文本流已发出后再发送相同语音。
- 本地后端使用已有 GPT-SoVITS 音色、参考音频与缓存；可选 MiniMax 使用固定音色ID和环境变量密钥，不替用户创建或复刻音色。
- 可选MiniMax接口依据[官方非流式语音合成文档](https://platform.minimax.io/docs/api-reference/speech-t2a-http)。只使用官方站点，检查业务状态与WAV内容，不自动重试或切换音色。
- 语音识别与TTS独立；本插件不会让仅支持文字/图片的主模型直接听取原始音频。

源目录为 `plugins_src/astrbot_plugin_GPT_SoVITS/`；运行副本在 `data/plugins/`。配置结构见 `_conf_schema.json`，语音策略与MiniMax客户端分别在 `core/voice_policy.py` 和 `core/minimax_client.py`。

离线验证：在项目根目录执行 `.\python\python.exe -m unittest discover -s tests -p 'test_whale_*.py' -q`。语音策略、发送与MiniMax请求使用模拟对象，不调用收费API或QQ。
