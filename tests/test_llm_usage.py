import asyncio
import json

import httpx
import pytest
from loguru import logger

from app.llm.client import LlmClientError, request_llm_decision
from app.llm.config import LlmServerConfig


@pytest.fixture
def usage_records():
    records = []
    sink = logger.add(
        lambda message: records.append(message.record['extra']),
        filter=lambda record: record['extra'].get('llm_event') == 'usage',
    )
    try:
        yield records
    finally:
        logger.remove(sink)


def _stream(*events: dict) -> httpx.Response:
    body = ''.join(f'data: {json.dumps(event)}\n\n' for event in events)
    return httpx.Response(200, text=body + 'data: [DONE]\n\n',
                          headers={'content-type': 'text/event-stream'})


async def test_qwen_tail_usage_is_recorded_for_each_attempt(monkeypatch, usage_records):
    payloads = []

    async def handler(request):
        payloads.append(json.loads(request.content))
        is_retry = len(payloads) == 2
        choice = 'A1' if is_retry else 'bad-id'
        return _stream(
            {'choices': [{'delta': {'content': json.dumps({'choice': choice})},
                          'finish_reason': 'stop'}], 'usage': None},
            {'choices': [], 'usage': {
                'prompt_tokens': 1400, 'completion_tokens': 8,
                'prompt_tokens_details': {
                    'cached_tokens': 1024 if is_retry else 0,
                    'cache_creation_input_tokens': 0 if is_retry else 1024,
                },
            }},
        )

    cfg = LlmServerConfig(base_url='https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                          api_key='test', model='qwen3.8-max-0902', provider_id='qwen')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr('app.llm.client.get_llm_client', lambda: client)
        assert await request_llm_decision(cfg, 'system', 'user', ['A1']) == ('A1', '')

    assert all(p['stream_options'] == {'include_usage': True} for p in payloads)
    assert payloads[0]['messages'] == [
        {'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'user'},
    ]
    assert [r['llm_attempt'] for r in usage_records] == [1, 2]
    assert [r['llm_cached_tokens'] for r in usage_records] == [0, 1024]
    assert [r['llm_uncached_tokens'] for r in usage_records] == [1400, 376]
    assert [r['llm_cache_write_tokens'] for r in usage_records] == [1024, 0]
    assert all(r['llm_usage_available'] for r in usage_records)


async def test_deepseek_truncated_response_keeps_usage(monkeypatch, usage_records):
    async def handler(request):
        assert 'stream_options' not in json.loads(request.content)
        return _stream({'choices': [{'delta': {}, 'finish_reason': 'length'}], 'usage': {
            'prompt_tokens': 350, 'prompt_cache_hit_tokens': 64, 'prompt_cache_miss_tokens': 286,
            'completion_tokens': 100, 'completion_tokens_details': {'reasoning_tokens': 90},
        }})

    cfg = LlmServerConfig(base_url='https://api.deepseek.com/v1', api_key='test',
                          model='deepseek-v4-flash', provider_id='deepseek')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr('app.llm.client.get_llm_client', lambda: client)
        with pytest.raises(LlmClientError) as error:
            await request_llm_decision(cfg, 'system', 'user', ['A1'])

    assert error.value.kind == LlmClientError.KIND_LENGTH
    record, = usage_records
    assert record['llm_finish_reason'] == 'length'
    assert record['llm_prompt_tokens'] == 350
    assert record['llm_cached_tokens'] == 64
    assert record['llm_uncached_tokens'] == 286
    assert record['llm_completion_tokens'] == 100
    assert record['llm_reasoning_tokens'] == 90


@pytest.mark.parametrize(('reasoning', 'effort', 'mode'), [
    (False, 'low', 'always-on'),
    (True, 'medium', 'explicit-on'),
])
async def test_official_gpt_sol_records_stream_usage(monkeypatch, usage_records, reasoning, effort, mode):
    async def handler(request):
        assert str(request.url) == 'https://api.openai.com/v1/chat/completions'
        payload = json.loads(request.content)
        assert payload['reasoning_effort'] == effort
        assert payload['stream_options'] == {'include_usage': True}
        assert 'temperature' not in payload and 'top_p' not in payload
        assert 'max_tokens' not in payload
        assert ('max_completion_tokens' in payload) is reasoning
        return _stream(
            {'choices': [{'delta': {'content': '{"choice":"A1"}'}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {
                'prompt_tokens': 1500, 'completion_tokens': 41,
                'prompt_tokens_details': {'cached_tokens': 1024, 'cache_write_tokens': 320},
                'completion_tokens_details': {'reasoning_tokens': 32},
            }},
        )

    cfg = LlmServerConfig(base_url='https://api.openai.com/v1', api_key='test',
                          model='gpt-6.1-sol', provider_id='relay_gpt')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr('app.llm.client.get_llm_client', lambda: client)
        assert await request_llm_decision(cfg, 'system', 'user', ['A1'], reasoning=reasoning) == ('A1', '')

    record, = usage_records
    assert record['llm_reasoning_mode'] == mode
    assert record['llm_usage_available']
    assert record['llm_cached_tokens'] == 1024
    assert record['llm_cache_write_tokens'] == 320
    assert record['llm_uncached_tokens'] == 476
    assert record['llm_reasoning_tokens'] == 32


async def test_cancelled_http_attempt_records_missing_usage(monkeypatch, usage_records):
    entered = asyncio.Event()

    async def handler(_request):
        entered.set()
        await asyncio.Future()

    cfg = LlmServerConfig(base_url='https://api.deepseek.com/v1', api_key='test',
                          model='deepseek-v4-flash', provider_id='deepseek')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr('app.llm.client.get_llm_client', lambda: client)
        task = asyncio.create_task(request_llm_decision(cfg, 'system', 'user', ['A1']))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    record, = usage_records
    assert record['llm_usage_available'] is False
    assert record['llm_prompt_tokens'] is None
    assert record['llm_cached_tokens'] is None
