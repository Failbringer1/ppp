"""AI backends.

* claude_cli - runs the local Claude Code CLI (`claude -p`) logged into a Claude
  subscription; no API key needed.
* api        - Anthropic API with ANTHROPIC_API_KEY.
* manual     - no automation: the panel shows the prompt, you paste it into a
  Claude chat and paste the answer back.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil

from ..config import get_config


class ManualAnswerRequired(Exception):
    """The job must be answered by hand on the AI page."""


class AIError(Exception):
    pass


def resolve_mode() -> str:
    cfg = get_config()
    mode = cfg.ai_mode
    if mode != "auto":
        return mode
    if cfg.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which(cfg.claude_bin):
        return "claude_cli"
    return "manual"


async def complete(system: str, prompt: str, mode: str | None = None) -> str:
    mode = mode or resolve_mode()
    if mode == "claude_cli":
        return await _claude_cli(system, prompt)
    if mode == "api":
        return await _anthropic_api(system, prompt)
    raise ManualAnswerRequired()


async def _claude_cli(system: str, prompt: str, timeout: int = 600) -> str:
    cfg = get_config()
    workdir = cfg.data_dir / "ai_workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    args = [
        cfg.claude_bin, "-p",
        "--output-format", "json",
        "--tools", "",                 # pure text generation, no file or shell access
        "--no-session-persistence",
        "--model", cfg.claude_model,
        "--system-prompt", system,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(workdir),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AIError(f"claude CLI: нет ответа за {timeout} с") from None
    if proc.returncode != 0:
        raise AIError(f"claude CLI завершился с кодом {proc.returncode}: {(err or out).decode()[-800:]}")
    try:
        data = json.loads(out.decode("utf-8"))
    except json.JSONDecodeError:
        return out.decode("utf-8").strip()
    if data.get("is_error"):
        raise AIError(f"claude CLI: {data.get('result') or data}")
    return str(data.get("result", "")).strip()


async def _anthropic_api(system: str, prompt: str) -> str:
    import anthropic

    cfg = get_config()
    client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key or None)
    resp = await client.beta.messages.create(
        model=cfg.anthropic_model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise AIError("Модель отказалась отвечать на этот запрос")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def parse_json(text: str):
    """Lenient JSON extraction from a model answer (fenced block or first {...}/[...])."""
    text = (text or "").strip()
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise AIError("В ответе нет JSON")
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"Не удалось разобрать JSON: {e}") from e
