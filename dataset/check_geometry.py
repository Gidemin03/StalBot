"""Read-only inspection of source images; write QA crops outside the dataset."""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from builder_core import BOXES, digest, read_image, row_boxes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source_root, output = args.dataset.resolve(), args.output.resolve()
    if output == source_root or source_root in output.parents:
        parser.error("QA output must be outside the source dataset")
    output.mkdir(parents=True, exist_ok=True)
    files = sorted((source_root / "hard_cases").glob("*.png")) + sorted(
        (source_root / "screenshots/pending").glob("*.png"))
    panels, report = [], []
    for path in files:
        image, sha = read_image(path)
        panel = Image.new("RGB", (840, 650), "#16191e")
        draw = ImageDraw.Draw(panel)
        draw.text((8, 4), path.name, fill="white")
        for row in range(10):
            left = 8
            for key, box in row_boxes(row).items():
                crop = image.crop(box)
                destination = output / path.stem / f"r{row + 1:02}_{key}.png"
                destination.parent.mkdir(parents=True, exist_ok=True)
                crop.save(destination)
                with Image.open(destination) as stored:
                    assert stored.size == crop.size and stored.tobytes() == crop.tobytes()
                if key != "full_lot":
                    scaled = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.NEAREST)
                    panel.paste(scaled, (left, 28 + row * 61))
                    left += scaled.width + 4
        assert digest(path.read_bytes()) == sha, "Source changed"
        report.append({"file": str(path), "sha256": sha, "size": list(image.size), "crops": 40})
        if path.parent.name == "hard_cases":
            panels.append(panel)
    for index in range(0, len(panels), 2):
        sheet = Image.new("RGB", (1680, 650))
        for offset, panel in enumerate(panels[index:index + 2]):
            sheet.paste(panel, (840 * offset, 0))
        sheet.save(output / f"hard_cases_{index // 2 + 1}.png")
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Verified {len(files)} images / {len(files) * 10 * len(BOXES)} pixel-exact crops; source hashes unchanged")


if __name__ == "__main__":
    main()
