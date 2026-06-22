"""aprilcam.client.host_codes — bijective base-26 codec and host/camera store.

Provides a compact, memorable addressing scheme for lab-scale deployments
(≤26 hosts, ≤26 cameras per host):

  - **Local host cameras** use a single letter: A, B, C, …
  - **Remote host cameras** use host-letter + camera-letter: FA, FB, …

The mapping is 1-indexed bijective base-26 (A=1 … Z=26, AA=27, AB=28, …).

Store
-----
The host store is persisted as ``<data_dir>/hosts.json``.  It tracks:

- Every host that has been probed (``aprilcam probe``).
- The stable numeric identifier assigned to each host.
- The cameras enumerated from each host's daemon, with stable numeric IDs.

Stability guarantee: once a host/camera pair is assigned a number, re-running
``aprilcam probe`` keeps that number even if new hosts/cameras appear.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aprilcam.config import Config


# ---------------------------------------------------------------------------
# Bijective base-26 codec
# ---------------------------------------------------------------------------

_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def num_to_alpha(n: int) -> str:
    """Convert a positive integer *n* to bijective base-26 (A=1, Z=26, AA=27).

    Args:
        n: Positive integer (≥1).

    Returns:
        Upper-case alphabetic string.

    Raises:
        ValueError: When *n* < 1.

    Examples:
        >>> num_to_alpha(1)
        'A'
        >>> num_to_alpha(26)
        'Z'
        >>> num_to_alpha(27)
        'AA'
        >>> num_to_alpha(52)
        'AZ'
        >>> num_to_alpha(702)
        'ZZ'
    """
    if n < 1:
        raise ValueError(f"num_to_alpha requires n ≥ 1, got {n!r}")
    digits: list[str] = []
    while n > 0:
        n, r = divmod(n - 1, 26)
        digits.append(_ALPHA[r])
    return "".join(reversed(digits))


def alpha_to_num(s: str) -> int:
    """Convert a bijective base-26 string back to a positive integer.

    Args:
        s: Upper- or lower-case alphabetic string (e.g. ``"A"``, ``"AA"``).

    Returns:
        Corresponding positive integer.

    Raises:
        ValueError: When *s* is empty or contains non-alphabetic characters.

    Examples:
        >>> alpha_to_num('A')
        1
        >>> alpha_to_num('Z')
        26
        >>> alpha_to_num('AA')
        27
    """
    s = s.upper()
    if not s or not s.isalpha():
        raise ValueError(f"alpha_to_num requires a non-empty alpha string, got {s!r}")
    result = 0
    for ch in s:
        result = result * 26 + (_ALPHA.index(ch) + 1)
    return result


# ---------------------------------------------------------------------------
# Code helpers
# ---------------------------------------------------------------------------


def code_for(host_num: int, cam_num: int, is_local: bool) -> str:
    """Return the short code for a camera on a host.

    Local (``is_local=True``) cameras are a single letter:
    camera 1 → ``A``, camera 2 → ``B``, …

    Remote cameras are host-letter + camera-letter:
    host 6, camera 2 → ``FB``.

    Args:
        host_num: 1-indexed host number from the store.
        cam_num: 1-indexed camera number within the host.
        is_local: True when this is the local host (num 1 by convention).

    Returns:
        Short alpha code string (1 or 2 characters for ≤26 hosts/cameras).
    """
    if is_local:
        return num_to_alpha(cam_num)
    return num_to_alpha(host_num) + num_to_alpha(cam_num)


def resolve_code(
    code: str,
    store: dict,
) -> tuple[dict, dict]:
    """Resolve a camera code to (host_entry, camera_entry) from *store*.

    A single-letter code refers to the local host's camera with that number.
    A two-letter code refers to host ``alpha_to_num(code[0])``'s camera
    ``alpha_to_num(code[1])``.

    Args:
        code: 1 or 2 upper-case letter camera code (e.g. ``"A"``, ``"FB"``).
        store: Store dict as returned by :func:`load_store`.

    Returns:
        ``(host_entry, camera_entry)`` dicts from the store.

    Raises:
        ValueError: When the code format is invalid, or the referenced host
            or camera is not found in the store.
    """
    code = code.upper()
    if not code or not code.isalpha() or len(code) > 2:
        raise ValueError(
            f"Camera code must be 1 or 2 alphabetic characters, got {code!r}"
        )

    hosts: list[dict] = store.get("hosts", [])

    if len(code) == 1:
        # Local host: first host with kind="local", or num=1
        cam_num = alpha_to_num(code)
        local_host = next(
            (h for h in hosts if h.get("kind") == "local"),
            next((h for h in hosts if h.get("num") == 1), None),
        )
        if local_host is None:
            raise ValueError(
                f"Code {code!r}: no local host in store. Run 'aprilcam probe' first."
            )
        cameras: list[dict] = local_host.get("cameras", [])
        cam = next((c for c in cameras if c.get("num") == cam_num), None)
        if cam is None:
            raise ValueError(
                f"Code {code!r}: camera #{cam_num} not found on local host. "
                f"Run 'aprilcam probe' to refresh."
            )
        return local_host, cam

    # Two-letter: host + camera
    host_num = alpha_to_num(code[0])
    cam_num = alpha_to_num(code[1])
    host_entry = next((h for h in hosts if h.get("num") == host_num), None)
    if host_entry is None:
        raise ValueError(
            f"Code {code!r}: host #{host_num} ('{code[0]}') not found in store. "
            f"Run 'aprilcam probe' to refresh."
        )
    cameras = host_entry.get("cameras", [])
    cam = next((c for c in cameras if c.get("num") == cam_num), None)
    if cam is None:
        raise ValueError(
            f"Code {code!r}: camera #{cam_num} not found on host "
            f"'{host_entry.get('host')}'. Run 'aprilcam probe' to refresh."
        )
    return host_entry, cam


def resolve_host_token(token: str, store: dict) -> str:
    """Resolve *token* to a hostname/IP if it is a single-letter host code.

    A token is a host code when it is exactly one upper-case letter AND that
    letter's numeric value corresponds to a host in the store.  Otherwise the
    token is returned unchanged (it is already a hostname or IP).

    Args:
        token: User-supplied ``--host`` value.
        store: Store dict as returned by :func:`load_store`.

    Returns:
        Resolved hostname/IP, or *token* unchanged if it is not a host code.
    """
    if not token or len(token) != 1 or not token.isalpha():
        return token

    host_num = alpha_to_num(token.upper())
    hosts: list[dict] = store.get("hosts", [])
    host_entry = next((h for h in hosts if h.get("num") == host_num), None)
    if host_entry is None:
        return token  # Not a known host code — pass through unchanged.

    # Prefer the stored hostname; fall back to first address.
    stored_host: str = host_entry.get("host", "")
    addresses: list[str] = host_entry.get("addresses", [])
    return stored_host or (addresses[0] if addresses else token)


def _norm_host(h: str | None) -> str:
    """Normalise a hostname for matching: lower-case, strip a trailing ``.local``."""
    s = (h or "").strip().lower()
    if s.endswith(".local"):
        s = s[: -len(".local")]
    return s


def find_host(
    store: dict,
    host: str | None = None,
    addresses: "list[str] | None" = None,
) -> "dict | None":
    """Find a stored host entry by hostname or address, tolerant of name forms.

    Matching is case-insensitive and treats ``vidar`` and ``vidar.local`` as the
    same host. It also matches when *host* is itself one of a stored host's
    addresses (e.g. a numeric IP), or when any of *addresses* overlaps a stored
    host's addresses.

    Args:
        store: Store dict as returned by :func:`load_store`.
        host: Hostname or IP to match (any form).
        addresses: Optional list of addresses to match against.

    Returns:
        The matching host entry dict, or ``None``.
    """
    nh = _norm_host(host) if host else ""
    addrset = set(addresses or [])
    if host:
        addrset.add(host)
    for h in store.get("hosts", []):
        if nh and _norm_host(h.get("host", "")) == nh:
            return h
        if addrset & set(h.get("addresses", [])):
            return h
    return None


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------

_STORE_FILENAME = "hosts.json"
_STORE_VERSION = 1


def _store_path(config: "Config") -> "from pathlib import Path; Path":
    """Return the absolute path of the hosts store file."""
    from pathlib import Path
    return Path(config.data_dir) / _STORE_FILENAME


def load_store(config: "Config") -> dict:
    """Load the hosts store from disk, returning an empty store on missing file.

    Args:
        config: Loaded :class:`~aprilcam.config.Config` instance (provides
            ``data_dir``).

    Returns:
        Store dict with schema ``{"version": 1, "hosts": [...]}``.
    """
    path = _store_path(config)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("store root is not a dict")
        return data
    except FileNotFoundError:
        return {"version": _STORE_VERSION, "hosts": []}
    except Exception:
        # Corrupt store: start fresh rather than crashing.
        return {"version": _STORE_VERSION, "hosts": []}


def save_store(config: "Config", store: dict) -> None:
    """Atomically write *store* to the hosts store file.

    Uses a temp-file + ``os.replace`` so partial writes are never visible.

    Args:
        config: Loaded :class:`~aprilcam.config.Config` instance.
        store: Store dict to persist.
    """
    path = _store_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(store, indent=2, ensure_ascii=False)
    # Write to a sibling temp file then atomically replace.
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".hosts_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Store merge (used by aprilcam probe)
# ---------------------------------------------------------------------------


def merge_probe_results(
    store: dict,
    probe_results: list[dict],
) -> dict:
    """Merge *probe_results* into *store* with stable numeric assignments.

    *probe_results* is a list of dicts, each describing one probed daemon::

        {
            "host": "vidar.local",
            "addresses": ["192.168.1.144"],
            "kind": "local" | "remote",
            "cameras": [
                {"enum": 6, "index": 0, "name": "imx296-88000", "slug": "imx296-88000"},
                ...
            ],
        }

    Stability rules:

    - The local host (``kind="local"``) is ALWAYS ``num=1`` (letter ``A``) and
      always listed first. ``num=1`` is reserved for it — if a remote currently
      holds 1 (e.g. it was probed before the local daemon existed) it is bumped.
    - Remote hosts are numbered from ``2`` (letter ``B``) upward.
    - A host already in the store is matched by ``host`` field or by any
      overlapping ``addresses``.  Matched remote hosts keep their existing
      ``num`` (≥2); new remotes get the next free number ≥2.
    - Within a host, cameras are matched by ``slug``; matched cameras keep
      their ``num``.  New cameras get the next free ``num`` on that host.

    Args:
        store: Existing store (may be empty).
        probe_results: List of probe result dicts (see above).

    Returns:
        Updated store dict.  The caller is responsible for saving it.
    """
    existing_hosts: list[dict] = store.get("hosts", [])

    # Remote hosts are numbered from 2 upward; num 1 (letter A) is reserved for
    # the local host, so the local daemon is ALWAYS "A" and always listed first.
    used_remote: set[int] = {
        h["num"]
        for h in existing_hosts
        if h.get("kind") != "local" and isinstance(h.get("num"), int) and h["num"] >= 2
    }

    def _next_remote_num() -> int:
        n = 2
        while n in used_remote:
            n += 1
        used_remote.add(n)
        return n

    def _find_existing_host(probe: dict) -> dict | None:
        """Match *probe* against an existing host by hostname (``.local``-tolerant)
        or overlapping addresses, so re-probing never duplicates a host."""
        return find_host(
            {"hosts": existing_hosts},
            host=probe.get("host", ""),
            addresses=probe.get("addresses", []),
        )

    updated_hosts: list[dict] = list(existing_hosts)  # start with existing

    for probe in probe_results:
        is_local = probe.get("kind") == "local"
        p_cameras: list[dict] = probe.get("cameras", [])

        existing = _find_existing_host(probe)

        if existing is not None:
            # Update addresses and cameras in-place on the matched entry.
            existing["addresses"] = list(
                set(existing.get("addresses", [])) | set(probe.get("addresses", []))
            )
            existing["kind"] = probe.get("kind", existing.get("kind", "remote"))
            if is_local:
                existing["num"] = 1
            elif not isinstance(existing.get("num"), int) or existing["num"] < 2:
                existing["num"] = _next_remote_num()
            _merge_cameras(existing, p_cameras)
        else:
            new_num = 1 if is_local else _next_remote_num()
            new_host: dict = {
                "num": new_num,
                "kind": probe.get("kind", "remote"),
                "host": probe.get("host", ""),
                "addresses": list(probe.get("addresses", [])),
                "cameras": [],
            }
            _merge_cameras(new_host, p_cameras)
            updated_hosts.append(new_host)

    # Invariant: the local host is always num 1 (letter A). Force it to 1 and
    # bump any OTHER host that holds 1 (e.g. a remote probed before the local
    # daemon existed) to the next free remote number.
    local = next((h for h in updated_hosts if h.get("kind") == "local"), None)
    if local is not None:
        for h in updated_hosts:
            if h is not local and h.get("num") == 1:
                h["num"] = _next_remote_num()
        local["num"] = 1

    # Sort by num for stable JSON output.
    updated_hosts.sort(key=lambda h: h.get("num", 0))
    store["version"] = _STORE_VERSION
    store["hosts"] = updated_hosts
    return store


def _merge_cameras(host_entry: dict, probe_cameras: list[dict]) -> None:
    """Merge *probe_cameras* into the cameras list of *host_entry* in-place."""
    existing_cams: list[dict] = host_entry.setdefault("cameras", [])
    cam_nums: set[int] = {c["num"] for c in existing_cams}

    def _next_cam_num() -> int:
        return (max(cam_nums) + 1) if cam_nums else 1

    for pc in probe_cameras:
        slug = pc.get("slug", "")
        existing_cam = next(
            (c for c in existing_cams if c.get("slug") == slug), None
        )
        if existing_cam is not None:
            # Refresh mutable fields; keep stable num.
            existing_cam["enum"] = pc.get("enum", existing_cam.get("enum"))
            existing_cam["index"] = pc.get("index", existing_cam.get("index"))
            existing_cam["name"] = pc.get("name", existing_cam.get("name"))
        else:
            new_num = _next_cam_num()
            cam_nums.add(new_num)
            existing_cams.append(
                {
                    "num": new_num,
                    "enum": pc.get("enum"),
                    "index": pc.get("index"),
                    "name": pc.get("name", ""),
                    "slug": slug,
                }
            )
