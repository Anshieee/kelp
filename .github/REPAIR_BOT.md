# OpenRouter repair bot

The `repair bot` GitHub Actions workflow can be started manually with an open
pull request number or automatically after the `ci` workflow fails on a pull
request. It uses Aider with `nvidia/nemotron-3.5-super-vl:free`, and pushes a
repair commit to that pull request's branch using the workflow's `GITHUB_TOKEN`.

## Setup and limits

Add an `OPENROUTER_API_KEY` **repository secret** in GitHub Settings → Secrets
and variables → Actions. The workflow validates that the repository is public
and rejects pull requests from forks. It will not run against private code.
Never enable or use this workflow for private repositories, and never send
private code to OpenRouter.

The selected OpenRouter free tier is limited to **20 requests per minute and
50 requests per day**. A repair job is capped at two Aider attempts; avoid
repeated manual runs so the shared daily quota is not exhausted. OpenRouter
availability and model limits may change.

The workflow requests only `contents: write` and `pull-requests: read`, does not
force-push, and cannot bypass branch protection. A protected branch may reject
the push, in which case a maintainer must review and apply the repair through
the repository's normal process. Fork PRs and closed PRs are not eligible.

## Supply chain and key scope

`aider-chat` is pinned to an exact version (`0.86.2`). That release's metadata
pins its unconditional runtime dependencies with `==`, so the pin fixes most of
the installed tree; pip, setuptools, and build backends are not covered, and
there are no hashes. Bump it on purpose, in its own change. pip and the log
redaction script run in Python's isolated mode (`-I`), so a module in the PR
checkout cannot shadow them.

Aider runs with `--no-suggest-shell-commands` and stdin from `/dev/null`. Aider
treats end-of-input as the default answer, which is "yes" for "Run shell
command?", so without the flag a model steered by PR content or log text could
run commands while the key is in the environment.

`OPENROUTER_API_KEY` is set only on the step that runs Aider. Before that step,
the job refuses to run if the PR tree contains a `.env` file or any `.aider*`
file, because Aider reads both from the working tree (`.env` overrides the
environment) and a PR could otherwise point Aider at a different API base while
the key is present.

The automatic trigger is `workflow_run`, so GitHub runs the copy of this
workflow on the default branch, not the copy in the PR: a change to this file
takes effect for automatic runs only after it merges. A manual
`workflow_dispatch` runs the copy on the ref it is dispatched from.
