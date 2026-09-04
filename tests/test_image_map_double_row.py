from pathlib import Path
import json
import tempfile
import unittest
from xml.etree import ElementTree

from PIL import Image

from backend.templates.image_map_renderer import ImageMapHeadlessRenderer


class ImageMapDoubleRowTests(unittest.TestCase):
    def test_double_row_geometry_without_bitmap_guides_or_duplicated_layers(self):
        project_root = Path(__file__).resolve().parents[1]
        fixture = (
            project_root
            / "image-map-headless-renderer"
            / "test"
            / "double-row.json"
        )
        document = json.loads(fixture.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="double-row-render-") as directory:
            output = Path(directory) / "double-row.png"
            renderer = ImageMapHeadlessRenderer(
                project_root,
                dpi=96,
                timeout_seconds=30,
                render_retry_attempts=1,
            )
            try:
                result = renderer.render(
                    output_path=output,
                    layout=document,
                    output_format="png",
                    include_svg=True,
                )
            finally:
                renderer.close()

            self.assertEqual((result["css_width"], result["css_height"]), (2039, 1488))
            self.assertEqual(result["object_count"], 2)

            with Image.open(output) as image:
                pixels = image.convert("RGB")
                # These points sit on rebuilt content/bleed guide positions,
                # away from the authored sample objects.
                for point in ((76, 100), (500, 76), (500, 728), (500, 760), (500, 836), (992, 100)):
                    self.assertEqual(pixels.getpixel(point), (255, 255, 255))
                lower_row = pixels.crop((0, 760, pixels.width, pixels.height))
                self.assertEqual(
                    lower_row.getextrema(),
                    ((255, 255), (255, 255), (255, 255)),
                )

        svg_root = ElementTree.fromstring(
            result["svg"].split("<!-- Creator: CorelDRAW -->", 1)[1]
        )
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        guides = svg_root.findall("svg:line", namespace)
        vertical = [guide for guide in guides if guide.get("x1") == guide.get("x2")]
        horizontal = [guide for guide in guides if guide.get("y1") == guide.get("y2")]
        self.assertEqual(len(vertical), 8)
        self.assertEqual(len(horizontal), 8)
        self.assertTrue(
            all(
                guide.get("y1") == "0"
                and abs(float(guide.get("y2")) - 1487.36) < 0.001
                for guide in vertical
            )
        )


if __name__ == "__main__":
    unittest.main()
