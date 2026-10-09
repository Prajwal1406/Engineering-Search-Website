#!/usr/bin/env python3
"""Search multiple Engineering Search V2 SQLite databases from one GUI.

This is a search-only application. It does not require openpyxl and does not
modify the databases. Databases must use the schema created by
engineering_search_v2.py (files, rows_data, rows_fts).
"""
from __future__ import annotations

import argparse
import os
import queue
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_DB_FOLDER = os.getenv("ENGINEERING_SEARCH_DB_FOLDER", str(Path(__file__).resolve().parent / "databases"))
MAX_RESULTS_PER_DATABASE = 1500
MAX_TOTAL_RESULTS = 5000


def friendly_name(path: Path) -> str:
    name = path.stem
    replacements = {
        "Engineering_Search_V2_Area1": "Area 1",
        "Engineering_Search_V2_Area2": "Area 2",
        "Engineering_Search_V2_Area3": "Area 3",
        "Engineering_Search_V2_Area4": "Area 4",
    }
    return replacements.get(name, name)


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA query_only=ON")
    return con


def valid_v2_database(path: Path) -> tuple[bool, str]:
    try:
        con = open_readonly(path)
        names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('files','rows_data','rows_fts')"
            )
        }
        con.close()
        missing = {"files", "rows_data", "rows_fts"} - names
        if missing:
            return False, "Missing: " + ", ".join(sorted(missing))
        return True, ""
    except Exception as exc:
        return False, str(exc)


def fts_query(raw: str) -> str:
    terms = [term.strip() for term in raw.split() if term.strip()]
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def search_one_database(path: Path, raw_query: str, prioritize_bom: bool):
    started = time.perf_counter()
    con = open_readonly(path)
    try:
        bom_sort = "f.is_bom DESC," if prioritize_bom else ""
        sql = f"""
            SELECT
                f.file_name,
                r.sheet_name,
                r.row_number,
                r.row_text,
                f.is_bom,
                f.is_old,
                datetime(f.modified_ns/1000000000,'unixepoch','localtime'),
                f.full_path
            FROM rows_fts x
            JOIN rows_data r ON r.id = x.rowid
            JOIN files f ON f.id = r.file_id
            WHERE rows_fts MATCH ?
              AND f.status = 'active'
            ORDER BY {bom_sort} f.is_old ASC, f.modified_ns DESC
            LIMIT ?
        """
        rows = con.execute(sql, (fts_query(raw_query), MAX_RESULTS_PER_DATABASE)).fetchall()
        return rows, time.perf_counter() - started, None
    except Exception as exc:
        return [], time.perf_counter() - started, str(exc)
    finally:
        con.close()


def launch_gui(db_folder: Path):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class MultiDatabaseSearch(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Engineering Search - Multiple Databases")
            self.geometry("1500x860")
            self.minsize(1050, 620)
            self.db_folder = db_folder
            self.db_vars: dict[Path, tk.BooleanVar] = {}
            self.search_running = False
            self.result_queue: queue.Queue = queue.Queue()
            self.build_ui()
            self.reload_databases()

        def build_ui(self):
            source_box = ttk.LabelFrame(self, text="Search areas / databases", padding=8)
            source_box.pack(fill="x", padx=10, pady=(10, 5))

            toolbar = ttk.Frame(source_box)
            toolbar.pack(fill="x")
            self.folder_label = ttk.Label(toolbar, text=str(self.db_folder))
            self.folder_label.pack(side="left", fill="x", expand=True)
            ttk.Button(toolbar, text="Choose Folder", command=self.choose_folder).pack(side="right")
            ttk.Button(toolbar, text="Refresh", command=self.reload_databases).pack(side="right", padx=5)
            ttk.Button(toolbar, text="Select All", command=lambda: self.set_all(True)).pack(side="right", padx=5)
            ttk.Button(toolbar, text="Clear All", command=lambda: self.set_all(False)).pack(side="right")

            self.db_frame = ttk.Frame(source_box)
            self.db_frame.pack(fill="x", pady=(7, 0))

            search_bar = ttk.Frame(self, padding=(10, 5))
            search_bar.pack(fill="x")
            ttk.Label(search_bar, text="Part / keyword:").pack(side="left")
            self.query_entry = ttk.Entry(search_bar, width=52)
            self.query_entry.pack(side="left", padx=8)
            self.query_entry.bind("<Return>", lambda event: self.start_search())
            self.prioritize_bom = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                search_bar, text="Prioritize BOM / 部品表", variable=self.prioritize_bom
            ).pack(side="left", padx=8)
            self.search_button = ttk.Button(search_bar, text="Search Selected", command=self.start_search)
            self.search_button.pack(side="left", padx=4)
            self.clear_button = ttk.Button(search_bar, text="Clear Results", command=self.clear_results)
            self.clear_button.pack(side="left")

            self.status = ttk.Label(self, text="Select databases, enter a keyword, and search.", padding=(10, 2))
            self.status.pack(fill="x")
            self.progress = ttk.Progressbar(self, mode="indeterminate")

            columns = ("area", "file", "sheet", "row", "evidence", "bom", "old", "modified", "path")
            self.tree = ttk.Treeview(self, columns=columns, show="headings")
            headings = {
                "area": "Area / Database", "file": "File", "sheet": "Sheet", "row": "Row",
                "evidence": "Matching row content", "bom": "BOM?", "old": "Old?",
                "modified": "Modified", "path": "Full path",
            }
            widths = {
                "area": 155, "file": 250, "sheet": 120, "row": 60, "evidence": 480,
                "bom": 55, "old": 55, "modified": 135, "path": 520,
            }
            for column in columns:
                self.tree.heading(column, text=headings[column])
                self.tree.column(column, width=widths[column], anchor="w")
            self.tree.pack(fill="both", expand=True, padx=10, pady=8)
            self.tree.bind("<Double-1>", lambda event: self.open_file())

            actions = ttk.Frame(self, padding=(10, 0, 10, 10))
            actions.pack(fill="x")
            ttk.Button(actions, text="Open File", command=self.open_file).pack(side="left")
            ttk.Button(actions, text="Open Folder", command=self.open_folder).pack(side="left", padx=6)
            ttk.Button(actions, text="Copy Path", command=self.copy_path).pack(side="left")
            self.count_label = ttk.Label(actions, text="0 results")
            self.count_label.pack(side="right")

        def choose_folder(self):
            selected = filedialog.askdirectory(initialdir=str(self.db_folder))
            if selected:
                self.db_folder = Path(selected)
                self.folder_label.config(text=str(self.db_folder))
                self.reload_databases()

        def reload_databases(self):
            for widget in self.db_frame.winfo_children():
                widget.destroy()
            self.db_vars.clear()

            if not self.db_folder.is_dir():
                self.status.config(text=f"Database folder not found: {self.db_folder}")
                return

            candidates = sorted(self.db_folder.glob("*.db"), key=lambda p: p.name.casefold())
            valid_count = 0
            for index, path in enumerate(candidates):
                valid, reason = valid_v2_database(path)
                if not valid:
                    continue
                valid_count += 1
                var = tk.BooleanVar(value=True)
                self.db_vars[path] = var
                size_gib = path.stat().st_size / (1024 ** 3)
                label = f"{friendly_name(path)}  ({size_gib:.2f} GiB)"
                check = ttk.Checkbutton(self.db_frame, text=label, variable=var)
                check.grid(row=(valid_count - 1) // 2, column=(valid_count - 1) % 2, sticky="w", padx=(0, 30), pady=2)

            self.status.config(text=f"Found {valid_count} compatible V2 database(s).")

        def set_all(self, state: bool):
            for var in self.db_vars.values():
                var.set(state)

        def selected_databases(self):
            return [path for path, var in self.db_vars.items() if var.get()]

        def clear_results(self):
            if self.search_running:
                return
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.count_label.config(text="0 results")
            self.status.config(text="Results cleared.")

        def start_search(self):
            if self.search_running:
                return
            raw_query = self.query_entry.get().strip()
            selected = self.selected_databases()
            if not raw_query:
                self.status.config(text="Enter a part number or keyword first.")
                self.query_entry.focus_set()
                return
            if any(len(term) < 3 for term in raw_query.split()):
                messagebox.showinfo(
                    "Search term too short",
                    "Each search term must contain at least 3 characters.\nExamples: QY80, PS21, PLC, シーケンサ",
                )
                return
            if not selected:
                messagebox.showinfo("No database selected", "Select at least one database.")
                return

            self.search_running = True
            self.search_button.config(state="disabled")
            self.clear_button.config(state="disabled")
            self.query_entry.config(state="disabled")
            self.config(cursor="wait")
            self.status.config(text=f'Searching {len(selected)} database(s) for "{raw_query}"...')
            self.progress.pack(fill="x", padx=10, pady=(3, 0))
            self.progress.start(12)
            threading.Thread(
                target=self.perform_multi_search,
                args=(selected, raw_query, self.prioritize_bom.get()),
                daemon=True,
            ).start()

        def perform_multi_search(self, databases, raw_query, prioritize_bom):
            overall_started = time.perf_counter()
            results = []
            timings = []
            errors = []
            workers = min(4, len(databases))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                jobs = {
                    executor.submit(search_one_database, path, raw_query, prioritize_bom): path
                    for path in databases
                }
                for future in as_completed(jobs):
                    path = jobs[future]
                    try:
                        rows, elapsed, error = future.result()
                        timings.append((friendly_name(path), elapsed, len(rows)))
                        if error:
                            errors.append(f"{friendly_name(path)}: {error}")
                        for row in rows:
                            results.append((friendly_name(path),) + row)
                    except Exception as exc:
                        errors.append(f"{friendly_name(path)}: {exc}")

            # Global ordering: BOM first if requested, then current files, then modified date.
            # Row layout: area,file,sheet,row,evidence,bom,old,modified,path
            results.sort(
                key=lambda r: (
                    -int(r[5]) if prioritize_bom else 0,
                    int(r[6]),
                    str(r[0]).casefold(),
                    str(r[1]).casefold(),
                    int(r[3]),
                )
            )
            if len(results) > MAX_TOTAL_RESULTS:
                results = results[:MAX_TOTAL_RESULTS]
            elapsed = time.perf_counter() - overall_started
            self.after(0, self.finish_search, results, elapsed, timings, errors)

        def finish_search(self, results, elapsed, timings, errors):
            self.progress.stop()
            self.progress.pack_forget()
            self.search_button.config(state="normal")
            self.clear_button.config(state="normal")
            self.query_entry.config(state="normal")
            self.config(cursor="")
            self.search_running = False

            for item in self.tree.get_children():
                self.tree.delete(item)
            for row in results:
                shown = list(row)
                shown[5] = "Yes" if row[5] else "No"
                shown[6] = "Yes" if row[6] else "No"
                self.tree.insert("", "end", values=shown)

            self.count_label.config(text=f"{len(results):,} results")
            timing_text = ", ".join(f"{name}: {count} in {seconds:.2f}s" for name, seconds, count in sorted(timings))
            message = f"{len(results):,} matching row(s) found across {len(timings)} database(s) in {elapsed:.2f} seconds."
            if len(results) >= MAX_TOTAL_RESULTS:
                message += f" Showing the first {MAX_TOTAL_RESULTS:,}."
            self.status.config(text=message)
            if errors:
                messagebox.showwarning("Some databases could not be searched", "\n".join(errors))
            self.query_entry.focus_set()

        def selected_path(self):
            selected = self.tree.selection()
            if not selected:
                return None
            return self.tree.item(selected[0], "values")[-1]

        def open_file(self):
            path = self.selected_path()
            if not path:
                return
            if Path(path).exists():
                os.startfile(path)
            else:
                messagebox.showwarning("Unavailable", f"File is unavailable:\n{path}")

        def open_folder(self):
            path = self.selected_path()
            if path:
                subprocess.Popen(["explorer", "/select,", path])

        def copy_path(self):
            path = self.selected_path()
            if path:
                self.clipboard_clear()
                self.clipboard_append(path)

    MultiDatabaseSearch().mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Search selected Engineering Search V2 databases together.")
    parser.add_argument("--db-folder", default=DEFAULT_DB_FOLDER, help="Folder containing V2 .db files")
    args = parser.parse_args()
    try:
        launch_gui(Path(args.db_folder))
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
