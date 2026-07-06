import cv2
import time

# Configuration from documentation
UNIT_IP = "10.42.0.1"
# Requesting the front camera with h264 codec as shown in documentation examples
STREAM_URL = f"http://{UNIT_IP}/api/livestream.mp4?media=frontcam.codec(h264)"
DURATION_SECONDS = 10

def stream_gaze_data():
    print(f"Connecting to eye tracker at {STREAM_URL}...")
    
    # Initialize the video capture with the fragmented MP4 endpoint
    cap = cv2.VideoCapture(STREAM_URL)
    
    if not cap.isOpened():
        print("Error: Could not connect to the eye tracker stream.")
        return

    print("Connected successfully. Streaming data for 10 seconds...\n")
    start_time = time.time()
    frame_count = 0

    try:
        while True:
            # Check if 10 seconds have elapsed
            elapsed_time = time.time() - start_time
            if elapsed_time >= DURATION_SECONDS:
                print(f"\n[Status] 10 seconds elapsed. Stopping stream.")
                break

            # Read a frame from the livestream
            ret, frame = cap.read()
            if not ret:
                print("[Warning] Failed to grab frame or stream interrupted.")
                break

            frame_count += 1
            
            # Print streaming telemetry updates
            print(f"Reading stream... Time Elapsed: {elapsed_time:.1f}s | Frames received: {frame_count}", end="\r")

    finally:
        # Always clean up and release the stream resource
        cap.release()
        print("Stream connection closed cleanly.")

if __name__ == "__main__":
    stream_gaze_data()