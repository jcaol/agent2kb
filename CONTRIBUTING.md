# Contributing

Contributions are welcome, including bug reports, documentation fixes, new ingest handlers and
reports from real storage-provider deployments.

## Development setup

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb   # or your fork
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,all]"

pytest                 # unit tests; integration tests are skipped by default
ruff check .           # lint (line length 100)
ruff format <file>     # optional; match the surrounding style
a2kb --help            # the entry point is agent2kb.main:app
```

While developing, run the code in your checkout (`python -m agent2kb.main ...`) rather than an
`a2kb` that may be installed elsewhere and configured for real data, and use a scratch `HOME`
so that your real `~/.kb/config.yaml` is never read.

Python 3.10 or newer. `all` installs every optional provider SDK; the extras are
listed in `pyproject.toml`. If you work on only one area, `pip install -e ".[dev]"` plus
the extra for that area is sufficient, because tests that need a missing SDK simulate it.

### Integration tests

`tests/integration/` talks to a real S3 endpoint and is skipped unless
`KB_IT_S3_ENDPOINT` (and credentials) are set. Use a throwaway server, for example a
local SeaweedFS (`weed server -s3 ...`), never a bucket that holds real data. The tests
create and delete their own prefix:

```bash
KB_IT_S3_ENDPOINT=http://127.0.0.1:18333 KB_IT_ACCESS_KEY=<key> KB_IT_SECRET_KEY=<secret> \
  pytest tests/integration -v
```

`KB_IT_REGION` and `KB_IT_BUCKET` are optional; the header of
`tests/integration/test_s3_live.py` has the details.

### Docker end-to-end

`scripts/docker-e2e.sh` builds the image, starts the whole stack with `docker compose up` and
exercises it the way a user would, in its own compose project (`a2kb-e2e`) so it never touches
your real install. It needs Docker with Compose v2 and network access, and is what
`.github/workflows/docker-e2e.yml` runs in CI. `KB_E2E_KEEP=1` leaves the stack running for
inspection. The bootstrap logic behind it is also covered by ordinary unit tests
(`tests/test_bootstrap.py`, `tests/test_connect.py`), which need no Docker.

## The compatibility contract

Existing knowledge bases and the scripts, agents and cron jobs around them must keep
working. `tests/test_contract.py` pins that surface:

- the `a2kb` command, its subcommands, flags and exit codes
- KB selection (`-p/--profile`, `$KB_PROFILE`, `.kb/config.yaml` pointer autodetect)
- config file formats (v2 and the legacy ones), field names and `KB_*` environment variables
- the S3 layout (`kbs/<name>/{raw,wiki,outputs}/`, and `projects/<name>/...` for old
  configs), raw file naming and frontmatter fields
- `python3 -m agent2kb.llm_run` and its exit codes

**Do not break this contract without a deprecation path**: keep the old behaviour
working, warn when it is used, and say so in `CHANGELOG.md`. A missing new config field
must mean the old behaviour. If a contract test has to change, explain why in the
commit message; a changed assertion is a breaking change.

## Code rules

- All S3 access goes through `agent2kb/s3_client.py`, which enforces the KB's prefix.
  Never call boto3 from a command handler.
- Calls to the `claude` and `codex` CLIs go through `agent2kb/cli_runner.py`, with its
  isolation flags. Never spawn them directly.
- Shell scripts do not name models; they use `python3 -m agent2kb.llm_run`.
- Optional dependencies are imported lazily through `agent2kb.extras.import_optional`, so a
  missing package says which extra to install.
- Anything that sends content to a third party must be reflected in
  [docs/privacy.md](docs/privacy.md) and surface a privacy warning.
- Match the surrounding code in naming, comment density and idiom. Comments explain why, not what.

## Pull requests

- Keep a PR to one logical change; add or update tests.
- `pytest` and `ruff check .` must pass.
- Update `CHANGELOG.md` (the Unreleased section) for anything a user would notice.
- Never include real credentials, real `config.yaml` files or real KB content in a
  commit, test fixture or issue.

Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Licence and Contributor Licence Agreement

agent2kb is licensed under the GNU Affero General Public License v3.0 (`AGPL-3.0-only`, see
[LICENSE](LICENSE)), and the maintainers may also offer it under a commercial licence. To make
that possible, every contributor must agree to the [Contributor Licence Agreement](CLA.md)
before a contribution can be merged. You keep the copyright in your work; the agreement grants
the project the right to distribute your contribution under the AGPL and under other licence
terms.

Signing takes one comment: on your first pull request the CLA bot asks you to post
`I have read the CLA Document and I hereby sign the CLA`, and records your signature for all
later pull requests. Contributions must not include code copied from
projects whose licence is incompatible with the AGPL-3.0 or with commercial relicensing (for
example code under a licence that forbids sublicensing); name the source and licence of any
third-party code in the pull request.

