import hashlib
import json
import time

from kb import llm, llm_stages
from kb.youtube import json_output, media, model_response, transcripts, translation, translation_language, translation_pool
from kb.youtube.faults import local_fault


VERSION = 'youtube-notes-v3'
TITLE_STAGE = 'translate-title-v1'
MAX_LANGUAGE_TRIES = 3
CONTEXT_LIMIT = 14000
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


def cached(directory, cache, key, create, validate):
    if key in cache:
        try:
            return validate(cache[key])
        except (RuntimeError, ValueError, TypeError):
            cache.pop(key)
            if directory is not None:
                media.save_json(directory / 'notes-cache.json', cache)
    output = create()
    result = validate(output)
    cache[key] = output
    if directory is not None:
        media.save_json(directory / 'notes-cache.json', cache)
    return result


def signature(stage, selected, text):
    value = '\n'.join((VERSION, stage, selected.provider, selected.model_id, text))
    return hashlib.sha256(value.encode()).hexdigest()


def translate(text, selected, deadline, stopped):
    return translated_output(text, translation_request(text, selected, deadline, stopped))


def native_chinese(text):
    return (not translation_language.foreign_script(text) and
            translation_language.chinese_output(text, text))


def original_comparison(blocks):
    if any(not native_chinese(block) for block in blocks):
        return '\n\n## 原文对照（未改写）\n\n' + '\n\n'.join(blocks)
    return ''


def translation_request(text, selected, deadline, stopped, directory=None, cache=None):
    if native_chinese(text):
        return text
    output = call(translation.prompt(text), selected, deadline, stopped)
    try:
        translation.render(text, output)
    except RuntimeError:
        translated = []
        for position, unit in enumerate(translation.units(text)):
            block = unit['text']
            create = lambda block=block: call(translation.prompt(block), selected, deadline, stopped)
            validate = lambda output, position=position: translation.unit_output(text, position, output)
            if cache is not None:
                key = signature('translate-unit-v1', selected, block)
                translated.append(cached(directory, cache, key, create, validate))
            else:
                translated.append(validate(create()))
        output = json.dumps({'translations': translated}, ensure_ascii=False)
        translation.render(text, output)
    return output


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


def translate_title(title, selected, deadline, stopped, cache):
    try:
        output = call(translation.prompt(title), selected, deadline, stopped)
    except model_response.OutputError:
        return title
    try:
        value = translated_output(title, output)
    except (RuntimeError, ValueError, TypeError):
        return title
    cache[signature(TITLE_STAGE, selected, title)] = output
    return value


def summary_output(output, source=None, quotes=(), chinese_source=False):
    """兼容历史摘要缓存的校验，不参与新的字幕翻译流程。"""
    if not isinstance(output, str) or not output.strip():
        raise RuntimeError('视频分段要点不是有效中文，原文和译文已缓存')
    if not translation_language.chinese_output(output, source or '', quotes, chinese_source=chinese_source):
        raise translation.LanguageError('视频分段要点不是有效中文，原文和译文已缓存', 'summary')
    if len(output) > CONTEXT_LIMIT:
        raise RuntimeError('分段要点未能压缩到可处理范围，原文和译文已缓存')
    return output


def note_payload(output, prompt='', source='', quotes=(), chinese_source=False):
    """兼容历史结构化成稿的校验，不参与新的字幕翻译流程。"""
    try:
        payload = json_output.object_payload(output, prompt, required_fields=('title', 'summary', 'body'))
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError('视频笔记返回格式无效，原文与译文已缓存') from None
    if (not isinstance(payload, dict) or any(not isinstance(payload.get(field), str) or
            not payload[field].strip() for field in ('title', 'summary', 'body'))):
        raise RuntimeError('视频笔记不是完整中文成品，未入库')
    if any(not translation_language.chinese_output(payload[field], source, quotes, chinese_source=chinese_source)
           for field in ('title', 'summary', 'body')):
        raise translation.LanguageError('视频笔记不是完整中文成品，未入库', 'note')
    return payload


def build(row, transcript, deadline, stopped):
    if stopped() or time.monotonic() >= deadline:
        raise media.Interrupted('视频处理已停止或时间预算用尽')
    blocks = transcripts.blocks(transcript['segments'])
    if not blocks:
        raise RuntimeError('没有可翻译的视频字幕正文')
    if native_chinese(row['entry']['title']) and all(native_chinese(block) for block in blocks):
        return translated_entry(row, transcript, blocks, blocks)
    directory = media.cache_dir(row['video_id'])
    translator = model('translate')
    fingerprint = (json.dumps(transcript, ensure_ascii=False, sort_keys=True) + '\n' +
                   translator.provider + '\n' + translator.model_id)
    key = signature('language-failure-v2-translation', translator, fingerprint)
    path = directory / 'notes-language-failures.json'
    failures = media.read_json(path)
    if not isinstance(failures, dict):
        failures = {}
    attempts = value if isinstance(value := failures.get(key), int) and value >= 0 else 0
    if attempts < MAX_LANGUAGE_TRIES:
        try:
            result = build_translation(row, transcript, blocks, deadline, stopped, directory, translator)
        except (RuntimeError, ValueError, TypeError) as error:
            if (isinstance(error, media.Interrupted) or local_fault(error) or
                    isinstance(error, media.ToolFailure)):
                raise
            if stopped() or time.monotonic() >= deadline:
                raise media.Interrupted('视频处理已停止或时间预算用尽') from None
            attempts += 1
            failures[key] = attempts
            media.save_json(path, failures)
            if attempts < MAX_LANGUAGE_TRIES:
                raise
        else:
            if key in failures:
                failures.pop(key)
                media.save_json(path, failures)
            return result
    if stopped() or time.monotonic() >= deadline:
        raise media.Interrupted('视频处理已停止或时间预算用尽')
    return fallback(row, transcript, directory, translator, deadline, stopped)


def fallback(row, transcript, directory, translator, deadline, stopped):
    cache = media.read_json(directory / 'notes-cache.json')
    if not isinstance(cache, dict):
        cache = {}
    sections = []
    blocks = transcripts.blocks(transcript['segments'])
    for block in blocks:
        if stopped() or time.monotonic() >= deadline:
            raise media.Interrupted('视频处理已停止或时间预算用尽')
        stage = 'translate' if native_chinese(block) else translation.STAGE
        try:
            value = translated_output(block, block if native_chinese(block) else
                                      cache[signature(stage, translator, block)])
        except (KeyError, RuntimeError, ValueError, TypeError):
            for position, unit in enumerate(translation.units(block)):
                try:
                    value = translation.unit_output(block, position,
                                                    cache[signature('translate-unit-v1', translator, unit['text'])])
                except (KeyError, RuntimeError, ValueError, TypeError):
                    sections.append((False, unit['text']))
                else:
                    sections.append((True, value))
        else:
            sections.append((True, value))
    if stopped() or time.monotonic() >= deadline:
        raise media.Interrupted('视频处理已停止或时间预算用尽')
    original = '\n\n'.join(blocks)
    if not original.strip():
        raise RuntimeError('没有可保留的视频原文')
    origin = ORIGINS.get(transcript['origin'], transcript['origin'])
    if any(chinese for chinese, value in sections):
        body = ('> 自动中文整理的某个阶段连续三轮未生成可用成品，已停止该阶段自动重试。'
                '以下保留已通过校验的中文资料（原文或自动译文），不是已核验内容笔记；请结合原视频核对。\n\n')
        body += '\n\n'.join(f'### 第 {position} 段：' + ('中文资料（待核验）' if chinese else '原文（未翻译）') +
                            '\n\n' + value for position, (chinese, value) in enumerate(sections, 1))
        if all(chinese for chinese, value in sections):
            body += original_comparison(blocks)
        provenance = 'translation-fallback' if all(chinese for chinese, value in sections) else 'mixed-fallback'
        title = '整理待校验：'
    else:
        body = ('> 自动中文整理连续三轮未生成可用成品，已停止自动重试。以下仅保留完整原文，'
                     '不是中文译文或已核验笔记；请结合原视频核对。\n\n'
                '## 原文（未翻译）\n\n' + original)
        title, provenance = '原文待校验：', 'original-fallback'
    body += f'\n\n> 原文依据：{origin}；未分析视频画面。'
    if stopped() or time.monotonic() >= deadline:
        raise media.Interrupted('视频处理已停止或时间预算用尽')
    original_title = row['entry']['title']
    translated_title = cached_title(original_title, translator, cache) or original_title
    if not native_chinese(original_title):
        body += '\n\n## 视频原题\n\n' + original_title
    return {'url': row['entry']['url'], 'title': title + translated_title, 'body': body,
            'published': row['entry']['published'], 'author': row['channel'],
            'content_kind': 'markdown', 'body_provenance': 'youtube:' + provenance}


def build_translation(row, transcript, blocks, deadline, stopped, directory, translator):
    cache = media.read_json(directory / 'notes-cache.json')
    if not isinstance(cache, dict):
        cache = {}
    translated = {}
    jobs = {}
    title = row['entry']['title']
    title_key = signature(TITLE_STAGE, translator, title)
    title_value = cached_title(title, translator, cache)
    if title_value is not None:
        translated[title_key] = title_value
    else:
        jobs[title_key] = (title, {}, {})
    for block in blocks:
        if stopped() or time.monotonic() >= deadline:
            raise media.Interrupted('视频处理已停止或时间预算用尽')
        key = signature(translation.STAGE, translator, block)
        if native_chinese(block):
            translated[key] = block
            continue
        if key in cache:
            try:
                translated[key] = translated_output(block, cache[key])
                continue
            except (RuntimeError, ValueError, TypeError):
                cache.pop(key)
                media.save_json(directory / 'notes-cache.json', cache)
        seed = {identity: cache[identity] for unit in translation.units(block)
                if (identity := signature('translate-unit-v1', translator, unit['text'])) in cache}
        jobs[key] = (block, seed, dict(seed))

    def create(key, halted):
        block, _, local = jobs[key]
        if key == title_key:
            return translate_title(block, translator, deadline, halted, local)
        return cached(None, local, key,
                      lambda: translation_request(block, translator, deadline, halted, cache=local),
                      lambda output: translated_output(block, output))

    def receive(key, future):
        if stopped() or time.monotonic() >= deadline:
            raise media.Interrupted('视频翻译已停止或时间预算用尽')
        try:
            value = future.result()
        except media.Interrupted:
            raise
        except Exception:
            merge(key)
            raise
        merge(key)
        return value

    def merge(key):
        _, seed, local = jobs[key]
        changed = False
        for identity in seed.keys() - local.keys():
            if cache.get(identity) == seed[identity]:
                cache.pop(identity, None)
                changed = True
        for identity, value in local.items():
            if identity not in seed or value != seed[identity]:
                cache[identity] = value
                changed = True
        if changed:
            media.save_json(directory / 'notes-cache.json', cache)

    results = translation_pool.run(jobs, create, receive, deadline, stopped) if jobs else {}
    translated.update(results)
    if stopped() or time.monotonic() >= deadline:
        raise media.Interrupted('视频处理已停止或时间预算用尽')
    return translated_entry(row, transcript, blocks,
                            [translated[signature(translation.STAGE, translator, block)] for block in blocks],
                            translated[title_key])


def translated_entry(row, transcript, blocks, translated, title=None):
    origin = ORIGINS.get(transcript['origin'], transcript['origin'])
    complete = '\n\n'.join(translated)
    chinese_source = all(native_chinese(block) for block in blocks)
    heading = '中文字幕原文' if chinese_source else '中文译文（自动生成）'
    caution = ('以上为字幕原文，未进行摘要或内容改写。' if chinese_source else
               '译文已进行分段覆盖与语言校验，未做人工事实核查，请结合原视频核对。')
    body = ('## ' + heading + '\n\n' + complete +
            f'\n\n> 内容依据：{origin}；未分析视频画面。字幕和语音识别可能有误。'
            + caution
            + original_comparison(blocks))
    if not native_chinese(row['entry']['title']):
        body += '\n\n## 视频原题\n\n' + row['entry']['title']
    return {'url': row['entry']['url'], 'title': title or row['entry']['title'], 'body': body,
            'published': row['entry']['published'], 'author': row['channel'],
            'content_kind': 'markdown', 'body_provenance': 'youtube:' + transcript['origin']}
