#!/usr/bin/env python3
"""Shared helper for PlugLayer composite GitHub actions.

Subcommands:
  detail <body_file>                 print best-effort error detail from an API response body
  outputs <body_file> [k[=path]...]  unwrap the ok/data envelope and append k=value lines to $GITHUB_OUTPUT
  build-args                         print KEY=VALUE lines parsed from $PLUGLAYER_BUILD_ENV_JSON
  env-payload                        print the env-apply JSON payload from $PLUGLAYER_ENV_JSON/$PLUGLAYER_MERGE/$PLUGLAYER_RESTART_MODE
  wait-task <task_id>                poll the task until a terminal status; writes final_status to $GITHUB_OUTPUT
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

TERMINAL = {"completed", "failed", "cancelled"}


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


def cmd_env_payload(_args):
    raw = os.environ.get("PLUGLAYER_ENV_JSON", "").strip()
    env_vars = json.loads(raw) if raw else {}
    if not isinstance(env_vars, dict):
        print("env_json must be a JSON object", file=sys.stderr)
        return 1
    print(json.dumps({
        "env_vars": env_vars,
        "merge": os.environ.get("PLUGLAYER_MERGE", "true").lower() != "false",
        "restart_mode": os.environ.get("PLUGLAYER_RESTART_MODE", "restart"),
    }))
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
