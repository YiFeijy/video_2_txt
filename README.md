# B站视频文字稿工具

输入一个公开 B站视频链接或 BV 号，输出三份可对照的字幕：画面硬字幕、语音识别稿和双轨对照稿。另有纯文字稿、可直接加载的推荐 SRT、来源信息和处理参数。正片有片尾广告时，可手动设置截断时间。

## 安装

需要 Python 3.10+、`ffmpeg` 和 `ffprobe`。首次语音识别会下载所选模型；默认的 `large-v3-turbo` 模型约 1.6 GB。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bili_transcript.py --help
```

## 使用

```bash
.venv/bin/python bili_transcript.py 'https://www.bilibili.com/video/BV1RK4y197G7/'
```

输出在 `transcripts/BV号/`；多 P 且指定 `--page` 时在 `transcripts/BV号/p序号/`。其中 `推荐文字稿.txt` 和 `推荐字幕.srt` 优先采用画面文字；`语音文字稿.txt` 和 `语音转写.srt` 独立保留自动识别结果；`双轨对照.md` 和 `双轨对照.srt` 用于查看差异。若 B站提供独立字幕轨，工具还会导出 `平台字幕.srt`，不把它混作语音或硬字幕。

片尾广告需要由使用者给出时间点。本项目第二条视频的正片可这样处理：

```bash
.venv/bin/python bili_transcript.py 'https://www.bilibili.com/video/BV1HUvmBZEv7/' \
  --stop-at 32:23 --audio-stop-at 32:20.47 --ocr-bottom 0.18
```

其他常用选项：

```bash
--page 2                 # 多 P 视频；链接中 ?p=2 也会自动识别
--model small            # 减少模型大小和计算量，准确率通常也会降低
--language auto          # 自动判断语音语言
--device cuda            # 使用 GPU
--ocr-fps 2              # 每秒抽两帧，减少漏掉短字幕的机会
--ocr-bottom 0.28        # 把画面底部 28% 作为硬字幕搜索区域
--no-ocr                 # 只做语音转写
--no-asr                 # 只提取画面硬字幕
--audio-file audio.m4a   # 使用本地音频，避免重复下载
--video-file video.mp4   # 使用本地视频，避免重复下载
--model-dir .models      # 指定模型缓存目录
-o my_transcripts        # 指定输出根目录
```

本地媒体示例：

```bash
.venv/bin/python bili_transcript.py BV1RK4y197G7 \
  --audio-file video.mp4 --video-file video.mp4 --stop-at 00:20
```

`cache/` 保存下载媒体和识别结果，重跑同一链接时可复用。改变本地媒体、模型、抽帧率、画面区域或截断时间会产生对应的新缓存。字幕与文字稿使用 UTF-8。

## 工作方式与边界

1. 查询 B站公开接口，选择音轨和低清视频；若有独立字幕轨，单独导出。
2. 用 `faster-whisper` 转写音频；用 `RapidOCR` 识别抽帧后的底部字幕。
3. 合并连续重复的画面字幕，按时间近似配对语音，输出 SRT、TXT、Markdown 和 `manifest.json`。

目前支持公开、可播放的 B站 BV 链接。需要登录或付费权限的视频、短链、其他网站尚未支持。B站接口变化时，媒体下载可能失效。OCR 依赖底部区域和抽帧率，极短字幕或不在底部的字幕可能漏掉；语音识别也可能把人名、数字和快语速读错。双轨对照保留两种来源的原文，时间配对并非逐字对齐。平台字幕由未签名的公开接口获取，可能返回不相关内容，不能作为已核实的原文；应对照画面和语音确认。工具不自动判断广告起点，应先查看视频再设置 `--stop-at`。

## 同类项目及可借鉴点

以下按项目文档中的功能对比，未对识别准确率或处理速度做同条件实测。

| 项目 | 比本工具成熟的地方 | 适合借鉴的做法 |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 多站点下载、登录 Cookie、现成/自动字幕获取 | 作为媒体下载后端，减少 B站接口改版带来的维护工作 |
| [bili-transcript](https://github.com/uraraneko/bili-transcript) | 安装成 CLI、浏览器 Cookie、现成字幕优先、TXT/SRT/JSON 可选 | 增加 Cookie、JSON 导出和字幕优先模式 |
| [bilibili-content](https://github.com/Air000000/bilibili-content) | 多 P 选择状态、字幕内容校验和回退 | 结构化处理状态，以及防止无关字幕进入结果 |
| [bilibili-video-transcriber](https://github.com/adolescen-he/bilibili-video-transcriber) | 项目文档说明使用 Wbi 签名接口、三级校验、字幕到 Whisper 的降级链 | 优先补齐平台 AI 字幕可信度验证 |
| [WhisperX](https://github.com/m-bain/whisperX) | 词级时间对齐、说话人区分 | 需要精确时轴或多人对话时作为可选识别后端 |

本工具的侧重点是把画面硬字幕和独立语音识别同时保留、按时间对照，并按指定时间去掉片尾广告。按上述项目文档，没有发现它们直接提供相同的双轨对照。改进优先级：先接入可信的平台字幕读取/验证，再改进下载后端，最后按需要增加词级对齐。
