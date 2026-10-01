#!/usr/bin/env python3
"""Turn one public Bilibili video into speech, screen, and comparison transcripts."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def log(message: str) -> None:
    print(message, flush=True)


def parse_time(value: str | None) -> float | None:
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) > 3:
        raise ValueError(f"无效时间：{value}")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError as exc:
        raise ValueError(f"无效时间：{value}") from exc
    if seconds < 0:
        raise ValueError("时间不能为负数")
    return seconds


def bvid_from_input(value: str) -> str:
    match = BV_RE.search(value)
    if not match:
        raise ValueError("目前支持 B站 BV 号或包含 BV 号的视频链接")
    if "://" in value:
        host = urllib.parse.urlparse(value).hostname or ""
        if host not in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
            raise ValueError("视频链接必须来自 bilibili.com")
    return match.group(0)


def request_bytes(url: str, referer: str, timeout: int = 40) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def request_json(url: str, referer: str) -> dict[str, Any]:
    payload = json.loads(request_bytes(url, referer).decode("utf-8"))
    if isinstance(payload, dict) and payload.get("code", 0) != 0:
        raise RuntimeError(f"B站接口返回错误 {payload.get('code')}: {payload.get('message')}")
    return payload


def download(urls: list[str], target: Path, referer: str) -> Path:
    if target.is_file() and target.stat().st_size:
        try:
            if ffprobe_duration(target) > 0:
                return target
        except (OSError, ValueError, subprocess.CalledProcessError):
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for url in urls:
        temp = target.with_suffix(target.suffix + ".part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
            with urllib.request.urlopen(request, timeout=120) as source, temp.open("wb") as dest:
                while chunk := source.read(1024 * 1024):
                    dest.write(chunk)
            if not temp.stat().st_size:
                raise RuntimeError("下载结果为空")
            temp.replace(target)
            if ffprobe_duration(target) <= 0:
                raise RuntimeError("下载的媒体文件无法读取")
            return target
        except (OSError, urllib.error.URLError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            errors.append(str(exc))
            temp.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
    raise RuntimeError("所有下载地址都失败：" + "; ".join(errors[-3:]))


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        text=True,
        capture_output=True,
    )
    return float(result.stdout.strip())


def media_fingerprint(path: Path) -> str:
    stat = path.stat()
    identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def media_info(bvid: str, page: int | None, cache: Path) -> dict[str, Any]:
    referer = f"https://www.bilibili.com/video/{bvid}/"
    view = request_json(
        "https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode({"bvid": bvid}),
        referer,
    )["data"]
    pages = view.get("pages") or []
    if page is None:
        if len(pages) > 1:
            raise ValueError(f"此视频有 {len(pages)} P；请指定 --page N")
        page = 1
    if not 1 <= page <= len(pages):
        raise ValueError(f"此视频共有 {len(pages)} P；--page 必须在该范围内")
    cid = pages[page - 1]["cid"]
    params = urllib.parse.urlencode({"bvid": bvid, "cid": cid})
    player = request_json(f"https://api.bilibili.com/x/player/v2?{params}", referer)["data"]
    playurl = request_json(f"https://api.bilibili.com/x/player/playurl?{params}&qn=16&fnval=16", referer)["data"]
    metadata = {
        "bvid": bvid,
        "title": view.get("title", bvid),
        "page": page,
        "cid": cid,
        "duration": pages[page - 1].get("duration") or view.get("duration"),
        "source": referer,
        "subtitle_tracks": (player.get("subtitle") or {}).get("subtitles", []),
    }
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"metadata": metadata, "playurl": playurl}


def media_urls(playurl: dict[str, Any], kind: str) -> tuple[list[str], str]:
    dash = playurl.get("dash") or {}
    if dash and (dash.get("audio") if kind == "audio" else dash.get("video")):
        if kind == "audio":
            formats = dash.get("audio") or []
            chosen = max(formats, key=lambda item: item.get("bandwidth", 0))
            extension = ".m4a"
        else:
            formats = dash.get("video") or []
            eligible = [item for item in formats if item.get("height", 9999) <= 360]
            chosen = min(eligible or formats, key=lambda item: (abs(item.get("height", 360) - 360), item.get("bandwidth", 0)))
            extension = ".m4v"
        return [chosen.get("baseUrl") or chosen.get("base_url"), *(chosen.get("backupUrl") or chosen.get("backup_url") or [])], extension
    durl = playurl.get("durl") or []
    if not durl:
        raise RuntimeError("播放接口没有返回可下载的音视频地址")
    first = durl[0]
    return [first["url"], *(first.get("backup_url") or [])], ".mp4"


def official_subtitle(tracks: list[dict[str, Any]], cache: Path, referer: str) -> list[tuple[float, float, str]]:
    if not tracks:
        return []
    tracks = sorted(tracks, key=lambda item: (not str(item.get("lan", "")).startswith("zh"), str(item.get("lan", ""))))
    for track in tracks:
        url = track.get("subtitle_url") or track.get("url")
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        try:
            payload = json.loads(request_bytes(url, referer).decode("utf-8"))
            rows = [
                (float(row["from"]), float(row["to"]), str(row["content"]))
                for row in payload.get("body", [])
            ]
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            continue
        if rows:
            (cache / "official_track.json").write_text(
                json.dumps({"track": track, "body": payload.get("body", [])}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return rows
    return []


def transcribe(
    audio_file: Path, cache: Path, model_name: str, language: str, device: str, model_dir: Path, end: float
) -> list[tuple[float, float, str]]:
    fingerprint = media_fingerprint(audio_file)
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{model_name}_{language}_{device}_{end:.2f}_{fingerprint}")
    path = cache / f"speech_{key}.json"
    if path.is_file():
        return [(float(a), float(b), str(t)) for a, b, t in json.loads(path.read_text(encoding="utf-8"))]
    import numpy as np
    from faster_whisper import WhisperModel

    wav_path = cache / f"audio_16k_{fingerprint}.wav"
    if not wav_path.is_file():
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(audio_file), "-ar", "16000", "-ac", "1", str(wav_path)],
            check=True,
        )
    with wave.open(str(wav_path), "rb") as wav:
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    audio = audio[: int(end * 16000)]
    model = WhisperModel(model_name, device=device, compute_type="int8", download_root=str(model_dir))
    segments, _ = model.transcribe(
        audio, language=None if language == "auto" else language, beam_size=5, vad_filter=True, condition_on_previous_text=False
    )
    result = []
    for segment in segments:
        result.append((float(segment.start), float(segment.end), segment.text.strip()))
        if len(result) % 50 == 0:
            log(f"语音识别：{len(result)} 条，处理到 {segment.end:.1f} 秒")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def ocr_batch(paths: list[str], fps: float, confidence: float) -> list[dict[str, Any]]:
    import cv2
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result = []
    for name in paths:
        image = cv2.imread(name)
        if image is None:
            continue
        height = image.shape[0]
        found, _ = engine(image)
        parts = []
        for box, value, score in found or []:
            y = sum(point[1] for point in box) / 4
            x = sum(point[0] for point in box) / 4
            if score >= confidence and 0.14 * height <= y <= 0.85 * height and re.search(r"[\u4e00-\u9fff]", value):
                parts.append((y, x, value, score))
        band = max(8, height * 0.17)
        parts.sort(key=lambda item: (round(item[0] / band), item[1]))
        result.append(
            {
                "time": (int(Path(name).stem) - 1) / fps,
                "text": "".join(item[2] for item in parts),
                "confidence": min((item[3] for item in parts), default=0.0),
            }
        )
    return result


def extract_screen(
    video_file: Path, cache: Path, fps: float, bottom: float, confidence: float, workers: int, end: float
) -> list[dict[str, Any]]:
    fingerprint = media_fingerprint(video_file)
    key = f"fps{fps:g}_bottom{bottom:g}_conf{confidence:g}_end{end:.2f}_{fingerprint}"
    path = cache / f"ocr_{key}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    frames = cache / f"frames_{fps:g}_{bottom:g}_{end:.2f}_{fingerprint}"
    frames.mkdir(parents=True, exist_ok=True)
    marker = frames / ".complete"
    if not marker.is_file():
        crop = f"fps={fps},crop=iw:ih*{bottom}:0:ih*(1-{bottom})"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(video_file), "-t", str(end), "-vf", crop, "-vsync", "0", str(frames / "%06d.png")],
            check=True,
        )
        marker.write_text("complete\n", encoding="utf-8")
    frame_paths = [str(path) for path in sorted(frames.glob("*.png"))]
    batches = [frame_paths[i : i + 100] for i in range(0, len(frame_paths), 100)]
    rows = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(ocr_batch, batch, fps, confidence) for batch in batches]
        for index, future in enumerate(futures, 1):
            rows.extend(future.result())
            log(f"画面识别：{index}/{len(batches)} 批")
    rows.sort(key=lambda row: row["time"])
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def similar(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 3 and len(shorter) < len(longer) and shorter in longer and len(longer) - len(shorter) <= 3:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.86


def merge_screen(rows: list[dict[str, Any]], fps: float, stop: float) -> list[tuple[float, float, str]]:
    cues = []
    run = []

    def close() -> None:
        if not run:
            return
        counts = Counter(row["text"] for row in run)
        text = max(counts, key=lambda value: (counts[value], len(value)))
        cues.append((run[0]["time"], min(stop, run[-1]["time"] + 1 / fps), text))
        run.clear()

    for row in rows:
        if row["time"] >= stop:
            break
        text = row["text"].strip()
        if not text:
            if run and row["time"] - run[-1]["time"] > 1 / fps:
                close()
            continue
        if run and not (row["time"] - run[-1]["time"] <= 2 / fps and similar(text, run[-1]["text"])):
            close()
        run.append(row)
    close()
    return [(a, b, text) for a, b, text in cues if a < b]


def clip(cues: list[tuple[float, float, str]], end: float) -> list[tuple[float, float, str]]:
    return [(a, min(b, end), text) for a, b, text in cues if a < end and a < min(b, end)]


def combine(
    screen: list[tuple[float, float, str]], speech: list[tuple[float, float, str]]
) -> tuple[list[tuple[float, float, str, str]], list[tuple[float, float, str]]]:
    assigned = [[] for _ in screen]
    voice_only = []
    cursor = 0
    for start, end, text in speech:
        while cursor < len(screen) and screen[cursor][1] <= start:
            cursor += 1
        best, best_score = None, -1.0
        for index in range(max(0, cursor - 1), len(screen)):
            s_start, s_end, s_text = screen[index]
            if s_start >= end:
                break
            overlap = max(0.0, min(end, s_end) - max(start, s_start))
            if overlap <= 0:
                continue
            score = overlap / max(0.1, end - start) + 0.8 * SequenceMatcher(None, text, s_text).ratio()
            if score > best_score:
                best, best_score = index, score
        if best is not None:
            assigned[best].append((start, end, text))
        elif end - start >= 0.35:
            voice_only.append((start, end, text))
    rows = [
        (
            min([a, *(start for start, _, _ in assigned[index])]),
            max([b, *(end for _, end, _ in assigned[index])]),
            text,
            " ".join(value for _, _, value in assigned[index]),
        )
        for index, (a, b, text) in enumerate(screen)
    ]
    rows.extend((a, b, "", text) for a, b, text in voice_only)
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows, voice_only


def srt_time(seconds: float) -> str:
    total = round(seconds * 1000)
    hours, total = divmod(total, 3_600_000)
    minutes, total = divmod(total, 60_000)
    secs, millis = divmod(total, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def short_time(seconds: float) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes):02}:{secs:04.1f}"


def write_srt(path: Path, cues: list[tuple[float, float, str]]) -> None:
    path.write_text(
        "\n\n".join(
            f"{index}\n{srt_time(a)} --> {srt_time(b)}\n{text}"
            for index, (a, b, text) in enumerate(cues, 1)
        ) + "\n",
        encoding="utf-8",
    )


def render(
    output: Path,
    bvid: str,
    metadata: dict[str, Any],
    screen: list[tuple[float, float, str]],
    speech: list[tuple[float, float, str]],
    official: list[tuple[float, float, str]],
    args: argparse.Namespace,
    screen_end: float,
    speech_end: float,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    rows, voice_only = combine(screen, speech)
    write_srt(output / "画面字幕.srt", screen)
    write_srt(output / "语音转写.srt", speech)
    if official:
        write_srt(output / "平台字幕.srt", official)
    else:
        (output / "平台字幕.srt").unlink(missing_ok=True)
        (output / "平台文字稿.txt").unlink(missing_ok=True)
    dual = [
        (a, b, "\n".join(label + value for label, value in (("画面字幕：", text), ("语音识别：", voice)) if value))
        for a, b, text, voice in rows
    ]
    write_srt(output / "双轨对照.srt", dual)
    main = list(screen) if screen else list(speech)
    if screen:
        # Include speech from long intervals with no screen caption.
        main.extend(
            (a, b, text)
            for a, b, text in voice_only
            if b - a >= 2.5
        )
        main.sort(key=lambda item: (item[0], item[1]))
    write_srt(output / "推荐字幕.srt", main)
    for name, cues in (("画面文字稿.txt", screen), ("语音文字稿.txt", speech), ("推荐文字稿.txt", main)):
        (output / name).write_text(
            "\n".join(f"[{short_time(a)}] {text}" for a, _, text in cues) + "\n", encoding="utf-8"
        )
    if official:
        (output / "平台文字稿.txt").write_text(
            "\n".join(f"[{short_time(a)}] {text}" for a, _, text in official) + "\n", encoding="utf-8"
        )
    lines = [
        f"# {metadata.get('title') or bvid}",
        "",
        f"视频：{metadata.get('source') or 'https://www.bilibili.com/video/' + bvid + '/'}",
        "",
        "画面字幕来自抽帧 OCR；语音栏为独立自动识别结果。时间配对是近似的，保留措辞差异。",
        f"画面处理到 {short_time(screen_end)}；语音处理到 {short_time(speech_end)}。",
        "语音可能有错字；短于抽帧间隔的字幕可能漏检。平台字幕如存在，会单独导出。",
        "",
        "| 时间 | 画面字幕 | 语音识别 |",
        "| --- | --- | --- |",
    ]
    for a, b, text, voice in rows:
        lines.append(f"| {short_time(a)}–{short_time(b)} | {text.replace('|', '&#124;')} | {voice.replace('|', '&#124;')} |")
    (output / "双轨对照.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {
        "source": metadata.get("source"),
        "bvid": bvid,
        "title": metadata.get("title"),
        "page": metadata.get("page"),
        "screen_end": screen_end,
        "speech_end": speech_end,
        "model": args.model,
        "language": args.language,
        "ocr_fps": args.ocr_fps,
        "ocr_bottom": args.ocr_bottom,
        "counts": {"screen": len(screen), "speech": len(speech), "official": len(official), "dual": len(rows)},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B站链接/BV号 → 语音、画面字幕和双轨文字稿")
    parser.add_argument("url", help="B站视频链接或 BV 号")
    parser.add_argument("-o", "--output", type=Path, default=Path("transcripts"), help="输出根目录")
    parser.add_argument("--page", type=int, help="多 P 视频的分 P 序号；多 P 时必填")
    parser.add_argument("--model", default="large-v3-turbo", help="faster-whisper 模型")
    parser.add_argument("--language", default="zh", help="语音语言；auto 表示自动识别")
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"), help="识别设备")
    parser.add_argument("--model-dir", type=Path, default=Path(__file__).parent / ".models", help="语音模型缓存目录")
    parser.add_argument("--ocr-fps", type=float, default=1.0, help="每秒抽帧数")
    parser.add_argument("--ocr-bottom", type=float, default=0.22, help="OCR 画面底部占比")
    parser.add_argument("--ocr-confidence", type=float, default=0.58, help="OCR 最低置信度")
    parser.add_argument("--workers", type=int, default=4, help="OCR 并行进程数")
    parser.add_argument("--stop-at", help="正片结束时间，例如 32:23；同时裁剪语音和画面")
    parser.add_argument("--audio-stop-at", help="语音单独结束时间，可早于 --stop-at")
    parser.add_argument("--audio-file", type=Path, help="本地音频/视频文件，跳过音频下载")
    parser.add_argument("--video-file", type=Path, help="本地视频文件，跳过视频下载")
    parser.add_argument("--no-asr", action="store_true", help="跳过语音识别")
    parser.add_argument("--no-ocr", action="store_true", help="跳过画面文字识别")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bvid = bvid_from_input(args.url)
        if args.page is None and "://" in args.url:
            page_values = urllib.parse.parse_qs(urllib.parse.urlparse(args.url).query).get("p", [])
            if page_values:
                args.page = int(page_values[0])
        if args.no_asr and args.no_ocr:
            parser.error("--no-asr 与 --no-ocr 不能同时使用")
        if not 0 < args.ocr_bottom < 1 or args.ocr_fps <= 0 or args.workers < 1:
            parser.error("OCR 参数超出范围")
        screen_requested = parse_time(args.stop_at)
        speech_requested = parse_time(args.audio_stop_at)
        output = args.output.resolve() / bvid
        if args.page is not None:
            output /= f"p{args.page}"
        cache = output / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        offline = (args.no_asr or args.audio_file is not None) and (args.no_ocr or args.video_file is not None)
        if offline:
            source = args.video_file or args.audio_file
            assert source is not None
            duration = ffprobe_duration(source)
            metadata = {"bvid": bvid, "title": bvid, "source": f"https://www.bilibili.com/video/{bvid}/", "page": args.page or 1, "duration": duration}
            playurl = {}
            official = []
        else:
            info = media_info(bvid, args.page, cache)
            metadata, playurl = info["metadata"], info["playurl"]
            duration = float(metadata["duration"])
            official = official_subtitle(metadata["subtitle_tracks"], cache, metadata["source"])
        screen_end = min(duration, screen_requested if screen_requested is not None else duration)
        speech_end = min(screen_end, speech_requested if speech_requested is not None else screen_end)
        if screen_end <= 0 or speech_end <= 0:
            parser.error("结束时间必须大于零")
        referer = metadata["source"]
        if not args.no_asr:
            if args.audio_file:
                audio_file = args.audio_file.resolve()
            else:
                urls, ext = media_urls(playurl, "audio")
                log("下载音轨…")
                audio_file = download(urls, cache / ("audio" + ext), referer)
            log("语音识别…")
            speech = clip(transcribe(audio_file, cache, args.model, args.language, args.device, args.model_dir, speech_end), speech_end)
        else:
            speech = []
        if not args.no_ocr:
            if args.video_file:
                video_file = args.video_file.resolve()
            else:
                urls, ext = media_urls(playurl, "video")
                log("下载低清画面…")
                video_file = download(urls, cache / ("video" + ext), referer)
            log("画面字幕 OCR…")
            raw_screen = extract_screen(video_file, cache, args.ocr_fps, args.ocr_bottom, args.ocr_confidence, args.workers, screen_end)
            screen = merge_screen(raw_screen, args.ocr_fps, screen_end)
        else:
            screen = []
        render(output, bvid, metadata, screen, speech, clip(official, screen_end), args, screen_end, speech_end)
        log(f"完成：{output}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
