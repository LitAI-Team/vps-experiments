# VPS Gaze Poller

`gaze_poller.py` records where a person wearing Viewpointsystem (VPS) eye tracking
glasses is looking. While it runs, it polls the glasses for the current gaze point
(30 times per second by default) and, for each new sample:

- appends the gaze coordinates and timestamp to `gaze.txt`
- saves the current front camera image as `screenshot_<timestamp>.jpg`

Both go into the same output directory, so each screenshot can be matched to its
gaze line through the timestamp.

## Requirements

- Python 3 (standard library only)
- [ffmpeg](https://ffmpeg.org/) on the `PATH`, used to decode the front camera video
  (macOS: `brew install ffmpeg`)
- A computer on the same network as the glasses (default unit IP: `10.42.0.1`)

## Running

```
python gaze_poller.py [UNIT-IP] [--rate HZ] [--outdir DIR]
```

| Option     | Default     | Meaning                                       |
|------------|-------------|-----------------------------------------------|
| `UNIT-IP`  | `10.42.0.1` | IP address of the glasses                     |
| `--rate`   | `30`        | Polling rate in Hz                            |
| `--outdir` | `.`         | Directory for `gaze.txt` and the screenshots  |

Example:

```
python gaze_poller.py 10.42.0.1 --outdir frames
```

The console shows a welcome message and the output directory. Press **Ctrl+C** to quit.

## Output

`gaze.txt` contains one line per sample:

```
x=812.35, y=540.12, timestamp=332038700000
```

- `x`, `y`: gaze point in front camera pixel coordinates
- `timestamp`: glasses timestamp in nanoseconds since the glasses booted

If a polling tick has no new gaze sample, nothing is written for that tick. `gaze.txt`
is appended to, so several runs into the same directory end up in the same file.

Each screenshot is the most recent front camera frame at the moment of polling.
Because video decoding lags a little, the image can be a few tens of milliseconds
behind the gaze sample.

## How it works

The glasses provide live streams over RTSP (see `doc/livestream-api-documentation.pdf`):

- **Gaze data** (`media=gazedata.format(klv)`, section 2.4.3): received and decoded in
  Python using the KLV parsing helpers from `vps_gaze_reader.py`.
- **Front camera** (`media=frontcam.codec(h264)`, section 2.4.1): decoded by an ffmpeg
  subprocess that converts each frame to JPEG.

Background threads keep the latest gaze sample and the latest frame. The main loop
reads both at the polling rate and writes them to disk.

## Mapping gaze to the area between ArUco markers

`map_gaze_to_aruco.py` post-processes recordings. It goes through every directory whose
name starts with `frame` and maps each gaze point from front camera pixels to the area
between four ArUco markers (for example, markers placed at the corners of a document
on screen).

### Marker setup

The markers come from the `DICT_4X4_50` dictionary and must be arranged like this, as
seen by the camera:

```
ID 0 ---------- ID 1
 |               |
 |  target area  |
 |               |
ID 3 ---------- ID 2
```

The target area is the rectangle bounded by the corner of each marker that faces the
centre.

### Requirements

- Python 3 with OpenCV and numpy:
  ```
  python3 -m venv .venv
  .venv/bin/pip install opencv-contrib-python-headless numpy
  ```
- [ffmpeg](https://ffmpeg.org/) on the `PATH` for H.264 output. Without ffmpeg, the
  script falls back to OpenCV's own video writer.

### Running

```
.venv/bin/python map_gaze_to_aruco.py [ROOT] [--width W] [--height H] [--fps F] [--min-markers 3|4]
```

| Option          | Default                 | Meaning                                              |
|-----------------|-------------------------|------------------------------------------------------|
| `ROOT`          | `.`                     | Directory containing the `frame*` directories        |
| `--width`       | `600`                   | Width of the target area in pixels                   |
| `--height`      | from the observed ratio | Height of the target area in pixels                  |
| `--fps`         | screenshot rate         | Frame rate of the animation                          |
| `--min-markers` | `3`                     | Minimum number of visible markers for a homography   |

### What it does

For each `frame*` directory:

- **Markers:** finds the ArUco markers on every `screenshot_<timestamp>.jpg`.
- **Gaze lookup:** takes x,y from `gaze.txt` for each screenshot. The timestamps
  normally match exactly; otherwise the nearest sample within 50 ms is used.
- **Homography:** maps screenshot pixels to the target area. The area is 600 px wide by
  default, and its height comes from the width/height ratio seen in the frames.
  `(0,0)` is the inner corner of marker 0.
- **Missing markers:** markers are often not detected when the head moves quickly
  (motion blur). When exactly one marker is missing, the homography is computed from the
  other three, using marker positions learned from frames where all four were found.
  With fewer than three markers, the frame is not mapped. Use `--min-markers 4` to map
  only frames where all four markers were found.

### Output

Two files are written into each directory:

- `gaze_animation.mp4`: all screenshots in sequence, with two panels side by side:
  - left: the screenshot with the detected markers, the outline of the target area, and
    the gaze point with a short trail
  - right: the target area flattened to a straight-on view (with a 15% margin), showing
    the mapped gaze point, or "no homography" when the frame could not be mapped
- `gaze_mapped.csv`: one row per screenshot with these columns:

  | Column                  | Meaning                                                    |
  |-------------------------|------------------------------------------------------------|
  | `timestamp`             | Screenshot timestamp                                       |
  | `screenshot`            | Screenshot file name                                       |
  | `markers`               | IDs of the detected markers, e.g. `0123`                   |
  | `gaze_x`, `gaze_y`      | Gaze point in screenshot pixels                            |
  | `target_x`, `target_y`  | Gaze point in target area pixels (empty if not mapped)     |
  | `target_u`, `target_v`  | Gaze point as a fraction of the target area (0 to 1)       |
  | `inside`                | `1` if the gaze point lies inside the target area, else `0` |

### Example results

For the recordings in this repository:

| Directory                     | All 4 markers | 3 markers | Not mapped |
|-------------------------------|---------------|-----------|------------|
| `frames_30Hz_focus_on_aruco`    | 194           | 89        | 97 / 380   |
| `frames_30Hz_focus_on_assassin` | 536           | 58        | 84 / 678   |
| `frames_60Hz_focus_on_aruco`    | 149           | 114       | 334 / 597  |

## Other files

- `vps_gaze_reader.py`: the vendor's reference reader for the gaze stream, which can also
  capture raw data to a file (run it without arguments for usage)
- `doc/livestream-api-documentation.pdf`: the VPS livestream API documentation
