"""Local singing for 鲸鱼娘: DiffSinger (莲华) -> optional RVC -> QQ Record(wav).

No NetEase/QQ Music download. Original / jianpu / local song txt only.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, register

from .song_session import SongSession, SongSessionStore
from .theme_songs import (
    fit_lyrics,
    load_templates,
    parse_theme_request,
    select_template,
    template_to_pipeline_kwargs,
)


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").strip()


@register(
    "whale_sing",
    "whale",
    "DiffSinger 本地唱歌，可选本机 RVC 变声（不下载商业曲库）",
    "1.1.0",
)
class WhaleSing(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._song_sessions = SongSessionStore()
        self._templates = load_templates(Path(__file__).resolve().parent / "templates")

    def _cfg(self, key: str, default=None):
        v = self.config.get(key)
        return default if v is None or v == "" else v

    def _admins(self) -> set[str]:
        try:
            return {str(a) for a in (self.context.get_config().get("admins_id") or [])}
        except Exception:
            return set()

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if not bool(self._cfg("admin_only", False)):
            return True
        return str(event.get_sender_id()) in self._admins()

    async def _run_diffsinger(
        self,
        *,
        lyrics: str | None = None,
        jianpu: str | None = None,
        notes: str | None = None,
        song_file: str | None = None,
        bpm: int | None = None,
        key: str | None = None,
        transpose: int | None = None,
    ) -> Path:
        py = _norm(str(self._cfg("sing_python", "")))
        script = _norm(str(self._cfg("sing_script", "")))
        out_dir = Path(_norm(str(self._cfg("out_dir", "E:/whale-bot/data/temp/whale_sing"))))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_wav = out_dir / f"sing_{int(time.time())}_{os.getpid()}.wav"

        if not py or not Path(py).exists():
            raise FileNotFoundError(
                f"找不到 sing Python：{py}。请先跑 voice-backends 里的 setup_sing.ps1"
            )
        if not script or not Path(script).exists():
            raise FileNotFoundError(f"找不到 sing.py：{script}")

        # --no-reverb：直接写 PCM WAV，不依赖 ffmpeg（qq_official 要 RIFF/WAV）
        cmd = [py, script, "-o", str(out_wav), "--no-reverb"]
        if song_file:
            cmd += ["--song", song_file]
        else:
            if not lyrics:
                raise ValueError("需要歌词 lyrics，或指定曲谱文件")
            cmd += ["--lyrics", lyrics]
            if jianpu:
                cmd += ["--jianpu", jianpu, "--key", key or str(self._cfg("default_key", "C4"))]
            elif notes:
                cmd += ["--notes", notes]
            else:
                raise ValueError("需要简谱 jianpu 或音名 notes，或 --song 曲谱文件")
            cmd += ["--bpm", str(bpm if bpm is not None else int(self._cfg("default_bpm", 90)))]
            cmd += [
                "--transpose",
                str(transpose if transpose is not None else int(self._cfg("default_transpose", 5))),
            ]

        timeout = int(self._cfg("timeout_sec", 180))
        logger.info(f"[whale_sing] run: {' '.join(cmd[:4])} ... -> {out_wav}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(script).parent),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"DiffSinger 超时（>{timeout}s）")

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", "replace")[-1500:]
            out = (stdout or b"").decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"DiffSinger 失败 code={proc.returncode}\n{err}\n{out}")

        if not out_wav.exists() or out_wav.stat().st_size < 1000:
            # sing.py may write mp3 if -o ends with .mp3; we force .wav — if engine wrote sibling, find it
            raise RuntimeError(f"未生成有效 WAV：{out_wav}")
        return out_wav

    async def _maybe_rvc(self, wav: Path) -> Path:
        if not bool(self._cfg("rvc_enabled", False)):
            return wav
        model = _norm(str(self._cfg("rvc_model_path", "")))
        if not model or not Path(model).exists():
            logger.warning("[whale_sing] rvc_enabled 但模型不存在，跳过变声: %s", model)
            return wav
        base = str(self._cfg("rvc_base_url", "http://127.0.0.1:8001")).rstrip("/")
        index = _norm(str(self._cfg("rvc_index_path", "")))
        f0 = int(self._cfg("rvc_f0_up_key", 0))
        out = wav.with_name(wav.stem + "_rvc.wav")

        timeout = aiohttp.ClientTimeout(total=int(self._cfg("timeout_sec", 180)))
        params = {"res_type": "blob", "f0_up_key": str(f0)}
        data = aiohttp.FormData()
        data.add_field(
            "input_audio",
            wav.read_bytes(),
            filename=wav.name,
            content_type="audio/wav",
        )
        data.add_field("modelpath", model)
        if index and Path(index).exists():
            data.add_field(
                "index_file",
                Path(index).read_bytes(),
                filename=Path(index).name,
                content_type="application/octet-stream",
            )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base}/inference", data=data, params=params) as resp:
                body = await resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"RVC HTTP {resp.status}: {body[:300]!r}")
                out.write_bytes(body)
        if out.stat().st_size < 1000:
            raise RuntimeError("RVC 返回音频过小")
        return out

    def _to_pcm_wav_if_needed(self, path: Path) -> Path:
        """Ensure RIFF WAV for qq_official silk path. Prefer already-wav."""
        if path.suffix.lower() == ".wav":
            # quick magic check
            try:
                with open(path, "rb") as f:
                    if f.read(4) == b"RIFF":
                        return path
            except OSError:
                pass
        # fallback: copy as .wav name; AstrBot may convert; try ffmpeg if present
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return path
        dest = path.with_suffix(".pcm16.wav")
        import subprocess

        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(dest),
            ],
            check=False,
            capture_output=True,
        )
        return dest if dest.exists() and dest.stat().st_size > 1000 else path

    async def _sing_pipeline(self, **kwargs) -> Path:
        wav = await self._run_diffsinger(**kwargs)
        wav = await self._maybe_rvc(wav)
        return self._to_pcm_wav_if_needed(wav)


    def _session_key_parts(self, event: AstrMessageEvent) -> tuple[str, str]:
        sid = str(getattr(event, "unified_msg_origin", None) or event.get_group_id() or event.get_sender_id() or "")
        uid = str(event.get_sender_id() or "")
        return sid, uid

    def _build_theme_kwargs(self, event: AstrMessageEvent, req: dict) -> dict:
        """Build synth kwargs from theme request / revision; updates song_session."""
        sid, uid = self._session_key_parts(event)
        prev = self._song_sessions.get(sid, uid)
        revision = req.get("revision_of")
        theme = req.get("theme") or (prev.theme if prev else "")
        recipient = req.get("recipient") or (prev.recipient if prev else "")
        mood = req.get("mood") or (prev.mood if prev else None)

        if revision and not prev:
            raise ValueError("我还不记得上一首诶，先说主题再改～比如：唱个晚安曲")

        bpm = None
        tpl = None
        lyrics = None

        if revision == "tempo" and prev:
            tpl = self._templates.get(prev.template_id) or select_template(self._templates, prev.theme, prev.mood)
            lyrics = prev.lyrics
            # Faster within template range
            base = tpl.bpm if tpl else prev.bpm
            bpm = min(base + 20, int(base * 1.25) + 5)
            recipient = prev.recipient
            theme = prev.theme
        elif revision == "add_name" and prev:
            tpl = self._templates.get(prev.template_id) or select_template(self._templates, prev.theme)
            recipient = recipient or "你"
            lyrics, _ = fit_lyrics(tpl, prev.theme, recipient)
            bpm = prev.bpm
            theme = prev.theme
        elif revision == "change_mood" and prev:
            # Pick mood from raw text
            raw = req.get("raw") or ""
            if "开心" in raw or "庆祝" in raw:
                mood = "happy"
                theme = "庆祝"
            elif "俏皮" in raw or "调皮" in raw:
                mood = "playful"
                theme = "俏皮"
            elif "晚安" in raw or "温柔" in raw:
                mood = "comforting"
                theme = "晚安"
            elif "加油" in raw:
                mood = "happy"
                theme = "加油"
            tpl = select_template(self._templates, theme, mood)
            lyrics, _ = fit_lyrics(tpl, theme, prev.recipient or recipient)
            recipient = prev.recipient or recipient
        else:
            tpl = select_template(self._templates, theme, mood)
            if not tpl:
                raise FileNotFoundError("没有可用的主题短歌模板")
            lyrics, _ = fit_lyrics(tpl, theme, recipient)

        assert tpl is not None and lyrics is not None
        kwargs = template_to_pipeline_kwargs(tpl, lyrics, bpm=bpm)
        # Persist session for this user only
        ss = SongSession(
            template_id=tpl.template_id,
            theme=theme or tpl.theme,
            recipient=recipient or "",
            lyrics=lyrics,
            bpm=int(kwargs.get("bpm") or tpl.bpm),
            version=(prev.version + 1) if prev else 1,
            mood=mood or tpl.mood,
        )
        self._song_sessions.put(sid, uid, ss)
        return kwargs

    def _parse_song_arg(self, raw: str) -> dict:
        """Parse `/唱 ...` body into kwargs.

        Forms:
          /唱 demo
          /唱 歌词…… | 1 2 3 5 3--
          /唱 歌词…… | notes:C4:1 D4:1
        """
        raw = (raw or "").strip()
        raw = re.sub(r"^[/／]?唱(?:歌)?\s*", "", raw, count=1).strip()
        if not raw:
            return {}
        songs_dir = Path(_norm(str(self._cfg("songs_dir", "E:/whale-bot/sing/songs"))))
        # bare name -> song file
        if "|" not in raw and "：" not in raw and "notes:" not in raw.lower():
            candidate = songs_dir / (raw if raw.endswith(".txt") else f"{raw}.txt")
            if candidate.exists():
                return {"song_file": str(candidate)}
            # maybe absolute path
            p = Path(_norm(raw))
            if p.exists() and p.suffix == ".txt":
                return {"song_file": str(p)}
            raise FileNotFoundError(
                f"曲谱不存在：{candidate}\n可用：{', '.join(x.stem for x in songs_dir.glob('*.txt')) or '（空）'}"
            )

        # lyrics | jianpu
        if "|" in raw:
            left, _, right = raw.partition("|")
            lyrics = left.strip()
            right = right.strip()
            if right.lower().startswith("notes:"):
                return {"lyrics": lyrics, "notes": right[6:].strip()}
            return {"lyrics": lyrics, "jianpu": right}

        raise ValueError(
            "用法：\n"
            "/唱 demo\n"
            "/唱 小鲸鱼晚安 | 3_ 5_ 6 5 ~3 1--\n"
            "/唱 小鲸鱼晚安 | notes:E4:1 G4:1 A4:1 G4:1 E4:2"
        )

    @filter.command("唱", alias={"唱歌", "whale_sing"})
    async def cmd_sing(self, event: AstrMessageEvent):
        if not bool(self._cfg("enabled", True)):
            yield event.plain_result("唱歌插件关着，先在配置里打开 enabled。")
            return
        if not self._allowed(event):
            yield event.plain_result("这个只能管理员用哦～")
            return
        try:
            raw_msg = event.message_str or ""
            theme_req = parse_theme_request(raw_msg)
            if theme_req:
                kwargs = self._build_theme_kwargs(event, theme_req)
            else:
                kwargs = self._parse_song_arg(raw_msg)
            if not kwargs:
                songs_dir = Path(_norm(str(self._cfg("songs_dir", "E:/whale-bot/sing/songs"))))
                names = ", ".join(x.stem for x in songs_dir.glob("*.txt")) or "（还没有曲谱）"
                yield event.plain_result(
                    "用法：/唱 demo  或  /唱 歌词 | 简谱  或  /唱 给小林唱个晚安曲\n"
                    f"曲谱：{names}\n"
                    "只唱原创/自写简谱；不下载网易云商业歌。"
                )
                return
            yield event.plain_result("哼～本大肥鱼去排练一下，等我一会儿…")
            wav = await self._sing_pipeline(**kwargs)
            yield event.chain_result([Record.fromFileSystem(str(wav))])
        except Exception as e:
            logger.exception("[whale_sing] cmd failed")
            yield event.plain_result(f"唱砸了：{e}")

    @filter.llm_tool(name="whale_sing")
    async def tool_sing(
        self,
        event: AstrMessageEvent,
        lyrics: str = "",
        jianpu: str = "",
        song_name: str = "",
        bpm: int = 0,
        key: str = "",
        transpose: int = 999,
        theme: str = "",
        recipient: str = "",
        mood: str = "",
        revision: str = "",
    ):
        """唱原创短歌。优先 theme 主题短歌（晚安/加油/庆祝/俏皮）；也可 lyrics+jianpu 或 song_name。禁止下载商业歌。

        Args:
            theme(string): 主题，如 晚安/加油/庆祝/俏皮；只说主题时用这个
            recipient(string): 送给谁（名字），可空
            mood(string): comforting/happy/playful，可空
            revision(string): tempo|add_name|change_mood，改自己上一首
            lyrics(string): 中文歌词（与简谱音数对应）
            jianpu(string): 简谱；与 song_name/theme 二选一
            song_name(string): songs 目录曲谱名（如 demo）
            bpm(int): 速度，0 默认
            key(string): 简谱调
            transpose(int): 移调；999 默认
        """
        if not bool(self._cfg("enabled", True)):
            return "唱歌插件未启用"
        if not self._allowed(event):
            return "无权限"
        try:
            kwargs: dict = {}
            if theme or revision:
                req = {
                    "theme": theme,
                    "recipient": recipient,
                    "mood": mood or None,
                    "revision_of": revision or None,
                    "raw": f"{theme} {mood} {revision}",
                }
                kwargs = self._build_theme_kwargs(event, req)
            elif song_name:
                songs_dir = Path(_norm(str(self._cfg("songs_dir", "E:/whale-bot/sing/songs"))))
                p = songs_dir / (song_name if song_name.endswith(".txt") else f"{song_name}.txt")
                if not p.exists():
                    return f"曲谱不存在：{p.name}"
                kwargs["song_file"] = str(p)
            else:
                if not lyrics or not jianpu:
                    return "需要 theme，或 lyrics+jianpu，或 song_name"
                kwargs["lyrics"] = lyrics
                kwargs["jianpu"] = jianpu
                if key:
                    kwargs["key"] = key
            if bpm and bpm > 0:
                kwargs["bpm"] = int(bpm)
            if transpose != 999:
                kwargs["transpose"] = int(transpose)
            wav = await self._sing_pipeline(**kwargs)
            await event.send(event.chain_result([Record.fromFileSystem(str(wav))]))
            return "已发送歌声（本地合成，非商业曲库下载）"
        except Exception as e:
            logger.exception("[whale_sing] tool failed")
            return f"唱歌失败：{e}"
