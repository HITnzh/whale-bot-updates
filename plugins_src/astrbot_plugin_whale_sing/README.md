# astrbot_plugin_whale_sing

用户操作统一见项目根目录的 `使用说明.txt`；本页补充唱歌插件的参数、音轨与验证方法。

默认原创唱歌：ACE-Step 生成完整歌曲 → BS-RoFormer 分离人声与伴奏 → 原有 RVC 只转换人声 → 与原伴奏混音 → QQ 语音。

- 命令：`/唱 一首温柔的晚安曲`、`/唱 歌词 你的原创歌词`
- LLM 工具：`whale_sing(theme="考试加油", style="轻快钢琴", duration=30)`，也可以直接给原创 `lyrics`，不再需要简谱。
- 简谱入口：`/唱 demo`、`/唱 歌词 | 简谱`。DiffSinger 脚本输出独立音轨，简谱模式的伴奏也不会进入 RVC。
- 原曲翻唱：私聊上传音频文件后发送 `/翻唱`；或将音频放入根目录 `covers/`，使用 `/翻唱列表` 和 `/翻唱 歌曲名`。
- **不**对接网易云/QQ 音乐下载

## 首次准备

在项目根目录的 PowerShell 运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\voice-backends\music-generation\setup.ps1 -DownloadModels
```

安装器使用独立环境，不升级原有 AstrBot/RVC 依赖。模型较大，准备期间需要联网。完成后运行 `启动唱歌生成API.bat`（8002）以及原有的 `启动换声API.bat`（8001），再在 AstrBot 中重载唱歌插件或下次正常启动时使用。安装或修改源码不会主动发送 QQ 消息。

原创生成默认使用 CPU；分轨只使用独立 CUDA 环境。CUDA 安装与远程生成地址配置详见 `voice-backends/music-generation/README.md`。

## 配置与结果

- 保留原有 `rvc_model_path`、`rvc_index_path`、`rvc_base_url` 等设置；需要使用角色音色时保持 `rvc_enabled=true`。不重训、不更换 RVC 模型。
- `rvc_timeout_sec` 默认1800秒，独立控制完整歌曲的换声等待；简谱合成继续使用 `timeout_sec`，生成/分轨使用 `generation_timeout_sec`。
- `generation_base_url` 默认 `http://127.0.0.1:8002`，可指向同协议的独立机器服务。若服务启用鉴权，在机器人启动环境中设置 `ACESTEP_API_KEY`。
- `default_duration_sec` 默认 30，生成短歌限制 15～60 秒；`generation_timeout_sec` 默认 1800，单次超时不会自动重提任务。
- `separator_python`、`separator_model_dir`、`ffmpeg_path` 留空会自动寻找本项目独立环境、模型及打包 FFmpeg。
- `separator_device` 默认为 `cuda`，CUDA 不可用或显存不足直接报告；不提供 CPU 分轨或回退。已有 `auto` 值按 CUDA-only 解释。
- CUDA 使用原生 FP16；`separator_segment_size` 默认401（约4秒），范围129～801；重叠数沿用模型配置。分轨进程结束后才调用 RVC，释放分轨自身显存。
- 分轨设备、片段大小和模型 YAML 参与翻唱缓存指纹。RVC 音色模型和索引无需更换。
- 与伴奏混音时 `rvc_f0_up_key` 应为 0，避免只改变人声调性。非零时会明确停止，不会偷偷改动原设置。
- 同时只制作一首，新的请求会提示等待，避免重复占用模型。
- 自然语言工具立即返回制作状态，在后台完成后另发语音，避免框架的短工具超时；插件卸载会取消其未完成任务。

每首作品保存在 `data/temp/whale_sing/song_<唯一ID>/`：

```text
song.json             歌词、生成参数、任务信息
generated_mix.wav     同一次生成的原始歌曲
vocals.wav            分离人声（简谱模式为直接导出的干人声）
instrumental.wav      同一时间轴的伴奏
rvc_vocals.wav        转换后人声（启用 RVC 时）
final.wav             44100 Hz 立体声成品
qq_preview.wav        24000 Hz 单声道 PCM16 QQ 预览
```

最终混音只使用 `rvc_vocals + instrumental`，不叠回原始完整歌曲。分轨失败、配置的 RVC 模型/索引缺失或生成失败时会明确报错，不伪装成已生成角色歌声。中间文件保留供试听比较。

## 离线验证

```powershell
.\python\python.exe -m unittest discover -s tests -p 'test_whale_*.py' -v
```

测试使用临时音频、模拟生成服务和模拟分轨，包含真实 FFmpeg 混音检查；不会调用真实收费服务或发送 QQ。模型真实效果和 QQ 压缩后听感需另外试听。

完整后端说明见 `voice-backends/music-generation/README.md`；语音后端总览见 `voice-backends/README-zh.md`。

## 测试原创歌曲生成

1. 双击 `查看唱歌下载进度.bat`，确认四个文件全部就绪。
2. 启动 `启动唱歌生成API.bat`（8002）与原有 `启动换声API.bat`（8001）；已经运行的服务不用重复启动。
3. 在 `http://localhost:6185/` 的插件页重载鲸鱼娘唱歌插件，确认版本为 2.2.0。使用自然语言工具时，下一次正常启动会应用更新后的人设。
4. 私聊发送 `/唱 一首温柔的晚安曲`。也可以用 `/唱 歌词 星光轻轻落在窗边，你的梦有微风相伴。` 跳过主题作词。
5. 预期先收到制作提示，随后收到语音；首次请求需要加载模型，CPU 生成可能较慢，请等完成或明确错误返回，不要连发。

`/唱 demo` 测试的是简谱引擎。成品及中间音轨在 `data/temp/whale_sing/song_*/`，可分别试听 `generated_mix.wav`、`rvc_vocals.wav`、`final.wav` 和 `qq_preview.wav`。

若提示无法连接 8001，先启动换声 API；无法连接 8002，先启动生成 API。后台 `/health` 显示服务启动不代表模型已经完成加载或音乐音质已验收。

## 测试原曲翻唱

**不需要启动 ACE-Step。** 本功能只使用已经安装的分轨器和现有 RVC。

方式一：私聊把 MP3/WAV/FLAC/M4A 等作为文件上传，收到回执后发送 `/翻唱`。同一条消息带 `/翻唱` 和音频也支持。群聊时请明确 @ 机器人，避免普通群文件触发回应。普通语音聊天不会被音乐上传回执打断；已正常进入对话的语音可在随后 `/翻唱` 时使用。

方式二：将音频放入 `E:\whale-bot\covers`，发送 `/翻唱列表`，然后 `/翻唱 歌名`。文件名有空格也支持；同名不同格式时写扩展名，如 `/翻唱 晚安.mp3`。仅使用曲库现有文件，不接受任意本机路径、网址，也不会自动从音乐平台下载。

- 上传引用按人物和会话隔离，保留15分钟；过期请重新上传。
- 默认源文件不超过100 MiB、音频不超过300秒；可在插件配置调整 `cover_max_upload_mb` 和 `cover_max_duration_sec`。超限会提示，不截断原歌。
- `whale_cover(song_name="")` 工具用于当前或最近本人上传的音频，填写实际曲库歌曲名则使用曲库；`whale_sing` 继续负责原创生成。
- 只转换原唱人声，保留原曲旋律与主要唱法，再混回同一次分轨的伴奏；不会自动改写为另一种歌手唱腔。
- 原歌曲不会修改或删除。相同音频字节、RVC 模型/索引和分轨设置匹配时复用缓存；更换模型后重新制作。
- 成功缓存仅保存 `final.wav`、`qq_preview.wav`、`cover.json`，位于 `data/temp/whale_sing/cover_cache/`；临时原音轨副本清理掉，减少空间占用。失败时保留诊断文件。
- 原始 Silk 文件不直接处理，请作为 MP3/WAV 文件上传，或使用平台已转换的语音。

首次验证建议用20～30秒、单人主唱、伴奏较干净的片段。检查角色音色、伴奏是否有明显原唱残留、人声节拍是否对齐，然后再测试完整歌曲及重复点播缓存。
