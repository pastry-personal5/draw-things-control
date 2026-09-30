"""The media tools a job uses besides draw-things-cli, as one value."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from draw_things_control.jobs.media.info import MediaInfo
from draw_things_control.jobs.media.stream_color import StreamColor

if TYPE_CHECKING:
    from draw_things_control.jobs.media.checks import MediaChecker

# Writes the last frame of a video to a PNG, decoded with the color its stream states; raises ValueError when it cannot.
FrameExtractor = Callable[[Path, Path, StreamColor], None]
# Writes color tags into a finished video's container, from the color its stream states; returns whether it changed the file.
VideoTagger = Callable[[Path, StreamColor], bool]
# Reads the color a video's stream states, before it is tagged; never raises.
ColorReader = Callable[[Path], StreamColor]
# Measures what an output file actually holds; raises ValueError when it cannot.
OutputMeasurer = Callable[[Path], MediaInfo]


@dataclass(frozen=True)
class MediaTools:
    """What a job needs for its videos: checks that ffmpeg and ffprobe exist, the last-frame extractor, and, when given, the tagger, the measurer, and the media checker."""

    require_ffmpeg: Callable[[], object]
    frame_extractor: FrameExtractor
    require_ffprobe: Callable[[], object] | None = None
    video_tagger: VideoTagger | None = None
    output_measurer: OutputMeasurer | None = None
    # None reads nothing: every video is then taken as stating no color (tests with fake tools).
    color_reader: ColorReader | None = None
    # Checks each file of a video job and says what it holds; None makes no checks (tests with fake tools).
    checker: MediaChecker | None = None
