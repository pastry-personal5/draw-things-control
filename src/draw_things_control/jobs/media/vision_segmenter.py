"""The Apple Vision ``Segmenter`` (Milestone 09's increment E): people with ``VNGeneratePersonSegmentationRequest``
(``balanced``), and faces with ``VNDetectFaceLandmarksRequest``, from a frame's PNG bytes.

pyobjc is imported when a segmenter is made, never at import, as ``checks.py`` imports LittleCMS, so no other command
loads it. As found on 2026-10-01 with pyobjc 12.2.2: the requests' ``init`` is unavailable, so they are made with
``initWithCompletionHandler_(None)``; the person mask is 8-bit (``kCVPixelFormatType_OneComponent8``) at Vision's own
resolution (512x384 for an 832x448 frame), stretched over the whole frame; and ``pointsInImageOfSize_`` gives landmark
points in image pixels with the origin at the lower left, so their y is turned over here. Each call runs in its own
autorelease pool, since a server's worker thread lives for days and nothing else would drain what Vision hands back.
"""

from __future__ import annotations

import importlib
import io
import sys
from typing import Any

import numpy as np
from loguru import logger
from PIL import Image

from draw_things_control.jobs.media.regions import Face, Segmentation, Segmenter

# Landmark regions read from each face, by the names Vision gives them.
LANDMARKS = {"contour": "faceContour", "left_eye": "leftEye", "right_eye": "rightEye", "left_brow": "leftEyebrow", "right_brow": "rightEyebrow", "lips": "outerLips"}


class VisionSegmenter:
    """Apple Vision's person mask and face landmarks of a frame (H, W, 3 sRGB values, 1.0 for white)."""

    def __init__(self) -> None:
        self._objc: Any = importlib.import_module("objc")
        self._vision: Any = importlib.import_module("Vision")
        self._quartz: Any = importlib.import_module("Quartz")
        self._foundation: Any = importlib.import_module("Foundation")

    def __call__(self, frame: np.ndarray) -> Segmentation:
        height, width = frame.shape[:2]
        png = png_bytes(frame)
        vision, quartz = self._vision, self._quartz
        with self._objc.autorelease_pool():
            data = self._foundation.NSData.dataWithBytes_length_(png, len(png))
            handler = vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
            person = vision.VNGeneratePersonSegmentationRequest.alloc().initWithCompletionHandler_(None)
            person.setQualityLevel_(vision.VNGeneratePersonSegmentationRequestQualityLevelBalanced)
            person.setOutputPixelFormat_(quartz.kCVPixelFormatType_OneComponent8)
            faces = vision.VNDetectFaceLandmarksRequest.alloc().initWithCompletionHandler_(None)
            succeeded, error = handler.performRequests_error_([person, faces], None)
            if not succeeded:
                raise ValueError(f"Vision could not read the frame: {error}")
            results = person.results()
            mask = self._mask(results[0].pixelBuffer()) if results else np.zeros((height, width), dtype=np.uint8)
            found = tuple(face for observation in faces.results() or () if (face := self._face(observation, width, height)) is not None)
        return Segmentation(mask, found)

    def _mask(self, buffer: Any) -> np.ndarray:
        """The person mask's bytes, copied out row by row, past each row's padding."""
        quartz = self._quartz
        if quartz.CVPixelBufferGetPixelFormatType(buffer) != quartz.kCVPixelFormatType_OneComponent8:
            raise ValueError("Vision's person mask is not 8-bit")
        quartz.CVPixelBufferLockBaseAddress(buffer, quartz.kCVPixelBufferLock_ReadOnly)
        try:
            width = quartz.CVPixelBufferGetWidth(buffer)
            height = quartz.CVPixelBufferGetHeight(buffer)
            row = quartz.CVPixelBufferGetBytesPerRow(buffer)
            raw = bytes(quartz.CVPixelBufferGetBaseAddress(buffer).as_buffer(row * height))
        finally:
            quartz.CVPixelBufferUnlockBaseAddress(buffer, quartz.kCVPixelBufferLock_ReadOnly)
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, row)[:, :width].copy()

    def _face(self, observation: Any, width: int, height: int) -> Face | None:
        landmarks = observation.landmarks()
        if landmarks is None:
            return None
        size = self._quartz.CGSizeMake(width, height)
        points: dict[str, np.ndarray] = {}
        for name, attribute in LANDMARKS.items():
            region = getattr(landmarks, attribute)()
            if region is None:
                points[name] = np.zeros((0, 2))
                continue
            located = region.pointsInImageOfSize_(size)
            # Lower-left origin to the top-left origin of the frame's rows.
            points[name] = np.array([(located[index].x, height - located[index].y) for index in range(region.pointCount())], dtype=np.float64).reshape(-1, 2)
        return Face(**points)


def png_bytes(frame: np.ndarray) -> bytes:
    """``frame`` as an 8-bit RGB PNG, quickly compressed: Vision reads it, nothing keeps it."""
    levels = np.clip(np.rint(frame * 255), 0, 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(levels, "RGB").save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def vision_segmenter() -> Segmenter | None:
    """Apple Vision's segmenter when this is macOS and pyobjc's Vision is installed and loads; None otherwise, so a
    check or a correction measures the whole frame as one region."""
    if sys.platform != "darwin":
        return None
    try:
        return VisionSegmenter()
    except ImportError:
        return None
    # Whatever breaks loading pyobjc, the checks and the correction go on without regions.
    except Exception as error:
        logger.warning("Apple Vision could not be loaded ({}); colors are measured and corrected over the whole frame", error)
        return None
