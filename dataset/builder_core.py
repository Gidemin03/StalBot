"""Geometry, label validation and recoverable storage for StalBot datasets."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

from PIL import Image

GEOMETRY_VERSION = "auction-1920-v1"
AUCTION = (882, 370, 1387, 766)
BOXES = {
    "full_lot": (887, 393, 1386, 430),
    "name": (927, 394, 1140, 413),
    "quantity": (889, 409, 926, 430),
    "buyout": (1230, 399, 1384, 424),
}
COLORS = ("white", "green", "blue", "purple", "red", "gold", "unknown")


def row_boxes(row: int, menu_after: int = 0) -> dict:
    """Zero-based lot index; menu_after is 0 (closed) or 1..9."""
    if type(menu_after) is not int or not 0 <= menu_after <= 9:
        raise ValueError("Положение меню должно быть 0..9")
    if type(row) is not int or not 0 <= row < (9 if menu_after else 10):
        raise ValueError("Недопустимый номер лота")
    offset = 37 * (row + int(bool(menu_after) and row >= menu_after))
    return {key: (x1, y1 + offset, x2, y2 + offset)
            for key, (x1, y1, x2, y2) in BOXES.items()}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_image(path: Path):
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as image:
        if image.size not in ((1920, 1080), (1920, 1081)):
            raise ValueError(f"Размер {image.size}: нужны 1920×1080 или 1920×1081 без масштабирования")
        image.load()
        return image.convert("RGB"), digest(raw)


def positive_integer(text: str) -> int:
    # Permit plain digits or conventional groups, never silently remove punctuation.
    value = str(text).strip().replace("\u00a0", " ").replace("\u202f", " ")
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?: [0-9]{3})+)", value):
        raise ValueError("Введите целое число (допустимы пробелы: 1 000 000)")
    number = int(value.replace(" ", ""))
    if number < 1:
        raise ValueError("Число должно быть больше нуля")
    return number


def validate_label(data: dict) -> dict:
    if data.get("status") == "skipped":
        reason = str(data.get("reason", "")).strip()
        if not reason:
            raise ValueError("Для пропуска укажите причину")
        return {"status": "skipped", "reason": reason}
    if data.get("status") != "labeled":
        raise ValueError("Лот ещё не размечен")
    name = str(data.get("name_text", "")).strip()
    if not name:
        raise ValueError("Введите видимое имя, включая | +N и точки сокращения")
    color = data.get("name_color")
    if color not in COLORS:
        raise ValueError("Выберите цвет имени")
    upgrade_state = data.get("upgrade_state")
    if upgrade_state not in ("VALUE", "NOT_VISIBLE", "UNKNOWN"):
        raise ValueError("Выберите состояние заточки")
    upgrade = None
    if upgrade_state == "VALUE":
        value = str(data.get("upgrade", "")).strip()
        if not re.fullmatch(r"[0-9]{1,2}", value) or not 0 <= int(value) <= 15:
            raise ValueError("Уровень заточки: целое число 0..15")
        upgrade = int(value)
    suffix = re.search(r"\|\s*\+([0-9]+)\s*$", name)
    if suffix and (upgrade_state != "VALUE" or upgrade != int(suffix[1])):
        raise ValueError("Уровень заточки не совпадает с видимым суффиксом имени")
    if upgrade_state == "VALUE" and not suffix:
        raise ValueError("Добавьте видимый суффикс | +N в name_text; если его нет, выберите NOT_VISIBLE")
    states = {}
    for field in ("quantity", "buyout"):
        state = data.get(field + "_state")
        if state not in ("VALUE", "ABSENT", "AMBIGUOUS"):
            raise ValueError(f"Выберите состояние {field}")
        states[field + "_state"] = state
        states[field] = (positive_integer(data.get(field, "")) if state == "VALUE"
                         else 1 if field == "quantity" and state == "ABSENT" else None)
    return {"status": "labeled", "name_text": name,
            "name_truncated": name.endswith(("..", "…")),
            "name_color": color, "upgrade_state": upgrade_state, "upgrade": upgrade,
            **states, "note": str(data.get("note", "")).strip()}


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class DatasetLock:
    """OS lock released even after process termination; no stale PID lock."""
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.handle = (root / ".builder.lock").open("a+b")
        try:
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Этот датасет уже открыт другим Dataset Builder") from None

    def close(self):
        self.handle.close()


class Store:
    """Caller holds DatasetLock for all operations.

    Crops are immutable. Atomic labels.json replacement is the commit point.
    Moving the source is recoverable and happens only after commit verification.
    """
    def __init__(self, root: Path):
        self.root = root.resolve()
        for folder in ("screenshots/pending", "screenshots/processed", "labels/drafts",
                       *("crops/" + key for key in BOXES)):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "labels/labels.json"
        self.index = self.read_index()

    def read_index(self):
        if not self.index_path.exists():
            return {"schema_version": 1, "screenshots": {}}
        with self.index_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if value.get("schema_version") != 1 or not isinstance(value.get("screenshots"), dict):
            raise ValueError("Неизвестный формат labels.json — файл не изменён")
        return value

    def pending(self):
        return sorted(p for p in (self.root / "screenshots/pending").iterdir()
                      if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"))

    def save_draft(self, sha: str, value: dict):
        atomic_json(self.root / "labels/drafts" / (sha + ".json"), value)

    def load_draft(self, sha: str):
        path = self.root / "labels/drafts" / (sha + ".json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _write_crop(self, relative: str, raw: bytes):
        destination = self.root / relative
        if destination.exists():
            if destination.read_bytes() != raw:
                raise ValueError(f"Конфликт кропа: {relative}; существующий файл не изменён")
            return
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic publication without overwriting any existing crop.
            os.link(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _verify(self, record: dict):
        for row in record["rows"]:
            for key, item in row.get("crops", {}).items():
                relative = item["path"]
                path = (self.root / relative).resolve()
                expected = (self.root / "crops" / key).resolve()
                if path.parent != expected or digest(path.read_bytes()) != item["sha256"]:
                    raise ValueError(f"Повреждён кроп: {relative}; исходник оставлен в pending")

    def _archive(self, source: Path, sha: str, record: dict):
        self._verify(record)
        if digest(source.read_bytes()) != sha:
            raise ValueError("Исходный скриншот изменился; перемещение отменено")
        destination = self.root / record["source"]
        if destination.parent.resolve() != (self.root / "screenshots/processed").resolve():
            raise ValueError("Недопустимый путь исходника в labels.json")
        if destination.exists():
            if digest(destination.read_bytes()) != sha:
                raise ValueError("Конфликт в processed; исходник оставлен в pending")
        else:
            # Both folders live on the same filesystem. Link first, unlink last.
            os.link(source, destination)
        source.unlink()
        (self.root / "labels/drafts" / (sha + ".json")).unlink(missing_ok=True)

    def recover(self, source: Path, sha: str) -> bool:
        record = self.index["screenshots"].get(sha)
        if record is None:
            return False
        self._archive(source, sha, record)
        return True

    def commit(self, source: Path, sha: str, menu_after: int, rows: list):
        if source.parent.resolve() != (self.root / "screenshots/pending").resolve():
            raise ValueError("Разрешена обработка только файлов из pending")
        if self.recover(source, sha):
            return
        image, actual_sha = read_image(source)
        if actual_sha != sha:
            raise ValueError("Исходник изменился во время разметки")
        count = 9 if menu_after else 10
        if len(rows) != count:
            raise ValueError("Нужно разметить или явно пропустить каждую строку")
        labels = [validate_label(row) for row in rows]
        record = {"original_name": source.name, "sha256": sha, "size": list(image.size),
                  "source": f"screenshots/processed/{sha}{source.suffix.lower()}",
                  "geometry_version": GEOMETRY_VERSION, "menu_after": menu_after, "rows": []}
        # Validate every label before writing the first crop.
        for number, label in enumerate(labels):
            boxes = row_boxes(number, menu_after)
            row = {"row": number + 1, **label, "roi": boxes, "crops": {}}
            if label["status"] == "labeled":
                for key, box in boxes.items():
                    buffer = io.BytesIO()
                    image.crop(box).save(buffer, format="PNG")
                    raw = buffer.getvalue()
                    relative = f"crops/{key}/{sha}_m{menu_after}_r{number + 1:02}.png"
                    self._write_crop(relative, raw)
                    row["crops"][key] = {"path": relative, "sha256": digest(raw)}
            record["rows"].append(row)
        self._verify(record)
        # Keep self.index unchanged if replacement fails.
        updated = {**self.index, "screenshots": {**self.index["screenshots"], sha: record}}
        atomic_json(self.index_path, updated)
        self.index = updated
        self._archive(source, sha, record)
