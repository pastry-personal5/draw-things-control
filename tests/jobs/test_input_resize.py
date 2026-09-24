"""Tests for decoding and resizing a job's first input image."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loguru import logger
from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageOps, ImageStat

from draw_things_control.jobs.input_resize import TemporaryInput, resize_image
from draw_things_control.jobs.input_size import ResizePlan, decode_image, resize_plan

DISPLAY_P3 = Path("/System/Library/ColorSync/Profiles/Display P3.icc")


class InputResizeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def save(self, image: Image.Image, name: str = "input.png", **options: object) -> Path:
        path = self.root / name
        image.save(path, **options)
        return path

    def resized(self, source: Path, plan: ResizePlan) -> Image.Image:
        destination = self.root / "out.png"
        resize_image(source, plan, destination)
        with Image.open(destination) as image:
            self.assertEqual(image.format, "PNG")
            return image.convert("RGB")

    def test_crop_keeps_the_aspect_ratio_and_leaves_no_bars(self) -> None:
        source = self.save(Image.new("RGB", (1920, 1080), "white"))
        image = self.resized(source, resize_plan("input.png", (1920, 1080), None, 850, None))
        self.assertEqual(image.size, (832, 448))
        self.assertEqual(image.getextrema(), ((255, 255),) * 3)

    def test_letterbox_keeps_the_aspect_ratio_and_pads_with_black(self) -> None:
        source = self.save(Image.new("RGB", (1920, 1080), "white"))
        image = self.resized(source, resize_plan("input.png", (1920, 1080), None, 1280, 720))
        self.assertEqual(image.size, (1280, 704))
        # The picture is 1252x704, centered: 14 px black bars on each side.
        self.assertEqual(image.getbbox(), (14, 0, 1266, 704))
        self.assertAlmostEqual(1252 / 704, 1920 / 1080, delta=1 / 704)
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))

    def test_crop_is_centered_and_an_odd_leftover_comes_off_the_bottom(self) -> None:
        # Each row's gray level is its row number, so the first kept row shows the top crop.
        rows = Image.new("L", (64, 71))
        rows.putdata([y for y in range(71) for _x in range(64)])
        image = self.resized(self.save(rows), resize_plan("input.png", (64, 71), None, 64, None))
        self.assertEqual(image.size, (64, 64))
        self.assertEqual((image.getpixel((0, 0))[0], image.getpixel((0, 63))[0]), (3, 66))

    def test_letterbox_puts_an_odd_leftover_on_the_right(self) -> None:
        image = self.resized(self.save(Image.new("RGB", (61, 64), "white")), resize_plan("input.png", (61, 64), None, 64, 64))
        self.assertEqual(image.getbbox(), (1, 0, 62, 64))

    def test_small_input_is_upscaled(self) -> None:
        image = self.resized(self.save(Image.new("RGB", (512, 512), "white")), resize_plan("input.png", (512, 512), None, 1024, None))
        self.assertEqual(image.size, (1024, 1024))
        self.assertEqual(image.getextrema(), ((255, 255),) * 3)

    def test_transparency_is_flattened_onto_black(self) -> None:
        source = self.save(Image.new("RGBA", (128, 64), (255, 0, 0, 0)))
        image = self.resized(source, resize_plan("input.png", (128, 64), None, 128, None))
        self.assertEqual(image.getextrema(), ((0, 0),) * 3)

    def test_exif_orientation_is_applied(self) -> None:
        exif = Image.Exif()
        exif[0x0112] = 6
        # Stored 448x832 (portrait); displayed 832x448 after the 90-degree rotation.
        source = self.save(Image.new("RGB", (448, 832), "white"), "rotated.jpg", exif=exif)
        plan = resize_plan("rotated.jpg", (832, 448), 6, 832, None)
        self.assertTrue(plan.needs_copy)
        self.assertEqual(self.resized(source, plan).size, (832, 448))

    # Fidelity: color, bit depth, geometry, and resampling quality.

    def test_16_bit_grayscale_is_scaled_not_clipped(self) -> None:
        source = self.save(Image.new("I;16", (128, 64), 32768), "gray16.png")
        with Image.open(source) as reopened:
            self.assertEqual(reopened.mode, "I;16")
        image = self.resized(source, resize_plan("gray16.png", (128, 64), None, 128, 64))
        self.assertEqual(image.getextrema(), ((128, 128),) * 3)

    def test_display_p3_is_converted_to_srgb(self) -> None:
        if not DISPLAY_P3.is_file():
            self.skipTest("macOS Display P3 profile not available")
        profile = DISPLAY_P3.read_bytes()
        source = self.save(Image.new("RGB", (128, 64), (200, 100, 50)), "p3.png", icc_profile=profile)
        red, green, blue = self.resized(source, resize_plan("p3.png", (128, 64), None, 128, 64)).getpixel((64, 32))
        # The same color written in sRGB numbers is more saturated: more red, less green and blue.
        self.assertGreater(red, 205)
        self.assertLess(green, 100)
        self.assertLess(blue, 45)
        gray = self.save(Image.new("RGB", (128, 64), (128, 128, 128)), "p3-gray.png", icc_profile=profile)
        for channel in self.resized(gray, resize_plan("p3-gray.png", (128, 64), None, 128, 64)).getpixel((64, 32)):
            self.assertAlmostEqual(channel, 128, delta=1)

    def test_srgb_profile_leaves_colors_alone(self) -> None:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source = self.save(Image.new("RGB", (128, 64), (200, 100, 50)), "srgb.png", icc_profile=profile)
        for channel, expected in zip(self.resized(source, resize_plan("srgb.png", (128, 64), None, 128, 64)).getpixel((64, 32)), (200, 100, 50), strict=True):
            self.assertAlmostEqual(channel, expected, delta=1)

    def test_srgb_profile_is_not_converted(self) -> None:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source = self.save(Image.new("RGB", (256, 128), (1, 2, 3)), "srgb.png", icc_profile=profile)
        with mock.patch("draw_things_control.jobs.input_resize.ImageCms.profileToProfile") as convert:
            image = self.resized(source, resize_plan("srgb.png", (256, 128), None, 128, 64))
        convert.assert_not_called()
        self.assertEqual(image.getpixel((64, 32)), (1, 2, 3))

    def test_unusable_profile_warns_and_keeps_the_pixels(self) -> None:
        source = self.save(Image.new("RGB", (128, 64), (200, 100, 50)), "bad-profile.png", icc_profile=b"not a profile")
        messages: list[str] = []
        sink = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            image = self.resized(source, resize_plan("bad-profile.png", (128, 64), None, 128, 64))
        finally:
            logger.remove(sink)
        self.assertEqual(image.getpixel((64, 32)), (200, 100, 50))
        self.assertEqual(len(messages), 1)
        self.assertIn("color profile", messages[0])

    def test_circle_stays_round_in_both_fits(self) -> None:
        source = Image.new("RGB", (1920, 1080), "black")
        ImageDraw.Draw(source).ellipse((660, 240, 1260, 840), fill="white")
        path = self.save(source)
        for plan in (resize_plan("input.png", (1920, 1080), None, 850, None), resize_plan("input.png", (1920, 1080), None, 1280, 720), resize_plan("input.png", (1920, 1080), None, None, 720)):
            with self.subTest(fit=plan.fit, target=plan.target_size):
                left, top, right, bottom = self.resized(path, plan).convert("L").point(lambda value: 255 if value >= 128 else 0).getbbox()
                self.assertAlmostEqual(right - left, bottom - top, delta=1)
                # Centered: equal space on the left and right of the circle.
                self.assertAlmostEqual(left, plan.target_size[0] - right, delta=1)

    def test_fractional_crop_is_centered_to_the_sub_pixel(self) -> None:
        # 1920x1080 at height 720 covers 1216x704 from a 1251.6 px wide scaled image: an uneven crop.
        mirror = Image.new("L", (1920, 1080))
        mirror.putdata([min(x, 1919 - x) % 256 for _y in range(1080) for x in range(1920)])
        image = self.resized(self.save(mirror), resize_plan("input.png", (1920, 1080), None, None, 720)).convert("L")
        self.assertEqual(image.size, (1216, 704))
        flipped = ImageOps.mirror(image)
        self.assertLessEqual(max(ImageChops.difference(image, flipped).getextrema()), 1)

    def test_downscaling_does_not_alias_and_keeps_brightness(self) -> None:
        # A one-pixel checkerboard has no detail that survives halving: it must become even gray, not moire,
        # and as bright as it looks: half the light, which is 188 in sRGB (averaging sRGB values gives 128).
        checker = Image.new("L", (1024, 1024))
        checker.putdata([255 * ((x + y) % 2) for y in range(1024) for x in range(1024)])
        image = self.resized(self.save(checker), resize_plan("input.png", (1024, 1024), None, 512, None)).convert("L")
        inner = image.crop((8, 8, 504, 504))
        low, high = inner.getextrema()
        self.assertLessEqual(high - low, 2)
        self.assertAlmostEqual(ImageStat.Stat(inner).mean[0], 188, delta=1)

    def edge_profile(self, source: Image.Image, plan: ResizePlan, row: int) -> tuple[float, float, float]:
        """Width of the 10-90% rise in output pixels, and overshoot above and below the edge, in percent."""
        values = list(self.resized(self.save(source), plan).convert("L").crop((0, row, plan.target_size[0], row + 1)).getdata())
        normalized = [(value - 50) / 150 for value in values]

        def crossing(level: float) -> float:
            index = next(index for index, value in enumerate(normalized) if value > level)
            return index - 1 + (level - normalized[index - 1]) / (normalized[index] - normalized[index - 1])

        return crossing(0.9) - crossing(0.1), (max(values) - 200) / 1.5, (50 - min(values)) / 1.5

    def slanted_edge(self, size: tuple[int, int]) -> Image.Image:
        width, height = size
        edge = Image.new("L", size)
        edge.putdata([200 if x > width / 2 + (y - height / 2) * 0.05 else 50 for y in range(height) for x in range(width)])
        return edge

    def test_downscaled_edges_stay_sharp_without_strong_halos(self) -> None:
        # Measured: sRGB-value Lanczos gives a 1.39 px edge and a 7.3% dark halo; plain linear-light Lanczos 1.14 px and 33%.
        width, overshoot, halo = self.edge_profile(self.slanted_edge((1920, 1080)), resize_plan("input.png", (1920, 1080), None, 850, None), 224)
        self.assertLessEqual(width, 1.40)
        self.assertLessEqual(max(overshoot, halo), 8)

    def test_thin_highlights_keep_their_light_when_downscaled(self) -> None:
        # A 2 px bright line on a dark background: its light must survive (sRGB-value Lanczos keeps only 77%).
        thin = Image.new("L", (1920, 1080), 40)
        thin.paste(255, (960, 0, 962, 1080))
        values = self.resized(self.save(thin), resize_plan("input.png", (1920, 1080), None, 850, None)).convert("L").crop((0, 224, 832, 225)).getdata()

        def linear(value: float) -> float:
            value /= 255
            return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

        light = sum(max(linear(value) - linear(40), 0) for value in values)
        ideal = (linear(255) - linear(40)) * 2 * 832 / 1920
        self.assertAlmostEqual(light / ideal, 1, delta=0.06)

    def test_upscaled_edges_stay_sharp(self) -> None:
        # Upscaling works in sRGB values: measured 2.19 px (1.1 source px); linear light would be 2.43 px.
        width, overshoot, halo = self.edge_profile(self.slanted_edge((512, 512)), resize_plan("input.png", (512, 512), None, 1024, None), 512)
        self.assertLessEqual(width, 2.25)
        self.assertLessEqual(max(overshoot, halo), 8)

    def test_rotation_only_is_lossless(self) -> None:
        exif = Image.Exif()
        exif[0x0112] = 6
        noise = Image.merge("RGB", [Image.effect_noise((448, 832), 80 + band) for band in range(3)])
        source = self.save(noise, "rotated.png", exif=exif)
        with Image.open(source) as opened:
            expected = ImageOps.exif_transpose(opened).convert("RGB")
        self.assertIsNone(ImageChops.difference(self.resized(source, resize_plan("rotated.png", (832, 448), 6, 832, None)), expected).getbbox())

    def test_decode_image_rejects_broken_pixel_data(self) -> None:
        path = self.save(Image.effect_noise((256, 256), 64).convert("RGB"), "noise.jpg")
        path.write_bytes(path.read_bytes()[:2000])
        with self.assertRaisesRegex(ValueError, "'input' could not be decoded"):
            decode_image(path)
        decode_image(self.save(Image.new("RGB", (8, 8)), "good.png"))

    def test_unconvertible_mode_reports_the_input(self) -> None:
        source = self.save(Image.new("RGB", (256, 128), "white"))
        with mock.patch("draw_things_control.jobs.input_resize.to_srgb", side_effect=ValueError("conversion from LAB to RGB not supported")):
            with self.assertRaisesRegex(ValueError, "Could not resize input .*input.png: conversion from LAB"):
                TemporaryInput(source, resize_plan("input.png", (256, 128), None, 128, None))

    def test_same_aspect_ratio_is_scaled_without_bars(self) -> None:
        image = self.resized(self.save(Image.new("RGB", (1664, 896), "white")), resize_plan("input.png", (1664, 896), None, 832, 448))
        self.assertEqual((image.size, image.getextrema()), ((832, 448), ((255, 255),) * 3))

    def test_temporary_input_is_removed_by_cleanup(self) -> None:
        source = self.save(Image.new("RGB", (1920, 1080), "white"))
        temporary = TemporaryInput(source, resize_plan("input.png", (1920, 1080), None, 850, None))
        self.assertTrue(temporary.path.is_file())
        self.assertEqual(temporary.path.name, "input-832x448.png")
        temporary.cleanup()
        temporary.cleanup()
        self.assertFalse(temporary.path.parent.exists())

    def test_failed_resize_removes_its_directory_and_reports_a_value_error(self) -> None:
        created: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(**options: str) -> str:
            created.append(real_mkdtemp(**options))
            return created[-1]

        source = self.save(Image.new("RGB", (1920, 1080), "white"))
        with mock.patch("draw_things_control.jobs.input_resize.tempfile.mkdtemp", tracking_mkdtemp), mock.patch("draw_things_control.jobs.input_resize.resize_image", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "Could not resize input .*disk full"):
                TemporaryInput(source, resize_plan("input.png", (1920, 1080), None, 850, None))
        self.assertFalse(Path(created[0]).exists())


if __name__ == "__main__":
    unittest.main()
