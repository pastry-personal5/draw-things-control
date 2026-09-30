"""Extract the last frame of a generated video with ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

from draw_things_control.jobs.media.stream_color import StreamColor, resolve_video_color
from draw_things_control.jobs.media.tools import find_ffprobe, require_ffmpeg

# The frame is decoded with the matrix and range stream_color.py resolves (the pixels' when they tell, else the
# stream's, BT.709 limited range when neither does), converted to RGB without alpha (owner decision: Draw Things reads
# alpha as a mask), and only then labeled sRGB, which is what Draw Things' models produce; labeling before the
# conversion would change the pixels. ffmpeg then writes the sRGB, cHRM, and gAMA chunks, and the label changes no
# pixel value. swscale's accurate rounding is asked for: without it, 8-bit YCbCr (H.264) decodes to 16-bit RGB about
# 1 to 1.5 levels dark (measured with ffmpeg 8.1.1; ProRes is the same either way).
DECODE_FLAGS = "accurate_rnd+full_chroma_int"
RGB_16 = "format=pix_fmts=rgb48be"
# The frame is the handoff, which the next run reads (owner decision, 2026-09-30, superseding 16-bit from ProRes only
# and the 257/256 rescale): 16-bit RGB from every source, holding one 8-bit value v per sample as v * 256 + 128.
# draw-things-cli reads a 16-bit PNG by its high byte (swift-png, >> 8), so it reads exactly v; a reader that divides by
# 65535 sees v within half a level. v is chosen so the next run sees what the model made, on average:
# - draw-things-cli truncates every value it writes (Int((v + 1) * 127.5)), so a decoded level e stands for e to e + 1:
#   half a level is added back. The half tapers to nothing at black, where every value at or below black also lands, so
#   black stays black.
# - A flat area cannot hold e + 0.5 in 8 bits, so each sample is rounded against a 2x2 ordered-dither threshold (owner
#   decision): a flat area then holds v and v + 1 in equal parts, 0.04 level from its mean on ProRes, where plain
#   rounding is half a level off in every flat area.
# ffmpeg's 16-bit RGB is about 256 times the 8-bit value (white is 65283), so e is the sample / 256. The format is set
# again after geq, or ffmpeg negotiates an 8-bit one for the PNG.
BAYER_2X2 = "(2*mod(X\\,2)+3*mod(Y\\,2)-4*mod(X\\,2)*mod(Y\\,2)+0.5)/4"


def _handoff_value(channel: str) -> str:
    level = f"({channel}(X\\,Y)/256)"
    return f"clip(floor({level}+0.5*min(1\\,{level})+{BAYER_2X2})\\,0\\,255)*256+128"


HANDOFF = f"format=pix_fmts=gbrp16le,geq=r='{_handoff_value('r')}':g='{_handoff_value('g')}':b='{_handoff_value('b')}',{RGB_16}"
SRGB_LABEL = "setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc"


def srgb_filter(color: StreamColor) -> str:
    """The ffmpeg filter chain that decodes with ``color``'s matrix and range, converts to the handoff's 16-bit RGB,
    and labels sRGB. A stated matrix this does not know is left to ffmpeg, which decodes with what the frame states."""
    matrix = color.decode_matrix
    decode = f"scale=in_color_matrix={matrix}:in_range={color.decode_range}:out_range=pc:flags={DECODE_FLAGS}" if matrix is not None else f"scale=out_range=pc:flags={DECODE_FLAGS}"
    return ",".join((decode, HANDOFF, SRGB_LABEL))


def extract_last_frame(video: Path, png: Path, color: StreamColor | None = None, *, executable: str = "ffmpeg") -> None:
    """Write the final frame of ``video`` to ``png``, as RGB without alpha labeled sRGB, never overwriting a file.
    ``color`` is what ``resolve_video_color`` gave before the video was tagged; without it, it is resolved now."""
    ffmpeg = require_ffmpeg(executable)
    if color is None:
        color = resolve_video_color(video, ffmpeg, find_ffprobe(ffmpeg))
    # Seek near the end and keep overwriting one image; the last frame decoded wins.
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-sseof", "-1", "-i", str(video), "-update", "1", "-q:v", "1", "-vf", srgb_filter(color), str(png)]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
    if result.returncode != 0 or not png.is_file():
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise ValueError(f"Could not extract the last frame of {video}: {detail}")
