"""``bot2048 vision`` sub-app: calibration, board parsing and glyph tools.

CLI output uses rich (library code itself stays print-free).
Image loading needs cv2 or Pillow (``pip install -e .[device]``); everything
else in the vision package runs on the core dependencies only.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Annotated, Any, cast

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from bot2048.core.types import BOARD_SIZE
from bot2048.vision.calibration import (
    DEFAULT_CALIBRATION_PATH,
    Calibration,
    CalibrationError,
)
from bot2048.vision.glyphs import (
    DEFAULT_GLYPHS_PATH,
    Glyph,
    GlyphError,
    GlyphStore,
    digit_components,
    normalize,
    white_mask,
)
from bot2048.vision.grid import detect_grid
from bot2048.vision.parser import (
    BoardParseError,
    Image,
    cell_crop,
    cell_half_size,
    parse_board_detailed,
)
from bot2048.vision.reference import PALETTE, STATE_ANCHORS_DICT, reference_calibration
from bot2048.vision.states import (
    ScreenState,
    StateAnchors,
    detect_state,
    load_calibration_bundle,
)

app = typer.Typer(name="vision", no_args_is_help=True, help="Board calibration and parsing.")
console = Console()
err_console = Console(stderr=True)


class MissingImageBackendError(RuntimeError):
    """Neither cv2 nor Pillow is installed -- cannot read image files."""


def _load_image_rgb(path: Path) -> Image:
    """Read an image file as RGB uint8 (cv2 if present, else Pillow)."""
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    try:
        cv2 = importlib.import_module("cv2")
    except ImportError:
        cv2 = None
    if cv2 is not None:
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise BoardParseError(f"cv2 could not decode {path}")
        return cast(Image, np.ascontiguousarray(bgr[:, :, ::-1], dtype=np.uint8))
    try:
        pil_image = importlib.import_module("PIL.Image")
    except ImportError:
        raise MissingImageBackendError(
            "reading images requires cv2 or Pillow -- install the device extras: "
            "pip install -e .[device]"
        ) from None
    with pil_image.open(path) as im:
        return cast(Image, np.asarray(im.convert("RGB"), dtype=np.uint8))


def _load_glyphs(path: Path | None) -> GlyphStore:
    """Packaged templates + the optional on-host store."""
    return GlyphStore.load_default(path if path is not None else DEFAULT_GLYPHS_PATH)


@app.command()
def calibrate(
    image: Annotated[
        Path | None,
        typer.Option(
            "--image",
            help="Screenshot of a normal board: the grid geometry is DETECTED "
            "from it (and the result verified) instead of trusting the "
            "reference-device constants.",
        ),
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the calibration JSON.")
    ] = DEFAULT_CALIBRATION_PATH,
) -> None:
    """Write the calibration (+state anchors) to JSON.

    Without --image the hand-measured reference-device calibration is
    written as-is. With --image the board panel is located in the frame
    (bot2048.vision.grid.detect_grid), the palette and state anchors come
    from the reference measurements, and the screenshot must classify as
    GAME/CONFIRM_NEW_GAME and parse before anything is written.
    """
    calib = reference_calibration()
    if image is not None:
        try:
            img = _load_image_rgb(image)
            geometry = detect_grid(img)
            calib = Calibration(
                grid_bbox=geometry.grid_bbox,
                cell_centers=geometry.cell_centers,
                palette=PALETTE,
                screen_size=(img.shape[1], img.shape[0]),
            )
            anchors = StateAnchors.from_dict(cast("dict[str, Any]", STATE_ANCHORS_DICT))
            det = detect_state(img, calib, anchors)
            reads = parse_board_detailed(img, calib, _load_glyphs(None))
        except (
            CalibrationError,
            BoardParseError,
            GlyphError,
            MissingImageBackendError,
            OSError,
        ) as exc:
            err_console.print(f"[red]verification failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"detected panel {geometry.panel_bbox} -> grid {geometry.grid_bbox}, "
            f"cell {geometry.cell_size}px"
        )
        console.print(f"verify {image.name}: state={det.state.value} ({det.reason})")
        console.print(f"verify board: {tuple(r.exponent for r in reads)}")
        if det.state not in (ScreenState.GAME, ScreenState.CONFIRM_NEW_GAME):
            err_console.print("[red]screenshot did not classify as a game screen[/red]")
            raise typer.Exit(code=1)
    payload = calib.to_dict()
    payload["state_anchors"] = StateAnchors.from_dict(
        cast("dict[str, Any]", STATE_ANCHORS_DICT)
    ).to_dict()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    source = f"geometry detected from {image.name}" if image is not None else "reference device"
    console.print(f"calibration written to [bold]{out}[/bold] ({source})")


@app.command()
def state(
    image: Annotated[Path, typer.Argument(help="Screenshot to classify (PNG/JPG).")],
    calibration: Annotated[
        Path, typer.Option("--calibration", "-c", help="Calibration JSON.")
    ] = DEFAULT_CALIBRATION_PATH,
) -> None:
    """Classify a screenshot into a ScreenState (debug tool)."""
    try:
        calib, anchors = load_calibration_bundle(calibration)
        img = _load_image_rgb(image)
        det = detect_state(img, calib, anchors)
    except (CalibrationError, MissingImageBackendError, BoardParseError, OSError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"{image.name}: [bold]{det.state.value}[/bold] "
        f"(reason: {det.reason}; board match {det.board_match_frac:.0%})"
    )


@app.command()
def parse(
    image: Annotated[Path, typer.Argument(help="Screenshot to parse (PNG/JPG).")],
    calibration: Annotated[
        Path, typer.Option("--calibration", "-c", help="Calibration JSON.")
    ] = DEFAULT_CALIBRATION_PATH,
    glyphs: Annotated[
        Path | None,
        typer.Option("--glyphs", help="Extra glyph store JSON (default: data/glyphs/glyphs.json)."),
    ] = None,
) -> None:
    """Parse a screenshot into a board and pretty-print it.

    Cells decided by the color FALLBACK (digits unreadable) are marked
    with ``*`` -- persistent fallbacks mean the glyph store needs new
    templates (``bot2048 vision add-glyphs``).
    """
    try:
        calib = Calibration.load(calibration)
        img = _load_image_rgb(image)
        reads = parse_board_detailed(img, calib, _load_glyphs(glyphs))
    except (
        CalibrationError,
        BoardParseError,
        GlyphError,
        MissingImageBackendError,
        OSError,
    ) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"{image.name}", show_header=False, padding=(0, 1))
    for row in range(BOARD_SIZE):
        cells = reads[row * BOARD_SIZE : (row + 1) * BOARD_SIZE]
        table.add_row(
            *(f"{r.value}{'*' if r.method == 'color' else ''}" if r.value else "." for r in cells)
        )
    console.print(table)
    console.print(f"exponents: {tuple(r.exponent for r in reads)}")
    worst = min(reads, key=lambda r: r.confidence)
    methods = {m: sum(1 for r in reads if r.method == m) for m in ("digits", "color", "empty")}
    console.print(
        f"methods: {methods}; worst cell {worst.index} "
        f"({worst.method}, confidence {worst.confidence:.2f})"
    )


def _parse_cell_label(spec: str) -> tuple[int, str]:
    """``r1c2=64`` -> (cell index 6, "64"). Raises typer.BadParameter."""
    try:
        pos, _, value = spec.partition("=")
        if not (
            len(pos) == 4
            and pos[0] == "r"
            and pos[2] == "c"
            and value.isdigit()
            and int(value) >= 2
        ):
            raise ValueError(spec)
        row, col = int(pos[1]), int(pos[3])
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            raise ValueError(spec)
    except ValueError as exc:
        raise typer.BadParameter(
            f"cell label must look like r0c3=16 (rows/cols 0-3, value >= 2), got {spec!r}"
        ) from exc
    return row * BOARD_SIZE + col, value


@app.command("add-glyphs")
def add_glyphs(
    image: Annotated[Path, typer.Argument(help="Screenshot with the new tile(s) (PNG/JPG).")],
    cell: Annotated[
        list[str],
        typer.Option(
            "--cell",
            help="Labeled cell, e.g. --cell r0c3=128 (repeatable). The printed "
            "value teaches one template per digit character.",
        ),
    ],
    calibration: Annotated[
        Path, typer.Option("--calibration", "-c", help="Calibration JSON.")
    ] = DEFAULT_CALIBRATION_PATH,
    store_path: Annotated[
        Path, typer.Option("--store", help="On-host glyph store to extend.")
    ] = DEFAULT_GLYPHS_PATH,
) -> None:
    """Teach the parser new digit glyphs from a labeled screenshot.

    Cuts the digit components out of each labeled cell, maps them left to
    right onto the digits of the label and appends them to the on-host
    store (merged with the packaged templates at parse time). Use this the
    first time a new tile (128, 256, ...) appears on a live board.
    """
    labels = [_parse_cell_label(spec) for spec in cell]
    try:
        calib = Calibration.load(calibration)
        img = _load_image_rgb(image)
        half = cell_half_size(calib)
        new_glyphs: list[Glyph] = []
        for index, text in labels:
            comps = digit_components(white_mask(cell_crop(img, calib.cell_centers[index], half)))
            if len(comps) != len(text):
                raise GlyphError(
                    f"cell r{index // BOARD_SIZE}c{index % BOARD_SIZE}: expected "
                    f"{len(text)} digit components for {text!r}, found {len(comps)} "
                    "(occluded or mislabeled?)"
                )
            for char, comp in zip(text, comps, strict=True):
                grid, aspect = normalize(comp)
                new_glyphs.append(Glyph(digit=char, grid=grid, aspect=aspect))
        existing = GlyphStore.load(store_path) if store_path.is_file() else GlyphStore()
        merged = existing.merged(GlyphStore(tuple(new_glyphs)))
        merged.save(store_path)
    except (
        CalibrationError,
        BoardParseError,
        GlyphError,
        MissingImageBackendError,
        OSError,
    ) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"added {len(new_glyphs)} glyph(s) "
        f"({''.join(sorted({g.digit for g in new_glyphs}))}) from {image.name}; "
        f"store [bold]{store_path}[/bold] now holds {len(merged)} template(s), "
        f"digits {''.join(sorted(merged.digits()))}"
    )
