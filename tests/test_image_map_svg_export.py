from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from backend.templates.image_map_renderer import ImageMapHeadlessRenderer


class ImageMapSvgExportTests(unittest.TestCase):
    def test_python_boundary_rejects_embedded_font_svg(self):
        project_root = Path(__file__).resolve().parents[1]
        document = {
            "objects": [
                {
                    "id": "workarea",
                    "type": "Image",
                    "originX": "center",
                    "originY": "center",
                    "left": 48,
                    "top": 48,
                    "width": 96,
                    "height": 96,
                    "backgroundColor": "#ffffff",
                }
            ]
        }
        with tempfile.TemporaryDirectory(prefix="svg-boundary-test-") as directory:
            output = Path(directory) / "preview.png"
            renderer = ImageMapHeadlessRenderer(project_root, dpi=96)

            def fake_render(_document, output_path, *_args, **_kwargs):
                output_path.write_bytes(b"not-a-real-image")
                return {
                    "svg": (
                        "<svg><style>@font-face{src:url("
                        "data:font/ttf;base64,AA==)}</style></svg>"
                    )
                }

            renderer._render_on_worker = fake_render
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "SVG 检测到嵌入字体二进制",
                ):
                    renderer.render(
                        output_path=output,
                        layout=document,
                        output_format="png",
                        include_svg=True,
                    )
                self.assertFalse(output.exists())
            finally:
                renderer.close()

    def test_svg_has_editable_guides_without_embedded_font_binary(self):
        project_root = Path(__file__).resolve().parents[1]
        font_path = Path("C:/Windows/Fonts/arial.ttf")
        self.assertTrue(font_path.is_file())
        document = {
            "objects": [
                {
                    "id": "workarea",
                    "type": "Image",
                    "originX": "center",
                    "originY": "center",
                    "left": 48,
                    "top": 48,
                    "width": 96,
                    "height": 96,
                    "workareaWidth": 96,
                    "workareaHeight": 96,
                    "backgroundColor": "#ffffff",
                    "src": "",
                    "printGuides": [
                        {"orientation": "vertical", "position": 24, "kind": "bleed"},
                        {"orientation": "horizontal", "position": 48, "kind": "content"},
                    ],
                },
                {
                    "id": "title",
                    "name": "标题文字",
                    "type": "Textbox",
                    "originX": "center",
                    "originY": "center",
                    "left": 48,
                    "top": 48,
                    "text": "TEST",
                    "fontFamily": "Arial",
                    "fontUrl": str(font_path),
                    "fontSize": 12,
                    "charSpacing": 100,
                    "fill": "#000000",
                },
            ]
        }
        with tempfile.TemporaryDirectory(prefix="svg-export-test-") as directory:
            output = Path(directory) / "preview.png"
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
                    order_number="SVG-TEST",
                )
            finally:
                renderer.close()

        svg = result["svg"]
        self.assertIn("<!-- Creator: CorelDRAW -->", svg)
        self.assertIn("font-family", svg)
        self.assertIn("@font-face", svg)
        self.assertIn("font-family:\"Arial\"", svg)
        self.assertIn("file:///C:/Windows/Fonts/arial.ttf", svg)
        self.assertNotIn("data:font", svg)
        self.assertNotIn(";base64,", svg)

        root = ElementTree.fromstring(svg.split("<!-- Creator: CorelDRAW -->", 1)[1])
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        bleed = root.find("svg:line[@id='print-guide-1']", namespace)
        content = root.find("svg:line[@id='print-guide-2']", namespace)
        self.assertEqual(bleed.get("stroke-dasharray"), "6 4")
        self.assertIsNone(content.get("stroke-dasharray"))
        title = root.find(".//*[@id='title']", namespace)
        self.assertIsNotNone(title)
        self.assertEqual(title.get("data-name"), "标题文字")
        self.assertEqual(len(root.findall(".//svg:text", namespace)), len("TEST"))
        self.assertEqual(len(root.findall(".//svg:tspan", namespace)), 0)


if __name__ == "__main__":
    unittest.main()
