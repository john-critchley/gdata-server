"""
Tests for runnable-sheet image output.

Run: python -m pytest test_sheet_image_output.py -v
"""
import base64
import os
import sys

import pytest


ROOT = os.path.dirname(__file__)
NOTES_BROWSER = os.path.join(ROOT, "notes-browser")
sys.path.insert(0, NOTES_BROWSER)


def test_show_matplotlib_figure_emits_png_image_item():
    pytest.importorskip("matplotlib")

    from sheet_kernel import SheetKernel

    kernel = SheetKernel()
    output, error, ok, show_items = kernel.run_cell(
        "figure",
        """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(2, 1))
ax.plot([0, 1, 2], [0, 1, 0])
show(fig)
plt.close(fig)
""",
    )

    assert ok is True, error
    assert output == ""
    assert error == ""
    assert len(show_items) == 1

    item = show_items[0]
    assert item["kind"] == "image"
    assert item["format"] == "png"
    png = base64.b64decode(item["data"])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 100


def test_sheet_cell_panel_renders_png_image_item():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("wx image rendering requires a display")

    import wx
    from sheet_kernel import ImageOutput
    from sheet_ui import SheetCellPanel

    pytest.importorskip("matplotlib")
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig, ax = plt.subplots(figsize=(1, 1))
    ax.plot([0, 1], [0, 1])
    fig.savefig(buf, format="png")
    plt.close(fig)
    png = buf.getvalue()
    item = ImageOutput(png).__show__()

    try:
        app = wx.App.Get() or wx.App(False)
    except SystemExit as exc:
        pytest.skip(f"wx display unavailable: {exc}")
    frame = wx.Frame(None, pos=(-10000, -10000), size=(300, 200))
    frame.Hide()

    class DummySheet:
        def run_cell(self, _cell_id):
            raise AssertionError("run_cell should not be called by set_output")

        def _bind_mousewheel_chain(self, _window):
            pass

    try:
        panel = SheetCellPanel(
            frame,
            DummySheet(),
            "image_cell",
            {"name": "image_cell", "lang": "python", "body": "show(fig)", "input_prompts": []},
        )
        panel.set_output("", ok=True, show_items=[item])

        children = []

        def walk(window):
            for child in window.GetChildren():
                children.append(child)
                walk(child)

        walk(panel.output_host)
        assert any(isinstance(child, wx.StaticBitmap) for child in children)
        assert panel.get_show_items() == [item]
    finally:
        frame.Destroy()
        # Keep an existing app alive; tests that created one can also leave it
        # for wx to clean up at process exit.
        _ = app
