"""Minimal OpenAI-compatible chat client for Crusoe Managed Inference. No agent framework.

Every call is an event: `agent_tool` before (what was asked), `llm_usage` after (model, real
token counts from the API, latency, cost if prices are configured). Retries use exponential
backoff with jitter. Responses keep the model's `reasoning_content` so the Agent flow page can
show why an agent decided what it did.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from sieve.store import Store

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def load_env() -> None:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
BASE_URL = os.environ.get("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1")
MODEL_LARGE = os.environ.get("MODEL_LARGE", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B")
MODEL_SMALL = os.environ.get("MODEL_SMALL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")


def _price(model: str) -> tuple[float, float] | None:
    """USD per million (input, output) tokens from env, e.g. PRICE_LARGE=0.30,1.20. None = unknown."""
    key = "PRICE_LARGE" if model == MODEL_LARGE else "PRICE_SMALL" if model == MODEL_SMALL else None
    raw = os.environ.get(key or "", "")
    try:
        a, b = (float(x) for x in raw.split(","))
        return a, b
    except ValueError:
        return None


class LLMError(Exception):
    pass


@dataclass
class Reply:
    content: str
    reasoning: str
    tokens_in: int
    tokens_out: int
    latency_s: float
    data: dict | None = None  # parsed JSON object, when one was asked for


def _post(model: str, messages: list[dict], max_tokens: int, timeout: float) -> dict:
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens,
                       "temperature": 0.3}).encode()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['CRUSOE_API_KEY']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def extract_json(text: str, expect: tuple[str, ...] = ()) -> dict:
    """The model's answer object. Replies can contain example or draft JSON before the final
    answer, so take the LAST top-level object that has one of the expected keys (else the last)."""
    text = re.sub(r"```(?:json)?", "", text)
    found, i = [], 0
    while (start := text.find("{", i)) != -1:
        depth, end = 0, None
        for j in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                end = j
                break
        if end is None:
            break
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                found.append(obj)
            i = end + 1
        except json.JSONDecodeError:
            i = start + 1
    if not found:
        raise ValueError("no JSON object in reply")
    keyed = [o for o in found if any(k in o for k in expect)]
    return (keyed or found)[-1]


async def chat(st: Store, run_id: str, agent_id: str, role: str, model: str, system: str,
               user: str, *, want_json: bool = True, expect: tuple[str, ...] = (), max_tokens: int = 4000,
               retries: int = 4, timeout: float = 180.0, parent_id: str | None = None) -> Reply:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    st.event(run_id, "agent_tool", {"role": role, "tool": "llm", "model": model,
                                    "prompt_chars": len(system) + len(user)},
             agent_id=agent_id, parent_id=parent_id)
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            d = await asyncio.to_thread(_post, model, messages, max_tokens, timeout)
            if "error" in d:
                raise LLMError(str(d["error"])[:300])
            msg = d["choices"][0]["message"]
            u = d.get("usage") or {}
            reply = Reply(msg.get("content") or "", msg.get("reasoning_content") or msg.get("reasoning") or "",
                          int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0),
                          time.time() - t0)
            price = _price(model)
            cost = None if price is None else (reply.tokens_in * price[0] + reply.tokens_out * price[1]) / 1e6
            st.add_usage(agent_id, reply.tokens_in, reply.tokens_out, cost or 0.0)
            st.event(run_id, "llm_usage", {"role": role, "model": model, "tokens_in": reply.tokens_in,
                                           "tokens_out": reply.tokens_out, "cost_usd": cost,
                                           "latency_s": round(reply.latency_s, 2), "attempt": attempt},
                     agent_id=agent_id, parent_id=parent_id)
            if want_json:
                try:
                    reply.data = extract_json(reply.content, expect)
                except ValueError:
                    if attempt < retries:  # ask once more, plainly
                        messages = messages + [{"role": "assistant", "content": reply.content[:2000]},
                                               {"role": "user", "content": "Return only the JSON object, nothing else."}]
                        last_err = "reply was not JSON"
                        continue
                    raise LLMError("reply was not JSON")
            return reply
        except (urllib.error.URLError, TimeoutError, LLMError, KeyError, json.JSONDecodeError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt == retries:
                break
            wait = min(30.0, 2 ** attempt) * (0.7 + 0.6 * random.random())
            st.event(run_id, "agent_fail", {"role": role, "error": last_err[:300], "retry": attempt + 1,
                                            "backoff_s": round(wait, 1)}, agent_id=agent_id,
                     parent_id=parent_id)
            await asyncio.sleep(wait)
    raise LLMError(last_err or "unknown failure")
