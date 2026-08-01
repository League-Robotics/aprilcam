"""The client-visible error taxonomy.

Every distinction a use case asserts about failure is a **type here,
not a message string**. The scenarios are explicit that failing must be
distinguishable from finding nothing (UC-13.S-5), that an unknown name
must list the alternatives (UC-27.S-3, UC-28.S-5), and that a rejected
push must name what was wrong (UC-03.S-3, UC-23.S-4). An adapter can
only honour those contracts if the client library raises something it
can dispatch on — so the interfaces raise these, and no adapter parses
message text.

Empty results are **not** errors and never raise: a field with no
daemons (UC-13), no tags (UC-10), or no annotations (UC-28.S-4) returns
empty. These types exist for the cases where the operation itself could
not be carried out or the request itself was wrong.
"""

from __future__ import annotations

from typing import Optional


class AprilCamError(Exception):
    """Base for every error the client library raises deliberately."""


class DiscoveryFailed(AprilCamError):
    """Discovery itself could not run — e.g. multicast blocked.

    Deliberately distinct from discovery finding nothing, which is an
    empty result (UC-13.S-5). Collapsing the two sends someone hunting
    for a dead daemon instead of a network problem.
    """


class ConnectionFailed(AprilCamError):
    """A named daemon did not answer.

    Carries the host so the error can say *which* daemon — during
    fan-out, one unreachable daemon is reported alongside the results
    that did come back, not allowed to fail the whole command.
    """

    def __init__(self, host: str, reason: Optional[str] = None) -> None:
        self.host = host
        self.reason = reason
        super().__init__(f"daemon at {host!r} did not answer"
                         + (f": {reason}" if reason else ""))


class UnknownTarget(AprilCamError):
    """A name the daemon does not know, with the names it does.

    The contract every unknown-name scenario asserts: the error lists
    the valid alternatives rather than leaving the caller to guess
    (UC-27.S-3, UC-28.S-5, UC-14's absent-camera distinction).
    """

    def __init__(self, requested: str, available: tuple[str, ...]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"unknown {self.kind} {requested!r}; available: "
            + (", ".join(available) or "(none)")
        )

    kind = "target"


class UnknownCamera(UnknownTarget):
    kind = "camera"


class UnknownPlayfield(UnknownTarget):
    kind = "playfield"


class UnknownGuide(UnknownTarget):
    kind = "guide"


class CameraNotPresent(AprilCamError):
    """A camera the registry knows -- addressed by slug or by its
    persistent number -- is not currently enumerated among live
    backends (UC-14, UC-25).

    Distinct from :class:`UnknownCamera`: the registry has a record for
    this camera, it simply is not present right now (unplugged, or a
    transient enumeration miss). A registry record outliving its
    hardware is the normal case (UC-25), not a rejection -- this type
    lets a caller distinguish "never registered" from "registered, but
    not here," which ``UnknownCamera`` alone could not (UC-14.S-4-ish
    reasoning, extended to number addressing by ticket 021-002).
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"camera {slug!r} is registered but not currently present")


class NotCalibrated(AprilCamError):
    """A deskewed or world-coordinate product was asked of an
    uncalibrated camera (UC-19).

    Raised instead of silently substituting a raw frame or pixel
    coordinates — a plausible-looking wrong answer is the failure mode
    this type exists to kill.
    """


class UnsupportedSetting(AprilCamError):
    """A config push named a key the hardware does not support or
    rejected (UC-03).

    The **whole push** fails with this, naming the offending key; the
    camera is left exactly as it was. Never a partial apply.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"unsupported or rejected setting: {key!r}")


class MalformedDocument(AprilCamError):
    """A configuration document was rejected whole (UC-02.S-2,
    UC-23.S-4).

    Includes the self-naming rule: a document without a ``name`` is
    malformed — the target is never inferred from the filename. Raised
    client-side by the loader where possible, so a bad file never even
    reaches the daemon.
    """


class InvalidTagId(AprilCamError):
    """A tag id outside its family's dictionary range (UC-21.S-3).

    Names the valid range; nothing is generated or written.
    """

    def __init__(self, family: str, tag_id: int, valid: range) -> None:
        self.family = family
        self.tag_id = tag_id
        self.valid = valid
        super().__init__(
            f"{family} id {tag_id} outside valid range "
            f"{valid.start}..{valid.stop - 1}"
        )


class AlreadyRunning(AprilCamError):
    """The singleton lock is held; names the holder (UC-13)."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        super().__init__(f"a daemon is already running: {holder}")
