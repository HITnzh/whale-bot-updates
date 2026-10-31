# 第三方插件来源

以下插件按上游源码原样收录（去掉 .git、tests、开发文档），许可证见各目录 LICENSE。
安装与当前配置说明见根目录 使用说明.txt 第5节、第8节。

| 目录 | 上游仓库 | 收录提交 | 许可证 | 本地改动 |
|---|---|---|---|---|
| astrbot_plugin_debounce | https://github.com/advent259141/astrbot_plugin_debounce | 0d8aaaf | 见 LICENSE | 无 |
| astrbot_plugin_sticker_sense | https://github.com/YanZephyr14/astrbot_plugin_sticker_sense | aa6245f | MIT | 无 |
| astrbot_plugin_self_learning | https://github.com/NickCharlie/astrbot_plugin_self_learning | 4544b08 | AGPL-3.0 | 未收录 docs/、image/、web_src/（前端源码；已构建的 web_res/ 保留） |
| astrbot_plugin_matsuko_cover | https://github.com/sdfsfsk/matsuko_cover | 61ef5ef | AGPL-3.0 | requirements.txt 固定 qqmusic-api-python==0.5.0（新版依赖 urllib3-future 会覆盖 urllib3） |
| astrbot_plugin_qq_face_to_text | https://github.com/SlimeABC/astrbot_plugin_qq_face_to_text | 97700f3 | 见 LICENSE | 无 |

AI点歌的后端由 astrbot_plugin_whale_sing/scripts/cover_gateway.py 提供（本项目自写，127.0.0.1:3333）。
