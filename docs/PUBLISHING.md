# Publishing `ngs-agent` to PyPI

The project name in `pyproject.toml` is `ngs-agent`, so once it is published anyone
can install with `pip install ngs-agent` (or `pip install NGS-Agent` — pip
normalises case/dashes automatically).

**Why this matters:** `pip install ngs-agent` currently fails everywhere because
the package has never been uploaded. PyPI names are first-come-first-served, so
publish the name soon before someone else takes it.

---

## Route A — Publish from your laptop (one-time, fastest)

### 1. Create a PyPI account and API token

1. Create an account at <https://pypi.org/account/register/> (and optionally the
   same username at <https://test.pypi.org> for the dry run below).
2. Go to **Account settings → API tokens → Add API token**.
3. Scope: **"Project: ngs-agent"** (narrowest). Copy the token — it starts with
   `pypi-` and is only shown once. Treat it like a password.

### 2. Build the distributions locally

```bash
cd /home/user/NGS-Agent
python -m venv .venv-publish && . .venv-publish/bin/activate
python -m pip install --upgrade pip build twine
python -m build          # creates dist/ngs_agent-0.2.1-py3-none-any.whl + .tar.gz
```

Inspect the wheel *before* uploading:

```bash
python -m zipfile -l dist/*.whl | less
```

Check that the five YAML signature files are inside `ngs_agent/signatures/`
(the `watch` command needs them at runtime).

### 3. Dry run on TestPyPI (recommended)

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-xxxxxxxxxxxx        # your token

twine upload --repository testpypi dist/*
```

Then verify in a **clean** environment (not your dev env):

```bash
python -m venv /tmp/verify && . /tmp/verify/bin/activate
pip install --index-url https://test.pypi.org/simple/ ngs-agent
ngsagent --version
ngsagent watch --help
```

> TestPyPI needs its own token, and by default it will not have uploaded your
> runtime dependencies — install those from PyPI first if the test install says
> a dependency is missing.

### 4. Upload to real PyPI

```bash
twine upload dist/*
```

### 5. Verify the real install

```bash
python -m venv /tmp/verify && . /tmp/verify/bin/activate
pip install ngs-agent
ngsagent watch demo_data/sample.log   # (once demo data is bundled — see notes)
```

---

## Route B — Automate with GitHub Actions + Trusted Publishing (recommended)

Trusted Publishing means PyPI gives GitHub permission to upload on your behalf —
**no password or token is stored in GitHub**.

1. Add the release workflow (already provided at
   `.github/workflows/publish.yml`):

   ```yaml
   # .github/workflows/publish.yml
   name: Publish to PyPI

   on:
     push:
       tags: ["v*"]
     workflow_dispatch: {}

   permissions:
     id-token: write   # required for Trusted Publishing
     contents: read

   jobs:
     publish:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: "3.12"
         - name: Install build tooling
           run: python -m pip install --upgrade build
         - name: Build distributions
           run: python -m build
         - name: Publish to PyPI
           uses: pypa/gh-action-pypi-publish@release/v1
   ```

2. Connect GitHub to PyPI (one-time, ~1 minute):
   - Go to <https://pypi.org/manage/account/publishing/>
   - Click **"Add a new pending publisher"**
   - Fill in:
     - Project name: `ngs-agent`
     - Owner: `ranaalyan1`
     - Repository: `NGS-Agent`
     - Workflow name: `publish.yml`
     - Environment: leave blank (or use `pypi` if you add one)

3. Publish by tagging a release:

   ```bash
  git tag v0.2.1
  git push origin v0.2.1
   ```

   The workflow builds `dist/` and uploads it automatically. GitHub shows the
   run under **Actions → Publish to PyPI**.

> Repeat for every version: bump `version` in `pyproject.toml`, commit,
> `git tag vX.Y.Z`, push. (Consider switching the tag/version to be created by
> `release-please` later if you want fully automatic releases.)

---

## Things to fix before/at first publish (they affect users)

These are repository issues, not PyPI issues — they only become visible *after*
the package is up, so keep the same guards in CI and the wheel audit:

| Issue | Status | Guard that keeps it fixed |
|---|---|---|
| `ngs_agent/demo_data/` is bundled in the wheel | DONE | Wheel audit checks `ngs_agent/demo_data/sample.log` and `ngs_agent/demo_data/sample.vcf` are present |
| `requires-python = ">=3.9"` | DONE | Wheel metadata audit checks `Requires-Python: >=3.9` |
| `ngs_agent/__main__.py` exists | DONE | `python -m ngs_agent --version` smoke test passes from a clean install |
| Only `ngsagent` is shipped as a console script | DONE | `entry_points.txt` contains exactly one console entry point |
| CLI version is single-sourced | DONE | `ngsagent --version` matches the installed distribution version |
| `src/ngs_agent` stays out of the wheel | DONE | Wheel audit fails if any `src/ngs_agent/*` file appears in the archive |

---

## Release checklist

1. All tests green: `python -m pytest -m "not integration"`
2. `python -m build --wheel` succeeds and the wheel audit passes (`ngs_agent/demo_data/*`, `ngs_agent/__main__.py`, `ngs_agent/signatures/*.yaml`, only `ngsagent` in `entry_points.txt`)
3. Clean-venv install + smoke test (`ngsagent --version`, `python -m ngs_agent --version`, `ngsagent demo`, `watch`, `analyze`)
4. Bump version to `0.2.1`, commit, tag `v0.2.1`, push tag
5. Confirm the Actions run published successfully
6. `pip install ngs-agent` from a clean venv resolves `0.2.1` and the demo smoke test passes
