"""Optional native GS2 compiler shared by servers and clients."""

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def find_compiler() -> Optional[str]:
    """Locate gs2test through GS2TEST_BIN, the checkout build, or PATH."""
    candidates = [
        os.environ.get("GS2TEST_BIN"),
        str(Path(__file__).resolve().parents[2] / "tests" / "tools" / "gs2test"),
        shutil.which("gs2test"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


class Gs2Compiler:
    """Compile source to bytes, or None, retaining diagnostics and cached results.

    The bounded per-instance cache includes failed compilations. Script names
    affect diagnostics only; identical source shares bytecode across names.
    """

    def __init__(self, binary: Optional[str] = None):
        self.binary = binary if binary is not None else find_compiler()
        self.last_error = ""
        self._cache = OrderedDict()

    @property
    def available(self) -> bool:
        return self.binary is not None

    def compile(self, source: str, name: str = "script") -> Optional[bytes]:
        self.last_error = ""
        if not self.available:
            self.last_error = "gs2test compiler unavailable"
            return None
        if not source.strip():
            self.last_error = "empty source"
            return None
        digest = hashlib.sha256(source.encode("utf-8")).digest()
        if digest in self._cache:
            self._cache.move_to_end(digest)
            result, self.last_error = self._cache[digest]
            return result
        result = None
        with tempfile.TemporaryDirectory(prefix="reborn-gs2-") as tmp:
            src_path = Path(tmp) / "in.gs2"
            out_path = Path(tmp) / "out.gs2bc"
            src_path.write_text(source, encoding="latin-1", errors="replace")
            try:
                proc = subprocess.run(
                    [self.binary, str(src_path), "-o", str(out_path)],
                    capture_output=True, text=True, timeout=30)
                if proc.returncode != 0 or not out_path.exists():
                    self.last_error = ((proc.stderr or proc.stdout).strip()
                                       or "compiler produced no bytecode")
                else:
                    result = out_path.read_bytes()
            except (OSError, subprocess.SubprocessError) as exc:
                self.last_error = str(exc)
        if self.last_error:
            self.last_error = self.last_error.splitlines()[0][:300]
            logger.debug("GS2 compile of %s failed: %s", name, self.last_error)
        self._cache[digest] = (result, self.last_error)
        if len(self._cache) > 256:
            self._cache.popitem(last=False)
        return result
