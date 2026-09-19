import pytest

from app.llm.reasoning import (
    infer_provider_dialect, infer_provider_type, resolve_reasoning_policy,
)


@pytest.mark.parametrize(('provider_type', 'model', 'expected'), [
    ('deepseek', 'deepseek-v4-flash', {'thinking': {'type': 'disabled'}}),
    ('qwen', 'qwen3.7-plus', {'enable_thinking': False}),
    ('kimi', 'kimi-k2.6', {'thinking': {'type': 'disabled'}, 'temperature': 0.6, 'top_p': 0.95}),
    ('doubao', 'doubao-1.5-thinking-pro', {'thinking': {'type': 'disabled'}}),
    ('openai', 'gpt-5.6', {'reasoning_effort': 'none'}),
    ('glm', 'glm-4.7-flash', {'thinking': {'type': 'disabled'}}),
])
def test_switchable_models_force_non_reasoning(provider_type, model, expected):
    result = resolve_reasoning_policy(provider_type, 'https://proxy.example.com/v1', model)
    assert result.mode == 'explicit-off'
    assert result.request_body == expected


@pytest.mark.parametrize(('provider_type', 'model'), [
    ('deepseek', 'deepseek-reasoner'), ('qwen', 'qwq-plus'),
    ('kimi', 'kimi-k2-thinking'), ('minimax', 'MiniMax-M2.7'),
    ('openai', 'o3-mini'), ('glm', 'glm-4.1v-thinking-flash'),
])
def test_reasoning_only_models_are_identified_without_request_precheck(provider_type, model):
    result = resolve_reasoning_policy(provider_type, 'https://proxy.example.com/v1', model)
    assert result.mode == 'reasoning-only'


def test_glm_5_3_flash_uses_official_and_orcarouter_dialects():
    official = resolve_reasoning_policy(
        'glm', 'https://open.bigmodel.cn/api/paas/v4', 'glm-5.3-flash', reasoning=True)
    orca = resolve_reasoning_policy(
        'custom', 'https://api.orcarouter.ai/v1', 'z-ai/glm-5.3-flash', reasoning=True)
    relay = resolve_reasoning_policy(
        'custom', 'https://proxy.example.com/v1', 'z-ai/glm-5.3-flash', reasoning=True)
    assert official.mode == orca.mode == relay.mode == 'always-on'
    assert official.request_body == {'reasoning_effort': 'low'}
    assert orca.request_body == {'reasoning_effort': 'medium'}
    assert relay.request_body == {'reasoning_effort': 'low'}


def test_full_glm_5_3_official_uses_high_and_orcarouter_uses_medium():
    official = resolve_reasoning_policy(
        'glm', 'https://open.bigmodel.cn/api/paas/v4', 'glm-5.3', reasoning=True)
    orca = resolve_reasoning_policy(
        'glm', 'https://api.orcarouter.ai/v1', 'z-ai/glm-5.3', reasoning=True)
    assert official.mode == orca.mode == 'always-on'
    assert official.request_body == {'reasoning_effort': 'high'}
    assert orca.request_body == {'reasoning_effort': 'medium'}


def test_provider_dialect_inference():
    assert infer_provider_dialect('https://open.bigmodel.cn/api/paas/v4') == 'official'
    assert infer_provider_dialect('https://api.orcarouter.ai/v1') == 'orcarouter'
    assert infer_provider_dialect('https://proxy.example.com/v1') == 'compatible'


def test_kimi_k3_qualified_model_uses_effort_without_sampling_parameters():
    result = resolve_reasoning_policy(
        'kimi', 'https://api.orcarouter.ai/v1', 'kimi/kimi-k3')
    assert result.provider_type == 'kimi'
    assert result.mode == 'always-on'
    assert result.request_body == {'reasoning_effort': 'low'}
    assert resolve_reasoning_policy(
        'kimi', 'https://api.orcarouter.ai/v1', 'kimi/kimi-k3',
        reasoning=True).request_body == {'reasoning_effort': 'high'}


@pytest.mark.parametrize('model', ['kimi/kimi-k2.5', 'kimi/kimi-k2.6'])
def test_kimi_k2_switchable_qualified_models_keep_non_reasoning_parameters(model):
    result = resolve_reasoning_policy(
        'kimi', 'https://api.orcarouter.ai/v1', model)
    assert result.provider_type == 'kimi'
    assert result.mode == 'explicit-off'
    assert result.accept_reasoning_response
    assert result.request_body == {
        'thinking': {'type': 'disabled'}, 'temperature': 0.6, 'top_p': 0.95}
    enabled = resolve_reasoning_policy(
        'kimi', 'https://api.orcarouter.ai/v1', model, reasoning=True)
    assert enabled.mode == 'explicit-on'
    assert enabled.request_body == {
        'thinking': {'type': 'enabled'}, 'temperature': 1.0, 'top_p': 0.95}


def test_claude_sonnet_5_disables_default_thinking_then_enables_medium_adaptive():
    quick = resolve_reasoning_policy(
        'custom', 'https://api.orcarouter.ai/v1', 'anthropic/claude-sonnet-5')
    deep = resolve_reasoning_policy(
        'custom', 'https://api.orcarouter.ai/v1', 'anthropic/claude-sonnet-5',
        reasoning=True)
    assert quick.request_body == {'thinking': {'type': 'disabled'}}
    assert deep.request_body == {
        'thinking': {'type': 'adaptive', 'display': 'summarized'},
        'output_config': {'effort': 'medium'},
    }


def test_kimi_k2_base_alias_remains_naturally_non_reasoning():
    result = resolve_reasoning_policy(
        'kimi', 'https://proxy.example.com/v1', 'kimi/kimi-k2')
    assert result.mode == 'naturally-off'
    assert result.request_body == {}


@pytest.mark.parametrize(('provider_type', 'model', 'expected'), [
    ('deepseek', 'deepseek-v4-flash', {
        'thinking': {'type': 'enabled'}, 'reasoning_effort': 'medium'}),
    ('qwen', 'qwen3.8-flash', {'enable_thinking': True}),
    ('openai', 'gpt-5.6-sol', {'reasoning_effort': 'medium'}),
])
def test_conditional_reasoning_explicitly_enables_supported_models(
        provider_type, model, expected):
    result = resolve_reasoning_policy(
        provider_type, 'https://proxy.example.com/v1', model, reasoning=True)
    assert result.mode == 'explicit-on'
    assert result.request_body == expected


@pytest.mark.parametrize('model', [
    'qwen3-32b', 'qwen3-235b-a22b', 'qwen3-30b-a3b', 'qwen3-14b', 'qwen3-8b', 'qwen3-0.6b',
    'qwen3.8-27b', 'qwen3.7-plus', 'qwen3.7-max', 'qwen3.6-35b-a3b', 'qwen3.5-flash',
    'qwen3-max', 'qwen3-max-preview', 'qwen3.7-max-preview', 'qwen-max', 'qwen-plus',
    'qwen-flash', 'qwen-turbo', 'qwen-plus-2025-04-28',
])
def test_thinking_by_default_qwen_models_force_enable_thinking_false(model):
    """型号名漏识别时不下发 enable_thinking=false，正文会全空（2026-09-16 实测 qwen3-32b）。

    qwen3.x 商业版（qwen3-max 等）文档上默认关闭，但混合开关同样归零风险，一并显式下发。
    """
    result = resolve_reasoning_policy(
        'qwen', 'https://dashscope.aliyuncs.com/compatible-mode/v1', model)
    assert result.mode == 'explicit-off'
    assert result.request_body == {'enable_thinking': False}


@pytest.mark.parametrize('model', ['qwen3-32b', 'qwen3-235b-a22b', 'qwen3.8-27b', 'qwen-max', 'qwen3-max'])
def test_qwen_open_source_and_commercial_families_can_still_enable_thinking(model):
    result = resolve_reasoning_policy(
        'qwen', 'https://dashscope.aliyuncs.com/compatible-mode/v1', model, reasoning=True)
    assert result.mode == 'explicit-on'
    assert result.request_body == {'enable_thinking': True}


@pytest.mark.parametrize('model', [
    'qwen3-coder-plus', 'qwen3-coder-480b-a35b', 'qwen3-vl-plus', 'qwen2.5-vl-72b',
    'qwen3-omni-flash',
])
def test_non_thinking_qwen_models_keep_plain_request(model):
    result = resolve_reasoning_policy('qwen', 'https://proxy.example.com/v1', model)
    assert result.mode == 'naturally-off'
    assert result.request_body == {}


@pytest.mark.parametrize('model', [
    'qwen3.8-2.4t-a95b', 'qwen3-235b-a22b-thinking-2507',
    'qwen3-next-80b-a3b-thinking', 'qwq-plus',
])
def test_thinking_only_qwen_models_are_identified(model):
    result = resolve_reasoning_policy('qwen', 'https://proxy.example.com/v1', model)
    assert result.mode == 'reasoning-only'
    assert result.request_body == {}


def test_legacy_provider_type_inference():
    assert infer_provider_type('https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3.7-plus') == 'qwen'
    assert infer_provider_type('https://proxy.local/v1', 'kimi-k2.6') == 'kimi'
    assert infer_provider_type('https://api.example.com/v1', 'mystery-model') == 'custom'
    # 能力矩阵外的千问老型号保持未知，不误报为可切换。
    assert resolve_reasoning_policy(
        'qwen', 'https://proxy.local/v1', 'qwen-long').mode == 'unknown'
