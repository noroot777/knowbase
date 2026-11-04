import hashlib
import time

from kb import llm, llm_stages
from kb.youtube import fulltext, media, model_response, transcripts, translation, translation_cache, translation_language, translation_pool
from kb.youtube.faults import local_fault


VERSION = 'youtube-notes-v3'
TITLE_STAGE = 'translate-title-v1'
ORIGINS = {'local_cache': '已有本地原文转录', 'uploaded_captions': '作者提供的字幕',
           'automatic_captions': 'YouTube 自动字幕', 'local_asr': '本地语音转录'}


def model(stage):
    selected = llm_stages.stage_model(stage)
    if not llm.has_key(selected.provider):
        raise RuntimeError(f'请先配置{selected.provider}的 API Key 和翻译模型')
    return selected


def available():
    try:
        model('translate')
        return True, ''
    except (RuntimeError, ValueError) as error:
        return False, str(error)


def call(prompt, selected, deadline, stopped):
    from kb.youtube.llm_request import request
    return request(prompt, selected, deadline, stopped)


def signature(stage, selected, text):
    value = '\n'.join((VERSION, stage, selected.provider, selected.model_id, text))
    return hashlib.sha256(value.encode()).hexdigest()


def native_chinese(text):
    return (not translation_language.foreign_script(text) and
            translation_language.chinese_output(text, text))


def original_comparison(blocks):
    if any(not native_chinese(block) for block in blocks):
        return '\n\n## 原文对照（未改写）\n\n' + '\n\n'.join(blocks)
    return ''


def translated_output(text, output):
    if not native_chinese(text):
        return translation.render(text, output)
    if not isinstance(output, str):
        raise RuntimeError('模型未返回中文译文，保留原始字幕等待重试')
    if not output.strip() or not translation_language.chinese_output(output, text):
        raise translation.LanguageError('模型未返回中文译文，保留原始字幕等待重试')
    return output.strip()


def cached_title(title, selected, cache):
    if native_chinese(title):
        return title
    for stage in (TITLE_STAGE, translation.STAGE):
        try:
            return translated_output(title, cache[signature(stage, selected, title)])
        except (KeyError, RuntimeError, ValueError, TypeError):
            continue
    return None


def build(row, transcript, deadline, stopped):
    if stopped() or time.monotonic() >= deadline:
        raise media.Interrupted('视频处理已停止或时间预算用尽')
    blocks = transcripts.blocks(transcript['segments'], limit=fulltext.MAX_SOURCE_CHARS)
    if not blocks:
        raise RuntimeError('没有可翻译的视频字幕正文')
    if native_chinese(row['entry']['title']) and all(native_chinese(block) for block in blocks):
        return translated_entry(row, transcript, blocks, blocks)
    directory = media.cache_dir(row['video_id'])
    translator = model('translate')
    return build_translation(row, transcript, blocks, deadline, stopped, directory, translator)


def build_translation(row, transcript, blocks, deadline, stopped, directory, translator):
    cache = media.read_json(directory / 'notes-cache.json')
    if not isinstance(cache, dict):
        cache = {}
    translated = {}
    jobs = {}
    title = row['entry']['title']
    title_key = signature(fulltext.TITLE_STAGE, translator, title)
    title_value = title if native_chinese(title) else cached_title(title, translator, cache)
    if title_value is None and title_key in cache:
        try:
            title_value = fulltext.validate(title, cache[title_key])
        except (RuntimeError, ValueError, TypeError):
            cache.pop(title_key)
    if title_value is not None:
        translated[title_key] = title_value
    else:
        jobs[title_key] = title
    planned = translation_cache.plan(transcript, blocks, translator, cache)
    blocks = [block for block, value in planned]
    for block, value in planned:
        if stopped() or time.monotonic() >= deadline:
            raise media.Interrupted('视频处理已停止或时间预算用尽')
        key = signature(fulltext.STAGE, translator, block)
        if value is not None:
            translated[key] = value
            continue
        jobs[key] = block

    def create(key, halted):
        block = jobs[key]
        try:
            output = call(fulltext.prompt(block), translator, deadline, halted)
            return fulltext.validate(block, output)
        except model_response.OutputError:
            if key != title_key:
                raise
        except RuntimeError as error:
            if key != title_key or isinstance(error, (media.Interrupted, media.ToolFailure)) or local_fault(error):
                raise
        return title

    def receive(key, future):
        if stopped() or time.monotonic() >= deadline:
            raise media.Interrupted('视频翻译已停止或时间预算用尽')
        value = future.result()
        if key != title_key or value != title:
            cache[key] = value
            media.save_json(directory / 'notes-cache.json', cache)
        return value

    results = translation_pool.run(jobs, create, receive, deadline, stopped) if jobs else {}
    translated.update(results)
    if stopped() or time.monotonic() >= deadline:
        raise media.Interrupted('视频处理已停止或时间预算用尽')
    return translated_entry(row, transcript, blocks,
                            [translated[signature(fulltext.STAGE, translator, block)] for block in blocks],
                            translated[title_key])


def translated_entry(row, transcript, blocks, translated, title=None):
    origin = ORIGINS.get(transcript['origin'], transcript['origin'])
    complete = '\n\n'.join(translated)
    chinese_source = all(native_chinese(block) for block in blocks)
    heading = '中文字幕原文' if chinese_source else '中文译文（自动生成）'
    caution = ('以上为字幕原文，未进行摘要或内容改写。' if chinese_source else
               '以上为字幕全文自动直译，未进行逐句语义审校或人工事实核查，请结合原视频核对。')
    body = ('## ' + heading + '\n\n' + complete +
            f'\n\n> 内容依据：{origin}；未分析视频画面。字幕和语音识别可能有误。'
            + caution
            + original_comparison(blocks))
    if not native_chinese(row['entry']['title']):
        body += '\n\n## 视频原题\n\n' + row['entry']['title']
    return {'url': row['entry']['url'], 'title': title or row['entry']['title'], 'body': body,
            'published': row['entry']['published'], 'author': row['channel'],
            'content_kind': 'markdown', 'body_provenance': 'youtube:' + transcript['origin']}
