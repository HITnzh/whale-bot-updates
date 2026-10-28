"""Separate a generated song in a short-lived, isolated audio-separator worker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"


class CudaUnavailableError(RuntimeError):
    """Signal a missing CUDA device separately from corrupt models or audio."""


def _is_cuda_failure(error: BaseException) -> bool:
    """Recognize allocation errors even when the library wraps them in SystemExit."""
    seen: set[int] = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        message = str(current).casefold()
        if isinstance(current, CudaUnavailableError) or any(
            token in message for token in (
                "cuda out of memory", "cuda error: out of memory", "cublas_status_alloc_failed",
            )
        ):
            return True
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    return False


def main(argv: list[str] | None = None) -> int:
    """Run explicit model setup or offline two-stem inference."""
    parser = argparse.ArgumentParser(description="生成歌曲分轨：人声与原伴奏")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--model-dir", type=Path,
        default=next(
            parent for parent in Path(__file__).resolve().parents
            if (parent / "voice-backends").is_dir() and (parent / "sing").is_dir()
        ) / "voice-backends" / "music-generation" / "models",
    )
    parser.add_argument("--device", choices=("cuda",), default="cuda")
    parser.add_argument("--segment-size", type=int, default=401)
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--download-model-only", action="store_true")
    args = parser.parse_args(argv)
    if not 129 <= args.segment_size <= 801:
        parser.error("--segment-size 必须在129～801之间。")
    started = time.monotonic()
    device = "cuda"
    torch = None
    if not args.download_model_only:
        if args.input is None or args.output_dir is None:
            parser.error("分轨需要 --input 和 --output-dir。")
        if not args.input.is_file():
            parser.error("输入歌曲文件不存在。")
        if not (args.model_dir / args.model).is_file():
            print("分轨模型尚未安装，请先运行 voice-backends/music-generation/setup.ps1。", file=sys.stderr)
            return 2
    if Path(args.model).name != args.model or args.model in (".", ".."):
        parser.error("--model 必须是模型文件名，不能包含目录。")
    args.model_dir = args.model_dir.resolve()
    # Avoid an inherited library variable silently redirecting the configured directory.
    os.environ["AUDIO_SEPARATOR_MODEL_DIR"] = str(args.model_dir)
    args.model_dir.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
        if not ffmpeg:
            for ancestor in Path(__file__).resolve().parents:
                candidates = sorted((ancestor / "voice-backends/RVC/tools").glob("ffmpeg-*/bin/ffmpeg.exe"))
                if candidates:
                    ffmpeg = str(candidates[-1])
                    break
        if not ffmpeg or not Path(ffmpeg).is_file():
            raise FileNotFoundError("分轨需要 FFmpeg，请设置 --ffmpeg 或恢复 RVC/tools 中的 FFmpeg。")
        os.environ["PATH"] = str(Path(ffmpeg).resolve().parent) + os.pathsep + os.environ.get("PATH", "")
        if not args.download_model_only:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
                torch.cuda.reset_peak_memory_stats()
            else:
                raise CudaUnavailableError("分轨配置为 CUDA，但当前分轨环境没有可用的 CUDA。")
        from audio_separator.separator import Separator

        class OfflineSeparator(Separator):
            """Forbid the library's implicit model and metadata downloads."""

            def download_file_if_not_exists(self, url, output_path):
                if not Path(output_path).is_file():
                    raise FileNotFoundError(
                        f"分轨资源缺失：{Path(output_path).name}，请先重新运行 setup.ps1。"
                    )

        separator_type = Separator if args.download_model_only else OfflineSeparator
        with tempfile.TemporaryDirectory(prefix="whale-stems-") as working:
            separator = separator_type(
                model_file_dir=str(args.model_dir),
                output_dir=working,
                output_format="WAV",
                sample_rate=44100,
                normalization_threshold=1.0,
                amplification_threshold=0.0,
                use_soundfile=True,
                use_autocast=False,
                use_native_fp16=device == "cuda",
                mdxc_params={
                    "segment_size": args.segment_size,
                    "override_model_segment_size": True,
                    "batch_size": 1,
                    "overlap": None,
                    "pitch_shift": 0,
                },
                log_level=logging.WARNING,
            )
            if args.download_model_only:
                separator.download_model_files(args.model)
                print(json.dumps({"model": args.model, "model_dir": str(args.model_dir)}))
                return 0
            separator.load_model(args.model)
            model = separator.model_instance
            if not args.download_model_only and getattr(getattr(model, "torch_device", None), "type", None) != "cuda":
                raise CudaUnavailableError("分轨器未使用 CUDA，已停止处理。")
            labels = [str(model.primary_stem_name), str(model.secondary_stem_name)]
            canonical = [label.strip().casefold().replace("_", " ") for label in labels]
            vocal_labels = {"vocals", "vocal"}
            backing_labels = {"instrumental", "no vocals", "other"}
            if sum(label in vocal_labels for label in canonical) != 1 or sum(
                label in backing_labels for label in canonical
            ) != 1:
                raise RuntimeError("请选择明确输出人声和完整伴奏的双轨分离模型。")
            names = {
                label: "vocals" if canonical[index] in vocal_labels else "instrumental"
                for index, label in enumerate(labels)
            }
            outputs = separator.separate(str(args.input.resolve()), custom_output_names=names)
            produced = {
                (Path(working) / output).resolve()
                for output in outputs
            }
            vocals = Path(working) / "vocals.wav"
            backing = Path(working) / "instrumental.wav"
            expected = {vocals.resolve(), backing.resolve()}
            if produced != expected or not all(path.is_file() and path.stat().st_size > 44 for path in expected):
                raise RuntimeError("分轨器没有返回完整、明确的人声和伴奏，已停止换声。")
            args.output_dir.mkdir(parents=True, exist_ok=True)
            result = {}
            for stem, source in (("vocals", vocals), ("instrumental", backing)):
                target = args.output_dir.resolve() / f"{stem}.wav"
                shutil.copyfile(source, target)
                result[stem] = str(target)
            result.update(
                device=device,
                precision=getattr(separator, "effective_precision", "unknown"),
                segment_size=args.segment_size,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            if device == "cuda" and torch is not None:
                result["peak_cuda_memory_mib"] = round(torch.cuda.max_memory_allocated() / 1024**2, 1)
            print(json.dumps(result))
        return 0
    except ImportError as error:
        print(f"分轨依赖不完整（{error}），请运行 voice-backends/music-generation/setup.ps1 -SeparatorOnly。", file=sys.stderr)
        return 2
    except SystemExit as error:
        if device == "cuda" and _is_cuda_failure(error):
            print("CUDA 显存不足，分轨已停止；请减少显卡占用或减小分轨片段后重试。", file=sys.stderr)
            return 3
        raise
    except Exception as error:
        if _is_cuda_failure(error) and (device == "cuda" or args.device == "cuda"):
            print(f"CUDA 分轨不可用：{str(error)[-500:]}", file=sys.stderr)
            return 3
        print(f"分轨失败：{str(error)[-600:]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
