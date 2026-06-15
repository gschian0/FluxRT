import random
import time
import cv2
import numpy as np

SHOW_NAMES = [
    "Quantum Flux TV",
    "Synthwave Sunday",
    "Cybernetic News Network",
    "Midnight AI Broadcast",
    "Neon Glow Live",
    "Neural Network Morning",
]

_current_show_str = SHOW_NAMES[0]
_show_last_changed = time.time()

def add_tv_overlay(frame_bgr: np.ndarray) -> np.ndarray:
    global _current_show_str, _show_last_changed
    now = time.time()
    
    if now - _show_last_changed > 15.0: # Change every 15 seconds
        _current_show_str = random.choice(SHOW_NAMES)
        _show_last_changed = now
        
    out = frame_bgr.copy()
    h, w = out.shape[:2]
    
    # Draw lower third background (semi-transparent)
    overlay = out.copy()
    cv2.rectangle(overlay, (0, h - 80), (w, h), (0, 0, 0), -1)
    
    # Draw "LIVE" box
    cv2.rectangle(overlay, (20, h - 60), (100, h - 20), (0, 0, 200), -1)
    cv2.addWeighted(overlay, 0.6, out, 0.4, 0, out)
    
    # Texts
    cv2.putText(out, "LIVE", (30, h - 33), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.putText(out, _current_show_str, (120, h - 35), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    
    # Add time
    current_time_str = time.strftime("%H:%M:%S")
    cv2.putText(out, current_time_str, (w - 150, h - 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    
    return out

