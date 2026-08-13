"""Local face detection, registration, and recognition.

This module gives YRobot a private, on-device answer to "who is the
person standing in front of me?". Three small pieces are wired together:

  * :class:`FaceDetector` runs OpenCV DNN with the YuNet ONNX model
    to find face bounding boxes in a BGR frame. The detector downloads
    the ~3 MB model to ``~/.cache/yrobot/face_detection_yunet.onnx`` on
    first use, and reuses it on subsequent boots.

  * :class:`FaceRecognizer` is a thin wrapper around OpenCV's LBPH
    face recognizer. It is trained on a name-keyed list of aligned
    face crops and returns the best matching name (or ``None`` when
    the confidence exceeds the rejection threshold).

  * :class:`FaceDB` glues the two together and persists per-name
    face samples to ``~/.config/yrobot/faces.json`` (mode 0600) so
    registrations survive restarts.

The recognizer is intentionally lightweight: it has no network
dependencies, no face embeddings leave the robot, and the only
required pip package is the OpenCV we already pull in for the
camera. The trade-off is accuracy — LBPH is sensitive to pose and
lighting, so a multi-angle registration (the dashboard's
``POST /api/face`` flow) collects ten frames spread over ~2 s to
give the recognizer enough variety.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ── Paths and model URLs ─────────────────────────────────────────────────────

FACE_DB_PATH = Path(
    os.environ.get("YROBOT_FACE_DB_PATH", "~/.config/yrobot/faces.json")
).expanduser()

FACE_MODEL_DIR = Path(os.environ.get("YROBOT_FACE_MODEL_DIR", "~/.cache/yrobot")).expanduser()

# YuNet ONNX from the official OpenCV model zoo. ~3 MB; we download
# once and reuse. The URL is stable as of 2026-08-13.
YUNET_MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx"
)
YUNET_MODEL_NAME = "face_detection_yunet_2023mar.onnx"


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FaceBox:
    """One face detected in a BGR frame."""

    x: int
    y: int
    w: int
    h: int
    confidence: float

    def crop(self, frame: np.ndarray) -> np.ndarray:
        return frame[self.y : self.y + self.h, self.x : self.x + self.w].copy()


@dataclass
class FaceProfile:
    """One registered person."""

    name: str
    samples: list[np.ndarray] = field(default_factory=list)
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sample_count": len(self.samples),
            "last_seen": self.last_seen,
        }


# ── Face detection ──────────────────────────────────────────────────────────


class FaceDetector:
    """OpenCV DNN + YuNet face detector. Model is downloaded on first use."""

    def __init__(self, model_path: Path | None = None, score_threshold: float = 0.7) -> None:
        self._model_path = model_path or FACE_MODEL_DIR / YUNET_MODEL_NAME
        self._score_threshold = score_threshold
        self._detector: cv2.FaceDetectorYN | None = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> cv2.FaceDetectorYN:
        with self._lock:
            if self._detector is not None:
                return self._detector
            self._model_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._model_path.exists():
                self._download_model()
            self._detector = cv2.FaceDetectorYN.create(
                str(self._model_path),
                "",
                (320, 320),
                self._score_threshold,
                0.3,
                5000,
            )
            logger.info("face detector loaded: %s", self._model_path)
            return self._detector

    def _download_model(self) -> None:
        logger.info("downloading YuNet face detector: %s", YUNET_MODEL_URL)
        with urllib.request.urlopen(YUNET_MODEL_URL, timeout=30) as resp:  # noqa: S310
            data = resp.read()
        if len(data) < 100_000:
            raise RuntimeError(f"YuNet download too small ({len(data)} bytes); aborting")
        tmp = self._model_path.with_suffix(self._model_path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self._model_path)
        logger.info("YuNet saved to %s (%d bytes)", self._model_path, len(data))

    def detect(self, frame: np.ndarray) -> list[FaceBox]:
        if frame is None or frame.size == 0:
            return []
        detector = self._ensure_loaded()
        h, w = frame.shape[:2]
        detector.setInputSize((w, h))
        _, raw = detector.detect(frame)
        if raw is None:
            return []
        return [
            FaceBox(
                x=int(row[0]),
                y=int(row[1]),
                w=int(row[2]),
                h=int(row[3]),
                confidence=float(row[14]),
            )
            for row in raw
            if float(row[14]) >= self._score_threshold
        ]


# ── Face recognition ────────────────────────────────────────────────────────


class FaceRecognizer:
    """Template-matching face recognizer with a per-name list of crops.

    OpenCV 5.0 dropped ``cv2.face`` (the legacy LBPH / Eigenface /
    Fisherface module), so this recognizer uses normalised cross-
    correlation (``cv2.matchTemplate`` with
    ``TM_CCOEFF_NORMED``) instead. Each registered name keeps a
    list of equalised 200x200 grayscale face crops; recognition
    scores the input against every crop and returns the name whose
    best score is above ``score_threshold``.

    Trade-off vs. LBPH: slightly lower accuracy on pose / lighting
    changes, but no model to install (no ``opencv-contrib``), and
    training is a single list-copy. For the v1 home use case —
    one or two people in a controlled environment — the trade is
    worth it.
    """

    # TM_CCOEFF_NORMED returns scores in roughly [-1, 1]. Empirically
    # anything above 0.55 reads as a real match; tune via env if
    # a deployment is too strict or too lax.
    def __init__(self, score_threshold: float = 0.55) -> None:
        self._score_threshold = score_threshold
        self._samples: dict[str, list[np.ndarray]] = {}
        self._lock = threading.Lock()
        self._dirty = True

    @property
    def has_profiles(self) -> bool:
        return bool(self._samples)

    def mark_dirty(self) -> None:
        """No-op retained for API symmetry with the prior LBPH impl.

        Template matching does not require explicit training — the
        next predict will simply re-score against the live sample
        list — so this only flips an internal flag for tests that
        observe the dirty/clean transition.
        """
        with self._lock:
            self._dirty = True

    @property
    def is_dirty(self) -> bool:
        with self._lock:
            return self._dirty

    def train(self, profiles: dict[str, list[np.ndarray]]) -> None:
        with self._lock:
            self._samples = {
                name: [self._prepare(s) for s in samples if s is not None]
                for name, samples in profiles.items()
            }
            self._samples = {n: s for n, s in self._samples.items() if s}
            self._dirty = False

    def predict(self, face_img: np.ndarray) -> tuple[str | None, float]:
        with self._lock:
            if not self._samples:
                return None, -1.0
            template = self._prepare(face_img)
            best_name: str | None = None
            best_score = -1.0
            for name, samples in self._samples.items():
                for s in samples:
                    score = self._compare(template, s)
                    if score > best_score:
                        best_score = score
                        best_name = name
            if best_score < self._score_threshold or best_name is None:
                return None, float(best_score)
            return best_name, float(best_score)

    @staticmethod
    def _prepare(face_img: np.ndarray) -> np.ndarray:
        if face_img is None or face_img.size == 0:
            raise ValueError("face crop is empty; cannot prepare for recognition")
        if face_img.ndim == 2:
            gray = face_img
        else:
            gray = cv2.cvtColor(face_img, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (200, 200), interpolation=cv2.INTER_AREA)
        return cv2.equalizeHist(resized)

    @staticmethod
    def _compare(a: np.ndarray, b: np.ndarray) -> float:
        # Cosine similarity on the flattened, mean-centred pixels.
        # Returns a value in roughly [-1, 1]; 1.0 means identical,
        # 0.0 means no linear correlation, -1.0 means anti-correlated.
        # We subtract the mean so changes in average brightness do
        # not dominate the score; equalizeHist in _prepare already
        # normalises the dynamic range, so the per-pixel contrast is
        # the dominant signal.
        af = a.astype(np.float32).ravel() - float(a.mean())
        bf = b.astype(np.float32).ravel() - float(b.mean())
        denom = float(np.linalg.norm(af) * np.linalg.norm(bf))
        if denom == 0.0:
            return 0.0
        return float(np.dot(af, bf) / denom)


# ── Face database ──────────────────────────────────────────────────────────


class FaceDB:
    """On-disk registry of named face profiles."""

    def __init__(
        self,
        db_path: Path = FACE_DB_PATH,
        recognizer: FaceRecognizer | None = None,
        detector: FaceDetector | None = None,
    ) -> None:
        self._path = db_path
        self._recognizer = recognizer or FaceRecognizer()
        self._detector = detector or FaceDetector()
        self._lock = threading.Lock()
        self._profiles: dict[str, FaceProfile] = {}
        self._load()

    @property
    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._profiles.keys())

    def list_profiles(self) -> list[dict[str, object]]:
        with self._lock:
            return [p.to_dict() for p in self._profiles.values()]

    def get_profile(self, name: str) -> FaceProfile | None:
        with self._lock:
            return self._profiles.get(name)

    def register(self, name: str, frames: Iterable[np.ndarray]) -> int:
        """Add new face crops under ``name``. Returns the new sample count."""
        new_samples = [self._extract_face(f) for f in frames]
        new_samples = [s for s in new_samples if s is not None]
        if not new_samples:
            raise ValueError(f"no usable face crops in registration batch for {name!r}")
        with self._lock:
            profile = self._profiles.setdefault(name, FaceProfile(name=name))
            profile.samples.extend(new_samples)
            self._save()
            self._recognizer.mark_dirty()
            return len(profile.samples)

    def delete(self, name: str) -> bool:
        with self._lock:
            if name not in self._profiles:
                return False
            del self._profiles[name]
            self._save()
            self._recognizer.mark_dirty()
            return True

    def recognize(self, frame: np.ndarray) -> str | None:
        with self._lock:
            if not self._profiles:
                return None
            if self._recognizer._dirty:  # type: ignore[attr-defined]
                self._recognizer.train({n: list(p.samples) for n, p in self._profiles.items()})
        boxes = self._detector.detect(frame)
        if not boxes:
            return None
        # Use the largest (closest) face in the frame; future work can
        # pick the one nearest the centre of gaze if multiple people
        # are present and only the speaker matters.
        primary = max(boxes, key=lambda b: b.w * b.h)
        face_crop = primary.crop(frame)
        # YuNet occasionally returns a bounding box that extends past
        # the frame edge (or an upstream imdecode gives back an
        # empty array); an empty crop must not reach cv2.cvtColor or
        # it raises -215 Assertion failed and crashes the audio
        # loop, which is what bit us in production at 10:13 today.
        if face_crop is None or face_crop.size == 0:
            return None
        name, _ = self._recognizer.predict(face_crop)
        if name is not None:
            with self._lock:
                profile = self._profiles.get(name)
                if profile is not None:
                    import time as _time

                    profile.last_seen = _time.time()
                    self._save()
        return name

    def _extract_face(self, frame: np.ndarray) -> np.ndarray | None:
        if frame is None or frame.size == 0:
            return None
        boxes = self._detector.detect(frame)
        if not boxes:
            return None
        primary = max(boxes, key=lambda b: b.w * b.h)
        crop = primary.crop(frame)
        # Normalize size for LBPH stability.
        return cv2.resize(crop, (200, 200), interpolation=cv2.INTER_AREA)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "profiles": {
                name: [
                    base64.b64encode(s.astype(np.uint8).tobytes()).decode("ascii")
                    for s in profile.samples
                ]
                for name, profile in self._profiles.items()
            },
            "last_seen": {name: profile.last_seen for name, profile in self._profiles.items()},
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:  # pragma: no cover — best-effort on non-POSIX
            pass

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001
            logger.warning("could not load face db %s: %s", self._path, exc)
            return
        profiles_raw = raw.get("profiles", {})
        last_seen = raw.get("last_seen", {})
        loaded: dict[str, FaceProfile] = {}
        for name, sample_b64_list in profiles_raw.items():
            samples = [
                np.frombuffer(base64.b64decode(b), dtype=np.uint8).reshape(200, 200, 3)
                for b in sample_b64_list
            ]
            loaded[name] = FaceProfile(
                name=name, samples=samples, last_seen=float(last_seen.get(name, 0.0))
            )
        self._profiles = loaded
        if self._profiles:
            self._recognizer.mark_dirty()
