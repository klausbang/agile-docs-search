import csv
import queue
import re
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


@dataclass
class DocumentRow:
    row_no: int
    doc_id: str
    document_number: str
    version: str
    name: str
    direct_link: str


@dataclass
class FilenameOptions:
    include_leading_doc_id: bool
    include_document_number: bool
    include_version: bool
    include_trailing_name: bool


def filename_from_headers(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None

    match_ext = re.search(r"filename\*=([^']*)''([^;]+)", content_disposition, re.IGNORECASE)
    if match_ext:
        return urllib.parse.unquote(match_ext.group(2)).strip('"')

    match_basic = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
    if match_basic:
        return match_basic.group(1)

    return None


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def is_no_document_payload(data: bytes) -> bool:
    return data.strip() in {b"No document found!", b"No files found!"}


def normalize_version(version: str) -> str:
    cleaned = version.strip()
    if not cleaned:
        return ""

    if re.match(r"^ver\b", cleaned, re.IGNORECASE):
        return cleaned.replace(" ", "")

    return f"Ver{cleaned}"


def build_output_filename(row: DocumentRow, inferred_name: str, options: FilenameOptions) -> str:
    source_name = safe_filename(inferred_name)
    source_path = Path(source_name)
    extension = source_path.suffix
    source_stem = source_path.stem

    prefix_parts: list[str] = []
    if options.include_document_number and row.document_number:
        prefix_parts.append(safe_filename(row.document_number))

    if options.include_version and row.version:
        prefix_parts.append(safe_filename(normalize_version(row.version)))

    if options.include_leading_doc_id and row.doc_id:
        prefix_parts.append(safe_filename(row.doc_id))

    base_stem = source_stem
    if prefix_parts:
        base_stem = f"{'_'.join(prefix_parts)}_{source_stem}"

    if options.include_trailing_name and row.name:
        name_part = safe_filename(row.name)
        if not base_stem.lower().endswith(name_part.lower()):
            base_stem = f"{base_stem}_{name_part}"

    return f"{base_stem}{extension}"


def download_file(
    url: str,
    output_dir: Path,
    row: DocumentRow,
    options: FilenameOptions,
    overwrite_existing: bool,
) -> tuple[Path | None, bool, bool, bool]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    with urllib.request.urlopen(request) as response:
        disposition = response.headers.get("Content-Disposition")
        inferred_name = filename_from_headers(disposition)

        if not inferred_name:
            parsed = urllib.parse.urlparse(url)
            fallback = Path(parsed.path).name or "downloaded_file"
            inferred_name = fallback

        data = response.read()
        if is_no_document_payload(data):
            return None, True, False, False

        filename = build_output_filename(row, inferred_name, options)
        output_path = output_dir / filename
        file_exists = output_path.exists()
        if file_exists and not overwrite_existing:
            return output_path, False, True, False

        output_path.write_bytes(data)
        was_overwritten = file_exists and overwrite_existing

    return output_path, False, False, was_overwritten


def unpack_zip_to_named_subfolder(zip_path: Path) -> Path:
    # Use the ZIP stem because a file and folder cannot share the same full name in one directory.
    extract_dir = zip_path.parent / zip_path.stem
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(extract_dir)
    return extract_dir


def parse_pasted_table(raw_text: str) -> list[DocumentRow]:
    lines = [line for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("No data provided.")

    reader = csv.reader(lines, delimiter="\t")
    all_rows = list(reader)
    header = all_rows[0]

    direct_link_idx = None
    doc_id_idx = None
    document_number_idx = None
    version_idx = None
    name_idx = None
    for idx, name in enumerate(header):
        name_clean = name.strip().lower()
        if name_clean == "direct link":
            direct_link_idx = idx
        if name_clean == "docid intern":
            doc_id_idx = idx
        if name_clean == "document number":
            document_number_idx = idx
        if name_clean in {"version", "ver.", "ver"}:
            version_idx = idx
        if name_clean == "name":
            name_idx = idx

    if direct_link_idx is None:
        raise ValueError("Could not find column 'Direct link' in pasted header row.")

    parsed_rows: list[DocumentRow] = []
    for row in all_rows[1:]:
        if direct_link_idx >= len(row):
            continue

        direct_link = row[direct_link_idx].strip()
        if not direct_link or not direct_link.lower().startswith("http"):
            continue

        doc_id = ""
        if doc_id_idx is not None and doc_id_idx < len(row):
            doc_id = row[doc_id_idx].strip()
        if not doc_id:
            parsed = urllib.parse.urlparse(direct_link)
            query = urllib.parse.parse_qs(parsed.query)
            doc_id = query.get("DOCUMENT_ID", [f"ROW_{len(parsed_rows) + 1}"])[0]

        document_number = ""
        if document_number_idx is not None and document_number_idx < len(row):
            document_number = row[document_number_idx].strip()

        version = ""
        if version_idx is not None and version_idx < len(row):
            version = row[version_idx].strip()

        name_value = ""
        if name_idx is not None and name_idx < len(row):
            name_value = row[name_idx].strip()

        parsed_rows.append(
            DocumentRow(
                row_no=len(parsed_rows) + 1,
                doc_id=doc_id,
                document_number=document_number,
                version=version,
                name=name_value,
                direct_link=direct_link,
            )
        )

    if not parsed_rows:
        raise ValueError("No valid rows found. Ensure pasted data includes HTTP links in 'Direct link'.")

    return parsed_rows


class DownloaderGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Agile Document Downloader")
        self.root.geometry("1300x760")

        self.default_output_dir = Path(__file__).resolve().parent / "downloads"
        self.default_output_dir.mkdir(parents=True, exist_ok=True)

        self.rows: list[DocumentRow] = []
        self.worker_thread: threading.Thread | None = None
        self.result_queue: queue.Queue[tuple] = queue.Queue()
        self.stop_requested = threading.Event()

        self._build_ui()
        self.root.after(150, self._process_queue)

    def _build_ui(self) -> None:
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill=tk.X)

        ttk.Label(top_frame, text="Output folder:").pack(side=tk.LEFT)
        self.output_var = tk.StringVar(value=str(self.default_output_dir))
        ttk.Entry(top_frame, textvariable=self.output_var, width=70).pack(side=tk.LEFT, padx=6)
        ttk.Button(top_frame, text="Browse", command=self._choose_output_folder).pack(side=tk.LEFT)

        ttk.Label(top_frame, text="Delay (seconds, min 1):").pack(side=tk.LEFT, padx=(14, 4))
        self.delay_var = tk.StringVar(value="5")
        ttk.Spinbox(top_frame, from_=1, to=120, textvariable=self.delay_var, width=6).pack(side=tk.LEFT)

        options_frame = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        options_frame.pack(fill=tk.X)
        ttk.Label(options_frame, text="Filename options:").pack(side=tk.LEFT)

        self.include_leading_doc_id_var = tk.BooleanVar(value=True)
        self.include_document_number_var = tk.BooleanVar(value=True)
        self.include_version_var = tk.BooleanVar(value=True)
        self.include_trailing_name_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(
            options_frame,
            text="Prefix DocID Intern",
            variable=self.include_leading_doc_id_var,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(
            options_frame,
            text="Prefix Document Number",
            variable=self.include_document_number_var,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(
            options_frame,
            text="Prefix Version",
            variable=self.include_version_var,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(
            options_frame,
            text="Suffix Name",
            variable=self.include_trailing_name_var,
        ).pack(side=tk.LEFT, padx=(8, 0))

        self.unpack_zip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="Unpack ZIP after download",
            variable=self.unpack_zip_var,
        ).pack(side=tk.LEFT, padx=(14, 0))

        self.overwrite_existing_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options_frame,
            text="Overwrite existing files (off = skip)",
            variable=self.overwrite_existing_var,
        ).pack(side=tk.LEFT, padx=(14, 0))

        control_frame = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        control_frame.pack(fill=tk.X)
        ttk.Button(control_frame, text="Load Pasted Table", command=self.load_pasted_table).pack(side=tk.LEFT)
        self.start_button = ttk.Button(control_frame, text="Start Downloads", command=self.start_downloads)
        self.start_button.pack(side=tk.LEFT, padx=8)
        self.stop_button = ttk.Button(control_frame, text="Stop", command=self.stop_downloads, state="disabled")
        self.stop_button.pack(side=tk.LEFT)
        ttk.Button(control_frame, text="Clear", command=self.clear_all).pack(side=tk.LEFT)

        progress_frame = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        progress_frame.pack(fill=tk.X)
        self.progress_var = tk.StringVar(value="Progress: 0 / 0")
        ttk.Label(progress_frame, textvariable=self.progress_var).pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate")
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0))

        table_frame = ttk.Frame(self.root, padding=(10, 6, 10, 6))
        table_frame.pack(fill=tk.BOTH, expand=True)

        paste_frame = ttk.LabelFrame(table_frame, text="Paste Agile Table Data Here")
        paste_frame.pack(fill=tk.X)
        self.paste_text = tk.Text(paste_frame, height=8, wrap="none")
        self.paste_text.pack(fill=tk.BOTH, expand=True)
        paste_hsb = ttk.Scrollbar(paste_frame, orient="horizontal", command=self.paste_text.xview)
        paste_hsb.pack(fill=tk.X)
        self.paste_text.configure(xscrollcommand=paste_hsb.set)

        trees_frame = ttk.Frame(table_frame)
        trees_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        left_frame = ttk.LabelFrame(trees_frame, text="Input")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_frame = ttk.LabelFrame(trees_frame, text="Output")
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        self.input_tree = ttk.Treeview(
            left_frame,
            columns=("row", "doc_id", "direct_link"),
            show="headings",
            selectmode="browse",
            height=18,
        )
        self.input_tree.heading("row", text="#")
        self.input_tree.heading("doc_id", text="DocID Intern")
        self.input_tree.heading("direct_link", text="Direct link")
        self.input_tree.column("row", width=50, anchor=tk.CENTER, stretch=False)
        self.input_tree.column("doc_id", width=140, anchor=tk.W, stretch=False)
        self.input_tree.column("direct_link", width=600, anchor=tk.W)
        self.input_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        input_hsb = ttk.Scrollbar(left_frame, orient="horizontal", command=self.input_tree.xview)
        input_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.input_tree.configure(xscrollcommand=input_hsb.set)

        self.output_text = tk.Text(right_frame, wrap="none", height=18)
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        output_vsb = ttk.Scrollbar(right_frame, orient="vertical", command=self.output_text.yview)
        output_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        output_hsb = ttk.Scrollbar(right_frame, orient="horizontal", command=self.output_text.xview)
        output_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.output_text.configure(yscrollcommand=output_vsb.set, xscrollcommand=output_hsb.set)

        self.output_text.insert("1.0", "Row\tStatus\tDetails\n")
        self.output_text.configure(state="disabled")

        input_vsb = ttk.Scrollbar(left_frame, orient="vertical", command=self.input_tree.yview)
        input_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.input_tree.configure(yscrollcommand=input_vsb.set)

        summary_frame = ttk.Frame(self.root, padding=(10, 6, 10, 10))
        summary_frame.pack(fill=tk.X)

        self.succeeded_var = tk.StringVar(value="Succeeded: 0")
        self.failed_var = tk.StringVar(value="Failed: 0")
        ttk.Label(summary_frame, textvariable=self.succeeded_var).pack(side=tk.LEFT)
        ttk.Label(summary_frame, textvariable=self.failed_var).pack(side=tk.LEFT, padx=(20, 0))

    def _choose_output_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get())
        if selected:
            self.output_var.set(selected)

    def _append_output_line(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.insert(tk.END, text + "\n")
        self.output_text.see(tk.END)
        self.output_text.configure(state="disabled")

    def clear_all(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Download in progress", "Wait for downloads to finish before clearing.")
            return

        self.rows = []
        self.paste_text.delete("1.0", tk.END)
        for item in self.input_tree.get_children():
            self.input_tree.delete(item)
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", "Row\tStatus\tDetails\n")
        self.output_text.configure(state="disabled")

        self.progress_bar["value"] = 0
        self.progress_bar["maximum"] = 0
        self.progress_var.set("Progress: 0 / 0")
        self.succeeded_var.set("Succeeded: 0")
        self.failed_var.set("Failed: 0")

    def load_pasted_table(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Download in progress", "Wait for downloads to finish before loading new data.")
            return

        raw_text = self.paste_text.get("1.0", tk.END)
        try:
            self.rows = parse_pasted_table(raw_text)
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        for item in self.input_tree.get_children():
            self.input_tree.delete(item)
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", "Row\tStatus\tDetails\n")
        self.output_text.configure(state="disabled")

        for row in self.rows:
            self.input_tree.insert(
                "",
                tk.END,
                iid=str(row.row_no),
                values=(row.row_no, row.doc_id, row.direct_link),
            )
            self._append_output_line(f"{row.row_no}\t[PENDING]\tPending")

        total = len(self.rows)
        self.progress_bar["value"] = 0
        self.progress_bar["maximum"] = total
        self.progress_var.set(f"Progress: 0 / {total}")
        self.succeeded_var.set("Succeeded: 0")
        self.failed_var.set("Failed: 0")

    def start_downloads(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Already running", "Downloads are already in progress.")
            return

        if not self.rows:
            messagebox.showwarning("No rows", "Paste and load table data first.")
            return

        try:
            delay_seconds = float(self.delay_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid delay", "Delay must be a number.")
            return

        if delay_seconds < 1.0:
            messagebox.showerror("Invalid delay", "Delay must be at least 1 second.")
            return

        output_dir = Path(self.output_var.get()).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        self.stop_requested.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker_thread = threading.Thread(
            target=self._download_worker,
            args=(
                self.rows,
                output_dir,
                delay_seconds,
                self._current_filename_options(),
                self.unpack_zip_var.get(),
                self.overwrite_existing_var.get(),
            ),
            daemon=True,
        )
        self.worker_thread.start()

    def _current_filename_options(self) -> FilenameOptions:
        return FilenameOptions(
            include_leading_doc_id=self.include_leading_doc_id_var.get(),
            include_document_number=self.include_document_number_var.get(),
            include_version=self.include_version_var.get(),
            include_trailing_name=self.include_trailing_name_var.get(),
        )

    def stop_downloads(self) -> None:
        if not self.worker_thread or not self.worker_thread.is_alive():
            messagebox.showinfo("Not running", "No active download run to stop.")
            return

        self.stop_requested.set()
        self.stop_button.configure(state="disabled")

    def _download_worker(
        self,
        rows: list[DocumentRow],
        output_dir: Path,
        delay_seconds: float,
        filename_options: FilenameOptions,
        unpack_zip_files: bool,
        overwrite_existing_files: bool,
    ) -> None:
        success_count = 0
        failure_count = 0
        total = len(rows)
        cancelled = False

        for index, row in enumerate(rows, start=1):
            if self.stop_requested.is_set():
                cancelled = True
                break

            status = "[PASSED]"
            details = ""

            try:
                saved_path, no_document_found, was_skipped, was_overwritten = download_file(
                    row.direct_link,
                    output_dir,
                    row,
                    filename_options,
                    overwrite_existing_files,
                )
                if no_document_found:
                    status = "[FAILED]"
                    details = "No documents found in container"
                    failure_count += 1
                elif was_skipped:
                    status = "[SKIPPED]"
                    details = f"File exists; skipped: {saved_path}"
                    success_count += 1
                else:
                    status = "[OVERWRITTEN]" if was_overwritten else "[PASSED]"
                    details = str(saved_path)
                    if unpack_zip_files and saved_path and saved_path.suffix.lower() == ".zip":
                        extracted_to = unpack_zip_to_named_subfolder(saved_path)
                        details = f"{saved_path} (unpacked to {extracted_to})"
                    success_count += 1
            except urllib.error.HTTPError as exc:
                status = "[FAILED]"
                details = f"HTTP {exc.code}: {exc.reason}"
                failure_count += 1
            except urllib.error.URLError as exc:
                status = "[FAILED]"
                details = f"Network error: {exc.reason}"
                failure_count += 1
            except Exception as exc:
                status = "[FAILED]"
                details = str(exc)
                failure_count += 1

            self.result_queue.put(
                (
                    "row_result",
                    row.row_no,
                    status,
                    details,
                    index,
                    total,
                    success_count,
                    failure_count,
                )
            )

            if index < total:
                sleep_step = 0.1
                elapsed = 0.0
                while elapsed < delay_seconds:
                    if self.stop_requested.is_set():
                        cancelled = True
                        break
                    time.sleep(sleep_step)
                    elapsed += sleep_step

                if cancelled:
                    break

        self.result_queue.put(("done", cancelled))

    def _process_queue(self) -> None:
        try:
            while True:
                item = self.result_queue.get_nowait()
                event_type = item[0]

                if event_type == "row_result":
                    _, row_no, status, details, index, total, succeeded, failed = item
                    self._append_output_line(f"{row_no}\t{status}\t{details}")
                    self.progress_bar["maximum"] = total
                    self.progress_bar["value"] = index
                    self.progress_var.set(f"Progress: {index} / {total}")
                    self.succeeded_var.set(f"Succeeded: {succeeded}")
                    self.failed_var.set(f"Failed: {failed}")
                elif event_type == "done":
                    _, cancelled = item
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    if cancelled:
                        messagebox.showinfo("Stopped", "Download run was stopped.")
                    else:
                        messagebox.showinfo("Completed", "Download run finished.")
        except queue.Empty:
            pass

        self.root.after(150, self._process_queue)


def main() -> None:
    root = tk.Tk()
    DownloaderGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
