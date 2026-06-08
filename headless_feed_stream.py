import json
import time

import cv2
import zmq

STREAM_URL = "http://amssamples.streaming.mediaservices.windows.net/91492735-c523-432b-ba01-faba6c2206a2/AzureMediaServicesPromo.ism/manifest(format=m3u8-aapl)"
PUB_ADDR = "tcp://127.0.0.1:5555"


def open_capture(url: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open stream: {url}")
    return cap


def main() -> None:
    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind(PUB_ADDR)
    time.sleep(0.5)

    print(f"[INGEST] Publishing frames on {PUB_ADDR}")
    print(f"[INGEST] Source stream: {STREAM_URL}")

    cap = open_capture(STREAM_URL)
    frame_count = 0
    started = time.time()

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("[INGEST] Frame read failed. Reconnecting in 2s...")
                cap.release()
                time.sleep(2)
                cap = open_capture(STREAM_URL)
                continue

            meta = {"dtype": str(frame.dtype), "shape": frame.shape}
            socket.send_multipart([json.dumps(meta).encode("utf-8"), frame.tobytes()])

            frame_count += 1
            if frame_count % 120 == 0:
                elapsed = max(time.time() - started, 1e-6)
                print(f"[INGEST] Sent {frame_count} frames ({frame_count / elapsed:.2f} fps)")

    except KeyboardInterrupt:
        print("\n[INGEST] Stopping...")
    finally:
        cap.release()
        socket.close(0)
        context.term()


if __name__ == "__main__":
    main()
