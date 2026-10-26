"""Autonomous GitHub OIDC deployment. No durable PlugLayer credential is used."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

CHUNK_BYTES = 4 * 1024 * 1024


class DeploymentError(RuntimeError):
    """Only these fixed messages are safe to display in CI logs."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def unwrap(value):
    return value.get("data", value) if isinstance(value, dict) else value


def mask(value):
    # GitHub command escaping; never emit raw credentials or server bodies.
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::add-mask::{escaped}", flush=True)


class Deployment:
    def __init__(self, env=None):
        self.env = os.environ if env is None else env
        self.origin = self.env["PLUGLAYER_API_URL"].rstrip("/")
        parsed = urlsplit(self.origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise DeploymentError("PlugLayer API must be a public HTTPS origin")
        self.app_id = self.env["PLUGLAYER_APP_ID"]
        self.prefix = "/v1/cicd/github/apps/" + quote(self.app_id, safe="")
        self.token = None
        self.token_until = 0.0
        self.opener = build_opener(NoRedirect())

    def transport(self, url, method="GET", body=None, headers=None, timeout=90):
        request = Request(url, data=body, headers=headers or {}, method=method)
        with self.opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def authenticate(self):
        request_url = self.env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
        request_token = self.env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
        parsed = urlsplit(request_url)
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".actions.githubusercontent.com")
                or parsed.username or parsed.password or not request_token):
            raise DeploymentError("GitHub identity is unavailable; set permissions.id-token to write on this job")
        separator = "&" if parsed.query else "?"
        url = request_url + separator + urlencode({"audience": self.env["PLUGLAYER_OIDC_AUDIENCE"]})
        identity = self.transport(url, headers={"Authorization": "Bearer " + request_token})["value"]
        mask(identity)
        result = self.transport(self.origin + "/v1/cicd/github/token", "POST",
            json.dumps({"connection_id": self.env["PLUGLAYER_CONNECTION_ID"], "identity_token": identity}).encode(),
            {"Content-Type": "application/json"})
        if result.get("app_id") != self.app_id:
            raise DeploymentError("Deployment connection belongs to a different app")
        self.token = result["access_token"]
        mask(self.token)
        self.token_until = time.monotonic() + min(int(result["expires_in"]), 900) - 60

    def request(self, path, method="GET", payload=None, raw=None, headers=None, retry=False, timeout=90):
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        attempts = 4 if retry else 1
        for attempt in range(attempts):
            try:
                if not self.token or time.monotonic() >= self.token_until:
                    self.authenticate()
                request_headers = {"Authorization": "Bearer " + self.token,
                    "Content-Type": "application/octet-stream" if raw is not None else "application/json", **(headers or {})}
                return unwrap(self.transport(self.origin + self.prefix + path, method, body, request_headers, timeout))
            except HTTPError as exc:
                if exc.code == 401:
                    self.token = None
                if exc.code not in {401, 429, 502, 503, 504} or attempt == attempts - 1:
                    raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt == attempts - 1:
                    raise
            time.sleep(min(2 ** attempt, 8))

    def deploy(self):
        archive = Path(self.env["PLUGLAYER_ARCHIVE_PATH"])
        if not archive.is_file() or archive.stat().st_size == 0:
            raise DeploymentError("Image archive is missing or empty")
        raw_env = self.env.get("PLUGLAYER_ENV_JSON", "").strip()
        if len(raw_env.encode()) > 65536:
            raise DeploymentError("Runtime environment input exceeds 64 KiB")
        runtime_env = json.loads(raw_env) if raw_env else {}
        if not isinstance(runtime_env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in runtime_env.items()):
            raise DeploymentError("Runtime environment must be a JSON object of string values")
        for value in runtime_env.values():
            if value:
                mask(value)
        session = self.request("/uploads", "POST", {"filename": archive.name, "size_bytes": archive.stat().st_size})
        upload_path = "/uploads/" + quote(session["upload_id"], safe="")
        chunk_size = min(CHUNK_BYTES, int(session["chunk_size"]))
        if chunk_size <= 0:
            raise DeploymentError("Server returned an invalid upload chunk size")
        offset = index = 0
        with archive.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                acknowledgement = self.request(upload_path + f"/chunks/{index}", "PUT", raw=chunk,
                    headers={"X-Upload-Offset": str(offset), "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()}, retry=True)
                offset += len(chunk)
                # Backend acknowledges the cumulative contiguous byte count.
                if acknowledgement.get("received_bytes") != offset:
                    raise DeploymentError("Upload acknowledgement does not match the chunk size")
                index += 1
                print(f"Uploaded {offset}/{archive.stat().st_size} bytes", flush=True)
        completion = {"tag": self.env["PLUGLAYER_IMAGE_TAG"], "env_vars": runtime_env, "wait_seconds": 0}
        try:
            result = self.request(upload_path + "/complete", "POST", completion, timeout=1800)
        except (HTTPError, URLError, TimeoutError, ConnectionError):
            # Completion mutates the app. Recover its receipt; never blindly
            # resubmit a deploy after a transport failure.
            deadline = time.monotonic() + 1800
            while True:
                result = self.request(upload_path, retry=True)
                if result.get("task_id"):
                    break
                if result.get("status") != "completing" or time.monotonic() >= deadline:
                    raise DeploymentError("Upload completion could not be verified; inspect the app before rerunning") from None
                time.sleep(5)
        task_id = result.get("task_id")
        if not task_id:
            raise DeploymentError("Deployment returned no task ID")
        self.output("task_id", task_id)
        deadline = time.monotonic() + min(max(int(self.env.get("PLUGLAYER_WAIT_TIMEOUT", "1800")), 1), 7200)
        while time.monotonic() < deadline:
            task = self.request("/tasks/" + quote(task_id, safe=""), retry=True)
            status = task["status"]
            print(f"Deployment {task_id}: {status}", flush=True)
            if status in {"completed", "failed", "cancelled"}:
                self.output("final_status", status)
                if status != "completed":
                    raise DeploymentError("Deployment failed; inspect the app's deployment task in PlugLayer")
                return
            time.sleep(10)
        self.output("final_status", "timeout")
        raise DeploymentError("Deployment is still unverified after the wait limit; inspect the task in PlugLayer")

    def output(self, key, value):
        if "\n" in value or "\r" in value:
            raise DeploymentError("Invalid action output")
        with open(self.env["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"{key}={value}\n")


def main():
    try:
        Deployment().deploy()
    except HTTPError as exc:
        print(f"PlugLayer request failed (HTTP {exc.code}). Check the deployment connection and workflow permissions.", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, ConnectionError):
        print("PlugLayer could not be reached; deployment success is unverified.", file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        # JSON decoder errors and KeyErrors can contain user input. Print only
        # our fixed messages; never dump exception objects, responses or env.
        message = str(exc) if isinstance(exc, DeploymentError) else "Invalid deployment input or response"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
