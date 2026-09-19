"""Shared Nemotron client: .env loading, an OpenAI-compatible chat client, and lenient JSON parsing.

Used by run_nemotron.py (decisions), xtract.py (signal extraction), draft_actions.py (letters),
extract_contract.py (contract terms) and ask.py (Q&A).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests

from common import ROOT, USER_AGENT

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
log = logging.getLogger("portguard.nemotron")


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name) or default  # empty values count as unset


class NemotronClient:
    """Minimal OpenAI-compatible chat-completions client (NIM, vLLM, Ollama, NVIDIA API).

    Optional knobs, each dropped automatically if a server rejects it:
      reasoning   "none" | "low" | "medium" | "high" -> `reasoning_effort` (None = model default; "none" = no thinking)
      structured  send a JSON Schema as `response_format` so the reply is forced into the expected shape
      seed        fixed sampling seed for reproducible runs
    """

    def __init__(self, base_url: str, model: Optional[str] = None, api_key: Optional[str] = None,
                 temperature: float = 0.2, max_tokens: int = 2048, timeout: float = 300,
                 reasoning: Optional[str] = None, structured: bool = True, seed: Optional[int] = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.temperature, self.max_tokens, self.timeout = temperature, max_tokens, timeout
        self.reasoning = reasoning if reasoning not in (None, "", "default") else None
        self.structured, self.seed = structured, seed
        self.model = model or self._detect_model()

    def _detect_model(self) -> str:
        resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=30)
        resp.raise_for_status()
        ids = [m["id"] for m in resp.json().get("data", [])]
        if not ids:
            raise RuntimeError(f"{self.base_url}/models lists no models; set NEMOTRON_MODEL")
        chosen = ([i for i in ids if "nemotron" in i.lower()] or ids)[0]
        log.info("NEMOTRON_MODEL not set; using %r (server lists: %s)", chosen, ", ".join(ids))
        return chosen

    def chat(self, system: str, user: str, max_tokens: Optional[int] = None,
             schema: Optional[dict] = None) -> tuple[str, dict]:
        """Return (message content, full response JSON). `schema` constrains the reply when structured is on."""
        body = {"model": self.model, "temperature": self.temperature, "max_tokens": max_tokens or self.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if schema is not None and self.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "portguard", "schema": schema}}
        if self.reasoning:
            body["reasoning_effort"] = self.reasoning
        if self.seed is not None:
            body["seed"] = self.seed
        resp = requests.post(f"{self.base_url}/chat/completions", headers=self.headers, timeout=self.timeout, json=body)
        if resp.status_code in (400, 422):  # a server that rejects a knob: drop knobs one at a time, least useful first
            for key, attr, off in (("reasoning_effort", "reasoning", None), ("seed", "seed", None),
                                   ("response_format", "structured", False)):
                if key in body:
                    log.warning("server rejected the request (%s); retrying without %s", resp.text[:100], key)
                    setattr(self, attr, off)
                    return self.chat(system, user, max_tokens, schema)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"].get("content") or "", data


def add_client_args(parser: argparse.ArgumentParser) -> None:
    """CLI flags shared by every script that calls Nemotron (defaults come from .env)."""
    load_dotenv()
    parser.add_argument("--base-url", default=env("NEMOTRON_BASE_URL", "http://localhost:8000/v1"))
    parser.add_argument("--model", default=env("NEMOTRON_MODEL"), help="default: auto-detect from GET /models")
    parser.add_argument("--temperature", type=float, default=float(env("NEMOTRON_TEMPERATURE", "0.2")))
    parser.add_argument("--max-tokens", type=int, default=int(env("NEMOTRON_MAX_TOKENS", "2048")))
    parser.add_argument("--timeout", type=float, default=float(env("NEMOTRON_TIMEOUT", "300")))
    parser.add_argument("--reasoning", choices=["default", "none", "low", "medium", "high"],
                        default=env("NEMOTRON_REASONING", "default"),
                        help="thinking effort (reasoning_effort); 'none' turns thinking off (default: model default)")
    parser.add_argument("--structured", choices=["on", "off"], default=env("NEMOTRON_STRUCTURED", "on"),
                        help="force replies into the expected JSON shape via response_format (default: on)")
    parser.add_argument("--seed", type=int, default=int(env("NEMOTRON_SEED")) if env("NEMOTRON_SEED") else None)


def client_from_args(args: argparse.Namespace) -> NemotronClient:
    return NemotronClient(args.base_url, args.model, env("NEMOTRON_API_KEY"), args.temperature, args.max_tokens,
                          args.timeout, args.reasoning, args.structured == "on", args.seed)


def client_from_env() -> NemotronClient:
    load_dotenv()
    return NemotronClient(env("NEMOTRON_BASE_URL", "http://localhost:8000/v1"), env("NEMOTRON_MODEL"),
                          env("NEMOTRON_API_KEY"), float(env("NEMOTRON_TEMPERATURE", "0.2")),
                          int(env("NEMOTRON_MAX_TOKENS", "2048")), float(env("NEMOTRON_TIMEOUT", "300")),
                          env("NEMOTRON_REASONING"), env("NEMOTRON_STRUCTURED", "on") == "on",
                          int(env("NEMOTRON_SEED")) if env("NEMOTRON_SEED") else None)


def _json_values(text: str, opener: str):
    text = _THINK_RE.sub("", text)
    decoder = json.JSONDecoder()
    for match in re.finditer(re.escape(opener), text):
        try:
            yield decoder.raw_decode(text, match.start())[0]
        except json.JSONDecodeError:
            continue


def extract_json(text: str) -> dict:
    """First JSON object in a model reply, ignoring <think> blocks, prose and ``` fences."""
    for obj in _json_values(text, "{"):
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")


def extract_json_list(text: str) -> list:
    """First JSON array in a model reply (or the first list value of a JSON object)."""
    for arr in _json_values(text, "["):
        if isinstance(arr, list):
            return arr
    obj = extract_json(text)
    for value in obj.values():
        if isinstance(value, list):
            return value
    raise ValueError(f"no JSON array in model reply: {text[:200]!r}")
