"""Inspection-based validation for ``deploy/`` (sprint 040 ticket 001).

Not a testing framework built for this sprint alone (sprint.md's Test
Strategy is explicit that deploy assets are "otherwise validated by
inspection, not a testing framework") -- just the one check named in the
ticket's acceptance criteria that adds real regression value with no
hardware dependency: **every unit file's ``ExecStart=``/``ExecStartPre=``
references a script or module path that actually exists in this repo, not
a v1-only flag or a module that doesn't exist.** ``bash -n``/``shellcheck``
on the two shell scripts is run by the programmer as a manual step (and
by ``scripts/test-gate.sh``'s own shell-lint pass, if any), not
duplicated here as a subprocess-per-test wrapper.

Also guards two easy-to-drift-apart facts a faked ``/sys`` tree or unit
test could never catch, because they are facts about *files*, not code:

* the module-config content embedded in ``provision-pi.sh`` (written
  inline so the script has no runtime dependency on the standalone
  files) matches ``deploy/modprobe.d/aprilcam.conf`` /
  ``deploy/modules-load.d/aprilcam.conf`` exactly -- both exist so a
  human can diff either one against a live host in ticket 004, and
  provision-pi.sh must actually install what those files document.
* the bridge template unit's ``ExecStart=`` invokes exactly the script
  this repo ships at ``deploy/aprilcam-camera-bridge``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_ROOT = REPO_ROOT / "deploy"
SRC_ROOT = REPO_ROOT / "src" / "aprilcam"

_EXEC_LINE_RE = re.compile(r"^Exec(Start|StartPre)=(.*)$", re.MULTILINE)


def _exec_lines(unit_text: str) -> list:
    """Every ``ExecStart=``/``ExecStartPre=`` value in a unit file, with
    the systemd line-continuation backslash already joined -- so a
    multi-line ``ExecStart=`` (like ``aprilcamd.service``'s) reads as one
    string, matching how systemd itself parses it."""
    joined = unit_text.replace("\\\n", " ")
    return [match.group(2).strip() for match in _EXEC_LINE_RE.finditer(joined)]


def test_daemon_unit_execstart_invokes_a_real_module_with_no_flags() -> None:
    """``aprilcamd.service``'s ``ExecStart=`` must be
    ``<python> -m aprilcam.daemon`` and nothing else -- no ``--tcp``,
    ``--unix``, ``--force``, or any other v1-only flag that doesn't exist
    on ``daemon/__main__.py:main()``."""
    text = (DEPLOY_ROOT / "aprilcamd.service").read_text()
    exec_lines = _exec_lines(text)
    assert exec_lines, "aprilcamd.service must have an ExecStart= line"
    (execstart,) = exec_lines
    parts = execstart.split()
    assert parts[-2:] == ["-m", "aprilcam.daemon"], (
        f"ExecStart={execstart!r} must end in exactly '-m aprilcam.daemon' "
        "(daemon/__main__.py's package entry point) with no extra arguments "
        "-- v2's daemon takes no flags"
    )
    # Everything before "-m aprilcam.daemon" must be a single interpreter
    # path (no flags of our own bolted on in front of it either).
    assert len(parts) == 3, f"ExecStart={execstart!r} must be '<python> -m aprilcam.daemon' with nothing else"
    interpreter = Path(parts[0])
    assert interpreter.name in ("python", "python3"), f"ExecStart's interpreter {parts[0]!r} does not look like a python executable"


def test_daemon_module_is_actually_importable_as_a_package_main() -> None:
    """``python -m aprilcam.daemon`` requires ``daemon/__main__.py`` to
    exist and define ``main`` -- confirms the ExecStart target is real,
    not just plausible-looking."""
    main_module = SRC_ROOT / "daemon" / "__main__.py"
    assert main_module.is_file(), "src/aprilcam/daemon/__main__.py must exist for 'python -m aprilcam.daemon' to work"
    tree = ast.parse(main_module.read_text(), filename=str(main_module))
    top_level_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "main" in top_level_names, "daemon/__main__.py must define main() -- aprilcamd.service's ExecStart calls it implicitly via -m"


def test_bridge_template_unit_invokes_the_shipped_bridge_script() -> None:
    text = (DEPLOY_ROOT / "aprilcam-bridge@.service").read_text()
    exec_lines = _exec_lines(text)
    assert exec_lines, "aprilcam-bridge@.service must have an ExecStart= line"
    (execstart,) = exec_lines
    assert execstart.startswith("/usr/local/bin/aprilcam-camera-bridge "), (
        f"ExecStart={execstart!r} must invoke the bridge script this repo ships"
    )
    bridge_script = DEPLOY_ROOT / "aprilcam-camera-bridge"
    assert bridge_script.is_file(), "deploy/aprilcam-camera-bridge must exist -- referenced by aprilcam-bridge@.service"
    # %i (instance name / camera position) and a /dev/videoN path, not a
    # single hardcoded device -- SUC's "not hardcoded to 70/71 alone".
    assert "%i" in execstart, f"ExecStart={execstart!r} must be parameterized by systemd's %i (camera position), not hardcoded"


def test_bridge_script_is_executable_and_takes_position_and_device_args() -> None:
    bridge_script = DEPLOY_ROOT / "aprilcam-camera-bridge"
    text = bridge_script.read_text()
    assert text.startswith("#!/bin/bash") or text.startswith("#!/usr/bin/env bash")
    assert "POS=" in text and "DEV=" in text, "bridge script must accept <position> <device> positional arguments"
    # The watchdog + stagger + queue lessons named in sprint.md/the ticket
    # must actually be present, not just described in a comment.
    assert "queue max-size-buffers=6 leaky=downstream" in text
    assert "sleep \"$((POS * 4))\"" in text
    assert "v4l2-ctl" in text and "--stream-mmap" in text
    # sprint 040 ticket 006: the watchdog must judge a crash from
    # gst-launch's own fault-handler output, not from loopback frame
    # delivery alone (see test_gst_crashed_* below for why).
    assert "Caught SIGSEGV" in text
    assert "gst_crashed" in text


def _run_frame_arrived(tmp_path: Path, fake_v4l2_ctl_script: str) -> int:
    """Source ``aprilcam-camera-bridge`` (which, per its own sourcing
    guard, does nothing else when sourced rather than executed) and call
    its ``frame_arrived`` function against a faked ``v4l2-ctl`` on
    ``PATH``, returning the function's exit code.

    A faked ``timeout`` is also put on ``PATH`` -- GNU coreutils'
    ``timeout`` (what `frame_arrived` calls, and what vidar/Ubuntu ships)
    is not guaranteed to exist on every machine that runs this offline
    test suite (e.g. it is absent by default on macOS), and this test
    must exercise the byte-counting logic itself, not the presence of an
    unrelated system tool.
    """
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "timeout").write_text("#!/bin/bash\nshift\nexec \"$@\"\n")
    (bin_dir / "timeout").chmod(0o755)
    v4l2_ctl = bin_dir / "v4l2-ctl"
    v4l2_ctl.write_text(fake_v4l2_ctl_script)
    v4l2_ctl.chmod(0o755)

    bridge_script = DEPLOY_ROOT / "aprilcam-camera-bridge"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "-c", f'source "{bridge_script}"; frame_arrived /dev/videoFAKE'],
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode


def test_frame_arrived_detects_a_real_stall_that_exits_zero(tmp_path: Path) -> None:
    """The exact bug found live on vidar.local (sprint 040 ticket 003
    Finding 4): a v4l2loopback device with no live producer makes
    `v4l2-ctl --stream-mmap` print an "unsupported stream type" error to
    stderr, write nothing to stdout, but **exit 0**. `frame_arrived` must
    still report a stall (non-zero exit) here -- if this regresses to an
    exit-status-only check, this is the test that catches it."""
    dead_v4l2_ctl = (
        "#!/bin/bash\n"
        "echo 'VIDIOC_STREAMON: unsupported stream type' >&2\n"
        "exit 0\n"
    )
    assert _run_frame_arrived(tmp_path, dead_v4l2_ctl) != 0


def test_frame_arrived_confirms_a_live_stream(tmp_path: Path) -> None:
    """A producer that actually writes frame bytes to stdout (and exits
    0) must be judged live, not a stall."""
    live_v4l2_ctl = "#!/bin/bash\nhead -c 4096 /dev/zero\nexit 0\n"
    assert _run_frame_arrived(tmp_path, live_v4l2_ctl) == 0


def test_frame_arrived_detects_a_stall_that_exits_nonzero(tmp_path: Path) -> None:
    """Belt-and-suspenders: a v4l2-ctl invocation that fails outright
    (nonzero exit, no bytes) must also be judged a stall -- the fix must
    not have narrowed the check to *only* trust byte count in a way that
    would somehow treat an outright failure as success."""
    failing_v4l2_ctl = "#!/bin/bash\nexit 1\n"
    assert _run_frame_arrived(tmp_path, failing_v4l2_ctl) != 0


def _run_gst_crashed(tmp_path: Path, log_contents: str) -> int:
    """Source ``aprilcam-camera-bridge`` and call ``gst_crashed`` against
    a log file containing ``log_contents``, returning the function's
    exit code."""
    import subprocess

    log_path = tmp_path / "gst.log"
    log_path.write_text(log_contents)
    bridge_script = DEPLOY_ROOT / "aprilcam-camera-bridge"
    result = subprocess.run(
        ["bash", "-c", f'source "{bridge_script}"; gst_crashed "{log_path}"'],
        capture_output=True,
        text=True,
    )
    return result.returncode


def test_gst_crashed_detects_the_real_sigsegv_spin_fault_message(tmp_path: Path) -> None:
    """The exact failure mode this watchdog exists for (sprint 040
    ticket 001's header comment; reproduced live on vidar in ticket 003
    Finding 4 and again in ticket 006): a libcamerasrc SIGSEGV leaves
    gst-launch's own fault handler printing this literal text (captured
    verbatim from vidar's journal) before it spins forever. Confirmed
    live in ticket 006 that this is the *only* reliable signal for this
    failure -- the loopback device itself keeps appearing to deliver
    fresh, changing frame content for minutes afterward, so
    `frame_arrived` alone never observes a stall here."""
    real_crash_log = (
        "[2:15:06.801526026] [13512]  INFO RPI pisp.cpp:1483 Sensor: ...\n"
        "** (gst-launch-1.0:13507): CRITICAL **: 00:14:04.912: void "
        "GstLibcameraSrcState::requestCompleted(libcamera::Request*): "
        "assertion 'wrap->request_.get() == request' failed\n"
        "Caught SIGSEGV\n"
        "Spinning.  Please run 'gdb gst-launch-1.0 13507' to continue "
        "debugging, Ctrl-C to quit, or Ctrl-\\ to dump core.\n"
    )
    assert _run_gst_crashed(tmp_path, real_crash_log) == 0


def test_gst_crashed_is_false_for_healthy_pipeline_output(tmp_path: Path) -> None:
    """Ordinary libcamera/GStreamer startup chatter (captured verbatim
    from a healthy vidar run) must never be mistaken for a crash."""
    healthy_log = (
        "[2:15:06.746198045] [13507]  INFO Camera camera_manager.cpp:330 "
        "libcamera v0.5.2+99-bfd68f78\n"
        "[2:15:06.801526026] [13512]  INFO RPI pisp.cpp:1483 Sensor: "
        "/base/axi/pcie@120000/rp1/i2c@80000/imx296@1a - Selected sensor "
        "format: 1456x1088-SBGGR10_1X10/RAW\n"
    )
    assert _run_gst_crashed(tmp_path, healthy_log) != 0


def test_gst_crashed_is_false_when_the_log_does_not_exist_yet(tmp_path: Path) -> None:
    """Right after launch, before gst-launch has written anything, the
    log file may not exist yet -- must be treated as "not crashed", not
    an error that aborts the watchdog loop."""
    import subprocess

    missing_log = tmp_path / "does-not-exist.log"
    bridge_script = DEPLOY_ROOT / "aprilcam-camera-bridge"
    result = subprocess.run(
        ["bash", "-c", f'source "{bridge_script}"; gst_crashed "{missing_log}"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_module_config_files_reproduce_vidars_confirmed_live_values() -> None:
    """Confirmed via SSH against vidar.local's real
    ``/etc/modprobe.d/aprilcam.conf`` while writing this ticket -- pinned
    here so a future edit cannot silently drift from the live, working
    host without a test noticing."""
    modprobe_conf = (DEPLOY_ROOT / "modprobe.d" / "aprilcam.conf").read_text()
    assert (
        "options v4l2loopback video_nr=70,71 "
        "card_label=aprilcam-imx296-88000,aprilcam-imx296-80000 "
        "exclusive_caps=1,1 max_buffers=16" in modprobe_conf
    )
    modules_load_conf = (DEPLOY_ROOT / "modules-load.d" / "aprilcam.conf").read_text()
    assert "v4l2loopback" in modules_load_conf.split("\n")


def test_provision_script_module_config_matches_the_standalone_files() -> None:
    """``provision-pi.sh`` embeds the module config inline (so it has no
    runtime dependency on shipping the standalone files alongside it) --
    that inline copy must match ``deploy/modprobe.d/aprilcam.conf`` /
    ``deploy/modules-load.d/aprilcam.conf`` exactly, or a fresh Pi
    provisioned by the script would silently diverge from the files a
    human would otherwise copy by hand."""
    provision_text = (DEPLOY_ROOT / "provision-pi.sh").read_text()
    modprobe_conf = (DEPLOY_ROOT / "modprobe.d" / "aprilcam.conf").read_text()
    modprobe_options_line = next(
        line for line in modprobe_conf.splitlines() if line.startswith("options v4l2loopback")
    )
    assert modprobe_options_line in provision_text, (
        "provision-pi.sh's embedded modprobe.d content has drifted from "
        "deploy/modprobe.d/aprilcam.conf -- keep them in sync"
    )
    modules_load_conf = (DEPLOY_ROOT / "modules-load.d" / "aprilcam.conf").read_text()
    module_name_line = next(line for line in modules_load_conf.splitlines() if line.strip() == "v4l2loopback")
    assert f"'{module_name_line}'" in provision_text, (
        "provision-pi.sh's embedded modules-load.d content has drifted "
        "from deploy/modules-load.d/aprilcam.conf -- keep them in sync"
    )


def test_units_reference_the_dedicated_aprilcam_service_user() -> None:
    """Sprint's Design Rationale: the dedicated ``aprilcam`` system user,
    never ``ros``/``jtl``/a human login (v1's ``jtl`` is what this sprint
    retires)."""
    for unit_name in ("aprilcamd.service", "aprilcam-bridge@.service"):
        text = (DEPLOY_ROOT / unit_name).read_text()
        assert "User=aprilcam" in text, f"{unit_name} must run as User=aprilcam"
        assert "User=jtl" not in text and "User=ros" not in text and "User=eric" not in text
        assert "SupplementaryGroups=video" in text


def test_no_unit_sets_the_nonexistent_apricam_system_env_or_v1_flags() -> None:
    """v2's ``Directories`` resolves FHS roots via ``os.access``, not
    ``euid == 0`` -- no ``APRILCAM_SYSTEM`` env var exists in v2, so no
    unit's actual directives (``Environment=``/``ExecStart=`` lines, not
    prose comments *about* why it's unneeded) may set it or any other
    v1-only daemon flag."""
    forbidden = ("APRILCAM_SYSTEM", "--tcp", "--unix", "--force", "APRILCAM_DATA_DIR")
    for path in (DEPLOY_ROOT / "aprilcamd.service", DEPLOY_ROOT / "aprilcam-bridge@.service"):
        directive_lines = [
            line
            for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for token in forbidden:
            offending = [line for line in directive_lines if token in line]
            assert not offending, f"{path.name} references v1-only {token!r} in a live directive: {offending}"


def test_daemon_unit_declares_the_three_fhs_directories() -> None:
    text = (DEPLOY_ROOT / "aprilcamd.service").read_text()
    for directive in ("ConfigurationDirectory=aprilcam", "StateDirectory=aprilcam", "RuntimeDirectory=aprilcam"):
        assert directive in text
    assert "Restart=on-failure" in text
    assert "StandardOutput=journal" in text
    assert "StandardError=journal" in text


def test_provision_script_and_bridge_script_pass_bash_syntax_check() -> None:
    """A lightweight, dependency-free stand-in for the ticket's ``bash
    -n`` acceptance criterion, so a syntax regression fails an ordinary
    ``pytest`` run and not only a manual shell invocation."""
    import subprocess
    import sys

    if sys.platform == "win32":
        return
    for script_name in ("provision-pi.sh", "aprilcam-camera-bridge"):
        script_path = DEPLOY_ROOT / script_name
        result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
        assert result.returncode == 0, f"bash -n {script_name} failed:\n{result.stderr}"
