# PlugLayer Reusable GitHub Actions

Composite actions used by PlugLayer-generated CI/CD workflows. Reference them as `pluglayer/actions/github/<name>@main`.

A typical deploy job:

```yaml
concurrency:
  group: pluglayer-deploy-<app_id>
  cancel-in-progress: false

steps:
  - uses: actions/checkout@v4
  - uses: pluglayer/actions/github/build-oci-image@main
    with:
      context: .
      image_name: ghcr.io/owner/repo
      image_tag: ${{ github.sha }}
      archive_path: .pluglayer/app.oci.tar
  - uses: pluglayer/actions/github/upload-image-to-pluglayer@main
    with:
      api_token: ${{ secrets.PLUGLAYER_API_KEY }}
      app_id: <app_id>
      archive_path: .pluglayer/app.oci.tar
      image_tag: ${{ github.sha }}
  - uses: pluglayer/actions/github/apply-env-and-restart@main
    with:
      api_token: ${{ secrets.PLUGLAYER_API_KEY }}
      app_id: <app_id>
      env_json: ${{ secrets.PLUGLAYER_ENV_JSON }}
```

## Actions

| Action | Purpose | Key inputs | Outputs |
| --- | --- | --- | --- |
| `github/build-oci-image` | Build a multi-arch OCI archive | `context`, `dockerfile`, `image_name`, `image_tag`, `archive_path`, `platforms`, `build_env_json` | `archive_path` |
| `github/upload-image-to-pluglayer` | Upload the archive to an existing app (no restart yet) | `api_token`, `app_id`, `archive_path`, `image_tag`, `registry_id` | `mirrored_image`, `app_id` |
| `github/redeploy-pluglayer-app` | Queue a redeploy and wait for the rollout task | `api_token`, `app_id`, `wait_for_completion` (true), `wait_timeout_seconds` (900), `poll_interval_seconds` (10) | `task_id`, `final_status` |
| `github/apply-env-and-restart` | Securely import env vars, restart/redeploy, and wait for the task | `api_token`, `app_id`, one of `env_json`/`env_text`/`env_file`, `env_format`, `merge`, `restart_mode`, `redeploy_strategy`, same wait inputs | `task_id`, `final_status` |

All API-calling actions accept `api_url` (default `https://api.pluglayer.com`).

## Secure runtime environment import

`pluglayer/actions/github/apply-env-and-restart@main` accepts exactly one environment source:

- `env_json`: a JSON object, preferably a masked GitHub secret
- `env_text`: dotenv/`KEY=VALUE`, JSON, or YAML text
- `env_file`: an explicit UTF-8 `.env`, `.json`, `.yaml`, or `.yml` runner file

The Action sends content—not a runner path—to PlugLayer, does not print imported values, creates request/response files with owner-only permissions, and removes them at the end. The Action rejects symlink/special files; the backend bounds input, never interpolates values, and rejects invalid/duplicate keys, nested config values, YAML anchors/tags, and ambiguous multiple sources.

### Recommended: JSON secret

Store `PLUGLAYER_ENV_JSON` as a GitHub Actions secret such as `{"API_URL":"https://api.example.com","TOKEN":"..."}`:

```yaml
- name: Apply runtime env and restart
  uses: pluglayer/actions/github/apply-env-and-restart@main
  with:
    api_token: ${{ secrets.PLUGLAYER_API_KEY }}
    app_id: app_123
    env_json: ${{ secrets.PLUGLAYER_ENV_JSON }}
    merge: true
    restart_mode: restart
```

### Secret dotenv file created on the runner

Do not commit a production `.env` file. Create it from a masked secret and pass the exact temporary path:

```yaml
- name: Create runtime env file
  shell: bash
  env:
    RUNTIME_ENV_FILE: ${{ secrets.PLUGLAYER_ENV_FILE }}
  run: |
    umask 077
    printf '%s' "$RUNTIME_ENV_FILE" > "${RUNNER_TEMP}/pluglayer-runtime.env"

- name: Apply runtime env and restart
  uses: pluglayer/actions/github/apply-env-and-restart@main
  with:
    api_token: ${{ secrets.PLUGLAYER_API_KEY }}
    app_id: app_123
    env_file: ${{ runner.temp }}/pluglayer-runtime.env
    env_format: dotenv
```

For a non-secret checked-in JSON/YAML configuration mapping, pass its repo-relative path as `env_file`. For non-secret entry text, use `env_text: |` followed by `KEY=VALUE` lines.

`merge: true` preserves unspecified variables; `merge: false` replaces the complete set and requires an explicit source so an omitted secret cannot accidentally clear the app. `restart_mode` is `restart`, `redeploy`, or `none`. The Action waits for a queued task by default and returns `task_id` plus `final_status`.

## Behavior guarantees

- Transient transport failures are retried; HTTP 4xx errors fail fast with the backend's real error detail.
- `redeploy-pluglayer-app` and `apply-env-and-restart` poll the queued task and fail the job with the rollout's actual failure message (including pod states); do not add custom polling loops around them.
- `final_status` is `completed`, `failed`, `cancelled`, `timeout`, `queued` (waiting disabled), or `none` (no task queued).
- Use one `concurrency` group per app id so rollouts for the same app never overlap.

Shared parsing/polling logic lives in [`github/_lib/pl.py`](github/_lib/pl.py); action YAML stays thin transport.
