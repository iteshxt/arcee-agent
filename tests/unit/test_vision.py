from PIL import Image, ImageDraw

from arcee.vision import Box, Vision, grid_cell, ink_centre, overlay_grid, parse_cell


def test_cells_and_labels() -> None:
    area = Box(0, 0, 1280, 720)

    assert grid_cell(5, 4, 8, 6, area) == Box(800, 480, 960, 600)
    assert parse_cell("F5", 8, 6) == (5, 4)
    assert parse_cell("The answer is c2.", 8, 6) == (2, 1)
    assert parse_cell("NONE", 8, 6) is None
    assert parse_cell("H9", 8, 6) is None  # off the grid


def test_the_point_snaps_to_what_is_drawn() -> None:
    image = Image.new("RGB", (200, 200), (240, 240, 240))
    ImageDraw.Draw(image).ellipse((130, 40, 150, 60), fill=(30, 30, 30))  # an icon at (140, 50)

    assert ink_centre(image, Box(100, 0, 200, 100)) == (140, 50)
    assert ink_centre(image, Box(0, 100, 100, 200)) == (50, 150)  # empty: the middle


def test_the_grid_is_drawn_on_a_copy() -> None:
    image = Image.new("RGB", (320, 240), "white")

    shown = overlay_grid(image, 4, 3)

    assert shown.getpixel((80, 120)) == (255, 0, 255)  # a cell border
    assert image.getpixel((80, 120)) == (255, 255, 255)


async def test_locating_takes_two_grid_rounds() -> None:
    image = Image.new("RGB", (1280, 720), (235, 235, 235))
    ImageDraw.Draw(image).rectangle((880, 490, 920, 510), fill=(40, 90, 200))  # at (900, 500)
    answers = iter(["F5", "D2"])  # (900, 500): coarse cell F5, then D2 of its zoomed crop
    asked: list[Image.Image] = []

    class Scripted(Vision):
        async def _send(self, image: Image.Image, question: str, max_tokens: int = 40) -> str:
            asked.append(image)
            return next(answers)

    point = await Scripted().locate(image, "the blue button")

    assert len(asked) == 2
    assert asked[1].width == 768  # the second round sees the cell zoomed in
    assert point is not None
    assert abs(point[0] - 900) <= 3
    assert abs(point[1] - 500) <= 3


async def test_not_on_screen_is_none() -> None:
    class Scripted(Vision):
        async def _send(self, image: Image.Image, question: str, max_tokens: int = 40) -> str:
            return "NONE"

    assert await Scripted().locate(Image.new("RGB", (640, 360)), "a unicorn") is None
