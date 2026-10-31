"""ACE-Step asynchronous API integration without local model dependencies."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp


async def generate_music(
    base_url: str,
    payload: dict,
    output: Path,
    *,
    timeout: float = 900,
    poll_interval: float = 2,
) -> dict:
    """Submit one generation and download its matching audio result.

    Args:
        base_url: ACE-Step API origin, normally localhost port 8002.
        payload: Official release_task request fields.
        output: Destination for the generated mix.
        timeout: Total wall-clock budget, including polling and download.
        poll_interval: Delay between task status queries.

    Returns:
        Task ID and generation metadata for the selected variation.

    Raises:
        RuntimeError: The service, task, or downloaded result is invalid.
        TimeoutError: The total generation budget was exhausted.
    """
    base_url = base_url.rstrip("/")
    origin = urlsplit(base_url)
    if origin.scheme not in {"http", "https"} or not origin.netloc:
        raise ValueError("生成服务地址必须是 http:// 或 https:// 地址")
    headers = {}
    if token := os.environ.get("ACESTEP_API_KEY", "").strip():
        headers["Authorization"] = f"Bearer {token}"

    async def perform() -> dict:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=min(60, timeout)), headers=headers
        ) as session:
            async with session.post(f"{base_url}/release_task", json=payload) as response:
                if response.status != 200:
                    raise RuntimeError(f"生成服务拒绝任务（HTTP {response.status}）")
                envelope = await response.json()
            if envelope.get("code", 200) != 200 or envelope.get("error"):
                raise RuntimeError("生成服务提交任务失败，请查看生成服务日志")
            data = envelope.get("data") or {}
            task_id = data.get("task_id") if isinstance(data, dict) else None
            if not task_id:
                raise RuntimeError("生成服务未返回 task_id")

            while True:
                async with session.post(
                    f"{base_url}/query_result", json={"task_id_list": [task_id]}
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(f"查询生成任务失败（HTTP {response.status}）")
                    envelope = await response.json()
                if envelope.get("code", 200) != 200 or envelope.get("error"):
                    raise RuntimeError("生成任务查询失败，请查看生成服务日志")
                rows = envelope.get("data")
                if not isinstance(rows, list):
                    raise RuntimeError("生成服务返回了无效任务列表")
                row = next((item for item in rows if isinstance(item, dict) and item.get("task_id") == task_id), None)
                if row is None:
                    raise RuntimeError("生成服务未找到刚提交的任务")
                status = str(row.get("status"))
                if status == "2":
                    raise RuntimeError("歌曲生成失败，请查看生成服务日志")
                if status == "1":
                    results = row.get("result")
                    if isinstance(results, str):
                        try:
                            results = json.loads(results)
                        except json.JSONDecodeError as exc:
                            raise RuntimeError("生成服务返回了无效歌曲结果") from exc
                    if not isinstance(results, list) or not results:
                        raise RuntimeError("任务已结束，但没有生成歌曲")
                    result = results[0]
                    if not isinstance(result, dict) or not result.get("file"):
                        raise RuntimeError("生成结果缺少音频下载地址")
                    download_url = urljoin(base_url + "/", str(result["file"]))
                    target = urlsplit(download_url)
                    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                        raise RuntimeError("生成结果的下载地址不属于已配置的生成服务")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    partial = output.with_suffix(output.suffix + ".part")
                    try:
                        async with session.get(download_url, allow_redirects=False) as audio:
                            if audio.status != 200:
                                raise RuntimeError(f"下载歌曲失败（HTTP {audio.status}）")
                            size = 0
                            with partial.open("wb") as stream:
                                async for chunk in audio.content.iter_chunked(1024 * 256):
                                    size += len(chunk)
                                    if size > 128 * 1024 * 1024:
                                        raise RuntimeError("生成音频超过 128 MB，已停止下载")
                                    stream.write(chunk)
                        with partial.open("rb") as stream:
                            magic = stream.read(12)
                        if size < 1000 or magic[:4] != b"RIFF" or magic[8:12] != b"WAVE":
                            raise RuntimeError("生成服务没有返回有效 WAV，请检查 audio_format 配置")
                        partial.replace(output)
                    finally:
                        partial.unlink(missing_ok=True)
                    return {"task_id": task_id, "result": result}
                if status != "0":
                    raise RuntimeError(f"生成服务返回未知任务状态：{status}")
                await asyncio.sleep(poll_interval)

    try:
        return await asyncio.wait_for(perform(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError("歌曲生成等待超时；远端任务可能仍在处理，请勿连续重复提交") from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(
            "连接生成服务失败；请先完成 voice-backends/music-generation/setup.ps1，"
            "再从「鲸鱼娘.bat」启动原创生成"
        ) from exc
