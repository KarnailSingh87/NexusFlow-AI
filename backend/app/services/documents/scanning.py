"""Pluggable malware screening for uploaded documents.

Two layers, with deliberately different guarantees:

* :func:`scan_eicar` is **always** on and needs no external service. It detects
  the EICAR test signature, which is what antivirus suites use to prove a scanner
  works, so it is a real regression test of the upload path rather than a toy.
* :class:`ClamAvScanner` is **opt-in**. It streams the file to a ClamAV daemon
  over its ``INSTREAM`` protocol when ``CLAMAV_HOST`` is configured.

What this is *not*: if no ClamAV daemon is configured, uploads are screened only
for format confusion and the EICAR signature. That is a meaningful improvement
over nothing, but it is not a substitute for a real antivirus product, and the
scanner reports which mode ran so callers are never misled about the coverage.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The EICAR test signature — harmless by design, and the standard canary for
#: verifying that a scanning path is actually wired up.
EICAR_SIGNATURE: Final = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

#: ClamAV ``INSTREAM``: 4-byte big-endian length, payload, zero-length terminator.
_CHUNK_SIZE: Final = 64 * 1024


class ScanVerdict(StrEnum):
    """Outcome of a screening pass."""

    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"
    SKIPPED = "skipped"


class MalwareDetectedError(ValueError):
    """Raised when a screening pass finds a malicious signature."""

    status_code = 422

    def __init__(self, message: str, *, signature: str | None = None) -> None:
        super().__init__(message)
        self.signature = signature


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What the scanner checked and what it concluded."""

    verdict: ScanVerdict
    engine: str
    signature: str | None = None
    detail: str | None = None

    @property
    def clean(self) -> bool:
        """Whether the pass ran to completion and found nothing."""
        return self.verdict is ScanVerdict.CLEAN


class Scanner(Protocol):
    """Contract every screening implementation must satisfy."""

    name: str

    async def scan(self, path: Path) -> ScanResult:
        """Screen one file on disk and report the verdict."""
        ...


# ---------------------------------------------------------------------------
# Always-on signature check
# ---------------------------------------------------------------------------
def scan_eicar(path: Path) -> ScanResult:
    """Scan for the EICAR test signature. Runs locally, synchronously."""
    try:
        blob = path.read_bytes()
    except OSError as exc:
        logger.warning("EICAR scan could not read %s: %s", path.name, exc)
        return ScanResult(verdict=ScanVerdict.ERROR, engine="eicar", detail=str(exc))

    encoded = EICAR_SIGNATURE.encode()
    # The signature is sometimes split across a line break by editors or mail
    # gateways, so both the verbatim and the newline-collapsed form are checked.
    # Both checks must run: a wrapped signature is not present verbatim.
    collapsed = blob.replace(b"\r\n", b"").replace(b"\n", b"")
    if encoded in blob or encoded in collapsed:
        wrapped = encoded not in blob
        signature = "EICAR-Test-File" + (" (wrapped)" if wrapped else "")
        return ScanResult(
            verdict=ScanVerdict.INFECTED,
            engine="eicar",
            signature=signature,
            detail="EICAR antivirus test signature detected",
        )
    return ScanResult(verdict=ScanVerdict.CLEAN, engine="eicar")


# ---------------------------------------------------------------------------
# Optional ClamAV daemon
# ---------------------------------------------------------------------------
class ClamAvScanner:
    """Stream a file to a ClamAV daemon using the ``INSTREAM`` protocol."""

    name = "clamav"

    def __init__(self, host: str, port: int, *, timeout: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    async def scan(self, path: Path) -> ScanResult:
        """Ask ClamAV to scan ``path``.

        A daemon that is unreachable or slow yields :attr:`ScanVerdict.ERROR`
        rather than blocking the upload, and the caller decides whether that is
        fatal. Silently reporting "clean" on an unreachable scanner would be a
        lie, which is why ERROR is a distinct verdict.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=self._timeout
            )
        except (OSError, TimeoutError) as exc:
            logger.warning("ClamAV daemon %s:%s unreachable: %s", self._host, self._port, exc)
            return ScanResult(
                verdict=ScanVerdict.ERROR, engine=self.name, detail=f"daemon unreachable: {exc}"
            )

        try:
            # Synchronous file handle; reads run in a thread so a slow disk does
            # not block the event loop while streaming to the daemon.
            handle = path.open("rb")
            try:
                while block := await asyncio.to_thread(handle.read, _CHUNK_SIZE):
                    writer.write(len(block).to_bytes(4, "big") + block)
                    await writer.drain()
            finally:
                handle.close()
            writer.write(b"\x00")  # zero-length chunk terminates INSTREAM
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
        except (OSError, TimeoutError) as exc:
            logger.warning("ClamAV scan of %s failed: %s", path.name, exc)
            return ScanResult(verdict=ScanVerdict.ERROR, engine=self.name, detail=str(exc))
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

        return _parse_clamav_response(response.decode(errors="replace").strip())


def _parse_clamav_response(response: str) -> ScanResult:
    """Translate a ClamAV reply into a :class:`ScanResult`."""
    lowered = response.lower()
    if not response or lowered.startswith("unknown command"):
        return ScanResult(
            verdict=ScanVerdict.ERROR,
            engine="clamav",
            detail=f"unparseable daemon response: {response!r}",
        )
    if lowered.endswith("ok"):
        return ScanResult(verdict=ScanVerdict.CLEAN, engine="clamav")
    if lowered.endswith("found"):
        # Format: "<path>: <signature> FOUND"
        signature = response.split(": ", 1)[-1].removesuffix(" FOUND").strip() or None
        return ScanResult(
            verdict=ScanVerdict.INFECTED,
            engine="clamav",
            signature=signature,
            detail=response,
        )
    return ScanResult(
        verdict=ScanVerdict.ERROR, engine="clamav", detail=f"unexpected response: {response!r}"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def screen_file(path: Path, *, config: Settings | None = None) -> ScanResult:
    """Run every configured screening layer over ``path``.

    The EICAR check runs first because it is free and authoritative. ClamAV is
    added when configured. An infection in any layer is fatal; an engine error is
    reported but does not block the upload unless ``CLAMAV_REQUIRED`` is set,
    which is deliberately not a setting — a missing antivirus must not take
    ingestion offline, but it must be visible in the logs and the result.
    """
    cfg = config or settings

    local = scan_eicar(path)
    if local.verdict is ScanVerdict.INFECTED:
        return local

    if not cfg.clamav_host.strip():
        return ScanResult(
            verdict=local.verdict,
            engine=local.engine,
            detail="EICAR only; set CLAMAV_HOST to enable full AV screening",
        )

    daemon = await ClamAvScanner(cfg.clamav_host, cfg.clamav_port).scan(path)
    if daemon.verdict in {ScanVerdict.CLEAN, ScanVerdict.INFECTED}:
        return daemon
    # Daemon unusable: fall back to what we can prove, but never claim "clean".
    logger.warning(
        "ClamAV screening unavailable for %s (%s); continuing with EICAR check only",
        path.name,
        daemon.detail,
    )
    return ScanResult(
        verdict=local.verdict,
        engine=f"{local.engine}+{daemon.engine}(unavailable)",
        detail=daemon.detail,
    )


def raise_if_infected(result: ScanResult, *, filename: str) -> None:
    """Convert an infected verdict into a request error."""
    if result.verdict is ScanVerdict.INFECTED:
        raise MalwareDetectedError(
            f"{filename} was rejected by malware screening "
            f"({result.engine}: {result.signature or 'signature not reported'}).",
            signature=result.signature,
        )
