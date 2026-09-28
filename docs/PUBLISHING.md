# Release and packaging

The package is named `ngs-agent`; its only console script is `ngs`. Runtime dependencies are listed in `pyproject.toml`. The optional `box` extra adds the web interface dependencies, and the `dev` extra adds tests and development tools.

## Build and inspect

```bash
python -m pip install --upgrade build
python -m build
```

Check that the wheel contains the `core/` and `doors/` packages, rule-signature YAML files, and the Box HTML page. Install the artifact in a clean virtual environment and check the command:

```bash
python -m venv /tmp/ngs-verify
/tmp/ngs-verify/bin/pip install dist/*.whl
/tmp/ngs-verify/bin/ngs --help
```

## Release checks

Before publishing a release, run the full test suite, build the Docker image, and smoke-test both the `ngs` CLI and the Box on port 8000. Confirm that package metadata, `core/version.py`, the changelog, and release notes agree. PyPI and GitHub publishing are explicit release-owner actions; this document does not publish anything.

The current supported installation examples are maintained in [README.md](../README.md) and [CLI_USER_GUIDE.md](../CLI_USER_GUIDE.md).
