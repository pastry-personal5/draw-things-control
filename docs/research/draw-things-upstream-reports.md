# Draw Things upstream reports (drafts)

Two issue drafts for [`drawthingsai/draw-things-community`](https://github.com/drawthingsai/draw-things-community), for
the owner to file (owner decision, 2026-09-30: nothing is sent without the owner). They come from
[the color drift research](color-drift.md) and were checked against the installed build in
[Milestone 09](../archive/phase-3/milestone-09-color-preservation.md)'s step 0.

## The build they cite

The installed `draw-things-cli` (`--version` prints `dev`) was built on 2026-09-23 from a local checkout of
`draw-things-community` at `da9b0c8e9e94a8c65d034ead23126d5325d256bd` (2026-09-22, "Force sync."), with no tracked
file modified. Its `Package.resolved` pins `swift-png` at `075dfb248ae327822635370e9d4f94a5d3fe93b2`. The research
read `0e9c1805eeb2898b23249e2764bbdbe8670e3461` (2026-09-29). Both commits have the same `pixelByte` and the same PNG
branch of `loadTrainingTensor`, with the same `swift-png` pin. Line numbers below are from `da9b0c8`.

## 1. `pixelByte` truncates instead of rounding

**Title:** draw-things-cli: output pixels are truncated to 8 bits, making every frame half a level dark

`Apps/DrawThingsCLI/DrawThingsCLI.swift:1991`:

```swift
private func pixelByte(_ value: FloatType) -> UInt8 {
  UInt8(min(max(Int((value + 1) * 127.5), 0), 255))
}
```

`Int(...)` truncates toward zero, so each value in `[-1, 1]` lands on the 8-bit level below it. The written image is
half a level dark on average, and 255 comes out only for an exact 1.0. It writes every PNG output (line 2007) and
every video frame (line 2718, the `kCVPixelFormatType_32BGRA` buffers `writeVideo` fills).

In an image-to-video chain, where each clip's last frame starts the next clip, the bias adds up from run to run.

**Suggested fix:** round, for example `Int(((value + 1) * 127.5).rounded())`, or `Int((value + 1) * 127.5 + 0.5)`
inside the clamp.

## 2. A 16-bit PNG input is read by its high byte

**Title:** draw-things-cli: a 16-bit PNG input is truncated to its high byte

`loadTrainingTensor` (`Apps/DrawThingsCLI/DrawThingsCLI.swift:5355`) reads a PNG with swift-png, then
`image.unpack(as: PNG.RGBA<UInt8>.self)`. For a 16-bit PNG, swift-png narrows each sample with a right shift
(`$0 &>> shift` in `Sources/png/convolution.swift` at the pinned `075dfb2`), which keeps the high byte and drops the
low one. That is a truncation: a value between two 8-bit levels always reads as the lower one, so continuous-tone
16-bit data comes in half a level dark on average.

Each sample then becomes `Float(v) / 127.5 - 1`, so 16-bit input carries no more than 8 bits into the model anyway.

**Suggested fix:** unpack 16-bit PNGs as `PNG.RGBA<UInt16>` and scale with rounding (`Float(v) / 32767.5 - 1`), or at
least round when narrowing (`(v + 128) / 257`).

**Also worth noting:** the PNG path ignores `iCCP`, `sRGB`, `gAMA`, and `cHRM`, so a Display P3 PNG is read as sRGB.
Other formats go through ImageIO and CoreGraphics, which do convert.

## How this project works around both

Milestone 08 hands `draw-things-cli` a 16-bit PNG whose samples are `v * 256 + 128`, which `>> 8` reads as exactly
`v`. It chooses `v` from the decoded level plus the truncated half level, rounded against a 2x2 ordered dither. See
[the handoff](../archive/phase-3/milestone-08-video-format-and-color.md#the-handoff).
