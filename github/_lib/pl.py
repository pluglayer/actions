#!/usr/bin/env python3
"""Shared helper for PlugLayer composite GitHub actions.

Subcommands:
  detail <body_file>                 print best-effort error detail from an API response body
  outputs <body_file> [k[=path]...]  unwrap the ok/data envelope and append k=value lines to $GITHUB_OUTPUT
  build-args                         print KEY=VALUE lines parsed from $PLUGLAYER_BUILD_ENV_JSON
  env-payload                        print a secure env-import payload from the PLUGLAYER_ENV_* inputs
  wait-task <task_id>                poll the task until a terminal status; writes final_status to $GITHUB_OUTPUT
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.request

TERMINAL = {"completed", "failed", "cancelled"}
MAX_ENV_INPUT_BYTES = 64 * 1024
ENV_FORMATS = {"auto", "dotenv", "json", "yaml"}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _unwrap(payload):
    if isinstance(payload, dict):
        data = payload.get("data")
        if payload.get("ok") is True and isinstance(data, dict):
            return data
        return payload
    return {}


def _dig(data, dotted):
    current = data
    for part in dotted.split("."):
        current = current.get(part) if isinstance(current, dict) else None
    return "" if current is None or isinstance(current, (dict, list)) else str(current)


def _append_outputs(pairs):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        for key, value in pairs:
            fh.write(f"{key}={value}\n")


def cmd_detail(args):
    path = args[0]
    payload = _read_json(path)
    if not isinstance(payload, dict):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except Exception:
            raw = ""
        print(raw[:2000] or "no error detail returned")
        return 0
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for source in (payload, data):
        for key in ("detail", "message", "error_message"):
            if source.get(key):
                print(str(source[key])[:2000])
                return 0
    print(json.dumps(payload)[:2000])
    return 0


def cmd_outputs(args):
    data = _unwrap(_read_json(args[0]))
    pairs = []
    for spec in args[1:]:
        key, _, dotted = spec.partition("=")
        pairs.append((key, _dig(data, dotted or key)))
    _append_outputs(pairs)
    return 0


def cmd_build_args(_args):
    raw = os.environ.get("PLUGLAYER_BUILD_ENV_JSON", "").strip()
    if not raw:
        return 0
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        print("build_env_json must be a JSON object of key/value pairs", file=sys.stderr)
        return 1
    for key, value in payload.items():
        if value is not None:
            print(f"{key}={value}")
    return 0


def _env_file_content(raw_path):
    path = Path(raw_path)
    if path.is_symlink():
        raise ValueError("env_file must be a regular file, not a symlink or special file")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"env_file could not be read: {exc}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("env_file must be a regular file, not a symlink or special file")
        with os.fdopen(descriptor, "rb") as fh:
            descriptor = -1
            data = fh.read(MAX_ENV_INPUT_BYTES + 1)
        if len(data) > MAX_ENV_INPUT_BYTES:
            raise ValueError(f"env_file must be at most {MAX_ENV_INPUT_BYTES} bytes")
        return data.decode("utf-8"), path.suffix.lower()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"env_file must be a readable UTF-8 file: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def cmd_env_payload(_args):
    raw_json = os.environ.get("PLUGLAYER_ENV_JSON", "").strip()
    env_text = os.environ.get("PLUGLAYER_ENV_TEXT", "")
    env_file = os.environ.get("PLUGLAYER_ENV_FILE", "").strip()
    sources = sum(bool(value) for value in (raw_json, env_text, env_file))
    if sources > 1:
        print("provide only one of env_json, env_text, or env_file", file=sys.stderr)
        return 1

    selected_format = os.environ.get("PLUGLAYER_ENV_FORMAT", "auto").strip().lower() or "auto"
    if selected_format not in ENV_FORMATS:
        print("env_format must be one of auto, dotenv, json, or yaml", file=sys.stderr)
        return 1

    payload = {
        "merge": os.environ.get("PLUGLAYER_MERGE", "true").lower() != "false",
        "restart_mode": os.environ.get("PLUGLAYER_RESTART_MODE", "restart"),
    }
    strategy = os.environ.get("PLUGLAYER_REDEPLOY_STRATEGY", "").strip()
    if strategy:
        payload["redeploy_strategy"] = strategy

    try:
        if raw_json:
            if len(raw_json.encode("utf-8")) > MAX_ENV_INPUT_BYTES:
                raise ValueError(f"env_json must be at most {MAX_ENV_INPUT_BYTES} bytes")
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        display_key = key if len(key) <= 80 else f"{key[:77]}..."
                        raise ValueError(f"duplicate env_json key '{display_key}'")
                    result[key] = value
                return result

            env_vars = json.loads(
                raw_json,
                object_pairs_hook=unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number '{value}'")),
            )
            if not isinstance(env_vars, dict):
                raise ValueError("env_json must be a JSON object")
            payload.update({"content": raw_json, "input_format": "json"})
        elif env_file:
            content, suffix = _env_file_content(env_file)
            if selected_format == "auto":
                selected_format = {".json": "json", ".yaml": "yaml", ".yml": "yaml"}.get(suffix, "dotenv")
            payload.update({"content": content, "input_format": selected_format})
        elif env_text:
            if len(env_text.encode("utf-8")) > MAX_ENV_INPUT_BYTES:
                raise ValueError(f"env_text must be at most {MAX_ENV_INPUT_BYTES} bytes")
            payload.update({"content": env_text, "input_format": selected_format})
        else:
            if not payload["merge"]:
                raise ValueError("merge=false requires an explicit env_json, env_text, or env_file source")
            payload["env_vars"] = {}
    except (json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload))
    return 0


def _get_task(task_id):
    base = "".join(os.environ["PLUGLAYER_API_URL"].split()).rstrip("/")
    request = urllib.request.Request(
        f"{base}/v1/plugin/tasks/{task_id}",
        headers={"Authorization": f"Bearer {os.environ['PLUGLAYER_API_TOKEN']}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _task_view(payload):
    data = _unwrap(payload)
    task = data.get("task") if isinstance(data.get("task"), dict) else data
    status = str(task.get("status") or "unknown").lower()
    detail = str(task.get("error_message") or "").strip()
    progress = task.get("progress")
    if not detail and isinstance(progress, dict):
        detail = str(progress.get("message") or "").strip()
    result = task.get("result")
    context = result.get("failure_context") if isinstance(result, dict) else None
    if isinstance(context, dict) and context.get("pod_states"):
        detail = f"{detail} Pod states: {context['pod_states']}.".strip()
    return status, detail


def cmd_wait_task(args):
    task_id = args[0]
    timeout = int(os.environ.get("PLUGLAYER_WAIT_TIMEOUT", "900"))
    interval = max(3, int(os.environ.get("PLUGLAYER_POLL_INTERVAL", "10")))
    deadline = time.monotonic() + timeout
    final_status, detail = "timeout", ""
    while time.monotonic() < deadline:
        try:
            status, detail = _task_view(_get_task(task_id))
            print(f"Task {task_id}: {status}", flush=True)
        except Exception as exc:  # transient network/API issue: keep polling
            print(f"Transient task poll issue: {exc}", flush=True)
            status = "unknown"
        if status in TERMINAL:
            final_status = status
            break
        time.sleep(interval)
    _append_outputs([("final_status", final_status)])
    if final_status == "completed":
        print("PlugLayer task completed.")
        return 0
    if final_status == "timeout":
        print(
            f"PlugLayer task {task_id} did not finish within {timeout}s. "
            "It may still be progressing; check the task or app status before retrying.",
            file=sys.stderr,
        )
    else:
        print(f"PlugLayer task {final_status}: {detail or 'no failure detail returned'}", file=sys.stderr)
    return 1


COMMANDS = {
    "detail": cmd_detail,
    "outputs": cmd_outputs,
    "build-args": cmd_build_args,
    "env-payload": cmd_env_payload,
    "wait-task": cmd_wait_task,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"usage: pl.py {{{'|'.join(COMMANDS)}}} ...", file=sys.stderr)
        return 2
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
