"""Run: python builder.py [--dataset PATH]. No game interaction or OCR."""
from __future__ import annotations

import argparse
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from builder_core import COLORS, DatasetLock, Store, read_image, row_boxes, validate_label


FIELDS = ("name_text", "name_color", "upgrade_state", "upgrade", "quantity_state",
          "quantity", "buyout_state", "buyout", "note", "reason")


def paste_into_entry(widget):
    """Replace the selection with Unicode clipboard text, once per paste."""
    try:
        text = widget.clipboard_get()
    except tk.TclError:
        widget.bell()
        return "break"
    if text and not widget.instate(("disabled",)) and not widget.instate(("readonly",)):
        if widget.selection_present():
            start, end = widget.index("sel.first"), widget.index("sel.last")
            widget.delete(start, end)
            widget.icursor(start)
        widget.insert("insert", text)
    return "break"


def entry_control_key(event):
    # Windows reports VK_V (86) regardless of the current keyboard layout.
    # Widget binding runs before TEntry class bindings; break prevents double paste.
    windows = event.widget.tk.call("tk", "windowingsystem") == "win32"
    if (windows and event.keycode == 86) or event.keysym.lower() == "v":
        return paste_into_entry(event.widget)
    return None


def enable_entry_paste(widget):
    widget.bind("<Control-KeyPress>", entry_control_key)
    widget.bind("<<Paste>>", lambda event: paste_into_entry(widget))
    menu = tk.Menu(widget, tearoff=False)
    menu.add_command(label="Вставить (Ctrl+V)", command=lambda: paste_into_entry(widget))

    def popup(event):
        widget.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    widget.bind("<Button-3>", popup)
    return menu


class Builder:
    def __init__(self, root: tk.Tk, store: Store):
        self.root, self.store = root, store
        self.source = None
        self.loading = True
        self.timer = None
        self.photos = []
        self.queue = store.pending()
        self.rows = []
        self.index = 0
        root.title("StalBot — Dataset Builder")
        root.geometry("1250x860")
        root.minsize(1200, 820)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.title = tk.StringVar()
        ttk.Label(root, textvariable=self.title, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=12, pady=8)
        top = ttk.Frame(root)
        top.pack(fill="x", padx=12)
        ttk.Label(top, text="Меню покупки после лота (0 = закрыто):").pack(side="left")
        self.menu = tk.IntVar(value=0)
        menu = ttk.Combobox(top, textvariable=self.menu, values=list(range(10)), state="readonly", width=4)
        menu.pack(side="left", padx=6)
        menu.bind("<<ComboboxSelected>>", self.change_menu)
        self.confirmed = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Проверил совпадение рамок со строками", variable=self.confirmed,
                        command=self.schedule_draft).pack(side="left", padx=12)
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True, padx=12, pady=8)
        left, right = ttk.Frame(body), ttk.Frame(body)
        left.pack(side="left", fill="y")
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))
        ttk.Label(left, text="Область аукциона • текущая строка выделена жёлтым").pack(anchor="w")
        self.overview = ttk.Label(left)
        self.overview.pack()
        self.progress = tk.StringVar()
        ttk.Label(left, textvariable=self.progress).pack(anchor="w", pady=5)
        self.listbox = tk.Listbox(left, height=10, exportselection=False, font=("Segoe UI", 10))
        self.listbox.pack(fill="x")
        self.listbox.bind("<<ListboxSelect>>", self.select_row)
        ttk.Button(left, text="Отложить скриншот (оставить в pending)", command=self.defer).pack(fill="x", pady=6)
        ttk.Button(left, text="Обновить очередь pending", command=self.refresh).pack(fill="x")
        self.preview = {}
        for key, caption in (("full_lot", "Полный лот"), ("name", "NAME ×2"),
                             ("quantity", "QTY ×2"), ("buyout", "BUYOUT ×2")):
            frame = ttk.Frame(right)
            frame.pack(fill="x")
            ttk.Label(frame, text=caption, width=13).pack(side="left")
            widget = ttk.Label(frame)
            widget.pack(side="left", pady=2)
            self.preview[key] = widget
        form = ttk.Frame(right)
        form.pack(fill="x", pady=10)
        form.columnconfigure(1, weight=1)
        self.variables = {key: tk.StringVar() for key in FIELDS}
        options = {"name_color": COLORS, "upgrade_state": ("VALUE", "NOT_VISIBLE", "UNKNOWN"),
                   "quantity_state": ("VALUE", "ABSENT", "AMBIGUOUS"),
                   "buyout_state": ("VALUE", "ABSENT", "AMBIGUOUS")}
        captions = ("Видимое имя целиком", "Цвет имени", "Состояние заточки", "Заточка 0..15",
                    "Состояние QTY", "Количество", "Состояние BUYOUT", "Цена выкупа, руб.",
                    "Заметка", "Причина пропуска")
        for row, (key, caption) in enumerate(zip(FIELDS, captions)):
            ttk.Label(form, text=caption).grid(row=row, column=0, sticky="w", pady=3)
            if key in options:
                widget = ttk.Combobox(form, textvariable=self.variables[key], values=options[key], state="readonly")
            else:
                widget = ttk.Entry(form, textvariable=self.variables[key])
                enable_entry_paste(widget)
            widget.grid(row=row, column=1, sticky="ew", padx=8, pady=3)
            if key == "name_text":
                self.name_entry = widget
            self.variables[key].trace_add("write", self.edited)
        ttk.Label(right, text="VALUE — видно значение; AMBIGUOUS — не разобрать.\n"
                  "QTY ABSENT — цифр нет, количество = 1; BUYOUT ABSENT — ‘---’.\n"
                  "NOT_VISIBLE — суффикса заточки нет (уровень не додумываем).\n"
                  "Цвет: white / green / blue / purple / red / gold; unknown — неизвестен.\n"
                  "Имя вводите как видно, включая | +N и ..; при неясном BID — пропуск.",
                  justify="left").pack(anchor="w")
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=10)
        ttk.Button(buttons, text="← Назад", command=lambda: self.navigate(-1)).pack(side="left")
        ttk.Button(buttons, text="Разметить → (Ctrl+Enter)", command=self.accept).pack(side="left", padx=5)
        ttk.Button(buttons, text="Пропустить лот", command=self.skip).pack(side="left")
        ttk.Button(right, text="Сохранить весь скриншот и перейти к следующему", command=self.commit).pack(fill="x")
        self.status = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.status, wraplength=1200).pack(fill="x", padx=12, pady=8)
        root.bind("<Control-Return>", lambda event: self.accept())
        root.bind("<Alt-Left>", lambda event: self.navigate(-1))
        root.bind("<Alt-Right>", lambda event: self.navigate(1))
        self.loading = False
        self.next_source()

    def error(self, exc):
        self.status.set(f"Ошибка: {exc}. Исходник не удаляется; повторите действие после устранения причины.")
        messagebox.showerror("Dataset Builder", str(exc), parent=self.root)

    def stash(self):
        if self.source is not None:
            status = self.rows[self.index].get("status", "unreviewed")
            self.rows[self.index] = {"status": status, **{k: v.get() for k, v in self.variables.items()}}

    def save_draft(self):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.source is None:
            return
        self.stash()
        self.store.save_draft(self.sha, {"geometry_version": "auction-1920-v1", "menu_after": self.menu.get(),
                                       "confirmed": self.confirmed.get(), "index": self.index, "rows": self.rows})

    def schedule_draft(self):
        if self.loading or self.source is None:
            return
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.timer = self.root.after(400, self.autosave)

    def autosave(self):
        self.timer = None
        try:
            self.save_draft()
            self.status.set("Черновик сохранён. Для завершения отметьте все строки и сохраните скриншот.")
        except Exception as exc:
            self.error(exc)

    def edited(self, *_):
        if not self.loading and self.source is not None:
            self.rows[self.index]["status"] = "unreviewed"
            self.schedule_draft()

    def next_source(self):
        self.source = None
        while self.queue:
            source = self.queue.pop(0)
            try:
                picture, sha = read_image(source)
                if self.store.recover(source, sha):
                    continue
                draft = self.store.load_draft(sha)
                menu = draft["menu_after"] if draft else 0
                row_boxes(0, menu)
                rows = draft["rows"] if draft else [{"status": "unreviewed"} for _ in range(10)]
                if len(rows) != (9 if menu else 10):
                    raise ValueError("Несовместимый черновик: неверное число строк")
                if draft and draft.get("geometry_version") != "auction-1920-v1":
                    raise ValueError("Несовместимая геометрия черновика")
                self.picture, self.sha, self.rows = picture, sha, rows
                self.menu.set(menu)
                self.confirmed.set(draft.get("confirmed", False) if draft else False)
                self.index = min(draft.get("index", 0), len(rows) - 1) if draft else 0
                self.source = source
                self.title.set(f"{source.name} — {picture.width}×{picture.height} — в очереди ещё {len(self.queue)}")
                self.show_row()
                self.status.set("Проверьте рамки. Если открыто меню покупки, укажите номер выбранного лота.")
                return
            except Exception as exc:
                self.error(exc)
        self.title.set("Очередь закончилась. Отложенные и ошибочные файлы остаются в pending.")
        self.status.set("Добавьте скриншоты и нажмите «Обновить очередь pending».")
        self.listbox.delete(0, tk.END)
        self.progress.set("")
        for widget in (self.overview, *self.preview.values()):
            widget.configure(image="")

    def show_row(self):
        self.loading = True
        try:
            row = self.rows[self.index]
            for key, variable in self.variables.items():
                value = row.get(key, "")
                variable.set("" if value is None else str(value))
            self.listbox.delete(0, tk.END)
            markers = {"labeled": "✓", "skipped": "—", "unreviewed": "?"}
            for number, item in enumerate(self.rows):
                self.listbox.insert(tk.END, f"{number + 1:2}  {markers.get(item.get('status'), '?')}  {item.get('name_text', item.get('reason', ''))}")
            self.listbox.selection_set(self.index)
            completed = sum(r.get("status") in ("labeled", "skipped") for r in self.rows)
            self.progress.set(f"Лот {self.index + 1}/{len(self.rows)} • готово {completed}/{len(self.rows)}")
            self.photos = []
            overview = self.picture.crop((880, 360, 1390, 790))
            draw = ImageDraw.Draw(overview)
            for number in range(len(self.rows)):
                x1, y1, x2, y2 = row_boxes(number, self.menu.get())["full_lot"]
                draw.rectangle((x1-880, y1-360, x2-881, y2-361), outline="yellow" if number == self.index else "#547b86")
            for key, box in row_boxes(self.index, self.menu.get()).items():
                crop = self.picture.crop(box)
                if key != "full_lot":
                    crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.NEAREST)
                    x1, y1, x2, y2 = box
                    draw.rectangle((x1-880, y1-360, x2-881, y2-361), outline={"name": "#72ff72", "quantity": "cyan", "buyout": "#ff80ff"}[key])
                photo = ImageTk.PhotoImage(crop)
                self.photos.append(photo)
                self.preview[key].configure(image=photo)
            photo = ImageTk.PhotoImage(overview)
            self.photos.append(photo)
            self.overview.configure(image=photo)
            self.name_entry.focus_set()
        finally:
            self.loading = False

    def change_menu(self, _=None):
        if self.source is None:
            return
        self.stash()
        count = 9 if self.menu.get() else 10
        self.rows = (self.rows + [{} for _ in range(10)])[:count]
        for row in self.rows:
            row["status"] = "unreviewed"
        self.confirmed.set(False)
        self.index = min(self.index, count - 1)
        self.show_row()
        self.schedule_draft()
        self.status.set("Геометрия изменена. Все строки необходимо проверить заново.")

    def select_row(self, _=None):
        if self.loading or self.source is None:
            return
        selection = self.listbox.curselection()
        if selection and selection[0] != self.index:
            self.navigate(selection[0] - self.index)

    def navigate(self, step):
        if self.source is None:
            return
        try:
            self.save_draft()
            self.index = max(0, min(len(self.rows) - 1, self.index + step))
            self.show_row()
        except Exception as exc:
            self.error(exc)

    def accept(self):
        if self.source is None:
            return
        try:
            self.stash()
            candidate = {**self.rows[self.index], "status": "labeled"}
            validate_label(candidate)
            self.rows[self.index] = candidate
            self.save_draft()
            self.index = min(self.index + 1, len(self.rows) - 1)
            self.show_row()
        except Exception as exc:
            self.error(exc)

    def skip(self):
        if self.source is None:
            return
        try:
            self.stash()
            candidate = {**self.rows[self.index], "status": "skipped"}
            validate_label(candidate)
            self.rows[self.index] = candidate
            self.save_draft()
            self.index = min(self.index + 1, len(self.rows) - 1)
            self.show_row()
        except Exception as exc:
            self.error(exc)

    def commit(self):
        if self.source is None:
            return
        try:
            self.save_draft()
            if not self.confirmed.get():
                raise ValueError("Сначала проверьте совпадение рамок со строками и отметьте флажок")
            self.store.commit(self.source, self.sha, self.menu.get(), self.rows)
            self.next_source()
        except Exception as exc:
            self.error(exc)

    def defer(self):
        try:
            self.save_draft()
            self.next_source()
        except Exception as exc:
            self.error(exc)

    def refresh(self):
        try:
            self.save_draft()
            self.queue = self.store.pending()
            self.next_source()
        except Exception as exc:
            self.error(exc)

    def close(self):
        try:
            self.save_draft()
            self.root.destroy()
        except Exception as exc:
            self.error(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--smoke-test", action="store_true", help="Initialize the UI and exit without saving a draft")
    args = parser.parse_args()
    root = tk.Tk()
    if args.smoke_test:
        root.withdraw()
    lock = None
    try:
        lock = DatasetLock(args.dataset)
        app = Builder(root, Store(args.dataset))
        if args.smoke_test:
            root.update_idletasks()
            if app.source is None:
                raise RuntimeError("Smoke test requires a readable, unprocessed screenshot")
            print(f"UI OK: {app.source.name}, {app.picture.size}, {len(app.rows)} rows")
            root.destroy()
        else:
            root.mainloop()
    except Exception as exc:
        if args.smoke_test:
            root.destroy()
            raise
        messagebox.showerror("Dataset Builder", str(exc), parent=root)
        root.destroy()
        raise SystemExit(1) from exc
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
