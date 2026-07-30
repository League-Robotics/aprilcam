# Playfield Simulator

Pygame-based playfield simulator that displays a window with four 4x4 ArUco
corner fiducials and four moving AprilTag 36h11 markers. The tags follow
different motion patterns:

- **Two edge-runners** -- travel clockwise around the playfield perimeter.
- **One circular mover** -- orbits the center of the playfield.
- **One center spinner** -- stays at the center and rotates in place.

Use the up/down arrow keys to adjust simulation speed. Press Escape or Q to
quit.

## Prerequisites

```
pip install pygame>=2.5
pip install opencv-contrib-python
```

The `aprilcam` package must be importable. Either install it or add the
project's `src/` directory to your `PYTHONPATH`:

```
export PYTHONPATH=/path/to/AprilTags/src:$PYTHONPATH
```

## Usage

```
python contrib/playfield/playfield_sim.py [OPTIONS]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--width` | 1200 | Window width in pixels |
| `--height` | 800 | Window height in pixels |
| `--tag-size` | 48 | Moving AprilTag side length in pixels |
| `--fiducial-size` | 48 | Corner fiducial side length in pixels |
| `--tag-ids` | 1,2,3,4 | Comma-separated AprilTag IDs (first 4 used) |
| `--fps` | 60 | Target frame rate |
| `--quiet-ratio` | 0.2857 | White quiet zone as fraction of tag side |
