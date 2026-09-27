# Resume in draw-things-cli

What `draw-things-cli` can resume after an interruption, and what that means for this project's chain resume ([Phase 3, Milestone 01](../phase-3/milestone-01-queue-run-manager.md#resume)). Researched 2026-09-27.

## Summary

| Area | Resumes? | What survives an interruption |
|------|----------|-------------------------------|
| `generate` (image and video) | No | Nothing. The output is written only after sampling and decoding finish |
| `generate --avc` (LongCat-Video-Avatar 1.5) | No | Nothing. Every segment is held in memory and one file is written at the end |
| `generate --remote`, `--cloud-compute` | No, by keyword search only | Nothing that the CLI keeps. See [Not verified](#not-verified) |
| Model downloads (`--download-missing`, `models ensure`) | Yes, automatically | Verified blocks in `<file>.partial` and `<file>.partial.map`. The next invocation continues from them |
| `train lora` | Yes, with `--resume` | The LoRA checkpoints written by `--save-every`. Only weights and the step count are restored |

`generate` has no resume flag, no checkpoint of intermediate latents, and no partial output. A run is atomic: an interrupted run costs all the time it had spent. The smallest unit this project can resume is therefore one run of a chain, which is what Milestone 01 does.

## Sources

- The installed binary, `~/.local/bin/draw-things-cli`, is byte-identical to `draw-things-cli` in the project root. `--version` prints `dev`.
- Its `--help` output for every subcommand. The `generate` help is unchanged from [draw-things-cli-generate-help.txt](draw-things-cli-generate-help.txt).
- Source: a clone of [drawthingsai/draw-things-community](https://github.com/drawthingsai/draw-things-community) at `da9b0c8` (2026-09-22), in `/Volumes/Work_Volume/archive/dt-com`. Line references below are to that commit. Upstream `e8f4786` (2026-09-27) is 18 commits ahead. Its changes to the CLI add `--local`, automatic cloud compute inside the embedding app, and `auth status`. None of them adds or changes a resume.
- A signal test against the real binary, described under [Signals](#signals).

## `generate`

- `LocalGenerationRunner.generate` runs `generateTensors` to completion and only then calls `saveOutputs` (`Apps/DrawThingsCLI/DrawThingsCLI.swift:2179`, `:2192`). There is no hook between steps that writes latents or frames to disk. The live preview (`--disable-preview` turns it off) goes to the terminal only.
- For a video, `writeVideo` deletes any existing file at the output path and points `AVAssetWriter` at it directly (`DrawThingsCLI.swift:2374`, `:2385`). Reading the source suggests that a kill during encoding can leave a truncated `.mov` or `.mp4` at the output path. This was not tested, and the PNG path was not checked.
- The CLI has no option to start from a step, a timestep, a latent, or a previous output other than as an ordinary `--image` input with `--strength`.

## `generate --avc`

Audio video continuation (`--avc`, `--segment-frames`, `--cond-frames`) is the CLI's only built-in long-horizon continuation. It is not a resume:

- It supports only LongCat-Video-Avatar 1.5, requires `--audio`, exactly one `--image`, `--cfg 1`, and local generation (`DrawThingsCLI.swift:4653`).
- Each segment after the first is conditioned on the last `--cond-frames` frames of the previous segment, 13 by default. By contrast, this project chains on one last frame through `--image`, the only continuation `generate` offers other models.
- Segment *i* uses the seed plus *i* (`Libraries/LocalImageGenerator/Sources/LongCatAvatar.swift:109`).
- All frames accumulate in memory (`LongCatAvatar.swift:94`, `:120`). One file is written after the last segment (`DrawThingsCLI.swift:4765`, `:4770`). An interruption loses every segment, and no option starts at a later segment.

## Model downloads

- The CLI downloads missing files from `https://static.libnnc.org/<file>` with `ResumableDownloader` (`DrawThingsCLI.swift:1617`, `:1628`).
- When a `HEAD` probe reports `Accept-Ranges: bytes`, it uses the segmented backend (`Libraries/Downloader/Sources/ResumableDownloader.swift:176`). `static.libnnc.org` does: on 2026-09-27, `wan_v2.2_a14b_hne_i2v_i8x.ckpt` returned `Accept-Ranges: bytes`, a `Content-Length` of 14,409,105,408, an `ETag`, and a `Last-Modified`. Check it again with:

  ```bash
  curl -sI https://static.libnnc.org/wan_v2.2_a14b_hne_i2v_i8x.ckpt | grep -i -E '^(accept-ranges|content-length|etag|last-modified):'
  ```

- The segmented backend writes blocks of 1 to 8 MiB to `<file>.partial` from up to 8 connections. It saves `<file>.partial.map` after each block (`SegmentedResumableDownloaderBackend.swift:51`, `:52`, `:368`). A kill therefore loses at most the blocks in flight.
- On the next start, it re-hashes each completed block and downloads only the missing or corrupt ones. It starts over when the remote length, `ETag`, or `Last-Modified` has changed (`SegmentedResumableDownloaderBackend.swift:136`, `:199`). The whole file is checked against the catalog's SHA-256 before it is moved into place (`:442`).
- The fallback backend, for servers without range support, writes URLSession resume data to `<file>.part` only on cancel or on an error (`URLSessionDownloadTaskResumableDownloaderBackend.swift:67`, `:215`). A killed standalone CLI never cancels (see [Signals](#signals)), so that backend would start over. It does not apply to `static.libnnc.org` today.

## `train lora`

`--resume <file>` (hidden alias `--train-checkpoint`) continues LoRA training from a checkpoint written by `--save-every N`, named `<output>_<step>_lora_f32.ckpt` (`DrawThingsCLI.swift:4106`):

- Only the file name is used. The file is looked up in the models directory, and the step comes from the number before `_lora_f32.ckpt` (`DrawThingsCLI.swift:4063`).
- It restores the LoRA weights and the step counter. The AdamW optimizer is created fresh (`Libraries/Trainer/Sources/LoRATrainer.swift:3309`).
- If the named file cannot be read, the trainer silently falls back to fresh weights but still starts counting at the parsed step (`LoRATrainer.swift:3236`–`3290`, `try?`).

## Signals

The standalone CLI installs no signal handler. `DrawThingsCLIContext.process()` passes no cancellation source (`DrawThingsCLIContext.swift:69`), no source file calls `signal`, `sigaction`, or `makeSignalSource`, and `nm -u` on the binary shows no `_signal` or `_sigaction` import. Its graceful cancellation (the `Generation aborted by user.` message and exit status 130, `DrawThingsCLI.swift:4228`) is reachable only when the Draw Things app embeds the CLI, where the host polls for cancellation (`IOSSystem/DrawThingsCLICommand.swift:101`).

Test: `generate` was started with `--prompt-file -` and a stdin that stays open. It was still running 4 s later, when it was signalled. Rerun it from the project root:

```bash
uv run python - <<'EOF'
import signal, subprocess, tempfile, time
output = f"{tempfile.mkdtemp()}/signal-test.mov"
for sent in (signal.SIGTERM, signal.SIGINT):
    child = subprocess.Popen(["draw-things-cli", "generate", "--offline", "--no-download-missing", "--model", "wan_v2.2_a14b_hne_i2v_i8x.ckpt", "--prompt-file", "-", "--output", output], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(4)
    alive = child.poll() is None
    child.send_signal(sent)
    stdout, stderr = child.communicate(timeout=10)
    print(f"{sent.name}: alive after 4 s={alive}, returncode={child.returncode}, stdout={stdout.decode()!r}, stderr={stderr.decode()!r}")
EOF
```

Result:

| Signal | `returncode` | Output |
|--------|--------------|--------|
| `SIGTERM` | `-15` | none |
| `SIGINT` | `-2` | none |

Both signals end the process at once, through the default action, with no cleanup. A shell reports the same exits as 143 and 130.

## What this means for this project

- Run-level resume is the finest resume available. A chain resume reruns the interrupted run from its start, and the time already spent on that run is lost. For long video runs, stopping between runs (in a cooldown) costs nothing, while stopping mid-run costs the run so far.
- With this build, the runner's `SIGTERM` grace period (`src/draw_things_control/core/process/runner.py`) never has anything to wait for: the child dies on `SIGTERM` at once. Keep the grace anyway, since a later build may handle the signal. A child that a signal ended reports a negative return code, which `exit_code_for_child_signal` (`src/draw_things_control/core/exit_codes.py`) already maps to 128 + N.
- Only succeeded runs feed a resume, so a truncated output left by a killed run is never used as the next input. The executor still keeps `record.output` for a failed run whose file exists (`src/draw_things_control/jobs/executor.py`). That file may be truncated.
- A run killed while downloading a model does not lose the download. The next invocation continues it, provided `--download-missing` stays on, which is the default.
- `--avc` conditions each segment on several frames rather than one, but only for LongCat-Video-Avatar 1.5. The CLI has no multi-frame continuation for Wan 2.2 or any other model.

## Not verified

- Whether rerunning run *k* with the same seed, input, and settings reproduces the same output bit for bit. Milestone 01 keeps the original seed but does not depend on identical pixels.
- What a kill during video encoding leaves at the output path. The behavior above is read from the source, not tested.
- Remote and cloud generation. A search of `Libraries/RemoteImageGenerator` for retry, reconnect, and resume found nothing, and `RemoteGenerationRunner` (`DrawThingsCLI.swift:2729`) was not read in full. Whether a cloud job keeps running on the server after the client dies was not checked.
