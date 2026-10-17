"""D113: the OpenAI-compatible writer of beats files (the local model behind `--storyboard-llm openai_compat`).

The writing task text is `WRITER_TASK` plus the `beats-guide` text; the subagent path reads the same guide (its own
prompt, verify/v2/beats/WRITER_PROMPT.md, adds tool steps that do not apply here). One call per file: no compile loop.
Health is checked first; a model that is not `ready` raises, nothing falls back to the subagent path.
"""
import json
import re
import time
import httpx

WRITER_TASK = ('你的任务只有一件：为下面的剧情写一份故事分镜（beats）JSON 文件。认真讲好这个故事：每一镜画面里要能看出剧情的关键信息'
               '（地点、天气和时间、服装变化、关键动物或物件、情绪变化）。只输出这个 JSON 对象，不要任何解释。')


class LlmError(RuntimeError):
    """kind: http | not_json | health"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def settings(config):
    block = dict((config or {}).get('llm') or {})
    if not block.get('base_url') or not block.get('model'):
        raise LlmError('health', 'config has no llm.base_url / llm.model')
    return block


def check_health(cfg, client=None):
    own = client is None
    client = client or httpx.Client(timeout=30, trust_env=False)
    try:
        rows = client.get(cfg['base_url'].rstrip('/') + '/models').raise_for_status().json().get('data', [])
    except (httpx.HTTPError, ValueError) as exc:
        raise LlmError('health', f'cannot reach {cfg["base_url"]}/models: {exc}') from exc
    finally:
        if own:
            client.close()
    row = next((r for r in rows if r.get('id') == cfg['model']), None)
    if row is None or row.get('state') != 'ready':
        raise LlmError('health', f'model {cfg["model"]!r} is not ready: {None if row is None else row.get("state")}')
    return row


def strip_reasoning(text):
    return re.sub(r'<think>.*?</think>', '', text or '', flags=re.S).strip()


def build_messages(guide, character, story, shot_count):
    return [{'role': 'system', 'content': WRITER_TASK + '\n\n' + guide},
            {'role': 'user', 'content': f'角色：{character}\n剧情：{story}\n镜数：{shot_count}'}]


def chat(cfg, messages, *, schema=None, thinking=None, max_tokens=None, retries=1, client=None):
    """One chat completion. Returns (text, record); record has seconds, usage, json_schema, thinking, attempts.
    Retries `retries` times on an HTTP error or timeout (each attempt is counted in record['attempts'])."""
    thinking = cfg.get('enable_thinking', False) if thinking is None else thinking
    body = {'model': cfg['model'], 'messages': messages, 'temperature': cfg.get('temperature', 0.7),
            'max_tokens': max_tokens or (8192 if thinking else cfg.get('max_tokens', 4096))}
    if schema is not None:
        body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'beats_v3', 'strict': True, 'schema': schema}}
    body['chat_template_kwargs'] = {'enable_thinking': bool(thinking)}  # the server's own default is off; always say it
    own = client is None
    client = client or httpx.Client(timeout=cfg.get('timeout_s', 180), trust_env=False,
                                    headers={'Authorization': 'Bearer ' + str(cfg.get('api_key') or 'x')})
    started, last = time.time(), None
    try:
        for attempt in range(1, retries + 2):
            try:
                resp = client.post(cfg['base_url'].rstrip('/') + '/chat/completions', json=body)
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
        else:
            raise LlmError('http', f'{type(last).__name__}: {last}')
    finally:
        if own:
            client.close()
    msg = data['choices'][0]['message']
    raw = msg.get('content') or ''
    record = {'seconds': round(time.time() - started, 1), 'usage': data.get('usage'), 'attempts': attempt,
              'json_schema': schema is not None, 'thinking': thinking, 'finish_reason': data['choices'][0].get('finish_reason'),
              'reasoning_chars': len(msg.get('reasoning_content') or '') + (len(raw) - len(strip_reasoning(raw)))}
    return strip_reasoning(raw), record


def write_beats(config, guide, character, story, shot_count, *, use_schema=True, thinking=None):
    from storycg.beats_v3 import beats_v3_schema
    cfg = settings(config)
    check_health(cfg)
    text, record = chat(cfg, build_messages(guide, character, story, shot_count),
                        schema=beats_v3_schema(shot_count) if use_schema else None, thinking=thinking)
    return text, record
