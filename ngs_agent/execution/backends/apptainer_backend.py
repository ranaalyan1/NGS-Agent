from __future__ import annotations

import shutil

from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.backends.containers import collect_bind_roots, resolve_image
from ngs_agent.execution.backends.process import run_streaming_process
from ngs_agent.execution.models import CommandResult, CommandSpec


class ApptainerBackend(ExecutionBackend):
    """Runs commands inside Apptainer/Singularity containers.

    Apptainer (formerly Singularity) is the container runtime of choice on
    shared HPC systems: it runs user-space, honours bind mounts, and can pull
    BioContainers images directly (``docker://`` URIs are supported). Commands
    are wrapped as ``apptainer exec --bind ... <image> <argv>`` with the same
    mount-at-identical-path strategy as the Docker backend.
    """

    name = "apptainer"

    def __init__(self, apptainer_binary: str | None = None) -> None:
        # Prefer apptainer, fall back to singularity for older clusters.
        if apptainer_binary is None:
            apptainer_binary = "apptainer" if shutil.which("apptainer") else "singularity"
        self._binary = apptainer_binary

    def is_available(self) -> bool:
        return shutil.which("apptainer") is not None or shutil.which("singularity") is not None

    def build_command(self, spec: CommandSpec) -> list[str]:
        """Return the full ``apptainer exec ...`` argv (also used in tests)."""
        image = resolve_image(spec)
        if not image:
            raise RuntimeError(
                f"No container image configured for command "
                f"'{spec.argv[0] if spec.argv else '<empty>'}'. "
                "Set NGS_CONTAINER_IMAGE / NGS_APPTAINER_IMAGE, add 'image' to the command "
                "metadata, or extend BIOCONTAINER_IMAGES."
            )
        # Allow an apptainer-specific override after the generic resolution.
        image = spec.metadata.get("image") or image
        wrapped: list[str] = [self._binary, "exec"]
        binds = collect_bind_roots(spec)
        if spec.cwd and spec.cwd not in binds:
            binds.append(spec.cwd)
        if binds:
            wrapped.extend(["--bind", ",".join(binds)])
        if spec.cwd:
            wrapped.extend(["--pwd", spec.cwd])
        for key, value in spec.env.items():
            wrapped.extend(["--env", f"{key}={value}"])
        wrapped.append(image)
        wrapped.extend(spec.argv)
        return wrapped

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        wrapped = self.build_command(spec)
        env: dict[str, str] = {}
        # Apptainer >= 1.1 supports --env; singularity needs prefixed env vars.
        if self._binary == "singularity":
            for key, value in spec.env.items():
                env[f"SINGULARITYENV_{key}"] = value

        console_callback = None
        if spec.stream_output:

            def console_callback(line: str, stream_name: str) -> None:  # noqa: F811 - conditional redefinition
                style = "white" if stream_name == "stdout" else "yellow"
                console.print(line, style=style)

        result = run_streaming_process(
            wrapped,
            console=console_callback,
            cwd=spec.cwd,
            env=env or None,
            timeout_seconds=spec.timeout_seconds,
        )
        if result.timed_out:
            raise RuntimeError(
                f"Apptainer command timed out after {spec.timeout_seconds}s: {' '.join(wrapped)}"
            )
        return CommandResult(
            backend=self.name,
            command=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            metadata={"image": resolve_image(spec), "wrapped_command": wrapped},
        )
