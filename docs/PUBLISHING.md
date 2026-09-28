# Publishing `ngs-agent` to PyPI

The project name in `pyproject.toml` is `ngs-agent`; anyone can install with
`pip install ngs-agent` (or `pip install NGS-Agent` — pip normalises
case/dashes automatically).

The v1 line has no LLM and no network calls at runtime: the wheel ships the
`core/` interpreter, the `doors/` CLI and Box, the YAML log signatures, and
the Box page.

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
cd NGS-Agent
python -m venv .venv-publish && . .venv-publish/bin/activate
python -m pip install --upgrade pip build twine
python -m build          # creates dist/*.whl + .tar.gz
```

Inspect the wheel *before* uploading:

```bash
python -m zipfile -l dist/*.whl | less
```

Check that the YAML signature files are inside `core/signatures/`
(the log diagnoser needs them at runtime) and that `doors/gui/index.html`
(the Box page) is in the wheel.

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
ngs --version
ngs --json path/to/sample_fastqc.zip
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
pip install "ngs-agent[box]"
ngs --version
ngs fixtures/fastqc/sample_fastqc.zip
```

---

## Route B — Automate with GitHub Actions + Trusted Publishing (recommended)

Trusted Publishing means PyPI gives GitHub permission to upload on your behalf —
**no password or token is stored in GitHub**.

1. The release workflow lives at `publish.yml` under `.github/`: it builds
   with `python -m build` and uploads with
   `pypa/gh-action-pypi-publish@release/v1` when a `v*` tag is pushed
   (or when dispatched manually).

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
   git tag v1.1.0
   git push origin v1.1.0
   ```

   The workflow builds `dist/` and uploads it automatically. GitHub shows the
   run under **Actions → Publish to PyPI**.

> Repeat for every version: bump `version` in `pyproject.toml` (and
> `core/version.py`), commit, `git tag vX.Y.Z`, push. (Consider switching the
> tag/version to be created by `release-please` later if you want fully
> automatic releases.)

---

## Release checklist

1. All tests green: `python -m pytest` (the receipts audit must print
   `0 findings without a valid receipt`)
2. Lint/typecheck green: `ruff check core doors tests scripts conftest.py`,
   `mypy core doors`
3. `python -m build` succeeds and the wheel contains `core/signatures/*.yaml`
   and `doors/gui/index.html`
4. Clean-venv install + smoke test (`ngs --version`, `ngs <fixture> --json`)
5. Bump version, commit, tag `vX.Y.Z`, push tag
6. Confirm the Actions run published successfully
7. `pip install ngs-agent` from a clean venv works
