import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from kb.config import DATA_DIR, VIDEOS_ROOT
from kb.youtube import transcripts
from kb.youtube.channels import VIDEO_ID
from kb.youtube.settings import model_path, of, tools


class Interrupted(RuntimeError):
    pass


def cache_dir(video_id):
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError('视频 ID 不正确')
    path = DATA_DIR / 'youtube' / video_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(path, payload):
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None


def terminate(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def run(arguments, output, deadline, stopped):
    if stopped() or time.monotonic() >= deadline:
        raise Interrupted('已停止或本轮视频处理时间预算用尽')
    with output.open('wb') as log:
        try:
            process = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != 'nt')
        except FileNotFoundError:
            raise RuntimeError(f'缺少命令 {arguments[0]}，请先安装对应工具') from None
        try:
            while process.poll() is None:
                if stopped() or time.monotonic() >= deadline:
                    raise Interrupted('已停止或本轮视频处理时间预算用尽')
                if output.stat().st_size > 8 * 1024 * 1024:
                    raise RuntimeError('工具输出异常过大，已停止本次处理')
                time.sleep(.2)
            if output.stat().st_size > 8 * 1024 * 1024:
                raise RuntimeError('工具输出异常过大，已停止本次处理')
            if process.returncode:
                message = output.read_text(encoding='utf-8', errors='replace')[-1000:]
                raise RuntimeError(f'{arguments[0]} 执行失败：{message}')
        finally:
            terminate(process)


def ytdlp(arguments, output, deadline, stopped):
    run(['yt-dlp', '--ignore-config', '--no-cache-dir', '--no-playlist', '--no-progress',
         '--no-warnings', '--socket-timeout', '15', '--retries', '1',
         '--extractor-retries', '1', *arguments], output, deadline, stopped)


def fresh_metadata(path, video_id):
    cached = read_json(path)
    if not isinstance(cached, dict) or cached.get('id') != video_id:
        return None
    lifetime = 900 if tracks(cached, False) or tracks(cached, True) else 120
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > lifetime:
        return None
    return cached


def probe(row, deadline, stopped, force=False):
    directory = cache_dir(row['video_id'])
    cached = None if force else fresh_metadata(directory / 'metadata.json', row['video_id'])
    if cached:
        return cached
    if not force and VIDEOS_ROOT.is_dir():
        for previous in sorted(VIDEOS_ROOT.glob('*' + row['video_id'] + '*')):
            cached = fresh_metadata(previous / 'metadata' / 'youtube_metadata.json', row['video_id'])
            if cached:
                return cached
    output = directory / 'probe.log'
    ytdlp(['--skip-download', '--dump-single-json', row['entry']['url']], output, deadline, stopped)
    try:
        payload = json.loads(output.read_text(encoding='utf-8'))
    except ValueError:
        raise RuntimeError('YouTube 元数据无效，未开始下载或转录') from None
    if not isinstance(payload, dict) or payload.get('id') != row['video_id']:
        raise RuntimeError('元数据的视频 ID 与候选不一致')
    save_json(directory / 'metadata.json', payload)
    return payload


def tracks(metadata, automatic=False):
    available = metadata.get('automatic_captions' if automatic else 'subtitles') or {}
    if not isinstance(available, dict):
        return []
    original = str(metadata.get('language') or '').split('-')[0]
    keys = [key for key in available if isinstance(key, str) and key != 'live_chat'
            and isinstance(available[key], list) and available[key]]
    if automatic:
        keys = [key for key in keys if any(isinstance(item, dict) and item.get('url') and
            not parse_qs(urlsplit(item['url']).query).get('tlang') for item in available[key])]
        if original:
            keys = [key for key in keys if key.split('-')[0] == original]
    keys.sort(key=lambda key: (key.split('-')[0] != original if original else not key.endswith('-orig'),
                               not key.startswith('en'), key))
    return keys


def local_transcript(video_id, duration):
    if not VIDEOS_ROOT.is_dir():
        return None
    for directory in sorted(VIDEOS_ROOT.glob('*' + video_id + '*')):
        metadata = read_json(directory / 'metadata' / 'youtube_metadata.json')
        path = directory / 'subtitles' / 'source.srt'
        if not isinstance(metadata, dict) or metadata.get('id') != video_id or not path.is_file():
            continue
        try:
            segments = transcripts.validate(transcripts.parse(path.read_text(encoding='utf-8'), '.srt'), duration)
            return {'origin': 'local_cache', 'language': metadata.get('language') or '', 'segments': segments}
        except (ValueError, OSError):
            continue
    return None


def captions(row, metadata, automatic, deadline, stopped):
    languages = tracks(metadata, automatic)
    if not languages:
        return None
    failures = []
    for position, language in enumerate(languages):
        directory = cache_dir(row['video_id']) / (('automatic' if automatic else 'uploaded') + f'-{position}')
        directory.mkdir(exist_ok=True)
        for stale in directory.glob('captions.*'):
            stale.unlink()
        try:
            ytdlp(['--skip-download', '--write-auto-subs' if automatic else '--write-subs',
                   '--sub-langs', language, '--sub-format', 'json3/vtt',
                   '-o', str(directory / 'captions.%(ext)s'), row['entry']['url']],
                  directory / 'download.log', deadline, stopped)
            for path in sorted(directory.glob('captions.*')):
                if path.suffix not in ('.json3', '.vtt'):
                    continue
                segments = transcripts.validate(transcripts.parse(path.read_text(encoding='utf-8'), path.suffix),
                                               float(metadata['duration']))
                return {'origin': 'automatic_captions' if automatic else 'uploaded_captions',
                        'language': language.removesuffix('-orig'), 'segments': segments}
            failures.append('工具未生成预期的字幕文件')
        except Interrupted:
            raise
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as error:
            failures.append(str(error))
    raise RuntimeError('；'.join(failures))


def asr(row, metadata, settings, deadline, stopped):
    directory = cache_dir(row['video_id'])
    output = directory / 'whisper.json'
    if output.is_file():
        try:
            segments = transcripts.validate(transcripts.parse(output.read_text(encoding='utf-8'), '.json'),
                                           float(metadata['duration']))
            return {'origin': 'local_asr', 'language': metadata.get('language') or '', 'segments': segments}
        except (ValueError, OSError, TypeError):
            pass
    status = tools(settings)
    if not status['asr_enabled']:
        raise RuntimeError('没有可用字幕，且本地语音转录已关闭')
    if not all(status[key] for key in ('ffmpeg', 'whisper', 'model')):
        raise RuntimeError('没有可用字幕；本地转录需要 FFmpeg、whisper-cli 和 Whisper 模型')
    original_tracks = tracks(metadata, True)
    language = str(metadata.get('language') or (original_tracks[0] if original_tracks else 'auto'))
    language = language.removesuffix('-orig').split('-')[0]
    model = model_path(settings)
    if '.en.' in model.name and language != 'en':
        raise RuntimeError('当前 Whisper 模型仅支持英文；此视频需要多语言模型，未开始转录')
    audio_files = [path for path in directory.glob('audio.*')
                   if path.suffix in ('.m4a', '.webm', '.opus', '.mp3', '.ogg', '.wav', '.aac', '.flac')]
    if not audio_files:
        ytdlp(['-f', 'bestaudio[abr<=160]/bestaudio', '--max-filesize', '100M',
               '-o', str(directory / 'audio.%(ext)s'), row['entry']['url']],
              directory / 'audio-download.log', deadline, stopped)
        audio_files = [path for path in directory.glob('audio.*')
                       if path.suffix in ('.m4a', '.webm', '.opus', '.mp3', '.ogg', '.wav', '.aac', '.flac')]
    if not audio_files:
        raise RuntimeError('未取得音频，不能运行本地语音转录')
    wave = directory / 'speech.wav'
    run(['ffmpeg', '-nostdin', '-y', '-i', str(audio_files[0]), '-vn', '-ar', '16000',
         '-ac', '1', '-acodec', 'pcm_s16le', str(wave)], directory / 'ffmpeg.log', deadline, stopped)
    output = directory / 'whisper'
    run(['whisper-cli', '-m', str(model), '-f', str(wave), '-l', language, '-oj', '-ojf',
         '-of', str(output), '-np', '-t', str(of(settings)['asr_threads'])],
        directory / 'asr.log', deadline, stopped)
    segments = transcripts.validate(transcripts.parse(output.with_suffix('.json').read_text(encoding='utf-8'), '.json'),
                                   float(metadata['duration']))
    return {'origin': 'local_asr', 'language': language, 'segments': segments}


def obtain(row, metadata, settings, deadline, stopped):
    directory = cache_dir(row['video_id'])
    payload = read_json(directory / 'transcript.json')
    if (isinstance(payload, dict) and payload.get('video_id') == row['video_id'] and
            payload.get('origin') in ('local_cache', 'uploaded_captions', 'automatic_captions', 'local_asr') and
            isinstance(payload.get('language'), str)):
        try:
            transcripts.validate(payload['segments'], float(metadata['duration']))
            return payload
        except (KeyError, ValueError, TypeError):
            pass
    payload = local_transcript(row['video_id'], float(metadata['duration']))
    errors = []
    for automatic in (False, True):
        if payload:
            break
        try:
            payload = captions(row, metadata, automatic, deadline, stopped)
        except Interrupted:
            raise
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as error:
            errors.append(str(error)[:300])
    if errors or not payload:
        (directory / 'metadata.json').unlink(missing_ok=True)
    if not payload:
        payload = asr(row, metadata, settings, deadline, stopped)
    payload.update(video_id=row['video_id'], caption_warnings=errors)
    save_json(directory / 'transcript.json', payload)
    for path in [directory / 'speech.wav', *directory.glob('audio.*')]:
        if path.is_file():
            path.unlink(missing_ok=True)
    return payload
