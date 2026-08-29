"""LLM 客户端 - OpenAI 兼容 API 交互（嵌入、摘要、聊天）"""

import asyncio
import logging
from itertools import cycle
from typing import Literal
import time

from openai import OpenAI

from src.config import settings
from src.cache import cache
from src.tokenizer import count_tokens
from src.metrics import LLM_ROUTE_HIT_TOTAL, LLM_ROUTE_FAIL_TOTAL, LLM_ROUTE_FALLBACK_TOTAL, LLM_ROUTE_CIRCUIT_OPEN_TOTAL

logger = logging.getLogger(__name__)


async def _get_global_model_config() -> dict:
    value = await cache.get_plugin_state("global-model-config")
    return value or {}


_clients: dict[tuple[str, str], OpenAI] = {}
_route_counters: dict[str, object] = {}
_route_failures: dict[tuple[str, str], int] = {}
_route_cooldowns: dict[tuple[str, str], float] = {}


def _get_client(*, api_key: str, base_url: str) -> OpenAI:
    key = (api_key, base_url)
    client = _clients.get(key)
    if client is None:
        # 150s hard timeout per request: zhipu glm-4-flash needs ~90s for pyramid
        # extraction (edgefn dead 403, so GLM is the only live route now).
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=150.0, max_retries=1)
        _clients[key] = client
    return client


def _primary_model_config() -> dict[str, str]:
    return {
        'api_key': settings.openai_api_key,
        'base_url': settings.openai_base_url,
        'openai_model': settings.openai_model,
        'summary_model': settings.summary_model,
    }


def _backup_model_config() -> dict[str, str] | None:
    if not (settings.backup_openai_api_key and settings.backup_openai_base_url and settings.backup_openai_model):
        return None
    return {
        'api_key': settings.backup_openai_api_key,
        'base_url': settings.backup_openai_base_url,
        'openai_model': settings.backup_openai_model,
        'summary_model': settings.backup_summary_model or settings.backup_openai_model,
    }


def _kg_local_model_config() -> dict[str, str] | None:
    """Local OpenAI-compatible route reserved for KG extraction."""
    if not (settings.kg_local_openai_base_url and settings.kg_local_openai_model):
        return None
    return {
        "api_key": settings.kg_local_openai_api_key or "local-mi50",
        "base_url": settings.kg_local_openai_base_url,
        "openai_model": settings.kg_local_openai_model,
        "summary_model": settings.kg_local_openai_model,
    }


def _zhipu_model_config() -> dict[str, str] | None:
    """Independent GLM fallback reserved for KG extraction."""
    if not settings.zhipu_api_key:
        return None
    return {
        "api_key": settings.zhipu_api_key,
        "base_url": settings.zhipu_base_url,
        "openai_model": settings.zhipu_model,
        "summary_model": settings.zhipu_model,
    }


def _tertiary_model_config() -> dict[str, str] | None:
    """Tertiary model (KAT-Coder) — uses backup API key."""
    tertiary_model = getattr(settings, "tertiary_openai_model", None)
    if not tertiary_model:
        tertiary_model = "KAT-Coder-Exp-72B-1010"
    if not (settings.backup_openai_api_key and settings.backup_openai_base_url):
        return None
    return {
        "api_key": settings.backup_openai_api_key,
        "base_url": settings.backup_openai_base_url,
        "openai_model": tertiary_model,
        "summary_model": getattr(settings, "tertiary_summary_model", None) or tertiary_model,
    }
    if settings.backup_openai_api_key and settings.backup_openai_base_url:
        return {
            'api_key': settings.backup_openai_api_key,
            'base_url': settings.backup_openai_base_url,
            'openai_model': _cached_tertiary_model,
            'summary_model': getattr(settings, 'tertiary_summary_model', None) or _cached_tertiary_model,
        }
    return None


def _quaternary_model_config() -> dict[str, str] | None:
    """V4-Flash model — uses primary API key."""
    if not (settings.openai_api_key and settings.openai_base_url):
        return None
    m = getattr(settings, "quaternary_openai_model", None)
    if not m:
        m = "DeepSeek-V4-Flash"
    return {
        "api_key": settings.openai_api_key,
        "base_url": settings.openai_base_url,
        "openai_model": m,
        "summary_model": getattr(settings, "quaternary_summary_model", None) or m,
    }

def _routing_mode() -> str:
    mode = (settings.llm_routing_mode or 'fallback').strip().lower()
    return mode if mode in {'fallback', 'load_balance'} else 'fallback'


# ── Dual-Model Task-Based Routing ──────────────────────────
# Heavy tasks (reasoning, synthesis, extraction) → backup (DeepSeek-R1)
# Quick tasks (chat, simple queries) → primary (DeepSeek-V3)
_TASK_ROUTE_MAP = {
    "summary": "backup",
    "kg_extract": "primary",
    "synthesize": "backup",
    "persona": "backup",
    "atom_build": "backup",
    "scenario_build": "backup",
    "bridge": "backup",
    "entity_expansion": "backup",
    "chat": "primary",
    "query": "primary",
    "context_inject": "primary",
    "quick_reply": "primary",
}

def _select_route_for_task(task_type: str | None) -> list[dict] | None:
    """Return custom route order for a task type, or None for default."""
    if not task_type or task_type not in _TASK_ROUTE_MAP:
        return None
    primary = _primary_model_config()
    backup = _backup_model_config()
    tertiary = _tertiary_model_config()
    quaternary = _quaternary_model_config()
    kg_local = _kg_local_model_config()
    zhipu = _zhipu_model_config()
    mode = getattr(settings, 'llm_routing_mode', 'fallback')
    preference = _TASK_ROUTE_MAP[task_type]
    # Keep KG extraction isolated from normal chat routing.
    # Prefer the local MI50 vLLM route, then GLM, then legacy edgefn fallbacks.
    if task_type == "kg_extract" and (kg_local or zhipu):
        routes = []
        for route in (kg_local, zhipu, tertiary, backup, primary):
            if route and route not in routes:
                routes.append(route)
        return routes
    if mode == 'load_balance':
        # load_balance: round-robin across all 3 models
        routes = [primary]
        if backup:
            routes.append(backup)
        if tertiary:
            routes.append(tertiary)
        if quaternary:
            routes.append(quaternary)
    elif preference == "backup" and backup:
        routes = [backup]
        if tertiary:
            routes.append(tertiary)
        routes.append(primary)
    elif tertiary:
        routes = [primary, tertiary]
    else:
        routes = [primary]

    # Dedup, then append the verified Zhipu/GLM route as a live fallback so that
    # chat / persona / atom / scenario tasks (pyramid worker) still work even
    # though the edgefn routes are all 403 (open circuit). MI50 (kg_local) is
    # intentionally NOT appended here — it is reserved for KG extraction only.
    deduped: list[dict] = []
    # Pyramid tasks (persona/atom/scenario/bridge/entity/synthesize): prefer the
    # verified zhipu route FIRST since all edgefn keys are dead (403 InvalidToken).
    if zhipu and task_type in ("persona", "atom_build", "scenario_build", "bridge", "entity_expansion", "synthesize"):
        deduped.append(zhipu)
    for route in routes:
        if route and route not in deduped:
            deduped.append(route)
    if zhipu and zhipu not in deduped:
        deduped.append(zhipu)
    return deduped


# ── Convenience functions ──────────────────────────────────

async def chat_completion_for_extraction(
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int = 3000,
) -> tuple[str, int]:
    """KG extraction — uses DeepSeek-R1 for better entity/relation recognition."""
    return await chat_completion(
        messages, temperature=temperature, max_tokens=max_tokens,
        task_type="kg_extract",
    )


async def chat_completion_quick(
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> tuple[str, int]:
    """Quick chat — uses DeepSeek-V3 for speed."""
    return await chat_completion(
        messages, temperature=temperature, max_tokens=max_tokens,
        task_type="chat",
    )



def _route_key(route: dict[str, str]) -> tuple[str, str]:
    return (route.get('api_key', ''), route.get('base_url', ''))


def _route_name(route: dict[str, str], *, operation: str) -> str:
    model_field = 'summary_model' if operation == 'summary' else 'openai_model'
    return route.get(model_field) or route.get('openai_model') or route.get('base_url') or 'unknown'


def _route_available(route: dict[str, str], *, operation: str) -> bool:
    key = _route_key(route)
    cooldown_until = _route_cooldowns.get(key, 0.0)
    if cooldown_until > time.time():
        LLM_ROUTE_CIRCUIT_OPEN_TOTAL.labels(operation=operation, route=_route_name(route, operation=operation)).inc()
        return False
    return True


async def _record_route_failure_async(route: dict[str, str], *, operation: str) -> None:
    key = _route_key(route)
    name = _route_name(route, operation=operation)
    failures = _route_failures.get(key, 0) + 1
    _route_failures[key] = failures
    LLM_ROUTE_FAIL_TOTAL.labels(operation=operation, route=name).inc()
    await cache.incr_llm_route_metric('fail', operation, name)
    await cache.incr_llm_route_window_metric('fail', operation, name)
    if failures >= settings.llm_route_failure_threshold:
        _route_cooldowns[key] = time.time() + settings.llm_route_cooldown_seconds
        _route_failures[key] = 0
        logger.warning('open circuit for %s route=%s cooldown=%ss', operation, name, settings.llm_route_cooldown_seconds)


async def _record_route_success_async(route: dict[str, str], *, operation: str) -> None:
    key = _route_key(route)
    _route_failures[key] = 0
    _route_cooldowns.pop(key, None)
    LLM_ROUTE_HIT_TOTAL.labels(operation=operation, route=_route_name(route, operation=operation)).inc()
    await cache.incr_llm_route_metric('hit', operation, _route_name(route, operation=operation))
    await cache.incr_llm_route_window_metric('hit', operation, _route_name(route, operation=operation))


def _route_text_configs(kind: Literal['chat', 'summary']) -> list[dict[str, str]]:
    primary = _primary_model_config()
    backup = _backup_model_config()
    tertiary = _tertiary_model_config()
    quaternary = _quaternary_model_config()
    if not backup:
        return [primary]
    if _routing_mode() == 'load_balance':
        configs = [primary]
        if backup:
            configs.append(backup)
        if tertiary:
            configs.append(tertiary)
        if quaternary:
            configs.append(quaternary)
        n = len(configs)
        counter = _route_counters.get(kind)
        if counter is None:
            counter = cycle(range(n))
            _route_counters[kind] = counter
        first_idx = next(counter)
        route = [configs[first_idx]]
        for i in range(1, n):
            route.append(configs[(first_idx + i) % n])
        return route
    return [primary, backup]


def _sync_create_embedding(text: str) -> list[float]:
    conf = _primary_model_config()
    client = _get_client(api_key=conf['api_key'], base_url=conf['base_url'])
    response = client.embeddings.create(
        model=settings.embedding_model,
        input=text,
        dimensions=settings.embedding_dimensions,
    )
    return response.data[0].embedding


def _sync_create_embeddings_batch(texts: list[str]) -> list[list[float]]:
    conf = _primary_model_config()
    client = _get_client(api_key=conf['api_key'], base_url=conf['base_url'])
    response = client.embeddings.create(
        model=settings.embedding_model,
        input=texts,
        dimensions=settings.embedding_dimensions,
    )
    return [item.embedding for item in response.data]


def _sync_generate_summary(messages_text: str, model_name: str, *, api_key: str, base_url: str) -> str:
    client = _get_client(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是一个对话摘要助手。请将以下对话内容压缩为简洁的摘要，"
                    "保留关键信息、决策、代码片段要点和用户偏好。"
                    "摘要应该足够详细，使后续对话能理解上下文。"
                    "输出纯文本摘要，不要加标题或格式标记。"
                ),
            },
            {
                "role": "user",
                "content": f"请摘要以下对话：\n\n{messages_text}",
            },
        ],
        temperature=1,
        top_p=0.95,
        max_tokens=8192,
        stream=False,
    )
    return response.choices[0].message.content or ""


def _sync_chat_completion(
    messages: list[dict[str, str]],
    model_name: str,
    temperature: float = 0.7,
    max_tokens: int = 2000,
    *,
    api_key: str,
    base_url: str,
) -> tuple[str, int]:
    client = _get_client(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        top_p=0.95,
        max_tokens=max_tokens,
        stream=False,
    )
    content = response.choices[0].message.content or ""
    total_tokens = response.usage.total_tokens if response.usage else 0
    return content, total_tokens


async def create_embedding(text: str) -> list[float]:
    return await asyncio.to_thread(_sync_create_embedding, text)


async def create_embeddings_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return await asyncio.to_thread(_sync_create_embeddings_batch, texts)


async def generate_summary(messages_text: str, task_type: str | None = None) -> str:
    conf = await _get_global_model_config()
    requested_model = conf.get("summary_model", settings.summary_model)
    errors: list[str] = []
    primary = _primary_model_config()
    attempted = 0

    # Task-based routing: summary → R1, quick → V3
    if task_type is None:
        task_type = "summary"
    route_order = _select_route_for_task(task_type) or _route_text_configs('summary')

    for route in route_order:
        if not _route_available(route, operation='summary'):
            continue
        attempted += 1
        is_primary = route['api_key'] == primary['api_key'] and route['base_url'] == primary['base_url']
        model_name = requested_model if is_primary else route.get('summary_model') or route.get('openai_model')
        try:
            result = await asyncio.to_thread(
                _sync_generate_summary,
                messages_text,
                model_name,
                api_key=route['api_key'],
                base_url=route['base_url'],
            )
            await _record_route_success_async(route, operation='summary')
            if attempted > 1:
                LLM_ROUTE_FALLBACK_TOTAL.labels(operation='summary').inc()
                await cache.incr_llm_route_metric('fallback', 'summary', _route_name(route, operation='summary'))
                await cache.incr_llm_route_window_metric('fallback', 'summary', _route_name(route, operation='summary'))
            return result
        except Exception as exc:
            errors.append(str(exc))
            await _record_route_failure_async(route, operation='summary')
            logger.warning('summary model route failed (%s @ %s): %s', model_name, route['base_url'], exc)
    if attempted == 0:
        raise RuntimeError('no summary routes available due to open circuit')
    raise RuntimeError('all summary model routes failed: ' + ' | '.join(errors))


async def chat_completion(
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 2000,
    task_type: str | None = None,
) -> tuple[str, int]:
    conf = await _get_global_model_config()
    requested_model = conf.get("openai_model", settings.openai_model)
    errors: list[str] = []
    primary = _primary_model_config()

    # Task-based routing
    route_override = _select_route_for_task(task_type)
    if route_override:
        routes = list(route_override)
    else:
        routes = list(_route_text_configs('chat'))
    # Prefer the local MI50 vLLM route for SHORT inputs only (it has an 8192-token
    # context). Long inputs (pyramid atom/persona material, 8000+ tokens) must go
    # to Zhipu/GLM (128k context) — otherwise MI50 returns 400 and we waste a
    # round-trip before falling back anyway.
    kg_local = _kg_local_model_config()
    zhipu = _zhipu_model_config()
    rough_input = sum(count_tokens(str(m.get("content", ""))) for m in messages)
    preferred = []
    # Pyramid tasks (atom_build / scenario_build / persona / bridge / entity /
    # synthesize) ALWAYS go to the FREE local MI50 llama.cpp route (:8081,
    # clamped n_ctx=2048) FIRST. atom_builder.py truncates their inputs to 1700
    # tokens, so they always fit :8081 and never need the overloaded/timeout-prone
    # GLM route. This is the core fix for the auto-persona watchdog failures:
    # previously every pyramid call hit GLM (which times out 150s under the
    # concurrent cron load) or fell back to dead edgefn keys -> 0 atoms extracted.
    _local_first_tasks = {"atom_build", "scenario_build", "persona",
                          "bridge", "entity_expansion", "synthesize"}
    # Pyramid tasks need RELIABLE JSON output. MI50 (:8081) is free but
    # intermittently returns truncated/malformed JSON (no exception raised),
    # which then fails JSON parsing in the caller and yields 0 atoms with NO
    # fallback. Zhipu/GLM is the reliable-JSON route, so it goes FIRST; MI50
    # remains a free fallback only.
    if task_type in _local_first_tasks:
        if zhipu:
            preferred.append(zhipu)
        if kg_local:
            preferred.append(kg_local)
    else:
        if kg_local and (rough_input + max_tokens) <= 1900:
            preferred.append(kg_local)
        if zhipu:
            preferred.append(zhipu)
    routes = preferred + [r for r in routes if r not in preferred]
    attempted = 0
    for route in routes:
        if not _route_available(route, operation='chat'):
            continue
        attempted += 1
        model_name = requested_model if route['base_url'] == primary['base_url'] and route['api_key'] == primary['api_key'] else route.get('openai_model')
        try:
            result = await asyncio.to_thread(
                _sync_chat_completion,
                messages,
                model_name,
                temperature,
                max_tokens,
                api_key=route['api_key'],
                base_url=route['base_url'],
            )
            await _record_route_success_async(route, operation='chat')
            if attempted > 1:
                LLM_ROUTE_FALLBACK_TOTAL.labels(operation='chat').inc()
                await cache.incr_llm_route_metric('fallback', 'chat', _route_name(route, operation='chat'))
                await cache.incr_llm_route_window_metric('fallback', 'chat', _route_name(route, operation='chat'))
            return result
        except Exception as exc:
            errors.append(str(exc))
            await _record_route_failure_async(route, operation='chat')
            logger.warning('chat model route failed (%s @ %s): %s', model_name, route['base_url'], exc)
    if attempted == 0:
        raise RuntimeError('no chat routes available due to open circuit')
    raise RuntimeError('all chat model routes failed: ' + ' | '.join(errors))
