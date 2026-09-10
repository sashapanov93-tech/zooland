import io
import os
import stat
import tempfile
import unittest

from PIL import Image, PngImagePlugin

from image_security import ImageSafetyError, sanitize_upload


class ImageSecurityTests(unittest.TestCase):
    def test_upload_is_reencoded_without_metadata_and_with_read_only_group_mode(self):
        source = io.BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "private test metadata")
        with Image.new("RGB", (32, 24), "#4b8f5a") as image:
            image.save(source, format="PNG", pnginfo=metadata)
        source.seek(0)

        with tempfile.TemporaryDirectory(prefix="zooland-image-test-") as destination:
            filename = sanitize_upload(source, destination)
            output_path = os.path.join(destination, filename)

            self.assertRegex(filename, r"^[0-9a-f]{32}\.webp$")
            self.assertEqual(stat.S_IMODE(os.stat(output_path).st_mode), 0o640)
            with Image.open(output_path) as result:
                self.assertEqual(result.format, "WEBP")
                self.assertEqual(result.size, (32, 24))
                self.assertFalse(result.getexif())
                self.assertNotIn("Comment", result.info)
                self.assertNotIn("icc_profile", result.info)
                self.assertNotIn("xmp", result.info)

    def test_non_image_is_rejected_without_publishing_file(self):
        with tempfile.TemporaryDirectory(prefix="zooland-image-test-") as destination:
            with self.assertRaises(ImageSafetyError):
                sanitize_upload(io.BytesIO(b"not an image"), destination)
            self.assertEqual(os.listdir(destination), [])


if __name__ == "__main__":
    unittest.main()
