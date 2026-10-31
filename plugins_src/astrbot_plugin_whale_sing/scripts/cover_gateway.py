"""Local Gradio gateway that lets matsuko_cover (AI点歌) use the existing cover pipeline.

matsuko_cover talks to an "RVCSVC-API" Gradio middleware (/show_model, /convert).
The official middleware only supports AMD GPUs, so this gateway exposes the same two
endpoints on 127.0.0.1:3333 and reuses whale_sing's own pipeline instead:
BS-Roformer CUDA separation -> existing RVC API (:8001, Hu Tao model) -> mix -> QQ preview.

Settings (separator, RVC URL/model/index, ffmpeg, output dir) are read from
data/config/astrbot_plugin_whale_sing_config.json so there is a single source of truth.
Run with the gateway venv:  voice-backends/cover-gateway/start_api.bat
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

import gradio as gr

HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))
_pkg = __import__(f"{PLUGIN_DIR.name}.cover_render", fromlist=["render_cover"])
_audio = __import__(f"{PLUGIN_DIR.name}.audio_processing", fromlist=["find_ffmpeg"])
render_cover, PIPELINE_VERSION = _pkg.render_cover, _pkg.PIPELINE_VERSION
find_ffmpeg, run_process = _audio.find_ffmpeg, _audio.run_process

ROOT = next((p for p in HERE.parents if (p / "voice-backends").is_dir()), HERE.parents[3])
CONFIG = ROOT / "data" / "config" / "astrbot_plugin_whale_sing_config.json"
GENERATOR_ROOT = ROOT / "voice-backends" / "music-generation"
RVC_MODELS = ROOT / "voice-backends" / "RVC" / "models"
SEPARATE_SCRIPT = PLUGIN_DIR / "scripts" / "separate_stems.py"
MAX_DURATION = 300.0
NETEASE_OUTER = "https://music.163.com/song/media/outer/url?id={id}.mp3"


def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def _norm(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(path or "").strip()))


def list_models() -> list[str]:
    """RVC voice models (.pth) available to the existing RVC API."""
    cfg = _cfg()
    names = sorted(p.name for p in RVC_MODELS.glob("*.pth")) if RVC_MODELS.is_dir() else []
    configured = Path(_norm(cfg.get("rvc_model_path", "")))
    if configured.is_file() and configured.name not in names:
        names.insert(0, configured.name)
    return names


def _model_files(name: str) -> tuple[Path, Path | None]:
    cfg = _cfg()
    configured = Path(_norm(cfg.get("rvc_model_path", "")))
    name = Path(str(name or "")).name or configured.name
    if configured.is_file() and configured.name == name:
        index = Path(_norm(cfg.get("rvc_index_path", "")))
        return configured, (index if index.is_file() else None)
    model = RVC_MODELS / name
    if not model.is_file() or model.suffix.lower() != ".pth":
        raise gr.Error(f"没有这个音色模型：{name}")
    matches = sorted(RVC_MODELS.glob(f"*{model.stem}*.index"))
    return model, (matches[0] if matches else None)


def _separator() -> tuple[Path, Path, str, int]:
    cfg = _cfg()
    python = Path(_norm(cfg.get("separator_python") or "")) if cfg.get("separator_python") else \
        GENERATOR_ROOT / "separator-cuda/.venv/Scripts/python.exe"
    model_dir = Path(_norm(cfg.get("separator_model_dir") or "")) if cfg.get("separator_model_dir") else \
        GENERATOR_ROOT / "models"
    model = str(cfg.get("separator_model") or "model_bs_roformer_ep_317_sdr_12.9755.ckpt")
    segment = int(cfg.get("separator_segment_size", 401) or 401)
    if not python.is_file():
        raise gr.Error("CUDA 分轨环境未安装（voice-backends/music-generation/separator-cuda）")
    if not (model_dir / model).is_file():
        raise gr.Error("分轨模型不存在，请先按使用说明下载分轨模型")
    return python, model_dir, model, segment


def _download_netease(song_id: str, dest: Path) -> Path:
    req = urllib.request.Request(NETEASE_OUTER.format(id=song_id), headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if "/404" in resp.geturl():
            raise gr.Error("这首网易云歌曲需要VIP或无版权，换一首或改用QQ音乐/上传音频")
        data = resp.read(60 * 1024 * 1024)
    is_mp3 = data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if len(data) < 50_000 or not is_mp3:
        raise gr.Error("网易云没有返回可用音频（多为VIP歌曲），换一首或改用上传音频")
    dest.write_bytes(data)
    return dest


async def _pipeline(source: Path, model: Path, index: Path | None, key_shift: int,
                    vocal_gain: float, inst_gain: float, shift_acc: bool, progress) -> tuple[str, str]:
    cfg = _cfg()
    ffmpeg = find_ffmpeg(_norm(cfg.get("ffmpeg_path", "")))
    out_dir = Path(_norm(cfg.get("out_dir") or str(ROOT / "data/temp/whale_sing"))).resolve()
    cache_root = out_dir / "cover_gateway_cache"
    python, model_dir, sep_model, segment = _separator()
    base = str(cfg.get("rvc_base_url") or "http://127.0.0.1:8001").rstrip("/")
    timeout = float(cfg.get("rvc_timeout_sec", 1800))

    # Trim long songs to 5 minutes (like the official middleware) instead of refusing them.
    work = Path(tempfile.mkdtemp(prefix="gw_", dir=out_dir))
    trimmed = work / "source.wav"
    await run_process(ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                      "-t", str(int(MAX_DURATION) - 1), "-map", "0:a:0", "-vn", "-ar", "44100", "-ac", "2",
                      "-c:a", "pcm_s16le", str(trimmed), timeout=180)

    async def separate(src: Path, job: Path):
        progress(0.3, desc="正在分离人声和伴奏...")
        await run_process(str(python), str(SEPARATE_SCRIPT), "--input", str(src), "--output-dir", str(job),
                          "--model", sep_model, "--model-dir", str(model_dir), "--ffmpeg", ffmpeg,
                          "--segment-size", str(segment), "--device", "cuda", timeout=1800)
        vocals, backing = job / "vocals.wav", job / "instrumental.wav"
        if not vocals.is_file() or not backing.is_file():
            raise RuntimeError("分轨没有同时得到人声和伴奏")
        if key_shift and shift_acc:
            shifted = job / "instrumental_shift.wav"
            ratio = 2 ** (key_shift / 12)
            await run_process(ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(backing),
                              "-af", f"asetrate=44100*{ratio:.8f},aresample=44100,atempo={1 / ratio:.8f}",
                              str(shifted), timeout=300)
            backing.unlink(missing_ok=True)
            shifted.replace(backing)
        if inst_gain != 1.0:
            gained = job / "instrumental_gain.wav"
            await run_process(ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(backing),
                              "-af", f"volume={inst_gain:.6g}", str(gained), timeout=120)
            backing.unlink(missing_ok=True)
            gained.replace(backing)
        return vocals, backing

    async def convert_rvc(vocals: Path) -> Path:
        progress(0.6, desc="正在用RVC换成角色音色...")
        body = await asyncio.to_thread(_post_rvc, base, vocals, model, index, key_shift, timeout)
        if len(body) < 1000 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
            raise RuntimeError("RVC 没有返回有效 WAV 人声（请先启动 RVC 换声服务 :8001）")
        output = vocals.with_name("rvc_vocals.wav")
        output.write_bytes(body)
        if vocal_gain != 1.0:
            gained = vocals.with_name("rvc_vocals_gain.wav")
            await run_process(ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(output),
                              "-af", f"volume={vocal_gain:.6g}", str(gained), timeout=120)
            gained.replace(output)
        return output

    fingerprint = {"gateway": 1, "rvc_base_url": base, "pitch_shift": int(key_shift),
                   "shift_accompaniment": bool(shift_acc), "vocal_gain": vocal_gain, "inst_gain": inst_gain,
                   "separator": [sep_model, segment]}
    for label, path in (("rvc_model", model), ("rvc_index", index)):
        if path is not None and path.is_file():
            st = path.stat()
            fingerprint[label] = {"path": str(path.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    before = {p.parent.name for p in cache_root.glob("*/qq_preview.wav")} if cache_root.is_dir() else set()
    try:
        progress(0.1, desc="正在准备歌曲...")
        preview = await render_cover(trimmed, cache_root, ffmpeg, fingerprint=fingerprint,
                                     separate=separate, convert=convert_rvc, max_duration=MAX_DURATION)
    finally:
        for p in work.glob("*"):
            p.unlink(missing_ok=True)
        work.rmdir()
    hit = Path(preview).parent.name in before
    progress(1.0, desc="缓存命中，直接返回" if hit else "翻唱完成")
    return str(preview), "true" if hit else "false"


def _post_rvc(base: str, vocals: Path, model: Path, index: Path | None, key: int, timeout: float) -> bytes:
    import uuid
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

    def file(name, path: Path, ctype):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                     f'Content-Type: {ctype}\r\n\r\n'.encode() + path.read_bytes() + b"\r\n")
    file("input_audio", vocals, "audio/wav")
    field("modelpath", str(model))
    if index is not None:
        file("index_file", index, "application/octet-stream")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    url = f"{base}/inference?res_type=blob&f0_up_key={int(key)}"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"RVC 换声失败（HTTP {resp.status}）")
        return resp.read()


def _db_to_gain(db) -> float:
    try:
        value = float(db or 0)
    except (TypeError, ValueError):
        value = 0.0
    value = max(-30.0, min(12.0, value))
    return 10 ** (value / 20)


def convert(song_name_src, key_shift, vocal_vol, inst_vol, model_dropdown, reverb_intensity=4,
            delay_intensity=0, f0_method="rmvpe", index_rate=0.75, filter_radius=3, uvr5_agg=10,
            uvr5_tta=False, uvr5_postprocess=False, uvr5_window_size=512, uvr5_high_end_process="mirroring",
            msst_batch_size=1, msst_num_overlap=4, msst_normalize=False, vocal_postprocess=False,
            shift_accompaniment=True, progress=gr.Progress()):
    """翻唱：song_name_src 为本机音频路径、网易云歌曲ID或链接。返回 (音频路径, 是否缓存命中)。"""
    src = str(song_name_src or "").strip()
    if not src:
        raise gr.Error("请输入歌曲ID、链接或本地音频路径")
    try:
        key = int(round(float(key_shift or 0)))
    except (TypeError, ValueError):
        key = 0
    key = max(-12, min(12, key))
    model, index = _model_files(model_dropdown)
    with tempfile.TemporaryDirectory(prefix="gw_dl_") as tmp:
        if os.path.isfile(src):
            source = Path(src).resolve()
        else:
            m = re.search(r"id=(\d+)", src) if src.startswith("http") else re.fullmatch(r"\d{1,15}", src)
            if not m:
                raise gr.Error("只支持本机音频路径或网易云歌曲ID")
            song_id = m.group(1) if src.startswith("http") else m.group(0)
            progress(0.05, desc="正在下载网易云歌曲...")
            source = _download_netease(song_id, Path(tmp) / f"netease_{song_id}.mp3")
        try:
            return asyncio.run(_pipeline(source, model, index, key, _db_to_gain(vocal_vol), _db_to_gain(inst_vol),
                                         bool(shift_accompaniment), progress))
        except gr.Error:
            raise
        except Exception as error:  # surface a readable reason to the chat
            raise gr.Error(str(error)[-300:] or type(error).__name__)


def cache_info():
    cfg = _cfg()
    root = Path(_norm(cfg.get("out_dir") or str(ROOT / "data/temp/whale_sing"))) / "cover_gateway_cache"
    files = [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
    return {"rvc": {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}}


with gr.Blocks(title="WhaleBot cover gateway") as app:
    gr.Markdown("鲸鱼娘翻唱网关：给 AI点歌（matsuko_cover）用的本地接口，复用现有分轨 + RVC。")
    t = lambda **k: gr.Textbox(visible=False, **k)  # noqa: E731
    n = lambda v: gr.Number(value=v, visible=False)  # noqa: E731
    c = lambda v: gr.Checkbox(value=v, visible=False)  # noqa: E731
    inputs = [t(label="song_name_src"), n(0), n(0), n(0), t(label="model_dropdown"), n(4), n(0),
              t(value="rmvpe"), n(0.75), n(3), n(10), c(False), c(False), n(512), t(value="mirroring"),
              n(1), n(4), c(False), c(False), c(True)]
    gr.Button("convert", visible=False).click(convert, inputs=inputs,
                                              outputs=[gr.Audio(type="filepath", visible=False), t()],
                                              api_name="convert")
    gr.Button("show_model", visible=False).click(list_models, inputs=[], outputs=[gr.JSON(visible=False)],
                                                 api_name="show_model")
    gr.Button("cache_info", visible=False).click(cache_info, inputs=[], outputs=[gr.JSON(visible=False)],
                                                 api_name="cache_info")

if __name__ == "__main__":
    app.queue(max_size=8, default_concurrency_limit=1)
    app.launch(server_name="127.0.0.1", server_port=int(os.environ.get("COVER_GATEWAY_PORT", "3333")),
               show_error=True, allowed_paths=[str(ROOT / "data" / "temp")])
