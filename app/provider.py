"""DeepSeek SSE client: usage and wall-clock measurements, no SDK or hidden retries."""
import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal
import httpx
from .domain import DomainError
from .model_config import ModelConfig


class ProviderError(DomainError):
    def __init__(self, code, message, metrics=None):
        super().__init__(code, message, 503)
        self.metrics = metrics or {}


def usage_cost(usage, config):
    if not isinstance(usage,dict) or type(usage.get('prompt_tokens')) is not int or type(usage.get('completion_tokens')) is not int:
        return None
    if usage['prompt_tokens'] < 0 or usage['completion_tokens'] < 0:
        return None
    return str((Decimal(usage['prompt_tokens']) * config.input_price + Decimal(usage['completion_tokens']) * config.output_price) / Decimal(1_000_000))


@dataclass
class Completion:
    content: str
    metrics: dict


class DeepSeekProvider:
    def __init__(self, config: ModelConfig, transport=None):
        self.config, self.transport = config, transport

    async def complete(self, messages, deadline_seconds=None):
        cfg = self.config
        if not cfg.api_key:
            raise ProviderError('NOT_CONFIGURED', '请在本地 .env 配置 DEEPSEEK_API_KEY', dict(llm_calls=0))
        limit = deadline_seconds or cfg.deadline_seconds
        start = time.perf_counter()
        metrics = dict(provider=cfg.provider, requested_model=cfg.model, resolved_model=None,
                       execution_mode='live_model', llm_calls=0, provider_ttft_ms=None, provider_total_ms=None,
                       usage=None, input_tokens=None, output_tokens=None, cost=None, cost_currency='CNY',
                       pricing_kind='list_price_estimate_uncached_upper', usage_complete=False, request_id=None,
                       retry_count=0, thinking='disabled')
        payload = dict(model=cfg.model, messages=messages, thinking={'type':'disabled'},
                       response_format={'type':'json_object'}, max_tokens=cfg.max_output_tokens,
                       stream=True, stream_options={'include_usage':True})
        content, finish, done = [], None, False
        # asyncio.wait_for enforces total time; socket timeout alone only bounds idle reads.
        async def request():
            nonlocal finish, done
            async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(limit), follow_redirects=False, trust_env=False) as client:
                metrics['llm_calls'] = 1
                async with client.stream('POST', cfg.base_url+'/chat/completions', headers={'Authorization':'Bearer '+cfg.api_key}, json=payload) as response:
                    metrics['request_id'] = response.headers.get('x-request-id')
                    if response.status_code != 200:
                        codes = {401:'PROVIDER_AUTH',402:'PROVIDER_BALANCE',403:'PROVIDER_FORBIDDEN',429:'PROVIDER_RATE_LIMIT'}
                        metrics['http_status'] = response.status_code
                        raise ProviderError(codes.get(response.status_code, 'PROVIDER_HTTP_ERROR'), '模型服务返回 HTTP '+str(response.status_code))
                    size = 0
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        value = line[5:].strip()
                        if not value:
                            continue
                        if value == '[DONE]':
                            done = True
                            break
                        chunk = json.loads(value)
                        metrics['resolved_model'] = chunk.get('model') or metrics['resolved_model']
                        metrics['completion_id'] = chunk.get('id') or metrics.get('completion_id')
                        if chunk.get('usage'):
                            metrics['usage'] = chunk['usage']
                        for choice in chunk.get('choices', []):
                            delta = choice.get('delta', {})
                            # Do not collect or persist reasoning_content.
                            piece = delta.get('content')
                            if piece:
                                if metrics['provider_ttft_ms'] is None:
                                    metrics['provider_ttft_ms'] = round((time.perf_counter()-start)*1000,3)
                                size += len(piece)
                                if size > 30000:
                                    raise ProviderError('PROVIDER_OUTPUT_LIMIT', '模型输出超过本地限额')
                                content.append(piece)
                            finish = choice.get('finish_reason') or finish
            if not done or finish != 'stop':
                raise ProviderError('PROVIDER_INCOMPLETE', '模型返回未完整结束（截断或拒绝）')
            if not content:
                raise ProviderError('PROVIDER_EMPTY', '模型返回空内容')
        error = None
        try:
            await asyncio.wait_for(request(), timeout=limit)
        except ProviderError as exc:
            error = exc
        except (asyncio.TimeoutError, httpx.TimeoutException):
            error = ProviderError('PROVIDER_TIMEOUT', '模型请求超过总截止时间')
        except httpx.RequestError:
            error = ProviderError('PROVIDER_NETWORK', '模型网络请求失败')
        except (ValueError, KeyError, TypeError, AttributeError):
            error = ProviderError('PROVIDER_INVALID_STREAM', '模型流式响应不可解析')
        metrics['provider_total_ms'] = round((time.perf_counter()-start)*1000,3)
        metrics['cost'] = usage_cost(metrics['usage'], cfg)
        metrics['usage_complete'] = metrics['cost'] is not None
        if metrics['usage_complete']:
            metrics['input_tokens'] = metrics['usage']['prompt_tokens']
            metrics['output_tokens'] = metrics['usage']['completion_tokens']
        if error:
            error.metrics = metrics
            raise error
        return Completion(''.join(content), metrics)
