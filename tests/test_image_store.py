from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import image_store  # noqa: E402
from image_store import ImageStoreError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16


def candidate(src: str, size: int = 1024, visible: bool = True) -> dict[str, object]:
    return {"src": src, "width": size, "height": size, "visible": visible}


class FilterCandidatesTests(unittest.TestCase):
    def test_keeps_content_images_and_drops_ui_noise(self) -> None:
        good = "https://lh3.googleusercontent.com/gg/abc=s1024"
        kept = image_store.filter_candidates(
            [
                candidate("https://www.gstatic.com/icon.png", size=24),  # too small: avatar/icon
                candidate(good),
                candidate(good),  # duplicate src
                candidate("https://evil.example/x.png"),  # host not allowed
                candidate("http://lh3.googleusercontent.com/x"),  # not https
                candidate("https://lh3.googleusercontent.com/hidden", visible=False),
                candidate("javascript:alert(1)"),
                "not-a-dict",
                {"src": "blob:https://gemini.google.com/1", "width": "oops", "height": 5, "visible": True},
            ],
            max_images=4,
        )
        self.assertEqual([item["src"] for item in kept], [good])

    def test_caps_count_and_tolerates_bad_input(self) -> None:
        many = [candidate(f"https://lh3.googleusercontent.com/gg/{i}") for i in range(12)]
        self.assertEqual(len(image_store.filter_candidates(many, max_images=3)), 3)
        self.assertEqual(len(image_store.filter_candidates(many, max_images=100)), image_store.MAX_IMAGES_LIMIT)
        self.assertEqual(len(image_store.filter_candidates(many, max_images=0)), 1)
        self.assertEqual(image_store.filter_candidates(None, max_images=4), [])

    def test_blob_and_data_image_are_page_local_and_allowed(self) -> None:
        self.assertTrue(image_store.url_is_allowed("blob:https://gemini.google.com/abc"))
        self.assertTrue(image_store.url_is_allowed("data:image/png;base64,AAAA"))
        self.assertFalse(image_store.url_is_allowed("data:text/html;base64,AAAA"))
        self.assertFalse(image_store.url_is_allowed("https://lh3.googleusercontent.com.evil.example/x"))
        self.assertFalse(image_store.url_is_allowed("https://user:pw@lh3.googleusercontent.com/x"))


class FullResolutionTests(unittest.TestCase):
    def test_rewrites_size_suffix_only_for_googleusercontent(self) -> None:
        self.assertEqual(
            image_store.full_resolution_url("https://lh3.googleusercontent.com/gg/abc=s512-rj"),
            "https://lh3.googleusercontent.com/gg/abc=s0",
        )
        self.assertEqual(
            image_store.full_resolution_url("https://lh3.googleusercontent.com/gg/abc=w800-h600-n"),
            "https://lh3.googleusercontent.com/gg/abc=s0",
        )
        self.assertIsNone(image_store.full_resolution_url("https://lh3.googleusercontent.com/gg/abc"))
        self.assertIsNone(image_store.full_resolution_url("https://www.gstatic.com/a=s512"))
        self.assertIsNone(image_store.full_resolution_url("blob:https://gemini.google.com/abc"))


class SniffAndSaveTests(unittest.TestCase):
    def test_sniff_uses_magic_bytes_not_claims(self) -> None:
        self.assertEqual(image_store.sniff_image(PNG), ("png", "image/png"))
        self.assertEqual(image_store.sniff_image(JPEG), ("jpg", "image/jpeg"))
        self.assertEqual(image_store.sniff_image(WEBP), ("webp", "image/webp"))
        self.assertIsNone(image_store.sniff_image(b"<html>not an image</html>"))
        self.assertIsNone(image_store.sniff_image(b"RIFF\x00\x00\x00\x00WAVE"))

    def test_save_writes_private_file_and_never_returns_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            info = image_store.save_image(PNG, Path(root) / "out", provider="gemini")
            path = Path(info["path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), PNG)
            self.assertTrue(path.name.startswith("gemini-") and path.name.endswith(".png"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            self.assertEqual(info["mime"], "image/png")
            self.assertEqual(info["bytes"], len(PNG))
            self.assertEqual(set(info) - {"duplicate"}, {"path", "mime", "bytes", "sha256"})

    def test_save_rejects_empty_oversized_and_non_images(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            with self.assertRaises(ImageStoreError):
                image_store.save_image(b"", directory, provider="gemini")
            with self.assertRaises(ImageStoreError):
                image_store.save_image(b"<html></html>", directory, provider="gemini")
            with self.assertRaises(ImageStoreError):
                image_store.save_image(PNG + b"\x00" * image_store.MAX_IMAGE_BYTES, directory, provider="gemini")
            self.assertEqual(list(directory.iterdir()), [])

    def test_saving_identical_bytes_twice_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            first = image_store.save_image(PNG, Path(root), provider="gemini")
            second = image_store.save_image(PNG, Path(root), provider="gemini")
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(len(list(Path(root).iterdir())), 1)


class ValidateAttachmentsTests(unittest.TestCase):
    def test_accepts_real_images_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            a = Path(root) / "a.png"
            b = Path(root) / "b.jpg"
            a.write_bytes(PNG)
            b.write_bytes(JPEG)
            self.assertEqual(
                image_store.validate_attachments([str(a), str(b), str(a)]),
                [str(a.resolve()), str(b.resolve())],
            )
            self.assertEqual(image_store.validate_attachments(None), [])

    def test_rejects_unsafe_or_invalid_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            good = Path(root) / "g.png"
            good.write_bytes(PNG)
            text = Path(root) / "t.png"
            text.write_text("not an image")
            empty = Path(root) / "e.png"
            empty.write_bytes(b"")
            big = Path(root) / "big.png"
            big.write_bytes(PNG + b"\x00" * image_store.MAX_IMAGE_BYTES)
            for bad in (
                "relative.png",
                str(Path(root) / "missing.png"),
                root,  # a directory
                str(text),  # wrong magic bytes
                str(empty),
                str(big),
                "",
                123,
            ):
                with self.assertRaises(ImageStoreError, msg=repr(bad)):
                    image_store.validate_attachments([bad])
            with self.assertRaises(ImageStoreError):
                image_store.validate_attachments("not-a-list")
            with self.assertRaises(ImageStoreError):
                image_store.validate_attachments([str(good)] * 2 + [str(text)])
            many = []
            for i in range(image_store.MAX_ATTACHMENTS + 1):
                path = Path(root) / f"{i}.png"
                path.write_bytes(PNG + bytes([i]))
                many.append(str(path))
            with self.assertRaises(ImageStoreError):
                image_store.validate_attachments(many)


class ResolveSaveDirTests(unittest.TestCase):
    def test_default_is_pictures_folder_outside_repository(self) -> None:
        resolved = image_store.resolve_save_dir(None, PROJECT_DIR.parent)
        self.assertEqual(resolved, Path("~/Pictures/MCP-ChatBridge/gemini").expanduser().resolve())

    def test_default_folder_is_per_provider(self) -> None:
        root = PROJECT_DIR.parent
        self.assertEqual(
            image_store.resolve_save_dir(None, root, "gemini"), Path("~/Pictures/MCP-ChatBridge/gemini").expanduser().resolve()
        )
        self.assertEqual(
            image_store.resolve_save_dir(None, root, "chatgpt"), Path("~/Pictures/MCP-ChatBridge/chatgpt").expanduser().resolve()
        )

    def test_rejects_relative_and_repository_paths(self) -> None:
        with self.assertRaises(ImageStoreError):
            image_store.resolve_save_dir("images", PROJECT_DIR.parent)
        with self.assertRaises(ImageStoreError):
            image_store.resolve_save_dir(str(PROJECT_DIR.parent), PROJECT_DIR.parent)
        with self.assertRaises(ImageStoreError):
            image_store.resolve_save_dir(str(PROJECT_DIR.parent / "sub" / "dir"), PROJECT_DIR.parent)
        with self.assertRaises(ImageStoreError):
            # traversal back into the repository must be caught after resolving
            image_store.resolve_save_dir(str(PROJECT_DIR.parent / "x" / ".." / "inside"), PROJECT_DIR.parent)

    def test_accepts_explicit_outside_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(
                image_store.resolve_save_dir(root, PROJECT_DIR.parent), Path(root).resolve()
            )


if __name__ == "__main__":
    unittest.main()
