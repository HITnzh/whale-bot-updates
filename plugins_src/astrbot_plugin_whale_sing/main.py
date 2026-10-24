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


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").strip()


@register(
    "whale_sing",
    "whale",
    "DiffSinger 本地唱歌，可选本机 RVC 变声（不下载商业曲库）",
    "1.0.0",
)
class WhaleSing(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

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
            kwargs = self._parse_song_arg(event.message_str or "")
            if not kwargs:
                songs_dir = Path(_norm(str(self._cfg("songs_dir", "E:/whale-bot/sing/songs"))))
                names = ", ".join(x.stem for x in songs_dir.glob("*.txt")) or "（还没有曲谱）"
                yield event.plain_result(
                    "用法：/唱 demo  或  /唱 歌词 | 简谱\n"
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
    ):
        """主人或用户请你唱一段原创/自写简谱时调用。用本地 DiffSinger 合成并发送语音。禁止点播受版权保护的商业歌曲名去下载翻唱。

        Args:
            lyrics(string): 中文歌词（与简谱音数对应；休止和连音不吃字）
            jianpu(string): 简谱，如 "3_ 5_ 6 5 ~3 1--"；与 song_name 二选一
            song_name(string): songs 目录里已有曲谱名（如 demo），有则优先
            bpm(int): 速度，0 表示用默认
            key(string): 简谱调，如 C4；空则默认
            transpose(int): 移调半音；999 表示用默认
        """
        if not bool(self._cfg("enabled", True)):
            return "唱歌插件未启用"
        if not self._allowed(event):
            return "无权限"
        try:
            kwargs: dict = {}
            if song_name:
                songs_dir = Path(_norm(str(self._cfg("songs_dir", "E:/whale-bot/sing/songs"))))
                p = songs_dir / (song_name if song_name.endswith(".txt") else f"{song_name}.txt")
                if not p.exists():
                    return f"曲谱不存在：{p.name}"
                kwargs["song_file"] = str(p)
            else:
                if not lyrics or not jianpu:
                    return "需要 lyrics+jianpu，或 song_name"
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
