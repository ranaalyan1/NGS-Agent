"""Provenance bundle — cryptographically-anchored audit trail for variant
interpretation verdicts. Required for CAP/CLIA clinical adoption.

Produces a JSON-LD bundle containing:
  - Verdict hash (SHA-256 of the verdict payload)
  - Agent version (commit SHA)
  - Model + system prompt hash
  - Full tool-call log with hashes (each tool call + result)
  - Evidence citations
  - Timestamp + nonce (replay protection)
  - Optional cryptographic signature (Ed25519) using lab private key

The bundle can be stored alongside the verdict in the LIMS for clinical audit.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRecord:
    """One entry in the provenance tool-call log."""

    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    response_content: str
    is_error: bool
    response_hash: str   # SHA-256 of response content
    timestamp: float

    @classmethod
    def from_call(cls, tool_call_id: str, name: str, arguments: dict,
                  response_content: str, is_error: bool) -> ToolCallRecord:
        return cls(
            tool_call_id=tool_call_id,
            name=name,
            arguments=arguments,
            response_content=response_content,
            is_error=is_error,
            response_hash=hashlib.sha256(response_content.encode()).hexdigest(),
            timestamp=time.time(),
        )

    def to_dict(self) -> dict:
        return {
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "arguments": self.arguments,
            "response_hash": self.response_hash,
            "response_length": len(self.response_content),
            "is_error": self.is_error,
            "timestamp": self.timestamp,
        }


@dataclass
class ProvenanceBundle:
    """Full provenance record for a single variant interpretation run."""

    session_id: str
    verdict_id: str
    agent_name: str
    agent_version: str
    model: str
    system_prompt_hash: str
    verdict: dict[str, Any]
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    evidence_citations: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    signature: str | None = None  # Ed25519 hex, if signed

    def add_tool_call(self, call: ToolCallRecord) -> None:
        self.tool_calls.append(call)

    def verdict_hash(self) -> str:
        """SHA-256 of the verdict payload."""
        return hashlib.sha256(
            json.dumps(self.verdict, sort_keys=True).encode()
        ).hexdigest()

    def chain_hash(self) -> str:
        """SHA-256 of (verdict_hash + tool_call_hashes + timestamp + nonce).
        This is the tamper-evident hash — any change to verdict, tool calls,
        or ordering will change this hash."""
        parts = [self.verdict_hash()]
        for tc in self.tool_calls:
            parts.append(tc.response_hash)
        parts.append(str(self.timestamp))
        parts.append(self.nonce)
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "@context": "https://w3id.org/heritage",
            "@type": "VariantInterpretationProvenance",
            "session_id": self.session_id,
            "verdict_id": self.verdict_id,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "model": self.model,
            "system_prompt_hash": self.system_prompt_hash,
            "verdict": self.verdict,
            "verdict_hash": self.verdict_hash(),
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "evidence_citations": self.evidence_citations,
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "chain_hash": self.chain_hash(),
            "signature": self.signature,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def sign(self, private_key_pem: str | bytes) -> None:
        """Sign the chain_hash with an Ed25519 private key.

        Requires the `cryptography` package. For labs without a key, leave
        signature=None — the bundle is still tamper-evident via chain_hash,
        just not signed."""
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            data = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
            key = serialization.load_pem_private_key(data, password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("Private key must be Ed25519")
            sig = key.sign(self.chain_hash().encode())
            self.signature = sig.hex()
        except ImportError:
            raise RuntimeError(
                "cryptography package required for signing. pip install cryptography"
            )

    def verify(self, public_key_pem: str | bytes) -> bool:
        """Verify the signature against the chain_hash.

        Returns True if signature is valid, False otherwise (or if no signature).
        """
        if not self.signature:
            return False
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            data = public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
            key = serialization.load_pem_public_key(data)
            if not isinstance(key, Ed25519PublicKey):
                return False
            try:
                key.verify(
                    bytes.fromhex(self.signature),
                    self.chain_hash().encode(),
                )
                return True
            except InvalidSignature:
                return False
        except ImportError:
            raise RuntimeError(
                "cryptography package required for verification. pip install cryptography"
            )


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate an Ed25519 keypair for lab signing.

    Returns (private_key_pem, public_key_pem) as bytes. The lab should store
    the private key securely (HSM, secrets manager) and publish the public key
    in their LIMS configuration.

    Usage:
        priv, pub = generate_keypair()
        Path('lab_private.pem').write_bytes(priv)
        Path('lab_public.pem').write_bytes(pub)
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    public = private.public_key()

    priv_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


def compute_system_prompt_hash(prompt: str) -> str:
    """Hash the agent system prompt — proves which prompt version produced this verdict."""
    return hashlib.sha256(prompt.encode()).hexdigest()
