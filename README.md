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

## Other files

- `vps_gaze_reader.py`: the vendor's reference reader for the gaze stream, which can also
  capture raw data to a file (run it without arguments for usage)
- `doc/livestream-api-documentation.pdf`: the VPS livestream API documentation
