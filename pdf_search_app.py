"""
PDF Search Application
======================
A Flask-based web UI for searching text within PDF files.

Requirements:
    pip install flask PyMuPDF

Usage:
    python pdf_search_app.py
    Then open http://localhost:5000 in your browser.
"""

import os
import re
import json
import threading
import uuid
import queue
import bisect
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, send_file, Response

try:
    import fitz  # PyMuPDF
except ImportError:
    raise SystemExit("PyMuPDF not found. Run:  pip install PyMuPDF")

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

BASE_DIR = Path(__file__).parent.resolve()
DEFAULT_SEARCH_DIR = str(BASE_DIR / "downloads")

# Active search sessions:  sid -> Queue
_sessions: dict[str, queue.Queue] = {}
_sessions_lock = threading.Lock()

CONTEXT_CHARS = 300  # characters of surrounding context shown per match
MAX_LINE_CONTEXT = 8  # max lines before/after sent for limited context view


# ── PDF helpers ────────────────────────────────────────────────────────────────
def get_snippets(page_text: str, term: str, full_word: bool, case_sensitive: bool) -> list[dict]:
  """Return all occurrence snippets for *term* in *page_text* with selected options."""
  # Keep exact line boundaries so slider=0 always maps to only the hit line.
  lines = page_text.splitlines()
  if not lines:
    lines = [""]

  # Light whitespace normalization for matching readability, without changing lines.
  lines = [re.sub(r"[ \t]+", " ", ln) for ln in lines]
  text = "\n".join(lines)

  pattern_str = re.escape(term)
  if full_word:
    pattern_str = r"(?<!\w)" + pattern_str + r"(?!\w)"
  flags = 0 if case_sensitive else re.IGNORECASE
  pattern = re.compile(pattern_str, flags)

  line_starts = []
  pos = 0
  for ln in lines:
    line_starts.append(pos)
    pos += len(ln) + 1  # +1 for newline in joined text

  results = []
  for m in pattern.finditer(text):
    s = max(0, m.start() - CONTEXT_CHARS)
    e = min(len(text), m.end() + CONTEXT_CHARS)

    line_idx = bisect.bisect_right(line_starts, m.start()) - 1
    line_idx = max(0, min(line_idx, max(len(lines) - 1, 0)))
    line_from = max(0, line_idx - MAX_LINE_CONTEXT)
    line_to = min(len(lines), line_idx + MAX_LINE_CONTEXT + 1)

    results.append({
      "before": ("..." if s > 0 else "") + text[s:m.start()].lstrip("\n"),
      "found": text[m.start():m.end()],
      "after": text[m.end():e].rstrip("\n") + ("..." if e < len(text) else ""),
      "line_context_lines": lines[line_from:line_to],
      "line_context_anchor": line_idx - line_from,
    })
  return results


def search_worker(search_dir: str, terms: list[str], q: queue.Queue, full_word: bool, case_sensitive: bool) -> None:
  """Background thread: walk *search_dir*, search every PDF, push results to *q*."""
  pdf_files = [
    os.path.join(root, f)
    for root, _, files in os.walk(search_dir)
    for f in files
    if f.lower().endswith(".pdf")
  ]

  total = len(pdf_files)
  q.put(("total", total))

  found_docs = 0
  for idx, pdf_path in enumerate(pdf_files):
    name = os.path.basename(pdf_path)
    q.put(("progress", {"n": idx + 1, "total": total, "name": name}))

    try:
      doc = fitz.open(pdf_path)
      hits: list[dict] = []
      for page_idx in range(len(doc)):
        page_text = doc[page_idx].get_text()
        for term in terms:
          for s in get_snippets(page_text, term, full_word, case_sensitive):
            hits.append({
              "page": page_idx + 1,
              "term": term,
              "before": s["before"],
              "found": s["found"],
              "after": s["after"],
              "line_context_lines": s["line_context_lines"],
              "line_context_anchor": s["line_context_anchor"],
            })
      doc.close()
      if hits:
        found_docs += 1
        q.put(("result", {
          "name": name,
          "path": pdf_path,
          "count": len(hits),
          "hits": hits,
        }))
    except Exception as exc:
      q.put(("err", {"name": name, "msg": str(exc)}))

  q.put(("done", {"pdfs": total, "found": found_docs}))


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    safe_dir = DEFAULT_SEARCH_DIR.replace("\\", "\\\\")
    return render_template_string(HTML_TEMPLATE, default_dir=safe_dir)


@app.route("/browse")
def browse():
    """Open a native directory-picker dialog and return the chosen path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(
            initialdir=DEFAULT_SEARCH_DIR,
            title="Select directory to search",
        )
        root.destroy()
        return jsonify({"path": folder or ""})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/search", methods=["POST"])
def search_start():
    """Start a background search and return a session ID."""
    body  = request.get_json(force=True)
    sdir  = (body.get("dir") or DEFAULT_SEARCH_DIR).strip()
    terms = [t.strip() for t in (body.get("terms", "")).splitlines() if t.strip()]
    opts = body.get("options", {})
    full_word = bool(opts.get("full_word", False))
    case_sensitive = bool(opts.get("case_sensitive", False))

    if not terms:
        return jsonify({"error": "Enter at least one search term."}), 400
    if not os.path.isdir(sdir):
        return jsonify({"error": f"Directory not found: {sdir}"}), 400

    sid: str = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    with _sessions_lock:
        _sessions[sid] = q

    threading.Thread(
      target=search_worker,
      args=(sdir, terms, q, full_word, case_sensitive),
      daemon=True,
    ).start()
    return jsonify({"sid": sid})


@app.route("/stream/<sid>")
def stream(sid: str):
    """Server-Sent Events stream for live search progress and results."""
    with _sessions_lock:
        q = _sessions.get(sid)
    if q is None:
        return "Session not found", 404

    def generate():
        try:
            while True:
                try:
                    msg_type, payload = q.get(timeout=90)
                except queue.Empty:
                    yield 'data: {"t":"hb"}\n\n'
                    continue

                yield f"data: {json.dumps({'t': msg_type, 'd': payload})}\n\n"

                if msg_type == "done":
                    with _sessions_lock:
                        _sessions.pop(sid, None)
                    break
        except GeneratorExit:
            with _sessions_lock:
                _sessions.pop(sid, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/pdf")
def serve_pdf():
    """Serve a PDF file for inline viewing."""
    path = request.args.get("path", "")
    abs_path = os.path.abspath(path)
    if not (os.path.isfile(abs_path) and abs_path.lower().endswith(".pdf")):
        return "File not found", 404
    return send_file(abs_path, mimetype="application/pdf")


# ── HTML Template ──────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta charset="UTF-8">
  <title>PDF Search</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:         #f1f5f9;
      --surface:    #ffffff;
      --border:     #e2e8f0;
      --text:       #0f172a;
      --muted:      #64748b;
      --primary:    #2563eb;
      --primary-dk: #1d4ed8;
      --hi-bg:      #fef08a;
      --hi-border:  #ca8a04;
      --hi-text:    #713f12;
      --danger:     #ef4444;
      --radius:     10px;
      --shadow:     0 1px 3px rgba(0,0,0,.1), 0 1px 2px rgba(0,0,0,.06);
    }

    html, body {
      height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.5;
    }

    /* ── Layout ── */
    #app { display: flex; height: 100vh; overflow: hidden; }

    /* ── Sidebar ── */
    #panel {
      width: 330px; min-width: 330px;
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex; flex-direction: column;
      padding: 20px 18px;
      overflow-y: auto;
      gap: 16px;
    }

    .panel-header { display: flex; align-items: center; gap: 10px; }
    .panel-header svg { color: var(--primary); flex-shrink: 0; }
    .panel-header h1 { font-size: 1.15rem; font-weight: 700; }

    .form-group { display: flex; flex-direction: column; gap: 6px; }
    label { font-weight: 600; font-size: 13px; }
    label .hint { font-weight: 400; color: var(--muted); margin-left: 5px; }

    input[type="text"], textarea {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 11px;
      font-size: 13px;
      font-family: inherit;
      color: var(--text);
      background: var(--bg);
      outline: none;
      transition: border-color .2s, background .2s;
    }
    input[type="text"]:focus, textarea:focus {
      border-color: var(--primary);
      background: #fff;
    }

    .dir-row { display: flex; gap: 6px; }
    .dir-row input { flex: 1; min-width: 0; }

    textarea { resize: vertical; min-height: 130px; }

    .checkbox-row {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 2px;
    }

    .checkbox-opt {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--text);
      font-weight: 500;
    }

    .checkbox-opt input[type="checkbox"] {
      width: 14px;
      height: 14px;
    }

    button {
      padding: 9px 15px;
      border-radius: 8px;
      border: none;
      cursor: pointer;
      font-size: 13px;
      font-weight: 600;
      font-family: inherit;
      transition: background .15s, transform .1s;
    }
    button:active:not(:disabled) { transform: scale(.98); }

    .btn-primary { background: var(--primary); color: #fff; width: 100%; }
    .btn-primary:hover:not(:disabled) { background: var(--primary-dk); }
    .btn-primary:disabled { background: #93c5fd; cursor: not-allowed; }

    .btn-outline {
      background: transparent; color: var(--text);
      border: 1px solid var(--border); white-space: nowrap;
    }
    .btn-outline:hover { background: var(--bg); }

    .btn-cancel {
      background: #fee2e2; color: var(--danger);
      border: 1px solid #fecaca; width: 100%;
    }
    .btn-cancel:hover { background: #fecaca; }

    /* ── Progress ── */
    #progress { display: flex; flex-direction: column; gap: 6px; }
    .prog-track {
      height: 6px; background: var(--border);
      border-radius: 99px; overflow: hidden;
    }
    .prog-fill {
      height: 100%; background: var(--primary);
      border-radius: 99px; width: 0%;
      transition: width .35s ease;
    }
    .prog-text { font-size: 12px; color: var(--muted); }

    /* ── Main / Results ── */
    #main {
      flex: 1; overflow-y: auto;
      padding: 24px 20px;
      display: flex; flex-direction: column; gap: 14px;
    }

    /* Placeholder */
    #placeholder {
      flex: 1; display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 14px; color: var(--muted); text-align: center; padding: 40px;
    }
    #placeholder .ph-icon { font-size: 52px; opacity: .45; }
    #placeholder p { max-width: 340px; line-height: 1.75; }

    /* Summary bar */
    #summary {
      font-size: 13px; color: var(--muted);
      padding: 9px 14px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
    }
    #summary strong { color: var(--text); }

    #lineContextControl {
      display: none;
      font-size: 12px;
      color: var(--muted);
      padding: 9px 14px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    #lineContextControl input[type="range"] {
      width: 220px;
      max-width: 100%;
    }

    /* ── Result card ── */
    .result-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .result-header {
      padding: 13px 15px;
      display: flex; align-items: center; gap: 10px;
      cursor: pointer; user-select: none;
      transition: background .15s;
    }
    .result-header:hover { background: #f8fafc; }

    .pdf-icon { font-size: 22px; flex-shrink: 0; }

    .result-info { flex: 1; min-width: 0; }
    .result-name {
      font-weight: 600; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis;
    }
    .result-name a {
      color: var(--text); text-decoration: none;
    }
    .result-name a:hover { color: var(--primary); text-decoration: underline; }
    .result-path {
      font-size: 11px; color: var(--muted); margin-top: 2px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }

    .badge {
      background: var(--primary); color: #fff;
      font-size: 11px; font-weight: 700;
      padding: 3px 9px; border-radius: 99px; flex-shrink: 0;
    }

    .chevron {
      color: var(--muted); font-size: 11px;
      transition: transform .2s; flex-shrink: 0;
    }
    .collapsed .chevron { transform: rotate(-90deg); }

    /* Collapse body */
    .result-body { border-top: 1px solid var(--border); }
    .collapsed .result-body { display: none; }

    /* ── Occurrence ── */
    .occurrence {
      padding: 11px 15px;
      border-bottom: 1px solid var(--border);
    }
    .occurrence:last-child { border-bottom: none; }

    .occ-meta {
      display: flex; align-items: center; gap: 8px;
      margin-bottom: 8px; flex-wrap: wrap;
    }
    .occ-page { font-weight: 600; font-size: 12px; color: var(--primary); }
    .occ-page a { color: inherit; text-decoration: none; }
    .occ-page a:hover { text-decoration: underline; }

    .occ-term {
      background: #dbeafe; color: var(--primary);
      font-size: 11px; font-weight: 600;
      padding: 2px 7px; border-radius: 5px;
    }

    .snippet {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 12px;
      font-family: 'Segoe UI', sans-serif;
      font-size: 13px;
      line-height: 1.75;
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-word;
    }

    mark {
      background: var(--hi-bg);
      color: var(--hi-text);
      border-radius: 3px;
      padding: 0 2px;
      font-weight: 700;
      border-bottom: 2px solid var(--hi-border);
    }

    /* ── Error card ── */
    .err-item {
      padding: 10px 15px;
      background: #fef2f2;
      border-left: 3px solid var(--danger);
      font-size: 12px; color: #b91c1c;
    }

    /* ── Scrollbar ── */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 99px; }
    ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

    /* ── Responsive ── */
    @media (max-width: 620px) {
      #app  { flex-direction: column; height: auto; overflow: visible; }
      #panel { width: 100%; min-width: unset; border-right: none; border-bottom: 1px solid var(--border); }
      #main { overflow-y: visible; }
    }
  </style>
</head>
<body>

<div id="app" data-default="{{ default_dir }}">

  <!-- ── Sidebar ── -->
  <aside id="panel">
    <div class="panel-header">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
        <line x1="16" y1="13" x2="8" y2="13"/>
        <line x1="16" y1="17" x2="8" y2="17"/>
        <polyline points="10 9 9 9 8 9"/>
      </svg>
      <h1>PDF Search</h1>
    </div>

    <div class="form-group">
      <label for="dirInput">Search Directory</label>
      <div class="dir-row">
        <input type="text" id="dirInput" placeholder="Path to search directory…">
        <button class="btn-outline" id="browseBtn" onclick="browseDir()">Browse</button>
      </div>
    </div>

    <div class="form-group">
      <label for="termsInput">
        Search Terms
        <span class="hint">one per line</span>
      </label>
      <textarea id="termsInput" rows="9"
        placeholder="Enter search terms, one per line&#10;&#10;Examples:&#10;hearing aid&#10;signal processing&#10;firmware update"></textarea>
      <div class="checkbox-row">
        <label class="checkbox-opt">
          <input type="checkbox" id="fullWordInput">
          Only full word/phrase
        </label>
        <label class="checkbox-opt">
          <input type="checkbox" id="caseSensitiveInput">
          Case sensitive
        </label>
      </div>
    </div>

    <button class="btn-primary" id="searchBtn" onclick="startSearch()">Search PDFs</button>
    <button class="btn-cancel"  id="cancelBtn" onclick="cancelSearch()" style="display:none">Cancel</button>

    <div id="progress" style="display:none">
      <div class="prog-track"><div class="prog-fill" id="progFill"></div></div>
      <div class="prog-text"  id="progText">Preparing…</div>
    </div>
  </aside>

  <!-- ── Main ── -->
  <main id="main">
    <div id="placeholder">
      <div class="ph-icon">🔍</div>
      <p>Select a directory and enter search terms, then click <strong>Search PDFs</strong>.</p>
    </div>
    <div id="summary" style="display:none"></div>
    <div id="lineContextControl">
      <span>Line context:</span>
      <input type="range" id="lineContextSlider" min="0" max="8" value="0">
      <span><strong id="lineContextValue">0</strong> line(s) before/after</span>
      <label class="checkbox-opt" style="margin-left:14px">
        <input type="checkbox" id="limitedOnlyInput" checked>
        Show only limited context
      </label>
    </div>
    <div id="resultsList"></div>
  </main>

</div><!-- #app -->

<script>
  // ── Init ────────────────────────────────────────────────────────────────────
  const defaultDir = document.getElementById('app').dataset.default;
  document.getElementById('dirInput').value = defaultDir;

  let evtSource = null;
  let activeSearchOptions = { full_word: false, case_sensitive: false };

  // ── Helpers ─────────────────────────────────────────────────────────────────
  function esc(str) {
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(String(str)));
    return d.innerHTML;
  }

  function setProgress(pct) {
    document.getElementById('progFill').style.width = pct + '%';
  }

  function setSearching(active) {
    const btn = document.getElementById('searchBtn');
    btn.disabled = active;
    btn.textContent = active ? 'Searching…' : 'Search PDFs';
    document.getElementById('cancelBtn').style.display = active ? '' : 'none';
    document.getElementById('progress').style.display = '';
  }

  function buildHighlightPattern(term, options) {
    const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = options.full_word ? '(?<!\\w)' + escaped + '(?!\\w)' : escaped;
    return new RegExp(pattern, options.case_sensitive ? 'g' : 'gi');
  }

  function renderLineSnippet(container, lines, anchor, term, options, radius) {
    const from = Math.max(0, anchor - radius);
    const to = Math.min(lines.length, anchor + radius + 1);
    const snippetLines = lines.slice(from, to);
    const rawText = snippetLines.join('\n');
    const escapedText = esc(rawText);
    const pattern = buildHighlightPattern(term, options);
    container.innerHTML = escapedText.replace(pattern, (m) => '<mark>' + m + '</mark>');
  }

  function refreshLimitedContext() {
    const slider = document.getElementById('lineContextSlider');
    const radius = Number(slider.value);
    document.getElementById('lineContextValue').textContent = String(radius);

    document.querySelectorAll('.limited-snippet').forEach((el) => {
      const lines = JSON.parse(el.dataset.lines || '[]');
      const anchor = Number(el.dataset.anchor || '0');
      const term = el.dataset.term || '';
      renderLineSnippet(el, lines, anchor, term, activeSearchOptions, radius);
    });
  }

  // ── Browse ──────────────────────────────────────────────────────────────────
  async function browseDir() {
    const btn = document.getElementById('browseBtn');
    btn.disabled = true; btn.textContent = '…';
    try {
      const r = await fetch('/browse');
      const d = await r.json();
      if (d.path) document.getElementById('dirInput').value = d.path;
      if (d.error) alert('Browse error: ' + d.error);
    } catch (e) {
      alert('Browse failed: ' + e);
    } finally {
      btn.disabled = false; btn.textContent = 'Browse';
    }
  }

  // ── Start search ────────────────────────────────────────────────────────────
  async function startSearch() {
    const dir   = document.getElementById('dirInput').value.trim();
    const terms = document.getElementById('termsInput').value.trim();
    const fullWord = document.getElementById('fullWordInput').checked;
    const caseSensitive = document.getElementById('caseSensitiveInput').checked;

    if (!dir)   { alert('Enter a directory path.');        return; }
    if (!terms) { alert('Enter at least one search term.'); return; }

    // Reset UI
    document.getElementById('placeholder').style.display = 'none';
    document.getElementById('summary').style.display = 'none';
    document.getElementById('lineContextControl').style.display = 'none';
    document.getElementById('resultsList').innerHTML = '';
    document.getElementById('lineContextSlider').value = '0';
    document.getElementById('lineContextValue').textContent = '0';
    setProgress(0);
    document.getElementById('progText').textContent = 'Starting…';
    setSearching(true);
    activeSearchOptions = { full_word: fullWord, case_sensitive: caseSensitive };

    try {
      const resp = await fetch('/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          dir,
          terms,
          options: {
            full_word: fullWord,
            case_sensitive: caseSensitive,
          },
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error || 'Search failed.');
        setSearching(false);
        return;
      }
      openStream(data.sid);
    } catch (e) {
      alert('Error starting search: ' + e);
      setSearching(false);
    }
  }

  // ── SSE stream ──────────────────────────────────────────────────────────────
  function openStream(sid) {
    if (evtSource) evtSource.close();
    evtSource = new EventSource('/stream/' + sid);

    let totalPdfs = 0;

    evtSource.onmessage = (evt) => {
      const { t, d } = JSON.parse(evt.data);

      if (t === 'hb') {
        return; // heartbeat – ignore
      } else if (t === 'total') {
        totalPdfs = d;
        document.getElementById('progText').textContent =
          'Searching 0 of ' + d + ' file' + (d !== 1 ? 's' : '') + '…';
      } else if (t === 'progress') {
        const pct = Math.round((d.n / d.total) * 100);
        setProgress(pct);
        document.getElementById('progText').textContent =
          'Searching ' + d.n + ' of ' + d.total + ': ' + d.name;
      } else if (t === 'result') {
        addResultCard(d);
        document.getElementById('lineContextControl').style.display = 'flex';
      } else if (t === 'err') {
        addErrorCard(d);
      } else if (t === 'done') {
        evtSource.close(); evtSource = null;
        setSearching(false);
        setProgress(100);
        document.getElementById('progText').textContent =
          'Done — searched ' + d.pdfs + ' file' + (d.pdfs !== 1 ? 's' : '') +
          ', found matches in ' + d.found + ' document' + (d.found !== 1 ? 's' : '') + '.';
        showSummary(d.pdfs, d.found);
        if (d.found === 0) {
          document.getElementById('placeholder').style.display = '';
          document.getElementById('placeholder').querySelector('p').innerHTML =
            'No matches found. Try different search terms or directory.';
        }
      }
    };

    evtSource.onerror = () => {
      if (evtSource) { evtSource.close(); evtSource = null; }
      setSearching(false);
    };
  }

  // ── Cancel ──────────────────────────────────────────────────────────────────
  function cancelSearch() {
    if (evtSource) { evtSource.close(); evtSource = null; }
    setSearching(false);
    document.getElementById('progText').textContent = 'Cancelled.';
  }

  // ── Summary bar ─────────────────────────────────────────────────────────────
  function showSummary(pdfs, found) {
    const el = document.getElementById('summary');
    el.innerHTML =
      'Searched <strong>' + pdfs  + '</strong> PDF file'  + (pdfs  !== 1 ? 's' : '') +
      ' &nbsp;·&nbsp; Found matches in <strong>' + found + '</strong> document' + (found !== 1 ? 's' : '');
    el.style.display = '';
  }

  // ── Result card ─────────────────────────────────────────────────────────────
  function addResultCard(data) {
    const { name, path, hits } = data;
    const encPath = encodeURIComponent(path);

    const card = document.createElement('div');
    card.className = 'result-card';

    // Header
    const header = document.createElement('div');
    header.className = 'result-header';
    header.innerHTML =
      '<span class="pdf-icon">📄</span>' +
      '<div class="result-info">' +
        '<div class="result-name">' +
          '<a href="/pdf?path=' + encPath + '" target="_blank" onclick="event.stopPropagation()">' +
          esc(name) + '</a>' +
        '</div>' +
        '<div class="result-path">' + esc(path) + '</div>' +
      '</div>' +
      '<span class="badge">' + hits.length + '</span>' +
      '<span class="chevron">▼</span>';

    header.addEventListener('click', () => card.classList.toggle('collapsed'));

    // Body: occurrences
    const body = document.createElement('div');
    body.className = 'result-body';

    hits.forEach((hit) => {
      const pdfUrl = '/pdf?path=' + encPath + '#page=' + hit.page;

      const occ = document.createElement('div');
      occ.className = 'occurrence';
      occ.innerHTML =
        '<div class="occ-meta">' +
          '<span class="occ-page"><a href="' + pdfUrl + '" target="_blank">Page ' + hit.page + '</a></span>' +
          '<span class="occ-term">' + esc(hit.term) + '</span>' +
        '</div>' +
        '<div class="snippet full-snippet">' +
          esc(hit.before) +
          '<mark>' + esc(hit.found) + '</mark>' +
          esc(hit.after) +
        '</div>';

      const limited = document.createElement('div');
      limited.className = 'snippet limited-snippet';
      limited.style.marginTop = '8px';
      limited.dataset.lines = JSON.stringify(hit.line_context_lines || []);
      limited.dataset.anchor = String(hit.line_context_anchor || 0);
      limited.dataset.term = hit.term || '';
      occ.appendChild(limited);
      renderLineSnippet(
        limited,
        hit.line_context_lines || [],
        Number(hit.line_context_anchor || 0),
        hit.term || '',
        activeSearchOptions,
        Number(document.getElementById('lineContextSlider').value || '0')
      );

      body.appendChild(occ);
    });

    card.appendChild(header);
    card.appendChild(body);

    document.getElementById('resultsList').appendChild(card);
    applyFullContextVisibility();
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  // ── Error card ──────────────────────────────────────────────────────────────
  function addErrorCard(data) {
    const card = document.createElement('div');
    card.className = 'result-card';
    card.innerHTML =
      '<div class="err-item">⚠ Could not read <strong>' +
      esc(data.name) + '</strong>: ' + esc(data.msg) + '</div>';
    document.getElementById('resultsList').appendChild(card);
  }

  document.getElementById('lineContextSlider').addEventListener('input', refreshLimitedContext);

  function applyFullContextVisibility() {
    const hide = document.getElementById('limitedOnlyInput').checked;
    document.querySelectorAll('.full-snippet').forEach((el) => {
      el.style.display = hide ? 'none' : '';
    });
  }

  document.getElementById('limitedOnlyInput').addEventListener('change', applyFullContextVisibility);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import webbrowser
    url = "http://localhost:5000"
    print(f"PDF Search App — {url}")
    print(f"Default search directory: {DEFAULT_SEARCH_DIR}")
    webbrowser.open(url)
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
