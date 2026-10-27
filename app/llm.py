"""Power layer: every model call goes through Meterless Nightshift (OpenAI-compatible
Runtime API). Nightshift routes to NVIDIA Nemotron; we record which model answered."""
import os
import time

from openai import OpenAI

from . import config  # noqa: F401  (loads .env.local)

_client = None


def client() -> OpenAI:
    global _client
    if _client is None:
        token = os.environ.get("METERLESS_RUNTIME_TOKEN")
        if not token:
            raise RuntimeError("METERLESS_RUNTIME_TOKEN is not set in .env.local")
        _client = OpenAI(
            api_key=token,
            base_url=os.environ.get("METERLESS_BASE_URL", "http://127.0.0.1:8088/v1"),
            default_headers={"meterless-app": "chainguard", "meterless-scope": "user"},
            timeout=180.0,
            max_retries=1,
        )
    return _client


def chat(messages, temperature=0.1, max_tokens=6000) -> dict:
    """One model call through Nightshift. Nemotron reasons before it answers, so an
    'empty response' usually means it ran out of room: retry with a bigger budget."""
    last_error = None
    for attempt, budget in enumerate((max_tokens, max_tokens * 2, max_tokens * 2)):
        started = time.time()
        try:
            response = client().chat.completions.create(
                model=os.environ.get("METERLESS_MODEL", "meterless/auto"),
                messages=messages,
                temperature=temperature,
                max_tokens=budget,
            )
        except Exception as error:  # 502 empty_response, timeouts, rate limits
            last_error = error
            time.sleep(2 + attempt * 3)
            continue
        content = response.choices[0].message.content or ""
        if content.strip():
            return {"content": content, "model": response.model,
                    "ms": int((time.time() - started) * 1000), "attempts": attempt + 1}
        last_error = RuntimeError("model returned an empty answer")
    raise RuntimeError(f"Nightshift call failed after 3 attempts: {last_error}")


def list_models():
    return [m.id for m in client().models.list().data]
