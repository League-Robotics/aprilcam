"""Unit tests for aprilcam.client.host_codes.

Covers:
  - num_to_alpha / alpha_to_num round-trip (including AA boundary)
  - code_for for local (1-letter) and remote (2-letter) cameras
  - resolve_code for local and remote codes, error cases
  - resolve_host_token letter→host resolution and pass-through
  - store load/save atomicity (temp-file replace) via tmp_path
  - merge_probe_results keeps numbers stable across re-probe
  - New hosts/cameras get the next free number
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aprilcam.client.host_codes import (
    alpha_to_num,
    code_for,
    load_store,
    merge_probe_results,
    num_to_alpha,
    resolve_code,
    find_host,
    resolve_host_token,
    save_store,
)


# ---------------------------------------------------------------------------
# num_to_alpha / alpha_to_num
# ---------------------------------------------------------------------------


class TestNumToAlpha:
    def test_single_letter_range(self):
        assert num_to_alpha(1) == "A"
        assert num_to_alpha(26) == "Z"

    def test_two_letter_boundary(self):
        assert num_to_alpha(27) == "AA"
        assert num_to_alpha(28) == "AB"
        assert num_to_alpha(52) == "AZ"
        assert num_to_alpha(53) == "BA"

    def test_zz(self):
        # Z=26, AZ=52, ZZ = 26*26+26 = 702
        assert num_to_alpha(702) == "ZZ"

    def test_invalid_zero(self):
        with pytest.raises(ValueError):
            num_to_alpha(0)

    def test_invalid_negative(self):
        with pytest.raises(ValueError):
            num_to_alpha(-1)


class TestAlphaToNum:
    def test_single_letters(self):
        assert alpha_to_num("A") == 1
        assert alpha_to_num("Z") == 26

    def test_two_letters(self):
        assert alpha_to_num("AA") == 27
        assert alpha_to_num("AB") == 28
        assert alpha_to_num("AZ") == 52
        assert alpha_to_num("BA") == 53
        assert alpha_to_num("ZZ") == 702

    def test_lowercase_accepted(self):
        assert alpha_to_num("a") == 1
        assert alpha_to_num("aa") == 27

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            alpha_to_num("")

    def test_non_alpha_raises(self):
        with pytest.raises(ValueError):
            alpha_to_num("1")

    def test_roundtrip(self):
        for n in [1, 2, 26, 27, 52, 53, 100, 702]:
            assert alpha_to_num(num_to_alpha(n)) == n


# ---------------------------------------------------------------------------
# code_for
# ---------------------------------------------------------------------------


class TestCodeFor:
    def test_local_single_letter(self):
        assert code_for(1, 1, is_local=True) == "A"
        assert code_for(1, 3, is_local=True) == "C"
        assert code_for(1, 26, is_local=True) == "Z"

    def test_remote_two_letters(self):
        assert code_for(6, 2, is_local=False) == "FB"
        assert code_for(1, 1, is_local=False) == "AA"
        assert code_for(26, 26, is_local=False) == "ZZ"

    def test_local_ignores_host_num(self):
        # is_local=True means only cam_num matters.
        assert code_for(99, 2, is_local=True) == "B"


# ---------------------------------------------------------------------------
# resolve_code
# ---------------------------------------------------------------------------


def _make_store(hosts: list[dict]) -> dict:
    return {"version": 1, "hosts": hosts}


class TestResolveCode:
    def test_local_single_letter(self):
        store = _make_store([
            {
                "num": 1, "kind": "local", "host": "myhost",
                "addresses": ["127.0.0.1"],
                "cameras": [
                    {"num": 1, "enum": 10, "index": 0, "name": "cam-a", "slug": "cam-a"},
                    {"num": 2, "enum": 11, "index": 1, "name": "cam-b", "slug": "cam-b"},
                ],
            }
        ])
        host_entry, cam_entry = resolve_code("A", store)
        assert host_entry["num"] == 1
        assert cam_entry["num"] == 1
        assert cam_entry["name"] == "cam-a"

    def test_local_second_camera(self):
        store = _make_store([
            {
                "num": 1, "kind": "local", "host": "myhost",
                "addresses": ["127.0.0.1"],
                "cameras": [
                    {"num": 1, "enum": 10, "index": 0, "name": "cam-a", "slug": "cam-a"},
                    {"num": 2, "enum": 11, "index": 1, "name": "cam-b", "slug": "cam-b"},
                ],
            }
        ])
        host_entry, cam_entry = resolve_code("B", store)
        assert cam_entry["name"] == "cam-b"

    def test_remote_two_letters(self):
        store = _make_store([
            {"num": 1, "kind": "local", "host": "local", "addresses": [], "cameras": []},
            {
                "num": 6, "kind": "remote", "host": "vidar.local",
                "addresses": ["192.168.1.144"],
                "cameras": [
                    {"num": 1, "enum": 6, "index": 0, "name": "imx296-88000", "slug": "imx296-88000"},
                    {"num": 2, "enum": 7, "index": 1, "name": "imx296-80000", "slug": "imx296-80000"},
                ],
            },
        ])
        host_entry, cam_entry = resolve_code("FB", store)
        assert host_entry["host"] == "vidar.local"
        assert cam_entry["num"] == 2
        assert cam_entry["name"] == "imx296-80000"

    def test_unknown_code_raises(self):
        store = _make_store([])
        with pytest.raises(ValueError, match="no local host"):
            resolve_code("A", store)

    def test_unknown_remote_host_raises(self):
        store = _make_store([
            {"num": 1, "kind": "local", "host": "local", "addresses": [], "cameras": []},
        ])
        with pytest.raises(ValueError, match="host #6"):
            resolve_code("FA", store)

    def test_too_long_raises(self):
        store = _make_store([])
        with pytest.raises(ValueError, match="1 or 2 alphabetic"):
            resolve_code("ABC", store)

    def test_lowercase_accepted(self):
        store = _make_store([
            {
                "num": 1, "kind": "local", "host": "myhost",
                "addresses": [],
                "cameras": [
                    {"num": 1, "enum": 0, "index": 0, "name": "cam", "slug": "cam"},
                ],
            }
        ])
        host_entry, cam_entry = resolve_code("a", store)
        assert cam_entry["num"] == 1


# ---------------------------------------------------------------------------
# resolve_host_token
# ---------------------------------------------------------------------------


class TestResolveHostToken:
    def _store_with_remote(self) -> dict:
        return _make_store([
            {"num": 1, "kind": "local", "host": "localhost", "addresses": [], "cameras": []},
            {"num": 6, "kind": "remote", "host": "vidar.local", "addresses": ["192.168.1.5"], "cameras": []},
        ])

    def test_letter_resolves_to_host(self):
        store = self._store_with_remote()
        # Host #6 = "F"
        result = resolve_host_token("F", store)
        assert result == "vidar.local"

    def test_unknown_letter_passes_through(self):
        store = self._store_with_remote()
        # No host #26 (Z) in store
        result = resolve_host_token("Z", store)
        assert result == "Z"

    def test_hostname_passes_through(self):
        store = self._store_with_remote()
        result = resolve_host_token("vidar.local", store)
        assert result == "vidar.local"

    def test_ip_passes_through(self):
        store = _make_store([])
        result = resolve_host_token("192.168.1.10", store)
        assert result == "192.168.1.10"

    def test_empty_passes_through(self):
        result = resolve_host_token("", _make_store([]))
        assert result == ""

    def test_lowercase_letter_resolves(self):
        store = _make_store([
            {"num": 1, "kind": "local", "host": "myhost", "addresses": [], "cameras": []},
        ])
        # Host #1 = "A"
        result = resolve_host_token("a", store)
        assert result == "myhost"

    def test_falls_back_to_address_when_no_host_field(self):
        store = _make_store([
            {"num": 2, "kind": "remote", "host": "", "addresses": ["10.0.0.1"], "cameras": []},
        ])
        result = resolve_host_token("B", store)
        assert result == "10.0.0.1"


# ---------------------------------------------------------------------------
# Store load/save
# ---------------------------------------------------------------------------


class TestStorePersistence:
    def _mock_config(self, data_dir: Path) -> MagicMock:
        cfg = MagicMock()
        cfg.data_dir = data_dir
        return cfg

    def test_load_missing_file_returns_empty(self, tmp_path):
        cfg = self._mock_config(tmp_path)
        store = load_store(cfg)
        assert store == {"version": 1, "hosts": []}

    def test_save_and_load_roundtrip(self, tmp_path):
        cfg = self._mock_config(tmp_path)
        store = {
            "version": 1,
            "hosts": [
                {"num": 1, "kind": "local", "host": "myhost", "addresses": [], "cameras": []}
            ],
        }
        save_store(cfg, store)
        loaded = load_store(cfg)
        assert loaded == store

    def test_save_is_atomic(self, tmp_path):
        """save_store uses tmp+replace so no partial writes are visible."""
        cfg = self._mock_config(tmp_path)
        store = {"version": 1, "hosts": []}
        save_store(cfg, store)
        # Verify the file is valid JSON
        raw = (tmp_path / "hosts.json").read_text()
        parsed = json.loads(raw)
        assert parsed == store

    def test_corrupt_store_returns_empty(self, tmp_path):
        cfg = self._mock_config(tmp_path)
        (tmp_path / "hosts.json").write_text("not-json{{{")
        store = load_store(cfg)
        assert store == {"version": 1, "hosts": []}


# ---------------------------------------------------------------------------
# merge_probe_results
# ---------------------------------------------------------------------------


class TestMergeProbeResults:
    def test_local_host_gets_num_1(self):
        store: dict = {"version": 1, "hosts": []}
        result = merge_probe_results(store, [
            {"host": "mybox", "addresses": [], "kind": "local", "cameras": []},
        ])
        hosts = result["hosts"]
        assert len(hosts) == 1
        assert hosts[0]["num"] == 1
        assert hosts[0]["kind"] == "local"

    def test_cameras_get_stable_nums(self):
        store: dict = {"version": 1, "hosts": []}
        cameras = [
            {"enum": 10, "index": 0, "name": "cam-a", "slug": "cam-a"},
            {"enum": 11, "index": 1, "name": "cam-b", "slug": "cam-b"},
        ]
        merge_probe_results(store, [
            {"host": "h", "addresses": [], "kind": "local", "cameras": cameras},
        ])
        cams = store["hosts"][0]["cameras"]
        assert cams[0]["num"] == 1
        assert cams[1]["num"] == 2

    def test_re_probe_keeps_existing_nums(self):
        """Camera nums are stable across re-probe (slug-matched)."""
        store: dict = {"version": 1, "hosts": []}
        cameras1 = [
            {"enum": 10, "index": 0, "name": "cam-a", "slug": "cam-a"},
            {"enum": 11, "index": 1, "name": "cam-b", "slug": "cam-b"},
        ]
        merge_probe_results(store, [
            {"host": "h", "addresses": [], "kind": "local", "cameras": cameras1},
        ])
        # Simulate re-probe: cam-b appears before cam-a (order changed).
        cameras2 = [
            {"enum": 11, "index": 1, "name": "cam-b", "slug": "cam-b"},
            {"enum": 10, "index": 0, "name": "cam-a", "slug": "cam-a"},
        ]
        merge_probe_results(store, [
            {"host": "h", "addresses": [], "kind": "local", "cameras": cameras2},
        ])
        cams = store["hosts"][0]["cameras"]
        by_slug = {c["slug"]: c for c in cams}
        # Original assignments must be preserved.
        assert by_slug["cam-a"]["num"] == 1
        assert by_slug["cam-b"]["num"] == 2

    def test_new_camera_gets_next_num(self):
        """A new camera that wasn't there before gets max+1."""
        store: dict = {"version": 1, "hosts": []}
        merge_probe_results(store, [
            {"host": "h", "addresses": [], "kind": "local", "cameras": [
                {"enum": 10, "index": 0, "name": "cam-a", "slug": "cam-a"},
            ]},
        ])
        # Second probe adds cam-b.
        merge_probe_results(store, [
            {"host": "h", "addresses": [], "kind": "local", "cameras": [
                {"enum": 10, "index": 0, "name": "cam-a", "slug": "cam-a"},
                {"enum": 11, "index": 1, "name": "cam-b", "slug": "cam-b"},
            ]},
        ])
        cams = store["hosts"][0]["cameras"]
        by_slug = {c["slug"]: c for c in cams}
        assert by_slug["cam-a"]["num"] == 1
        assert by_slug["cam-b"]["num"] == 2

    def test_remote_host_gets_incrementing_num(self):
        """Remote hosts get num > 1 (local=1 is reserved)."""
        store: dict = {"version": 1, "hosts": []}
        merge_probe_results(store, [
            {"host": "local", "addresses": [], "kind": "local", "cameras": []},
            {"host": "vidar.local", "addresses": ["192.168.1.5"], "kind": "remote", "cameras": []},
        ])
        nums = {h["host"]: h["num"] for h in store["hosts"]}
        assert nums["local"] == 1
        assert nums["vidar.local"] == 2

    def test_re_probe_host_keeps_num(self):
        """Host nums are stable across re-probe (matched by hostname)."""
        store: dict = {"version": 1, "hosts": []}
        merge_probe_results(store, [
            {"host": "vidar.local", "addresses": ["192.168.1.5"], "kind": "remote", "cameras": []},
        ])
        first_num = store["hosts"][0]["num"]
        # Second probe sees the same host.
        merge_probe_results(store, [
            {"host": "vidar.local", "addresses": ["192.168.1.5"], "kind": "remote", "cameras": []},
        ])
        assert store["hosts"][0]["num"] == first_num
        assert len(store["hosts"]) == 1  # not duplicated

    def test_address_match_deduplicates(self):
        """A host matched by address does not get a second entry."""
        store: dict = {"version": 1, "hosts": []}
        merge_probe_results(store, [
            {"host": "vidar.local", "addresses": ["192.168.1.5"], "kind": "remote", "cameras": []},
        ])
        # Second probe uses the IP instead of hostname.
        merge_probe_results(store, [
            {"host": "192.168.1.5", "addresses": ["192.168.1.5"], "kind": "remote", "cameras": []},
        ])
        assert len(store["hosts"]) == 1


# ---------------------------------------------------------------------------
# Root-level --host/--port extraction (cli/__init__)
# ---------------------------------------------------------------------------


class TestExtractGlobalFlags:
    """Test the root-level flag extraction in aprilcam.cli main()."""

    def _extract(self, args_list: list[str]):
        from aprilcam.cli import _extract_global_flags
        return _extract_global_flags(args_list)

    def test_host_before_command(self):
        remaining, host, port = self._extract(["--host", "vidar.local", "cameras"])
        assert host == "vidar.local"
        assert port is None
        assert remaining == ["cameras"]

    def test_host_after_command(self):
        remaining, host, port = self._extract(["cameras", "--host", "vidar.local"])
        assert host == "vidar.local"
        assert remaining == ["cameras"]

    def test_port_extracted(self):
        remaining, host, port = self._extract(["--port", "5281", "cameras"])
        assert port == "5281"
        assert remaining == ["cameras"]

    def test_daemon_host_alias(self):
        remaining, host, port = self._extract(["--daemon-host", "vidar.local", "cameras"])
        assert host == "vidar.local"
        assert remaining == ["cameras"]

    def test_daemon_port_alias(self):
        remaining, host, port = self._extract(["cameras", "--daemon-port", "9999"])
        assert port == "9999"
        assert remaining == ["cameras"]

    def test_equals_form(self):
        remaining, host, port = self._extract(["--host=vidar.local", "cameras"])
        assert host == "vidar.local"
        assert remaining == ["cameras"]

    def test_no_flags_unchanged(self):
        remaining, host, port = self._extract(["cameras", "--details"])
        assert remaining == ["cameras", "--details"]
        assert host is None
        assert port is None

    def test_both_before_command(self):
        remaining, host, port = self._extract(["--host", "h.local", "--port", "1234", "cameras"])
        assert host == "h.local"
        assert port == "1234"
        assert remaining == ["cameras"]

    def test_main_sets_env(self, monkeypatch):
        """aprilcam.cli.main() sets APRILCAM_DAEMON_HOST env var from --host."""
        import importlib
        import aprilcam.cli as cli

        calls: list = []

        def _fake_import(name):
            calls.append(name)
            raise ModuleNotFoundError("no module", name="mcp")

        monkeypatch.setattr(importlib, "import_module", _fake_import)
        # Remove the env var before the test to ensure a clean baseline.
        monkeypatch.delenv("APRILCAM_DAEMON_HOST", raising=False)

        import pytest as _pytest
        with _pytest.raises(ModuleNotFoundError):
            cli.main(["--host", "testhost.local", "tool"])

        assert os.environ.get("APRILCAM_DAEMON_HOST") == "testhost.local"
        # cli.main() sets os.environ directly (bypassing monkeypatch tracking).
        # Explicitly delete after assertion so subsequent tests are not contaminated.
        os.environ.pop("APRILCAM_DAEMON_HOST", None)


# ---------------------------------------------------------------------------
# cameras_cli prints a code column (mock DaemonControl + temp store)
# ---------------------------------------------------------------------------


class TestCamerasCliCodeColumn:
    """cameras_cli shows alpha codes when the host store has entries."""

    def _make_devices(self):
        from aprilcam.client.models import CameraDevice
        return [
            CameraDevice(index=0, name="cam-alpha", slug="cam-alpha", enum=10),
            CameraDevice(index=1, name="cam-beta", slug="cam-beta", enum=11),
        ]

    def test_code_shown_from_store(self, monkeypatch, tmp_path, capsys):
        """With no --host, `cameras` lists every host from the store, addressing
        local cameras by bare number and remote cameras by host-letter+number."""
        from aprilcam.cli import cameras_cli
        from unittest.mock import MagicMock

        # Store: local host [A] + a remote host [B], each with cameras.
        store = {
            "version": 1,
            "hosts": [
                {
                    "num": 1, "kind": "local", "host": "myhost",
                    "addresses": ["127.0.0.1"],
                    "cameras": [
                        {"num": 1, "enum": 10, "index": 0, "name": "cam-alpha", "slug": "cam-alpha"},
                        {"num": 2, "enum": 11, "index": 1, "name": "cam-beta", "slug": "cam-beta"},
                    ],
                },
                {
                    "num": 2, "kind": "remote", "host": "vidar",
                    "addresses": ["10.0.0.2"],
                    "cameras": [
                        {"num": 1, "enum": 6, "index": 0, "name": "imx296", "slug": "imx296"},
                    ],
                },
            ],
        }
        monkeypatch.delenv("APRILCAM_DAEMON_HOST", raising=False)
        monkeypatch.setattr("aprilcam.cli.cameras_cli.load_store", lambda *a, **kw: store)
        mock_cfg = MagicMock()
        monkeypatch.setattr("aprilcam.cli.cameras_cli.Config", MagicMock(load=MagicMock(return_value=mock_cfg)))

        rc = cameras_cli.main([])
        out = capsys.readouterr().out

        assert rc == 0, f"cameras_cli.main returned {rc}; stdout={out!r}"
        # Local host: letter A in header, cameras as bare numbers (10, 11).
        assert "myhost" in out and "[A]" in out and "(local)" in out
        assert "10" in out and "11" in out
        # Remote host: letter B in header, camera as host-letter+number (B6).
        assert "vidar" in out and "[B]" in out
        assert "B6" in out

    def test_hint_shown_when_no_store(self, monkeypatch, capsys):
        from aprilcam.cli import cameras_cli
        from unittest.mock import MagicMock

        devices = self._make_devices()
        dc = MagicMock()
        dc.enumerate_cameras.return_value = devices

        monkeypatch.setattr(
            cameras_cli.AppConfig,
            "load",
            classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
        )
        monkeypatch.setattr("aprilcam.cli.cameras_cli.connect_from_args", lambda *a, **kw: dc)
        monkeypatch.setattr("aprilcam.cli._daemon.connect_from_args", lambda *a, **kw: dc)
        empty_store = {"version": 1, "hosts": []}
        monkeypatch.setattr("aprilcam.cli.cameras_cli.load_store", lambda *a, **kw: empty_store)
        mock_cfg = MagicMock()
        monkeypatch.setattr("aprilcam.cli.cameras_cli.Config", MagicMock(load=MagicMock(return_value=mock_cfg)))

        rc = cameras_cli.main([])
        out = capsys.readouterr().out

        assert rc == 0
        assert "aprilcam probe" in out


# ---------------------------------------------------------------------------
# find_host — name/address matching tolerant of ".local" and IPs
# ---------------------------------------------------------------------------


class TestFindHost:
    """find_host matches a stored host across hostname forms and addresses."""

    STORE = {
        "version": 1,
        "hosts": [
            {
                "num": 1,
                "kind": "remote",
                "host": "vidar",
                "addresses": ["192.168.1.144"],
                "cameras": [],
            },
        ],
    }

    def test_match_exact(self) -> None:
        assert find_host(self.STORE, host="vidar")["num"] == 1

    def test_match_dot_local_suffix(self) -> None:
        # Stored as "vidar"; looked up as "vidar.local".
        assert find_host(self.STORE, host="vidar.local")["num"] == 1

    def test_match_case_insensitive(self) -> None:
        assert find_host(self.STORE, host="VIDAR.local")["num"] == 1

    def test_match_by_address(self) -> None:
        # A numeric IP host token matches the stored address.
        assert find_host(self.STORE, host="192.168.1.144")["num"] == 1

    def test_match_by_addresses_overlap(self) -> None:
        assert find_host(self.STORE, addresses=["192.168.1.144"])["num"] == 1

    def test_no_match_returns_none(self) -> None:
        assert find_host(self.STORE, host="other.local") is None

    def test_merge_dedupes_across_name_forms(self) -> None:
        # Probing the same host as "vidar" then "vidar.local" must not create a
        # second entry (find_host-backed _find_existing_host).
        store = {"version": 1, "hosts": []}
        merge_probe_results(
            store,
            [{"host": "vidar", "addresses": ["192.168.1.144"], "kind": "remote",
              "cameras": [{"slug": "imx296-88000", "enum": 6, "index": 0, "name": "a"}]}],
        )
        merge_probe_results(
            store,
            [{"host": "vidar.local", "addresses": ["192.168.1.144"], "kind": "remote",
              "cameras": [{"slug": "imx296-88000", "enum": 6, "index": 0, "name": "a"}]}],
        )
        assert len(store["hosts"]) == 1
