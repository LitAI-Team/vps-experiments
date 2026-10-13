#!/usr/bin/env python3
"""
Poll the VPS eye tracking glasses at 30 Hz and print the current gaze point.

The unit pushes gaze data as an RTSP/RTP stream of KLV units (see
livestream-api-documentation.pdf, section 2.4.3). A background thread receives
that stream and keeps the latest sample; the main loop polls it every 1/30 s
and prints it as:

    x=..., y=..., timestamp=...

x/y are gaze_2d (front camera pixel coordinates), timestamp is
glasses_timestamp (u64 nanoseconds, monotonic since boot of the glasses).
A tick is skipped when no new sample arrived since the previous one.

For every printed sample, the latest front camera frame is saved as a freeze
frame screenshot_<timestamp>.jpg (same glasses_timestamp as printed). The front
camera stream (section 2.4.1) is decoded by an ffmpeg subprocess.

Usage:
    python gaze_poller.py [UNIT-IP] [--rate HZ] [--outdir DIR]
                                    (default: 10.42.0.1, 30 Hz, current dir)

Requires the Python standard library and ffmpeg on the PATH.
"""
import argparse
import os
import socket
import subprocess
import threading
import time

from vps_gaze_reader import (ROOT_GAZE, _strip_rtp, decode, iter_klv_units,
                             parse_gaze_unit, rtsp_response)

TAG_TIMESTAMP = 1
TAG_GAZE_2D = 3
KEEPALIVE_INTERVAL = 20.0  # seconds; keeps the RTSP session from timing out


class GazeStream:
    """RTSP/UDP receiver for the gaze KLV stream; exposes the latest sample."""

    def __init__(self, ip, style="basic"):
        self.ip = ip
        self.url = f"rtsp://{ip}/api/livestream.whep?media=gazedata.format(klv).style({style})"
        self.latest = None  # (x, y, timestamp_ns)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cseq = 0
        self._session = ""

    def _send(self, method, target, extra=""):
        self._cseq += 1
        self.ctrl.sendall(f"{method} {target} RTSP/1.0\r\nCSeq: {self._cseq}\r\n{extra}\r\n".encode())
        return rtsp_response(self.ctrl)

    def start(self):
        self.ctrl = socket.create_connection((self.ip, 554), timeout=8)
        hdr, sdp = self._send("DESCRIBE", self.url, "Accept: application/sdp\r\n")
        if " 200" not in hdr.splitlines()[0]:
            raise RuntimeError(f"DESCRIBE failed: {hdr.splitlines()[0]}")
        control = self.url
        for line in sdp.splitlines():
            if line.startswith("a=control:") and "rtsp" in line:
                control = line.split("a=control:", 1)[1].strip()

        # RTP/RTCP need an even/odd consecutive port pair
        while True:
            self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.rtp.bind(("", 0))
            cport = self.rtp.getsockname()[1]
            self.rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.rtcp.bind(("", cport + 1))
                break
            except OSError:
                self.rtp.close(); self.rtcp.close()

        hdr, _ = self._send("SETUP", control,
                            f"Transport: RTP/AVP;unicast;client_port={cport}-{cport + 1}\r\n")
        if " 200" not in hdr.splitlines()[0]:
            raise RuntimeError(f"SETUP failed: {hdr.splitlines()[0]}")
        srv_rtp = None
        for line in hdr.split("\r\n"):
            if line.lower().startswith("session:"):
                self._session = line.split(":", 1)[1].split(";")[0].strip()
            if line.lower().startswith("transport:"):
                for part in line.split(";"):
                    if part.strip().lower().startswith("server_port="):
                        srv_rtp = int(part.split("=", 1)[1].split("-")[0])

        # open the return path through any NAT/firewall
        if srv_rtp:
            for _ in range(3):
                self.rtp.sendto(b"\x00", (self.ip, srv_rtp))

        hdr, _ = self._send("PLAY", self.url, f"Session: {self._session}\r\n")
        if " 200" not in hdr.splitlines()[0]:
            raise RuntimeError(f"PLAY failed: {hdr.splitlines()[0]}")

        self._thread = threading.Thread(target=self._receive, daemon=True)
        self._thread.start()

    def _receive(self):
        self.rtp.settimeout(0.5)
        last_keepalive = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_keepalive > KEEPALIVE_INTERVAL:
                try:
                    self._send("GET_PARAMETER", self.url, f"Session: {self._session}\r\n")
                except OSError:
                    pass
                last_keepalive = time.monotonic()
            try:
                pkt, _ = self.rtp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            for src, payload in iter_klv_units(_strip_rtp(pkt)):
                if src != ROOT_GAZE:
                    continue
                fields = parse_gaze_unit(payload)
                # fields are optional per sample; gaze_2d is absent when not valid
                if TAG_GAZE_2D not in fields or TAG_TIMESTAMP not in fields:
                    continue
                x, y = decode(fields[TAG_GAZE_2D], "f64x2")
                ts = decode(fields[TAG_TIMESTAMP], "u64")
                with self._lock:
                    self.latest = (x, y, ts)

    def poll(self):
        with self._lock:
            return self.latest

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._send("TEARDOWN", self.url, f"Session: {self._session}\r\n")
        except OSError:
            pass
        self.ctrl.close()
        self.rtp.close()
        self.rtcp.close()


class FrameStream:
    """Decodes the front camera RTSP stream with ffmpeg; exposes the latest frame as JPEG bytes."""

    def __init__(self, ip):
        self.url = f"rtsp://{ip}/api/livestream.whep?media=frontcam.codec(h264)"
        self.latest = None  # JPEG bytes
        self._lock = threading.Lock()

    def start(self):
        # ffmpeg re-encodes every decoded frame to JPEG and writes them back-to-back to stdout
        self._proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "udp",
             "-fflags", "nobuffer", "-flags", "low_delay", "-i", self.url,
             "-an", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        self._thread = threading.Thread(target=self._receive, daemon=True)
        self._thread.start()

    def _receive(self):
        buf = b""
        while True:
            chunk = self._proc.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
            # each frame is SOI (ff d8) ... EOI (ff d9); keep only the newest complete one
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if end < 0:
                    break
                with self._lock:
                    self.latest = buf[start:end + 2]
                buf = buf[end + 2:]

    def poll(self):
        with self._lock:
            return self.latest

    def stop(self):
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._thread.join(timeout=2)


def main():
    ap = argparse.ArgumentParser(description="Poll VPS glasses gaze data and print it.")
    ap.add_argument("ip", nargs="?", default="10.42.0.1", help="unit IP (default: 10.42.0.1)")
    ap.add_argument("--rate", type=float, default=30.0, help="polling rate in Hz (default: 30)")
    ap.add_argument("--outdir", default=".", help="directory for screenshot_<timestamp>.jpg (default: .)")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    frames = FrameStream(args.ip)
    frames.start()
    stream = GazeStream(args.ip)
    stream.start()
    period = 1.0 / args.rate
    last_ts = None
    next_tick = time.monotonic()
    try:
        while True:
            sample = stream.poll()
            if sample is not None and sample[2] != last_ts:
                x, y, ts = sample
                print(f"x={x:.2f}, y={y:.2f}, timestamp={ts}", flush=True)
                frame = frames.poll()
                if frame is not None:
                    with open(os.path.join(args.outdir, f"screenshot_{ts}.jpg"), "wb") as f:
                        f.write(frame)
                last_ts = ts
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind; resync
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        frames.stop()


if __name__ == "__main__":
    main()
