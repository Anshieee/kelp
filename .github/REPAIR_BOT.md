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
