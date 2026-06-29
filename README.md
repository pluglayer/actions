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
  - uses: pluglayer/actions/github/redeploy-pluglayer-app@main
    with:
      api_token: ${{ secrets.PLUGLAYER_API_KEY }}
      app_id: <app_id>
```

## Actions

| Action | Purpose | Key inputs | Outputs |
| --- | --- | --- | --- |
| `github/build-oci-image` | Build a multi-arch OCI archive | `context`, `dockerfile`, `image_name`, `image_tag`, `archive_path`, `platforms`, `build_env_json` | `archive_path` |
| `github/upload-image-to-pluglayer` | Upload the archive to an existing app (no restart yet) | `api_token`, `app_id`, `archive_path`, `image_tag`, `registry_id` | `mirrored_image`, `app_id` |
| `github/redeploy-pluglayer-app` | Queue a redeploy and wait for the rollout task | `api_token`, `app_id`, `wait_for_completion` (true), `wait_timeout_seconds` (900), `poll_interval_seconds` (10) | `task_id`, `final_status` |
| `github/apply-env-and-restart` | Merge env vars, restart/redeploy, wait for the task | `api_token`, `app_id`, `env_json`, `merge`, `restart_mode`, same wait inputs | `task_id`, `final_status` |

All API-calling actions accept `api_url` (default `https://api.pluglayer.com`).

## Behavior guarantees

- Transient transport failures are retried; HTTP 4xx errors fail fast with the backend's real error detail.
- `redeploy-pluglayer-app` and `apply-env-and-restart` poll the queued task and fail the job with the rollout's actual failure message (including pod states) — do not add your own polling or status-check loops around them.
- `final_status` is `completed`, `failed`, `cancelled`, `timeout`, `queued` (waiting disabled), or `none` (no task queued).
- Use one `concurrency` group per app id so rollouts for the same app never overlap.

Shared parsing/polling logic lives in [`github/_lib/pl.py`](github/_lib/pl.py); action YAML stays thin transport.
