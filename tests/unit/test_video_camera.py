"""Unit tests for VideoCamera."""

from pathlib import Path
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.camera import VideoCamera


MOVIES_DIR = Path(__file__).parent.parent / "movies"


def _is_lfs_pointer(path: Path) -> bool:
    try:
        return path.read_bytes()[:8].startswith(b"version ")
    except Exception:
        return False


@pytest.fixture
def bright_video():
    path = MOVIES_DIR / "bright-gsc.mov"
    if not path.exists() or _is_lfs_pointer(path):
        pytest.skip("Test video not available (LFS not checked out)")
    return path


class TestVideoCamera:

    def test_construction(self, bright_video):
        cam = VideoCamera(bright_video)
        assert cam.name == "bright-gsc"
        assert cam.index == -1
        assert not cam.is_open

    def test_open_and_read(self, bright_video):
        cam = VideoCamera(bright_video)
        cam.open()
        assert cam.is_open
        ok, frame = cam.read()
        assert ok
        assert frame is not None
        assert frame.shape[2] == 3  # BGR
        cam.close()
        assert not cam.is_open

    def test_context_manager(self, bright_video):
        with VideoCamera(bright_video) as cam:
            ok, frame = cam.read()
            assert ok
        assert not cam.is_open

    def test_resolution(self, bright_video):
        with VideoCamera(bright_video) as cam:
            cam.open()
            w, h = cam.resolution
            assert w > 0
            assert h > 0

    def test_frame_count(self, bright_video):
        cam = VideoCamera(bright_video)
        assert cam.frame_count > 0

    def test_fps(self, bright_video):
        cam = VideoCamera(bright_video)
        assert cam.fps > 0

    def test_read_to_eof(self, bright_video):
        cam = VideoCamera(bright_video)
        count = 0
        while True:
            ok, frame = cam.read()
            if not ok:
                break
            count += 1
            if count > 5:
                break
        assert count > 0
        cam.close()

    def test_loop_mode(self, bright_video):
        cam = VideoCamera(bright_video, loop=True)
        # Read past what would be EOF in a short test
        frames = []
        for _ in range(10):
            ok, frame = cam.read()
            if ok:
                frames.append(frame)
        assert len(frames) == 10
        cam.close()

    def test_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            VideoCamera("/nonexistent/video.mov")
