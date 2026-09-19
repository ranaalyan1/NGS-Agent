"""Adapter registry and evidence-retrieval configuration.

The registry is the single place where a deployment declares *which evidence
sources exist*. Nothing in the engine reaches out to a source directly, so an
air-gapped installation is produced by listing only local adapters — not by
patching code or setting a "disable network" flag that some path might forget
to honour.

Configuration is a plain dataclass that hashes deterministically
(:func:`ngs_agent.core.audit.configuration_hash`), because the audit record has
to say which sources were enabled when a decision was made.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from ngs_agent.core.errors import CoreError
from ngs_agent.core.evidence.adapters.clinvar import ClinVarAdapter
from ngs_agent.core.evidence.adapters.gene_mechanism import GeneMechanismAdapter, GeneMechanismTable
from ngs_agent.core.evidence.adapters.offline_pack import OfflineEvidencePackAdapter
from ngs_agent.core.evidence.base import AdapterDeclaration, EvidenceAdapter
from ngs_agent.core.evidence.cache import EvidenceCache
from ngs_agent.core.evidence.models import utc_now
from ngs_agent.core.evidence.recordings import (
    DEFAULT_CLINVAR_RECORDINGS,
    RecordingManifest,
    load_recordings,
)
from ngs_agent.core.evidence.transport import HttpTransport


class RegistryError(CoreError, ValueError):
    """An adapter configuration is invalid or names an unknown adapter."""


#: Adapters that require no network and are always safe to enable.
LOCAL_ADAPTERS: tuple[str, ...] = (
    "gene_mechanism",
    "offline_pack",
    "recorded_clinvar",
    "snapshot",
)

#: Adapters that contact an external service.
NETWORK_ADAPTERS: tuple[str, ...] = ("clinvar",)


@dataclass(frozen=True)
class EvidenceConfiguration:
    """Declarative description of the evidence sources for a run."""

    adapters: tuple[str, ...] = ("gene_mechanism",)
    #: Path to an offline evidence pack, if the ``offline_pack`` adapter is enabled.
    offline_pack_path: Path | None = None
    offline_pack_name: str = "offline_evidence_pack"
    #: Path to a curated gene/mechanism table override.
    gene_mechanism_table_path: Path | None = None
    #: Cache directory. ``None`` disables caching.
    cache_dir: Path | None = None
    cache_max_age_hours: int | None = None
    #: Directory of recorded E-utilities responses for the ``recorded_clinvar``
    #: adapter. ``None`` means the bundled recordings shipped with the package.
    recordings_dir: Path | None = None
    #: Explicit ClinVar release stamp. Required for live retrieval, because a
    #: record that does not say which release it came from cannot be reproduced.
    #: Defaults to ``eutils-live:<retrieval date>``.
    clinvar_source_version: str | None = None
    #: Whether network adapters are permitted at all. Air-gapped deployments set
    #: this False and the registry refuses to construct them.
    allow_network: bool = False
    ncbi_api_key: str | None = None
    ncbi_email: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_hashable(self) -> dict[str, Any]:
        """A JSON-able, secret-free representation for the configuration hash.

        API keys are replaced by a boolean so the audit record proves a key was
        configured without storing it.
        """
        return {
            "adapters": list(self.adapters),
            "offline_pack_path": str(self.offline_pack_path) if self.offline_pack_path else None,
            "offline_pack_name": self.offline_pack_name,
            "gene_mechanism_table_path": (
                str(self.gene_mechanism_table_path) if self.gene_mechanism_table_path else None
            ),
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
            "cache_max_age_hours": self.cache_max_age_hours,
            "recordings_dir": str(self.recordings_dir) if self.recordings_dir else None,
            "clinvar_source_version": self.clinvar_source_version,
            "allow_network": self.allow_network,
            "ncbi_api_key_configured": self.ncbi_api_key is not None,
            "ncbi_email_configured": self.ncbi_email is not None,
        }


class EvidenceRegistry:
    """Builds and holds the adapters for one run."""

    def __init__(
        self,
        configuration: EvidenceConfiguration,
        *,
        transport: HttpTransport | None = None,
        clock: Any = None,
        extra_adapters: Sequence[EvidenceAdapter] = (),
    ) -> None:
        self.configuration = configuration
        self._transport = transport
        self._clock = clock
        self._adapters: list[EvidenceAdapter] = []
        self._cache: EvidenceCache | None = None
        self._recording_manifest: RecordingManifest | None = None
        if configuration.cache_dir is not None:
            from datetime import timedelta

            self._cache = EvidenceCache(
                configuration.cache_dir,
                max_age=(
                    timedelta(hours=configuration.cache_max_age_hours)
                    if configuration.cache_max_age_hours
                    else None
                ),
            )
        self._build(extra_adapters)

    def _build(self, extra_adapters: Sequence[EvidenceAdapter]) -> None:
        for name in self.configuration.adapters:
            self._adapters.append(self._construct(name))
        self._adapters.extend(extra_adapters)
        seen: set[str] = set()
        for adapter in self._adapters:
            key = f"{adapter.declaration.name}:{adapter.declaration.adapter_version}"
            if key in seen:
                raise RegistryError(
                    f"Adapter {adapter.declaration.name} (v{adapter.declaration.adapter_version}) "
                    "is registered twice. Duplicate sources would double-count evidence."
                )
            seen.add(key)

    def _construct(self, name: str) -> EvidenceAdapter:
        config = self.configuration
        if name == "gene_mechanism":
            table = (
                GeneMechanismTable.load(config.gene_mechanism_table_path)
                if config.gene_mechanism_table_path
                else GeneMechanismTable.load()
            )
            return GeneMechanismAdapter(table, clock=self._clock)
        if name == "offline_pack":
            if config.offline_pack_path is None:
                raise RegistryError(
                    "The 'offline_pack' adapter is enabled but no offline_pack_path was supplied."
                )
            return OfflineEvidencePackAdapter(
                config.offline_pack_path,
                pack_name=config.offline_pack_name,
                clock=self._clock,
            )
        if name == "clinvar":
            if not config.allow_network:
                raise RegistryError(
                    "The 'clinvar' adapter requires network egress, but allow_network is False. "
                    "This deployment is configured for offline operation; use 'recorded_clinvar' "
                    "or 'offline_pack' instead."
                )
            now = self._clock() if callable(self._clock) else utc_now()
            return ClinVarAdapter(
                source_version=config.clinvar_source_version
                or f"eutils-live:{now.date().isoformat()}",
                transport=self._transport,
                clock=self._clock,
                api_key=config.ncbi_api_key,
                email=config.ncbi_email,
            )
        if name == "recorded_clinvar":
            directory = config.recordings_dir or DEFAULT_CLINVAR_RECORDINGS
            transport, manifest = load_recordings(directory)
            self._recording_manifest = manifest
            return ClinVarAdapter(
                source_version=config.clinvar_source_version
                or manifest.clinvar_release
                or f"recorded:{manifest.recorded_at}",
                transport=transport,
                clock=self._clock,
                api_key=config.ncbi_api_key,
                email=config.ncbi_email,
                # A recording is local data: throttling it would only slow tests
                # down without protecting a shared public resource.
                min_request_interval=timedelta(0),
                requires_network=False,
            )
        if name == "snapshot":
            raise RegistryError(
                "The 'snapshot' adapter is constructed by the replay path, not by configuration."
            )
        raise RegistryError(
            f"Unknown evidence adapter {name!r}. Known: "
            f"{', '.join(sorted((*LOCAL_ADAPTERS, *NETWORK_ADAPTERS)))}."
        )

    @property
    def adapters(self) -> tuple[EvidenceAdapter, ...]:
        return tuple(self._adapters)

    @property
    def cache(self) -> EvidenceCache | None:
        return self._cache

    def declarations(self) -> list[AdapterDeclaration]:
        return [adapter.declaration for adapter in self._adapters]

    @property
    def recording_manifest(self) -> RecordingManifest | None:
        """Manifest of the loaded recordings, when ``recorded_clinvar`` is enabled."""
        return self._recording_manifest

    def declarations_as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "adapter_version": item.adapter_version,
                "data_types": [data_type.value for data_type in item.data_types],
                "endpoint": item.endpoint,
                "license": item.license,
                "requires_network": item.requires_network,
                "hosted_by": item.hosted_by,
                "notes": item.notes,
            }
            for item in self.declarations()
        ]
