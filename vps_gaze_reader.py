#!/usr/bin/env python3
"""
VPS Next — gaze data (KLV) reference reader.

A single, self-contained example showing the full read path for VPS Next live
gaze data, so you can build an LSL relay (or any other consumer) on top of it:

  1. connect to the unit's RTSP gaze endpoint and receive the RTP/KLV stream
  2. parse the KLV (SMPTE ST 336 / smpte336m) units
  3. decode the gaze fields and print them

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
  Live capture from a unit (default transport: RTSP over UDP):
      python vps_gaze_reader.py live <UNIT-IP> [seconds]
      python vps_gaze_reader.py live 192.168.0.250 10

  Live capture and also save the raw received bytes to a file:
      python vps_gaze_reader.py live 192.168.0.250 10 --save capture.bin

  Parse an existing raw capture (e.g. the provided gaze_klv.bin sample):
      python vps_gaze_reader.py file gaze_klv.bin

  Force TCP-interleaved transport instead of UDP (note: current firmware
  returns 501 for this, so UDP is the working path — flag kept for future):
      python vps_gaze_reader.py live 192.168.0.250 10 --tcp

--------------------------------------------------------------------------
WHAT IT PRODUCES
--------------------------------------------------------------------------
  - Console output: the RTSP handshake status (DESCRIBE/SETUP/PLAY), the
    number of UDP packets received, then the first few gaze samples fully
    decoded (all present fields with values), and the total sample count.
  - With --save: a raw binary file containing the concatenated KLV payload
    bytes as received (RTP headers stripped). This is the same format as the
    provided gaze_klv.bin and can be re-parsed later with `file` mode.
  It does NOT produce an LSL stream — that is the relay you build on top of
  this (take the decoded per-sample fields and push them into a pylsl outlet;
  see the integration guide, sections 5 and 6).

--------------------------------------------------------------------------
NETWORK NOTES (learned from testing)
--------------------------------------------------------------------------
  - RTSP transport is UDP-only on current firmware (TCP-interleaved -> 501).
  - Across a subnet / NAT / VPN the RTP return path must be hole-punched:
    this reader reads server_port from the SETUP response and punches to it
    automatically. If you still get 0 packets, run the relay on the same
    subnet as the unit.
  - The glasses must be worn with gaze active, or no gaze KLV is produced.

--------------------------------------------------------------------------
VERIFIED FACTS BAKED IN (confirmed against live captures)
--------------------------------------------------------------------------
  - transport : RTSP, RTP payload smpte336m, SETUP over UDP
  - endianness: big-endian for all multi-byte values (u16/u64/f64)
  - lengths   : standard BER
  - keys      : 16-byte root UL + 1-byte local-set tags, BER item lengths
  - timestamp : glasses_timestamp = u64 nanoseconds, MONOTONIC (from boot)
  - IMPORTANT : fields are OPTIONAL per sample. A tag is present only when
                valid for that sample; absent tags mean "no value this sample".

Only the gaze source is served today; 9-DoF and ambient are not yet implemented.

This is a reference example, not a production relay: it does not implement
reconnection, RTP-fragment reassembly (not needed at 30 Hz, but a production
relay should guard for it), or the LSL output side.

Requires only the Python standard library.
"""
import socket
import struct
import sys
from urllib.parse import urlparse

# ---- KLV definitions ----------------------------------------------------
ROOT_PREFIX = bytes.fromhex("060e2b34020b01010f010101000000")  # 15-byte common prefix
ROOT_GAZE = 0x00  # last byte of the 16-byte root UL

# tag -> (name, type). type: u64 / u8 / bool / f64 / f64xN
GAZE_FIELDS = {
    1: ("glasses_timestamp", "u64"),   2: ("confidence", "bool"),
    3: ("gaze_2d", "f64x2"),           4: ("blink", "u8"),
    5: ("pupil_diameter_right", "f64"),6: ("pupil_diameter_left", "f64"),
    7: ("gaze_class", "u8"),           8: ("vergence_angle", "f64"),
    9: ("gaze_right_confidence", "bool"), 10: ("eyeball_center_right", "f64x3"),
    11: ("pupil_center_right", "f64x3"),  12: ("gaze_right", "f64x3"),
    13: ("gaze_right_reciprocal_distance", "f64"), 14: ("gaze_right_angular_speed", "f64"),
    15: ("eye_closed_right", "u8"),     16: ("pupil_2d_right_confidence", "bool"),
    17: ("pupil_2d_right", "f64x5"),    18: ("gaze_left_confidence", "bool"),
    19: ("eyeball_center_left", "f64x3"), 20: ("pupil_center_left", "f64x3"),
    21: ("gaze_left", "f64x3"),         22: ("gaze_left_reciprocal_distance", "f64"),
    23: ("gaze_left_angular_speed", "f64"), 24: ("eye_closed_left", "u8"),
    25: ("pupil_2d_left_confidence", "bool"), 26: ("pupil_2d_left", "f64x5"),
}


def read_ber_len(buf, i):
    """Decode a BER length starting at buf[i]. Returns (length, next_index)."""
    b = buf[i]; i += 1
    if b < 0x80:
        return b, i                      # short form
    n = b & 0x7f                         # long form: n length octets, big-endian
    return int.from_bytes(buf[i:i + n], "big"), i + n


def decode(val, typ):
    if typ == "u64":  return int.from_bytes(val, "big")
    if typ == "u8":   return val[0]
    if typ == "bool": return bool(val[0])
    if typ == "f64":  return struct.unpack(">d", val)[0]
    if typ.startswith("f64x"):
        n = int(typ[4:])
        return [struct.unpack(">d", val[8 * k:8 * k + 8])[0] for k in range(n)]
    return val


def parse_gaze_unit(payload):
    """Parse one gaze local-set payload -> {tag: raw_bytes}. Absent tags simply
    won't be in the dict (fields are optional per sample)."""
    fields, i, end = {}, 0, len(payload)
    while i < end:
        tag = payload[i]; i += 1
        ln, i = read_ber_len(payload, i)
        fields[tag] = payload[i:i + ln]; i += ln
    return fields


def iter_klv_units(blob):
    """Yield (source_id, payload_bytes) for each KLV unit found in blob."""
    start = 0
    while True:
        p = blob.find(ROOT_PREFIX, start)
        if p < 0:
            return
        src = blob[p + 15]
        i = p + 16
        try:
            length, i = read_ber_len(blob, i)
            yield src, blob[i:i + length]
        except IndexError:
            return
        start = p + 16


def print_sample(fields, n):
    ts = decode(fields[1], "u64") if 1 in fields else None
    print(f"\n--- gaze sample #{n}  (t = {ts/1e9:.6f} s)  {len(fields)} fields present ---"
          if ts is not None else f"\n--- gaze sample #{n} ---")
    for tag in sorted(fields):
        name, typ = GAZE_FIELDS.get(tag, (f"tag{tag}", "raw"))
        v = decode(fields[tag], typ)
        if isinstance(v, list):
            v = [round(x, 4) for x in v]
        elif isinstance(v, float):
            v = round(v, 4)
        print(f"    {tag:2} {name:32} = {v}")


# ---- RTSP/UDP capture ---------------------------------------------------
def rtsp_response(sock):
    hdr = b""
    while b"\r\n\r\n" not in hdr:
        c = sock.recv(1)
        if not c:
            break
        hdr += c
    text = hdr.decode("utf-8", "replace")
    length = next((int(l.split(":", 1)[1]) for l in text.split("\r\n")
                   if l.lower().startswith("content-length:")), 0)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return text, body.decode("utf-8", "replace")


def _strip_rtp(pkt):
    """Return the payload of one RTP packet (header + CSRC + extension stripped)."""
    if len(pkt) <= 12:
        return b""
    csrc = pkt[0] & 0x0F
    ext = (pkt[0] >> 4) & 0x01
    off = 12 + 4 * csrc
    if ext and len(pkt) >= off + 4:
        extlen = int.from_bytes(pkt[off + 2:off + 4], "big")
        off += 4 + 4 * extlen
    return pkt[off:]


def capture_live(ip, seconds, style="advanced", transport="udp"):
    url = f"rtsp://{ip}/api/livestream.whep?media=gazedata.format(klv).style({style})"
    ctrl = socket.create_connection((ip, 554), timeout=8)
    cseq = 0

    def send(method, target, extra=""):
        nonlocal cseq
        cseq += 1
        ctrl.sendall(f"{method} {target} RTSP/1.0\r\nCSeq: {cseq}\r\n{extra}\r\n".encode())
        return rtsp_response(ctrl)

    hdr, sdp = send("DESCRIBE", url, "Accept: application/sdp\r\n")
    print("[DESCRIBE]", hdr.splitlines()[0])
    control = url
    for line in sdp.splitlines():
        if line.startswith("a=control:") and "rtsp" in line:
            control = line.split("a=control:", 1)[1].strip()

    blob = bytearray()
    import time

    if transport == "tcp":
        # RTP interleaved on the RTSP TCP connection: frames are '$' <ch> <len:2> <rtp>
        hdr, _ = send("SETUP", control,
                      "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
        print("[SETUP]", hdr.splitlines()[0])
        if "200" not in hdr.splitlines()[0]:
            print("[!] SETUP (TCP interleaved) failed — unit may not support it."); ctrl.close(); return b""
        session = next((l.split(":", 1)[1].split(";")[0].strip()
                        for l in hdr.split("\r\n") if l.lower().startswith("session:")), "")
        hdr, _ = send("PLAY", url, f"Session: {session}\r\n")
        print("[PLAY]", hdr.splitlines()[0])
        print(f"[*] capturing {seconds}s (TCP interleaved) ...")
        ctrl.settimeout(seconds + 2)
        buf = b""
        t0 = time.time()
        try:
            while time.time() - t0 < seconds:
                chunk = ctrl.recv(65535)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4 and buf[0:1] == b"$":
                    length = int.from_bytes(buf[2:4], "big")
                    if len(buf) < 4 + length:
                        break
                    blob += _strip_rtp(buf[4:4 + length])
                    buf = buf[4 + length:]
                if buf and buf[0:1] != b"$":
                    nxt = buf.find(b"$")
                    buf = buf[nxt:] if nxt >= 0 else b""
        except socket.timeout:
            pass
    else:
        # UDP transport (default)
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind(("", 0))
        cport = rtp.getsockname()[1]
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rtcp.bind(("", cport + 1))
        except OSError:
            rtp.close(); rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rtp.bind(("", 0)); cport = rtp.getsockname()[1]; rtcp.bind(("", cport + 1))
        hdr, _ = send("SETUP", control,
                      f"Transport: RTP/AVP;unicast;client_port={cport}-{cport+1}\r\n")
        print("[SETUP]", hdr.splitlines()[0])
        if "200" not in hdr.splitlines()[0]:
            print("[!] SETUP failed."); ctrl.close(); return b""
        session = next((l.split(":", 1)[1].split(";")[0].strip()
                        for l in hdr.split("\r\n") if l.lower().startswith("session:")), "")

        # parse server_port from the Transport header for NAT hole-punching
        srv_rtp = srv_rtcp = None
        for line in hdr.split("\r\n"):
            if line.lower().startswith("transport:"):
                for part in line.split(";"):
                    if part.strip().lower().startswith("server_port="):
                        ports = part.split("=", 1)[1].split("-")
                        srv_rtp = int(ports[0])
                        srv_rtcp = int(ports[1]) if len(ports) > 1 else srv_rtp + 1

        # punch the NAT/firewall: send from our RTP/RTCP sockets to the server's
        # RTP/RTCP ports so the return path opens (this is what ffmpeg does).
        if srv_rtp:
            print(f"[*] hole-punching to server_port {srv_rtp}-{srv_rtcp}")
            for _ in range(3):
                try:
                    rtp.sendto(b"\x00", (ip, srv_rtp))
                    rtcp.sendto(b"\x00", (ip, srv_rtcp))
                except Exception:
                    pass

        hdr, _ = send("PLAY", url, f"Session: {session}\r\n")
        print("[PLAY]", hdr.splitlines()[0])
        print(f"[*] capturing {seconds}s (UDP) ...")
        rtp.settimeout(2.0)
        got = 0
        t0 = time.time()
        last_punch = 0
        while time.time() - t0 < seconds:
            # re-punch periodically until data starts flowing
            if got == 0 and srv_rtp and time.time() - last_punch > 1.0:
                try:
                    rtp.sendto(b"\x00", (ip, srv_rtp))
                except Exception:
                    pass
                last_punch = time.time()
            try:
                pkt, _ = rtp.recvfrom(65535)
            except socket.timeout:
                continue
            got += 1
            blob += _strip_rtp(pkt)
        print(f"[*] UDP packets received: {got}")
        rtp.close(); rtcp.close()

    try:
        cseq += 1
        ctrl.sendall(f"TEARDOWN {url} RTSP/1.0\r\nCSeq: {cseq}\r\nSession: {session}\r\n\r\n".encode())
    except Exception:
        pass
    ctrl.close()
    return bytes(blob)


# ---- main ---------------------------------------------------------------
def main():
    args = sys.argv[1:]
    save = None
    if "--save" in args:
        k = args.index("--save"); save = args[k + 1]; del args[k:k + 2]
    transport = "udp"
    if "--tcp" in args:
        transport = "tcp"; args.remove("--tcp")

    if len(args) < 2:
        print(__doc__); return
    mode = args[0]
    if mode == "live":
        ip = args[1]
        seconds = int(args[2]) if len(args) > 2 else 5
        blob = capture_live(ip, seconds, transport=transport)
        if save and blob:
            open(save, "wb").write(blob)
            print(f"[*] raw payload saved: {save} ({len(blob)} bytes)")
    elif mode == "file":
        blob = open(args[1], "rb").read()
    else:
        print("mode must be 'live' or 'file'"); return

    n = 0
    for src, payload in iter_klv_units(blob):
        if src != ROOT_GAZE:
            continue
        n += 1
        fields = parse_gaze_unit(payload)
        if n <= 5:                       # print first few in full
            print_sample(fields, n)

    print(f"\n[*] total gaze samples parsed: {n}")
    if n == 0:
        print("[i] No gaze KLV found. If live: check the unit is streaming gaze "
              "(glasses on, gaze active) and that RTSP/UDP is reachable. "
              "If UDP packets = 0, try --tcp (interleaved) or check the PC firewall.")


if __name__ == "__main__":
    main()
