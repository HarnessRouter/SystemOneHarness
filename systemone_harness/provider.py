"""The provider: one `decide(state, questions)` over the System One wire.

One wire, several hosts. OpenRouter serves it at `/api/alpha/decisions`, TypeSafe at
`/v1/systemone`; the body and the answers are the same. The adapter is a base URL, a path and a
header. It retries 429 and 529 with exponential backoff (TypeSafe's guidance), records usage,
the served model and the provider's request id per call, and never holds a prompt.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx

USER_AGENT = "systemone-harness/0.1.0"


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Decision:
    answers: dict
    usage: dict
    model: str
    request_id: str
    latency_ms: int
    raw: dict = field(default_factory=dict)


class DecisionProvider:
    def __init__(self, base_url: str, path: str, api_key: str, model: str,
                 headers: dict | None = None, timeout: float = 30.0, retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.api_key = api_key
        self.model = model
        self.headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json",
                        "user-agent": USER_AGENT, **(headers or {})}
        self.timeout = timeout
        self.retries = retries
        self._client = httpx.Client(timeout=timeout)

    def decide(self, state, questions: dict) -> Decision:
        body = {"model": self.model, "state": state, "questions": questions}
        delay = 0.5
        last: ProviderError | None = None
        for attempt in range(self.retries + 1):
            t0 = time.time()
            try:
                r = self._client.post(self.base_url + self.path, json=body, headers=self.headers)
            except httpx.HTTPError as exc:
                last = ProviderError(f"{type(exc).__name__}: {exc}")
                time.sleep(delay)
                delay *= 2
                continue
            latency = int((time.time() - t0) * 1000)
            if r.status_code in (429, 529) or r.status_code >= 500:
                last = ProviderError(f"HTTP {r.status_code} from the provider", r.status_code, r.text[:500])
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code != 200:
                raise ProviderError(f"HTTP {r.status_code} from the provider: {r.text[:500]}",
                                    r.status_code, r.text[:500])
            d = r.json()
            if not isinstance(d, dict) or "answers" not in d:
                raise ProviderError(f"the provider answered without `answers`: {r.text[:300]}", 200, r.text[:300])
            usage = d.get("usage") or {}
            return Decision(answers=d["answers"],
                            usage={"input_tokens": int(usage.get("input_tokens") or 0),
                                   "output_tokens": int(usage.get("output_tokens") or 0)},
                            model=str(d.get("model") or self.model), request_id=str(d.get("id") or ""),
                            latency_ms=latency, raw=d)
        raise last or ProviderError("the provider did not answer")

    def close(self) -> None:
        self._client.close()


class OpenRouterProvider(DecisionProvider):
    """Jev through OpenRouter's decisions endpoint (beta since 2026-09-18)."""

    def __init__(self, api_key: str, model: str = "~typesafe/jev-latest", **kw):
        super().__init__("https://openrouter.ai", "/api/alpha/decisions", api_key, model,
                         headers={"HTTP-Referer": "https://github.com/HarnessRouter/SystemOneHarness",
                                  "X-Title": "System One Harness"}, **kw)


class TypeSafeProvider(DecisionProvider):
    """Jev on TypeSafe's own API."""

    def __init__(self, api_key: str, model: str = "jev-latest", **kw):
        super().__init__("https://api.typesafe.ai", "/v1/systemone", api_key, model, **kw)


class RecordedProvider:
    """Answers from a script or a function, for tests and the benchmark's dry runs.

    `script` is a list of `answers` dicts consumed in order, or a callable
    `(state, questions) -> answers`. Usage is counted from the request size so the trace is
    shaped like a real one.
    """

    def __init__(self, script, model: str = "recorded/jev"):
        self.script = script
        self.model = model
        self.calls: list[tuple[object, dict]] = []

    def decide(self, state, questions: dict) -> Decision:
        self.calls.append((state, questions))
        if callable(self.script):
            answers = self.script(state, questions)
        else:
            if not self.script:
                raise ProviderError("the recorded script has no answers left")
            answers = self.script.pop(0)
        from .encoder import estimate_tokens
        return Decision(answers=answers, usage={"input_tokens": estimate_tokens({"state": state, "questions": questions}),
                                                "output_tokens": len(answers)},
                        model=self.model, request_id=f"rec-{len(self.calls)}", latency_ms=1, raw={"answers": answers})

    def close(self) -> None:
        pass


def provider_from_env(model: str | None = None) -> DecisionProvider:
    """OpenRouter when OPENROUTER_API_KEY is set, else TypeSafe when TYPESAFE_API_KEY is."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return OpenRouterProvider(key, model or os.environ.get("S1_MODEL", "~typesafe/jev-latest"))
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return TypeSafeProvider(key, model or os.environ.get("S1_MODEL", "jev-latest"))
    raise ProviderError("no provider key: set OPENROUTER_API_KEY or TYPESAFE_API_KEY")
