#!/usr/bin/env python3
"""Map gaze points from front camera screenshots into the area between four ArUco markers.

For every directory starting with "frame" (e.g. the output of gaze_poller.py), this script:

1. detects the ArUco markers (DICT_4X4_50, IDs 0..3) on each screenshot_<timestamp>.jpg
2. looks up the gaze x,y of that screenshot in gaze.txt (matched by timestamp)
3. computes a homography from screenshot pixels to the area between the markers
   (the rectangle spanned by the inner marker corners) and maps the gaze point with it
4. writes gaze_mapped.csv and an animation gaze_animation.mp4 into that directory

Expected marker layout (as seen by the camera):

    ID 0 ---------- ID 1
     |               |
     |    target     |
     |     area      |
     |               |
    ID 3 ---------- ID 2

Target coordinates are in pixels of a rectangle of size --width x --height, with (0, 0) at
the inner corner of marker 0. The CSV also contains normalized coordinates (0..1).

When only three markers are visible, the homography is fitted to their corners using the
marker layout learned from frames where all four were found (see --min-markers).

Requirements: opencv-contrib-python (or opencv-python >= 4.7), numpy, ffmpeg on the PATH
(falls back to OpenCV's mp4v writer if ffmpeg is missing).

Usage:
    python map_gaze_to_aruco.py [ROOT] [--width W] [--height H] [--fps FPS] [--min-markers N]
"""

import argparse
import bisect
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ARUCO_DICT = cv2.aruco.DICT_4X4_50
MARKER_IDS = (0, 1, 2, 3)  # top-left, top-right, bottom-right, bottom-left
# Index of each marker's corner that faces the target area. OpenCV returns marker corners
# in the order top-left, top-right, bottom-right, bottom-left.
INNER_CORNER = {0: 2, 1: 3, 2: 0, 3: 1}

GAZE_LINE = re.compile(r"x=([-\d.]+),\s*y=([-\d.]+),\s*timestamp=(\d+)")
SCREENSHOT_NAME = re.compile(r"screenshot_(\d+)\.jpg$")

PANEL_HEIGHT = 720
MARGIN = 0.15  # extra space around the target area in the right panel (fraction of size)
TRAIL_LENGTH = 15
MAX_GAZE_OFFSET_NS = 50_000_000  # accept a gaze sample at most 50 ms away from a screenshot

COLOR_GAZE = (0, 0, 255)
COLOR_TRAIL = (0, 200, 255)
COLOR_MARKERS = (0, 255, 0)
COLOR_TEXT = (255, 255, 255)


def read_gaze(path):
    """Return sorted lists of timestamps and (x, y) tuples from gaze.txt."""
    samples = {}
    for line in path.read_text().splitlines():
        m = GAZE_LINE.search(line)
        if m:
            samples[int(m.group(3))] = (float(m.group(1)), float(m.group(2)))
    timestamps = sorted(samples)
    return timestamps, [samples[t] for t in timestamps]


def lookup_gaze(timestamps, points, ts):
    """Gaze sample with exactly this timestamp, or the closest one within MAX_GAZE_OFFSET_NS."""
    i = bisect.bisect_left(timestamps, ts)
    candidates = [j for j in (i - 1, i) if 0 <= j < len(timestamps)]
    if not candidates:
        return None
    j = min(candidates, key=lambda j: abs(timestamps[j] - ts))
    return points[j] if abs(timestamps[j] - ts) <= MAX_GAZE_OFFSET_NS else None


def detect_markers(detector, image):
    """Return {marker_id: 4x2 corner array} for the expected marker IDs."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None:
        return {}
    return {int(i): c.reshape(4, 2) for i, c in zip(ids.ravel(), corners) if int(i) in MARKER_IDS}


def target_rectangle(width, height):
    """Target positions of the inner corners of markers 0, 1, 2, 3."""
    return np.float32([[0, 0], [width, 0], [width, height], [0, height]])


def inner_corners(markers):
    return np.float32([markers[i][INNER_CORNER[i]] for i in MARKER_IDS])


def estimate_aspect(detections):
    """Median height/width ratio of the inner quadrilateral over all frames with four markers."""
    ratios = []
    for markers in detections:
        if len(markers) == 4:
            q = inner_corners(markers)
            w = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
            h = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
            ratios.append(h / w)
    return float(np.median(ratios)) if ratios else None


def learn_layout(detections, target):
    """Median position of all 16 marker corners in target coordinates.

    Uses frames where all four markers are visible. The result lets us fit a homography
    when one marker is missing.
    """
    projected = {i: [] for i in MARKER_IDS}
    for markers in detections:
        if len(markers) == 4:
            H = cv2.getPerspectiveTransform(inner_corners(markers), target)
            for i in MARKER_IDS:
                projected[i].append(cv2.perspectiveTransform(markers[i][None], H)[0])
    if not projected[0]:
        return None
    return {i: np.median(np.stack(p), axis=0).astype(np.float32) for i, p in projected.items()}


def compute_homography(markers, target, layout, min_markers):
    """Homography from screenshot to target coordinates, or None."""
    if len(markers) == 4:
        return cv2.getPerspectiveTransform(inner_corners(markers), target)
    if layout is None or len(markers) < min_markers:
        return None
    src = np.concatenate([markers[i] for i in sorted(markers)])
    dst = np.concatenate([layout[i] for i in sorted(markers)])
    H, _ = cv2.findHomography(src, dst, 0)
    return H


def map_point(H, point):
    return cv2.perspectiveTransform(np.float32([[point]]), H)[0, 0]


def put_text(image, lines, origin=(10, 25)):
    x, y = origin
    for line in lines:
        (w, h), baseline = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
        cv2.rectangle(image, (x - 4, y - h - 4), (x + w + 4, y + baseline + 2), (0, 0, 0), cv2.FILLED)
        cv2.putText(image, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, COLOR_TEXT, 1, cv2.LINE_AA)
        y += 24


def draw_trail(image, points, scale=1.0, offset=(0, 0)):
    pts = [(int(round(p[0] * scale + offset[0])), int(round(p[1] * scale + offset[1])))
           for p in points if p is not None]
    for a, b in zip(pts, pts[1:]):
        cv2.line(image, a, b, COLOR_TRAIL, 2, cv2.LINE_AA)
    if pts and points[-1] is not None:
        cv2.circle(image, pts[-1], 10, COLOR_GAZE, 3, cv2.LINE_AA)
        cv2.drawMarker(image, pts[-1], COLOR_GAZE, cv2.MARKER_CROSS, 24, 2, cv2.LINE_AA)


def render_frame(image, markers, H, gaze_trail, mapped_trail, width, height, info):
    """Side-by-side frame: screenshot with original gaze | target area with mapped gaze."""
    # Left panel: original screenshot
    scale = PANEL_HEIGHT / image.shape[0]
    left = cv2.resize(image, (int(round(image.shape[1] * scale)), PANEL_HEIGHT))
    for i, c in markers.items():
        pts = np.int32(np.round(c * scale))
        cv2.polylines(left, [pts], True, COLOR_MARKERS, 2, cv2.LINE_AA)
        cv2.putText(left, str(i), tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.6, COLOR_MARKERS, 2)
    if H is not None:
        # Outline of the target area, projected back into the screenshot
        area = cv2.perspectiveTransform(target_rectangle(width, height)[None], np.linalg.inv(H))[0]
        cv2.polylines(left, [np.int32(np.round(area * scale))], True, (255, 200, 0), 1, cv2.LINE_AA)
    draw_trail(left, gaze_trail, scale)
    put_text(left, info["left"])

    # Right panel: target area (plus margin) rectified with the homography
    mx, my = MARGIN * width, MARGIN * height
    full_w, full_h = width + 2 * mx, height + 2 * my
    rscale = PANEL_HEIGHT / full_h
    right_w = int(round(full_w * rscale))
    if H is not None:
        shift = np.array([[rscale, 0, mx * rscale], [0, rscale, my * rscale], [0, 0, 1]])
        right = cv2.warpPerspective(image, shift @ H, (right_w, PANEL_HEIGHT))
    else:
        right = np.full((PANEL_HEIGHT, right_w, 3), 40, np.uint8)
    tl = (int(round(mx * rscale)), int(round(my * rscale)))
    br = (int(round((mx + width) * rscale)), int(round((my + height) * rscale)))
    cv2.rectangle(right, tl, br, (255, 200, 0), 1, cv2.LINE_AA)
    draw_trail(right, mapped_trail, rscale, (mx * rscale, my * rscale))
    put_text(right, info["right"])

    return np.hstack([left, right])


class VideoWriter:
    """Writes BGR frames to an H.264 MP4 via ffmpeg, or via OpenCV if ffmpeg is unavailable."""

    def __init__(self, path, size, fps):
        w, h = size
        self.proc = self.cv_writer = None
        if shutil.which("ffmpeg"):
            self.proc = subprocess.Popen(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                 str(path)],
                stdin=subprocess.PIPE)
        else:
            self.cv_writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

    def write(self, frame):
        if self.proc:
            self.proc.stdin.write(frame.tobytes())
        else:
            self.cv_writer.write(frame)

    def close(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait()
        else:
            self.cv_writer.release()


def process_directory(directory, detector, args):
    gaze_file = directory / "gaze.txt"
    if not gaze_file.exists():
        print(f"{directory}: no gaze.txt, skipped")
        return
    shots = sorted((int(m.group(1)), p) for p in directory.glob("screenshot_*.jpg")
                   if (m := SCREENSHOT_NAME.search(p.name)))
    if not shots:
        print(f"{directory}: no screenshots, skipped")
        return
    gaze_ts, gaze_pts = read_gaze(gaze_file)

    print(f"{directory}: detecting markers on {len(shots)} screenshots ...")
    detections = [detect_markers(detector, cv2.imread(str(p))) for _, p in shots]

    width = args.width
    height = args.height
    if height is None:
        aspect = estimate_aspect(detections)
        height = round(width * aspect) if aspect else width
    target = target_rectangle(width, height)
    layout = learn_layout(detections, target)

    if args.fps:
        fps = args.fps
    else:
        intervals = np.diff([ts for ts, _ in shots])
        fps = 1e9 / np.median(intervals) if len(intervals) else 30

    rows = []
    gaze_trail, mapped_trail = [], []
    writer = None
    counts = {"4 markers": 0, "3 markers": 0, "no homography": 0, "no gaze": 0}
    for (ts, path), markers in zip(shots, detections):
        image = cv2.imread(str(path))
        gaze = lookup_gaze(gaze_ts, gaze_pts, ts)
        H = compute_homography(markers, target, layout, args.min_markers)
        mapped = map_point(H, gaze) if (H is not None and gaze is not None) else None

        if gaze is None:
            counts["no gaze"] += 1
        if H is None:
            counts["no homography"] += 1
        else:
            counts[f"{len(markers)} markers"] += 1

        gaze_trail = (gaze_trail + [gaze])[-TRAIL_LENGTH:]
        mapped_trail = (mapped_trail + [mapped])[-TRAIL_LENGTH:]

        rows.append({
            "timestamp": ts,
            "screenshot": path.name,
            "markers": "".join(str(i) for i in sorted(markers)),
            "gaze_x": f"{gaze[0]:.2f}" if gaze else "",
            "gaze_y": f"{gaze[1]:.2f}" if gaze else "",
            "target_x": f"{mapped[0]:.2f}" if mapped is not None else "",
            "target_y": f"{mapped[1]:.2f}" if mapped is not None else "",
            "target_u": f"{mapped[0] / width:.4f}" if mapped is not None else "",
            "target_v": f"{mapped[1] / height:.4f}" if mapped is not None else "",
            "inside": (int(0 <= mapped[0] <= width and 0 <= mapped[1] <= height)
                       if mapped is not None else ""),
        })

        status = (f"homography from {len(markers)} markers" if H is not None
                  else f"no homography ({len(markers)} markers found)")
        info = {
            "left": [f"{directory.name}  t={ts}",
                     f"screenshot gaze: ({gaze[0]:.1f}, {gaze[1]:.1f})" if gaze else "no gaze sample"],
            "right": [status,
                      f"target gaze: ({mapped[0]:.1f}, {mapped[1]:.1f}) of {width}x{height}"
                      if mapped is not None else "target gaze: -"],
        }
        frame = render_frame(image, markers, H, gaze_trail, mapped_trail, width, height, info)
        if writer is None:
            writer = VideoWriter(directory / "gaze_animation.mp4", (frame.shape[1], frame.shape[0]), fps)
        writer.write(frame)
    writer.close()

    with open(directory / "gaze_mapped.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"{directory}: target area {width}x{height}, {fps:.1f} fps, "
          + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    print(f"  wrote {directory / 'gaze_animation.mp4'} and {directory / 'gaze_mapped.csv'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", nargs="?", default=".", help="directory containing the frame* directories")
    parser.add_argument("--width", type=int, default=600, help="target area width in pixels (default 600)")
    parser.add_argument("--height", type=int,
                        help="target area height in pixels (default: from the observed aspect ratio)")
    parser.add_argument("--fps", type=float, help="animation frame rate (default: screenshot rate)")
    parser.add_argument("--min-markers", type=int, default=3, choices=(3, 4),
                        help="minimum number of visible markers to compute a homography (default 3)")
    args = parser.parse_args()

    directories = sorted(p for p in Path(args.root).iterdir() if p.is_dir() and p.name.startswith("frame"))
    if not directories:
        sys.exit(f"No directories starting with 'frame' in {args.root}")

    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(ARUCO_DICT),
                                       cv2.aruco.DetectorParameters())
    for directory in directories:
        process_directory(directory, detector, args)


if __name__ == "__main__":
    main()
