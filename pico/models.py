"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import time
from http.client import IncompleteRead, RemoteDisconnected
import urllib.error
import urllib.request

OPENAI_COMPATIBLE_USER_AGENT = "pico/0.1"


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if base.endswith("/responses"):
        return base
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _normalize_chat_completions_base_url(base_url):
    base = str(base_url).rstrip("/")
    return base


def _responses_endpoint_url(base_url):
    base = str(base_url).rstrip("/")
    if base.endswith("/responses"):
        return base
    return base + "/responses"


def _chat_completions_endpoint_url(base_url):
    base = str(base_url).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _iter_sse_data(response):
    """Yield each data payload from a Server-Sent Events response."""
    data_lines = []

    def flush_data():
        nonlocal data_lines
        if not data_lines:
            return None
        payload = "\n".join(data_lines)
        data_lines = []
        return payload

    while True:
        raw_line = response.readline()
        if not raw_line:
            payload = flush_data()
            if payload is not None:
                yield payload
            return
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            payload = flush_data()
            if payload is not None:
                yield payload
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.lstrip(" ")
        if field == "data":
            data_lines.append(value)


def _append_stream_text(text, parts, on_text_delta):
    if not isinstance(text, str) or not text:
        return
    parts.append(text)
    if on_text_delta is not None:
        on_text_delta(text)


def _stream_openai_chat_text(response, on_text_delta=None):
    """Read Chat Completions SSE chunks and return the accumulated text."""
    parts = []
    usage = {}

    for payload in _iter_sse_data(response):
        if not payload or payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI-compatible error: malformed event stream payload: {payload}") from exc
        if event.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {event['error']}")

        if isinstance(event.get("usage"), dict):
            usage = event["usage"]

        choices = event.get("choices") or []
        if not choices:
            continue

        delta = choices[0].get("delta") or {}
        if isinstance(delta, dict):
            _append_stream_text(delta.get("content"), parts, on_text_delta)

    return "".join(parts), usage


def _stream_openai_responses_text(response, on_text_delta=None):
    """Read Responses API SSE chunks and return the accumulated text."""
    response_data = {}
    parts = []

    for payload in _iter_sse_data(response):
        if not payload or payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI-compatible error: malformed event stream payload: {payload}") from exc
        if event.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {event['error']}")

        if isinstance(event.get("delta"), str):
            _append_stream_text(event.get("delta"), parts, on_text_delta)
            response_data = event
            continue

        response = event.get("response")
        if isinstance(response, dict):
            response_data = response

    return "".join(parts), response_data


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个“看起来统一、其实没意义”的伪参数。
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, on_text_delta=None):
        """向 OpenAI-compatible `/responses` 接口发起一次模型调用。

        为什么存在：
        runtime 不应该知道 HTTP 细节、SSE 细节、usage 字段长什么样，
        更不应该自己去判断 prompt cache 参数要不要带。这个函数把这些后端
        细节都包起来，对上层暴露统一的 `complete()` 行为。

        输入 / 输出：
        - 输入：完整 prompt、最大输出 token，以及可选的 prompt cache 参数
        - 输出：模型最终文本；如果传入 `on_text_delta`，会在流式 chunk 到达时
          同步发出文本片段；同时把 usage / cached_tokens 等元数据写进
          `self.last_completion_metadata`

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的模型调用阶段，是稳定前缀缓存复用链路真正
        落到 provider API 的地方。
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": max_new_tokens,
            "stream": True,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # runtime 传入的是“稳定前缀”的签名，而不是整段 prompt 的签名。
        # 这样缓存复用针对的是稳定段，不会因为动态 history 每轮变化而失效。
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            _responses_endpoint_url(self.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            emitted_external_delta = False

            def forward_text_delta(delta):
                nonlocal emitted_external_delta
                if on_text_delta is not None and delta:
                    emitted_external_delta = True
                    on_text_delta(delta)

            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    headers = getattr(response, "headers", {}) or {}
                    content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
                    if content_type == "text/event-stream":
                        text, response_data = _stream_openai_responses_text(response, on_text_delta=forward_text_delta)
                    else:
                        body_text = response.read().decode("utf-8")
                        try:
                            response_data = json.loads(body_text)
                        except json.JSONDecodeError as exc:
                            raise RuntimeError(
                                "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
                            ) from exc
                        if response_data.get("error"):
                            raise RuntimeError(f"OpenAI-compatible error: {response_data['error']}")
                        text = _extract_openai_text(response_data)
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected, IncompleteRead) as exc:
                if not emitted_external_delta and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        # 这些元数据会一路传回 runtime，进入 trace 和 report，
        # 用来观察 prompt cache 是否真的命中。
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(response_data if isinstance(response_data, dict) else {}),
        }
        if text:
            return text
        raise RuntimeError("OpenAI-compatible error: could not extract text from response")


class OpenAIChatCompletionsModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_chat_completions_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, on_text_delta=None):
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_new_tokens,
            "stream": True,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            _chat_completions_endpoint_url(self.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            emitted_external_delta = False

            def forward_text_delta(delta):
                nonlocal emitted_external_delta
                if on_text_delta is not None and delta:
                    emitted_external_delta = True
                    on_text_delta(delta)

            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    headers = getattr(response, "headers", {}) or {}
                    content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
                    if content_type == "text/event-stream":
                        text, usage = _stream_openai_chat_text(response, on_text_delta=forward_text_delta)
                        response_data = {"usage": usage} if usage else {}
                    else:
                        body_text = response.read().decode("utf-8")
                        try:
                            response_data = json.loads(body_text)
                        except json.JSONDecodeError as exc:
                            raise RuntimeError(
                                "OpenAI chat completions error: backend returned non-JSON content that could not be parsed"
                            ) from exc
                        if response_data.get("error"):
                            raise RuntimeError(f"OpenAI chat completions error: {response_data['error']}")
                        text = _extract_openai_text(response_data)
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI chat completions request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected, IncompleteRead) as exc:
                if not emitted_external_delta and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI chat completions backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        self.last_completion_metadata = _extract_usage_cache_details(response_data if isinstance(response_data, dict) else {})
        if text:
            return text
        raise RuntimeError("OpenAI chat completions error: could not extract text from response")


def _extract_anthropic_text(data):
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, on_text_delta=None):
        # 为了保持统一接口，runtime 仍然会传缓存参数进来；
        # 这里只是显式丢弃，因为当前 Anthropic-compatible 路径没有接缓存复用。
        del prompt_cache_key, prompt_cache_retention, on_text_delta
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected, IncompleteRead) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        text = _extract_anthropic_text(data)
        if text:
            return text
        raise RuntimeError("Anthropic-compatible error: could not extract text from response")
