"""The media tools a job uses besides draw-things-cli, as one value."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from draw_things_control.jobs.media.info import MediaInfo

# Writes the last frame of a video to a PNG; raises ValueError when it cannot.
FrameExtractor = Callable[[Path, Path], None]
# Writes color tags into a finished video's container; returns whether it changed the file.
VideoTagger = Callable[[Path], bool]
# Measures what an output file actually holds; raises ValueError when it cannot.
OutputMeasurer = Callable[[Path], MediaInfo]


@dataclass(frozen=True)
class MediaTools:
    """What a job needs for its videos: checks that ffmpeg and ffprobe exist, the last-frame extractor, and, when given, the tagger and the measurer."""

    require_ffmpeg: Callable[[], object]
    frame_extractor: FrameExtractor
    require_ffprobe: Callable[[], object] | None = None
    video_tagger: VideoTagger | None = None
    output_measurer: OutputMeasurer | None = None
