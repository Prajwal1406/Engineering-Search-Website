import os
import re
import csv
import time
import sqlite3
import subprocess
from io import StringIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template_string, request, jsonify, Response

DEFAULT_DB_FOLDER = Path(os.getenv("ENGINEERING_SEARCH_DB_FOLDER", Path(__file__).resolve().parent / "databases"))
MAX_RESULTS_PER_DATABASE = 1500
RESULTS_PER_PAGE = 50

app = Flask(__name__)

DB_MAPPING = {
    "Engineering_Search_V2_Area1.db": "Area 1",
    "Engineering_Search_V2_Area2.db": "Area 2",
    "Engineering_Search_V2_Area3.db": "Area 3",
    "Engineering_Search_V2_Area4.db": "Area 4",
}

def get_available_dbs() -> dict:
    available = {}
    if not DEFAULT_DB_FOLDER.exists():
        return available

    for path in DEFAULT_DB_FOLDER.glob("*.db"):
        if path.name in DB_MAPPING:
            available[path.name] = DB_MAPPING[path.name]
        else:
            clean_name = path.stem.replace("Engineering_Search_V2_", "").replace("_", " ")
            available[path.name] = clean_name

    return available

def friendly_name(path: Path) -> str:
    return DB_MAPPING.get(path.name, path.stem)

def open_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA query_only=ON")
    return con

def fts_query(raw: str) -> str:
    terms = [term.strip() for term in raw.split() if term.strip()]
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)

def search_one_database(path: Path, raw_query: str, filters: dict):
    con = open_readonly(path)
    try:
        terms = [term.strip() for term in raw_query.split() if term.strip()]
        if not terms:
            return []

        # Separate 3+ char terms (FTS) from 1-2 char terms (LIKE)
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]

        where_clauses = ["f.status = 'active'"]
        params = []

        # Choose logic based on Search Scope mode
        if filters.get("file_wide") and len(terms) > 1:
            # File-Wide Search Mode
            for t in terms:
                if len(t) >= 3:
                    where_clauses.append("""
                        f.id IN (
                            SELECT r_sub.file_id 
                            FROM rows_fts x_sub 
                            JOIN rows_data r_sub ON r_sub.id = x_sub.rowid 
                            WHERE x_sub.rows_fts MATCH ?
                        )
                    """)
                    params.append(f'"{t}"')
                else:
                    where_clauses.append("""
                        f.id IN (
                            SELECT r_sub.file_id 
                            FROM rows_data r_sub 
                            WHERE r_sub.row_text LIKE ?
                        )
                    """)
                    params.append(f"%{t}%")
        else:
            # Standard Same-Row Search Mode
            if long_terms:
                fts_str = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
                where_clauses.append("x.rows_fts MATCH ?")
                params.append(fts_str)

            for st in short_terms:
                where_clauses.append("r.row_text LIKE ?")
                params.append(f"%{st}%")

        if filters.get("hide_old"):
            where_clauses.append("f.is_old = 0")

        if filters.get("ext") and filters["ext"] != "all":
            where_clauses.append("f.extension = ?")
            params.append(filters["ext"])

        if filters.get("date_range") and filters["date_range"] != "all":
            now_sec = time.time()
            years_map = {"1y": 1, "3y": 3, "5y": 5}
            cutoff_sec = now_sec - (years_map[filters["date_range"]] * 365 * 86400)
            where_clauses.append("f.modified_ns >= ?")
            params.append(int(cutoff_sec * 1e9))

        doc_cat = filters.get("doc_cat")
        if doc_cat in ("parts_list", "bom"):
            where_clauses.append("(f.is_bom = 1 OR r.sheet_name LIKE '%部品%' OR r.sheet_name LIKE '%BOM%')")
        elif doc_cat == "cost":
            where_clauses.append("(f.file_name LIKE '%見積%' OR f.file_name LIKE '%原価%' OR r.sheet_name LIKE '%価格%')")
        elif doc_cat == "spec":
            where_clauses.append("(f.file_name LIKE '%仕様%' OR r.sheet_name LIKE '%IO%' OR r.sheet_name LIKE '%割付%' OR r.sheet_name LIKE '%回路%')")

        if filters.get("exclude"):
            where_clauses.append("r.row_text NOT LIKE ?")
            params.append(f"%{filters['exclude']}%")

        where_sql = " AND ".join(where_clauses)
        parts_list_sort = "f.is_bom DESC," if filters.get("prioritize_parts_list") else ""

        if long_terms:
            from_sql = "rows_fts x JOIN rows_data r ON r.id = x.rowid JOIN files f ON f.id = r.file_id"
        else:
            from_sql = "rows_data r JOIN files f ON f.id = r.file_id"

        sql = f"""
            SELECT f.file_name, r.sheet_name, r.row_number, r.row_text, f.is_bom, f.is_old, f.full_path, f.extension
            FROM {from_sql}
            WHERE {where_sql}
            ORDER BY {parts_list_sort} f.is_old ASC, f.modified_ns DESC
            LIMIT ?
        """
        params.append(MAX_RESULTS_PER_DATABASE)
        rows = con.execute(sql, params).fetchall()
        return [ (friendly_name(path),) + r for r in rows ]
    except Exception:
        return []
    finally:
        con.close()

def highlight_matches(text: str, query: str) -> str:
    terms = [re.escape(t.strip()) for t in query.split() if t.strip()]
    if not terms:
        return text
    pattern = re.compile(r'(' + '|'.join(terms) + r')', re.IGNORECASE)
    return pattern.sub(r'<mark>\1</mark>', text)

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <title>Engineering Search System V2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        mark { background-color: #fef08a; color: #854d0e; padding: 0 2px; border-radius: 2px; font-weight: 600; }
    </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen p-6 font-sans">
    <div class="max-w-[1600px] mx-auto space-y-6">
        
        <!-- Header -->
        <div class="flex items-center justify-between bg-slate-800 p-5 rounded-xl border border-slate-700 shadow-lg">
            <div>
                <h1 data-i18n="title" class="text-2xl font-bold tracking-tight text-cyan-400">⚡ Engineering Search Portal</h1>
                <p data-i18n="subtitle" class="text-xs text-slate-400 mt-1">Multi-Database Full-Text Index | Enterprise Search Engine</p>
            </div>
            
            <div class="flex items-center gap-4">
                {% if query %}
                <div class="bg-slate-900 px-4 py-2 rounded-lg border border-slate-700 text-right">
                    <span data-i18n="results_found" class="text-xs text-slate-400 block">Results Found</span>
                    <span class="text-xl font-bold text-emerald-400">{{ total_results }}</span>
                </div>
                <div class="bg-slate-900 px-4 py-2 rounded-lg border border-slate-700 text-right">
                    <span data-i18n="search_time" class="text-xs text-slate-400 block">Search Time</span>
                    <span class="text-xl font-bold text-cyan-400">{{ search_time }}s</span>
                </div>
                {% endif %}

                <button type="button" id="btn-lang-toggle" class="bg-slate-700 hover:bg-slate-600 text-cyan-300 font-semibold px-3 py-2 rounded-lg border border-slate-600 text-xs transition cursor-pointer">
                    🌐 日本語
                </button>
            </div>
        </div>

        <!-- Search Controls -->
        <form id="search-form" method="GET" class="bg-slate-800 p-5 rounded-xl border border-slate-700 shadow-lg space-y-4">
            <div class="flex gap-3">
                <input type="text" id="query-input" name="q" value="{{ query|e }}" 
                       data-i18n-placeholder="search_placeholder"
                       placeholder="Search part numbers, PLC codes, specs (e.g. QX80, PS21, 割付)..." 
                       class="flex-grow bg-slate-900 border border-slate-600 rounded-lg px-4 py-3 text-white focus:outline-none focus:border-cyan-500 font-mono" required>
                <button type="submit" data-i18n="search_btn" class="bg-cyan-600 hover:bg-cyan-500 text-white font-semibold px-8 py-3 rounded-lg shadow-md cursor-pointer">
                    Search
                </button>
            </div>

            <!-- Prominent Search Scope Toggle -->
            <div class="flex items-center gap-6 text-xs text-slate-300 font-semibold bg-slate-900/60 p-2.5 rounded-lg border border-slate-700/80">
                <span data-i18n="search_mode_title" class="text-slate-400">Search Scope:</span>
                <label class="flex items-center space-x-1.5 cursor-pointer hover:text-cyan-400 transition">
                    <input type="radio" name="file_wide" value="0" {% if not file_wide %}checked{% endif %} class="text-cyan-500 focus:ring-0">
                    <span data-i18n="mode_row">🎯 Match Same Row</span>
                </label>
                <label class="flex items-center space-x-1.5 cursor-pointer hover:text-cyan-400 transition">
                    <input type="radio" name="file_wide" value="1" {% if file_wide %}checked{% endif %} class="text-cyan-500 focus:ring-0">
                    <span data-i18n="mode_file">📁 Match Anywhere in File</span>
                </label>
            </div>

            <div id="short-term-warning" class="hidden bg-amber-900/40 border border-amber-600/60 text-amber-300 text-xs px-3 py-2 rounded-lg flex items-center justify-between">
                <span>⚠️ <strong data-i18n="warn_title">Short Search Term:</strong> <span data-i18n="warn_msg">⚡ For fastest results, enter 3 or more characters when searching. Searches with 1 or 2 characters are supported but may take significantly longer.</span></span>
            </div>

            <div class="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                <span data-i18n="quick_presets" class="font-semibold text-slate-300">Quick Presets:</span>
                <button type="button" data-preset="QX80" class="btn-preset bg-slate-700 hover:bg-slate-600 px-2 py-1 rounded text-slate-200 cursor-pointer">QX80</button>
                <button type="button" data-preset="PS21" class="btn-preset bg-slate-700 hover:bg-slate-600 px-2 py-1 rounded text-slate-200 cursor-pointer">PS21</button>
                <button type="button" data-preset="IO割付" class="btn-preset bg-slate-700 hover:bg-slate-600 px-2 py-1 rounded text-slate-200 cursor-pointer">IO割付</button>
                <button type="button" data-preset="部品表" class="btn-preset bg-slate-700 hover:bg-slate-600 px-2 py-1 rounded text-slate-200 cursor-pointer">部品表</button>

                <div id="recent-container" class="hidden items-center gap-2 ml-4 border-l border-slate-700 pl-4">
                    <span data-i18n="recent_searches" class="font-semibold text-slate-400">Recent:</span>
                    <div id="recent-list" class="flex gap-1.5 flex-wrap"></div>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-4 gap-4 pt-4 border-t border-slate-700 text-xs">
                <div>
                    <label data-i18n="target_dbs" class="font-semibold text-slate-300 block mb-1">Target Databases:</label>
                    <div class="space-y-1 bg-slate-900 p-2.5 rounded border border-slate-700">
                        {% if all_dbs %}
                            {% for db_filename, db_label in all_dbs.items() %}
                            <label class="flex items-center space-x-2 text-slate-300 cursor-pointer">
                                <input type="checkbox" name="dbs" value="{{ db_filename }}" {% if db_filename in selected_dbs %}checked{% endif %} class="rounded text-cyan-500">
                                <span>{{ db_label }}</span>
                            </label>
                            {% endfor %}
                        {% else %}
                            <div class="text-rose-400 text-xs font-semibold py-1">
                                ⚠️ No databases found on disk.
                            </div>
                        {% endif %}
                    </div>
                </div>

                <div>
                    <label data-i18n="doc_cat" class="font-semibold text-slate-300 block mb-1">Document Category:</label>
                    <select name="doc_cat" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-slate-200 mb-2">
                        <option value="all" data-i18n="cat_all" {% if doc_cat == 'all' %}selected{% endif %}>All Document Types</option>
                        <option value="parts_list" data-i18n="cat_parts_list" {% if doc_cat in ['parts_list', 'bom'] %}selected{% endif %}>📋 Parts Lists</option>
                        <option value="cost" data-i18n="cat_cost" {% if doc_cat == 'cost' %}selected{% endif %}>💰 Estimates & Cost</option>
                        <option value="spec" data-i18n="cat_spec" {% if doc_cat == 'spec' %}selected{% endif %}>⚡ PLC & Specifications</option>
                    </select>

                    <label data-i18n="file_ext" class="font-semibold text-slate-300 block mb-1">File Extension:</label>
                    <select name="ext" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-slate-200">
                        <option value="all" data-i18n="ext_all" {% if ext == 'all' %}selected{% endif %}>All File Types (.xlsx, .xlsm, .csv)</option>
                        <option value=".xlsm" {% if ext == '.xlsm' %}selected{% endif %}>.xlsm (Macro-Enabled)</option>
                        <option value=".xlsx" {% if ext == '.xlsx' %}selected{% endif %}>.xlsx (Standard Workbook)</option>
                        <option value=".csv" {% if ext == '.csv' %}selected{% endif %}>.csv / .tsv</option>
                    </select>
                </div>

                <div>
                    <label data-i18n="date_range" class="font-semibold text-slate-300 block mb-1">Project Date Range:</label>
                    <select name="date_range" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-slate-200 mb-2">
                        <option value="all" data-i18n="date_all" {% if date_range == 'all' %}selected{% endif %}>All Time</option>
                        <option value="1y" data-i18n="date_1y" {% if date_range == '1y' %}selected{% endif %}>Last 1 Year</option>
                        <option value="3y" data-i18n="date_3y" {% if date_range == '3y' %}selected{% endif %}>Last 3 Years</option>
                        <option value="5y" data-i18n="date_5y" {% if date_range == '5y' %}selected{% endif %}>Last 5 Years</option>
                    </select>

                    <label data-i18n="exclude_kw" class="font-semibold text-slate-300 block mb-1">Exclude Keyword:</label>
                    <input type="text" name="exclude" value="{{ exclude|e }}" placeholder="e.g. コピー, backup" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-slate-200">
                </div>

                <div class="space-y-3 bg-slate-900 p-3 rounded border border-slate-700">
                    <label data-i18n="flags_sorting" class="font-semibold text-slate-300 block border-b border-slate-700 pb-1">Flags & Sorting:</label>
                    <label class="flex items-center space-x-2 text-slate-300 cursor-pointer">
                        <input type="checkbox" name="parts_list" value="1" {% if prioritize_parts_list %}checked{% endif %} class="rounded text-cyan-500">
                        <span data-i18n="prioritize_parts_list">Prioritize Parts List / 部品表</span>
                    </label>
                    <label class="flex items-center space-x-2 text-slate-300 cursor-pointer">
                        <input type="checkbox" name="hide_old" value="1" {% if hide_old %}checked{% endif %} class="rounded text-cyan-500">
                        <span data-i18n="hide_old">Hide Old / 旧 Files</span>
                    </label>
                    
                    {% if query %}
                    <a href="/export?{{ raw_query_string }}" data-i18n="export_csv" class="block text-center bg-slate-800 hover:bg-slate-700 text-cyan-400 font-semibold py-1.5 rounded border border-slate-600 mt-2">
                        📥 Export Results to CSV
                    </a>
                    {% endif %}
                </div>
            </div>
        </form>

        <!-- Results Table -->
        {% if query %}
        <div class="bg-slate-800 rounded-xl border border-slate-700 shadow-lg overflow-hidden">
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse text-xs">
                    <thead>
                        <tr class="bg-slate-900/80 text-slate-300 border-b border-slate-700 uppercase tracking-wider">
                            <th data-i18n="col_area" class="p-3 w-32">Area</th>
                            <th data-i18n="col_filename" class="p-3 w-64">File Name</th>
                            <th data-i18n="col_sheet" class="p-3 w-32">Sheet</th>
                            <th data-i18n="col_row" class="p-3 w-16 text-center">Row</th>
                            <th data-i18n="col_content" class="p-3">Matching Content</th>
                            <th data-i18n="col_actions" class="p-3 w-60 text-center">Actions</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/50 font-mono">
                        {% for r in results %}
                        <tr class="hover:bg-slate-700/40 transition-colors">
                            <td class="p-3 text-cyan-300 font-sans font-medium whitespace-nowrap">{{ r.area }}</td>
                            <td class="p-3 text-slate-200 font-sans font-medium">
                                <div>{{ r.file_name }}</div>
                                <div class="flex gap-1 mt-1 font-mono text-[10px]">
                                    {% if r.is_bom %}<span class="bg-emerald-900/60 text-emerald-300 px-1 rounded">Parts List</span>{% endif %}
                                    {% if r.is_old %}<span class="bg-amber-900/60 text-amber-300 px-1 rounded">OLD</span>{% endif %}
                                    <span class="bg-slate-700 text-slate-300 px-1 rounded">{{ r.ext }}</span>
                                </div>
                            </td>
                            <td class="p-3 text-slate-300 font-sans whitespace-nowrap">{{ r.sheet_name }}</td>
                            <td class="p-3 text-slate-400 text-center font-bold">{{ r.row_num }}</td>
                            <td class="p-3 text-slate-300 break-all leading-relaxed text-[11px]">
                                {{ r.content|safe }}
                            </td>
                            <td class="p-3 text-center space-x-1 whitespace-nowrap font-sans">
                                <button type="button" data-path="{{ r.path|e }}" data-sheet="{{ r.sheet_name|e }}" data-row="{{ r.row_num }}" data-i18n="btn_context" class="btn-preview-context bg-cyan-700 hover:bg-cyan-600 text-white px-2 py-1 rounded font-medium shadow cursor-pointer">
                                    👁️ Context
                                </button>
                                <button type="button" data-path="{{ r.path|e }}" data-i18n="btn_open" class="btn-open-file bg-emerald-600 hover:bg-emerald-500 text-white px-2 py-1 rounded font-medium shadow cursor-pointer">
                                    Open
                                </button>
                                <button type="button" data-path="{{ r.path|e }}" data-i18n="btn_folder" class="btn-open-folder bg-amber-600 hover:bg-amber-500 text-white px-2 py-1 rounded font-medium shadow cursor-pointer">
                                    Folder
                                </button>
                                <button type="button" data-path="{{ r.path|e }}" data-i18n="btn_copy" class="btn-copy-path bg-slate-700 hover:bg-slate-600 text-slate-200 px-2 py-1 rounded font-medium cursor-pointer">
                                    Copy UNC
                                </button>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            {% if total_pages > 1 %}
            <div class="flex items-center justify-between bg-slate-900 px-5 py-3 border-t border-slate-700 text-xs">
                <span class="text-slate-400">
                    <span data-i18n="page_showing">Showing page</span> <strong class="text-cyan-400">{{ current_page }}</strong> <span data-i18n="page_of">of</span> <strong>{{ total_pages }}</strong>
                </span>
                <div class="flex gap-2">
                    {% if current_page > 1 %}
                    <a href="?{{ query_params }}&page={{ current_page - 1 }}" data-i18n="btn_prev" class="bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded">◀ Previous</a>
                    {% endif %}
                    {% if current_page < total_pages %}
                    <a href="?{{ query_params }}&page={{ current_page + 1 }}" data-i18n="btn_next" class="bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded">Next ▶</a>
                    {% endif %}
                </div>
            </div>
            {% endif %}

        </div>
        {% endif %}

    </div>

    <!-- Context Preview Modal -->
    <div id="context-modal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-slate-800 border border-slate-700 rounded-xl shadow-2xl max-w-4xl w-full p-5 space-y-4 max-h-[85vh] flex flex-col">
            <div class="flex justify-between items-center border-b border-slate-700 pb-3">
                <h3 class="font-bold text-cyan-400 text-sm flex items-center gap-2">
                    <span>📑</span> <span id="modal-file-title">Surrounding Row Context</span>
                </h3>
                <button type="button" id="modal-close" class="text-slate-400 hover:text-white font-bold text-lg cursor-pointer">✕</button>
            </div>
            
            <div id="modal-body" class="overflow-y-auto space-y-1 font-mono text-xs flex-grow bg-slate-900 p-3 rounded border border-slate-700">
            </div>
        </div>
    </div>

    <script>
    const translations = {
        en: {
            title: "⚡ Engineering Search Portal",
            subtitle: "Multi-Database Full-Text Index | Enterprise Search Engine",
            results_found: "Results Found",
            search_time: "Search Time",
            search_placeholder: "Search part numbers, PLC codes, specs (e.g. QX80, PS21, 割付)...",
            search_btn: "Search",
            search_mode_title: "Search Scope:",
            mode_row: "🎯 Match Same Row",
            mode_file: "📁 Match Anywhere in File",
            quick_presets: "Quick Presets:",
            recent_searches: "Recent:",
            target_dbs: "Target Databases:",
            doc_cat: "Document Category:",
            file_ext: "File Extension:",
            date_range: "Project Date Range:",
            exclude_kw: "Exclude Keyword:",
            flags_sorting: "Flags & Sorting:",
            prioritize_parts_list: "Prioritize Parts List / 部品表",
            hide_old: "Hide Old / 旧 Files",
            export_csv: "📥 Export Results to CSV",
            cat_all: "All Document Types",
            cat_parts_list: "📋 Parts Lists",
            cat_cost: "💰 Estimates & Cost",
            cat_spec: "⚡ PLC & Specifications",
            ext_all: "All File Types (.xlsx, .xlsm, .csv)",
            date_all: "All Time",
            date_1y: "Last 1 Year",
            date_3y: "Last 3 Years",
            date_5y: "Last 5 Years",
            col_area: "Area",
            col_filename: "File Name",
            col_sheet: "Sheet",
            col_row: "Row",
            col_content: "Matching Content",
            col_actions: "Actions",
            btn_context: "👁️ Context",
            btn_open: "Open",
            btn_folder: "Folder",
            btn_copy: "Copy UNC",
            page_showing: "Showing page",
            page_of: "of",
            btn_prev: "◀ Previous",
            btn_next: "Next ▶",
            warn_title: "Short Search Term:",
            warn_msg: "⚡ For fastest results, enter 3 or more characters when searching. Searches with 1 or 2 characters are supported but may take significantly longer.",
            lang_toggle_text: "🌐 日本語"
        },
        ja: {
            title: "⚡ 技術情報検索ポータル",
            subtitle: "マルチデータベース全文検索 | 企業向け検索エンジン",
            results_found: "該当件数",
            search_time: "検索時間",
            search_placeholder: "品番、PLCコード、仕様（例: QX80, PS21, 割付）を検索...",
            search_btn: "検索",
            search_mode_title: "検索範囲:",
            mode_row: "🎯 同一行内マッチ",
            mode_file: "📁 ファイル全体マッチ",
            quick_presets: "クイック検索:",
            recent_searches: "履歴:",
            target_dbs: "対象データベース:",
            doc_cat: "ドキュメント分類:",
            file_ext: "ファイル拡張子:",
            date_range: "更新期間:",
            exclude_kw: "除外キーワード:",
            flags_sorting: "フラグ・並び替え:",
            prioritize_parts_list: "パーツリスト / 部品表を優先",
            hide_old: "旧ファイルを非表示",
            export_csv: "📥 CSVで出力",
            cat_all: "すべてのドキュメント",
            cat_parts_list: "📋 パーツリスト・部品表",
            cat_cost: "💰 見積・原価関係",
            cat_spec: "⚡ PLC・仕様書関係",
            ext_all: "すべての拡張子 (.xlsx, .xlsm, .csv)",
            date_all: "全期間",
            date_1y: "過去1年",
            date_3y: "過去3年",
            date_5y: "過去5年",
            col_area: "領域",
            col_filename: "ファイル名",
            col_sheet: "シート名",
            col_row: "行",
            col_content: "一致内容",
            col_actions: "操作",
            btn_context: "👁️ 前後行確認",
            btn_open: "開く",
            btn_folder: "フォルダ",
            btn_copy: "パス複製",
            page_showing: "表示中:",
            page_of: "/",
            btn_prev: "◀ 前へ",
            btn_next: "次へ ▶",
            warn_title: "検索文字数が短すぎます:",
            warn_msg: "💡 検索を高速に行うため、なるべく 3文字以上 で検索してください。1～2文字での検索も可能ですが、検索時間が長くなる場合があります。",
            lang_toggle_text: "🌐 English"
        }
    };

    function copyToClipboardFallback(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        } else {
            return new Promise((resolve, reject) => {
                const textArea = document.createElement("textarea");
                textArea.value = text;
                textArea.style.position = "fixed";
                textArea.style.left = "-999999px";
                textArea.style.top = "-999999px";
                document.body.appendChild(textArea);
                textArea.focus();
                textArea.select();
                try {
                    const successful = document.execCommand('copy');
                    document.body.removeChild(textArea);
                    successful ? resolve() : reject();
                } catch (err) {
                    document.body.removeChild(textArea);
                    reject(err);
                }
            });
        }
    }

    function getSavedLang() {
        try {
            const l = localStorage.getItem('app_lang');
            return translations[l] ? l : 'en';
        } catch(e) { return 'en'; }
    }

    let currentLang = getSavedLang();

    function setLanguage(lang) {
        if (!translations[lang]) lang = 'en';
        currentLang = lang;
        try { localStorage.setItem('app_lang', lang); } catch(e) {}
        
        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            if (translations[lang] && translations[lang][key]) {
                el.textContent = translations[lang][key];
            }
        });

        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            const key = el.getAttribute('data-i18n-placeholder');
            if (translations[lang] && translations[lang][key]) {
                el.placeholder = translations[lang][key];
            }
        });

        const langBtn = document.getElementById('btn-lang-toggle');
        if (langBtn) langBtn.textContent = translations[lang].lang_toggle_text;
    }

    function initApp() {
        setLanguage(currentLang);

        const queryInput = document.getElementById('query-input');
        const warnBox = document.getElementById('short-term-warning');

        function checkQueryLength() {
            if (!queryInput || !warnBox) return;
            const terms = queryInput.value.trim().split(/\s+/).filter(Boolean);
            const hasShort = terms.some(t => t.length < 3);
            if (hasShort && terms.length > 0) {
                warnBox.classList.remove('hidden');
            } else {
                warnBox.classList.add('hidden');
            }
        }
        if (queryInput) {
            queryInput.addEventListener('input', checkQueryLength);
            checkQueryLength();
        }

        const rawQuery = {{ query|tojson|safe }};
        const currentQuery = typeof rawQuery === 'string' ? rawQuery.trim() : '';
        let recents = [];
        try {
            recents = JSON.parse(localStorage.getItem('recent_searches') || '[]');
            if (!Array.isArray(recents)) recents = [];
        } catch(e) { recents = []; }

        if (currentQuery) {
            recents = recents.filter(q => typeof q === 'string' && q.toLowerCase() !== currentQuery.toLowerCase());
            recents.unshift(currentQuery);
            if (recents.length > 5) recents.pop();
            try { localStorage.setItem('recent_searches', JSON.stringify(recents)); } catch(e) {}
        }

        const recentContainer = document.getElementById('recent-container');
        const recentList = document.getElementById('recent-list');
        if (recents.length > 0 && recentContainer && recentList) {
            recentContainer.classList.remove('hidden');
            recentContainer.classList.add('flex');
            recentList.innerHTML = '';
            recents.forEach(q => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.dataset.preset = q;
                chip.className = 'btn-preset bg-cyan-950/80 hover:bg-cyan-900 border border-cyan-700/60 text-cyan-300 px-2 py-0.5 rounded text-[11px] cursor-pointer';
                chip.textContent = q;
                recentList.appendChild(chip);
            });
        }
    }

    document.addEventListener('click', function (e) {
        const presetBtn = e.target.closest('.btn-preset');
        if (presetBtn) {
            e.preventDefault();
            const keyword = presetBtn.getAttribute('data-preset');
            const queryInput = document.getElementById('query-input');
            const searchForm = document.getElementById('search-form');
            if (queryInput && searchForm && keyword) {
                queryInput.value = keyword;
                searchForm.submit();
            }
            return;
        }

        const langToggleBtn = e.target.closest('#btn-lang-toggle');
        if (langToggleBtn) {
            e.preventDefault();
            setLanguage(currentLang === 'en' ? 'ja' : 'en');
            return;
        }

        // Direct 1-Click Open File on Client PC
        const openFileBtn = e.target.closest('.btn-open-file');
        if (openFileBtn) {
            e.preventDefault();
            const filePath = openFileBtn.getAttribute('data-path');
            window.location.href = 'openpath:' + filePath;
            return;
        }

        // Direct 1-Click Open Folder on Client PC
        const openFolderBtn = e.target.closest('.btn-open-folder');
        if (openFolderBtn) {
            e.preventDefault();
            const filePath = openFolderBtn.getAttribute('data-path');
            const folderPath = filePath.substring(0, filePath.lastIndexOf('\\'));
            window.location.href = 'openpath:' + folderPath;
            return;
        }

        const copyBtn = e.target.closest('.btn-copy-path');
        if (copyBtn) {
            e.preventDefault();
            const filePath = copyBtn.getAttribute('data-path');
            copyToClipboardFallback(filePath).then(() => {
                const origText = copyBtn.innerText;
                copyBtn.innerText = currentLang === 'ja' ? '複製完了!' : 'Copied!';
                copyBtn.classList.add('bg-cyan-600');
                setTimeout(() => {
                    copyBtn.innerText = translations[currentLang] ? translations[currentLang].btn_copy : 'Copy UNC';
                    copyBtn.classList.remove('bg-cyan-600');
                }, 1500);
            }).catch(() => {
                prompt("Copy path manually:", filePath);
            });
            return;
        }

        const contextBtn = e.target.closest('.btn-preview-context');
        if (contextBtn) {
            e.preventDefault();
            const path = contextBtn.getAttribute('data-path');
            const sheet = contextBtn.getAttribute('data-sheet');
            const row = parseInt(contextBtn.getAttribute('data-row'), 10);

            const modal = document.getElementById('context-modal');
            const modalBody = document.getElementById('modal-body');
            const modalTitle = document.getElementById('modal-file-title');

            if (modalTitle) modalTitle.textContent = `${sheet} (Row ${row})`;
            if (modalBody) modalBody.innerHTML = '<div class="text-slate-400 p-4 text-center">Loading context...</div>';
            if (modal) modal.classList.remove('hidden');

            fetch('/api/context', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path, sheet, row})
            }).then(res => res.json()).then(data => {
                if (data.status === 'ok' && data.rows && data.rows.length > 0) {
                    if (modalBody) {
                        modalBody.innerHTML = '';
                        data.rows.forEach(r => {
                            const line = document.createElement('div');
                            line.className = `p-1.5 rounded flex gap-3 ${r.is_target ? 'bg-cyan-900/60 border border-cyan-500/50 text-cyan-200 font-bold' : 'text-slate-300'}`;
                            line.innerHTML = `<span class="text-slate-500 w-10 text-right select-none">${r.row_num}</span><span class="break-all">${r.text}</span>`;
                            modalBody.appendChild(line);
                        });
                    }
                } else {
                    if (modalBody) modalBody.innerHTML = '<div class="text-amber-400 p-4 text-center">Context unavailable for this record.</div>';
                }
            }).catch(() => {
                if (modalBody) modalBody.innerHTML = '<div class="text-rose-400 p-4 text-center">Error fetching row context.</div>';
            });
            return;
        }

        const modalClose = e.target.closest('#modal-close');
        const modal = document.getElementById('context-modal');
        if (modalClose || e.target === modal) {
            if (modal) modal.classList.add('hidden');
        }
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initApp);
    } else {
        initApp();
    }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    query = request.args.get("q", "").strip()
    available_dbs = get_available_dbs()
    
    selected_dbs = request.args.getlist("dbs")
    if selected_dbs:
        selected_dbs = [db for db in selected_dbs if db in available_dbs]
    else:
        selected_dbs = list(available_dbs.keys())
    
    filters = {
        "doc_cat": request.args.get("doc_cat", "all"),
        "ext": request.args.get("ext", "all"),
        "date_range": request.args.get("date_range", "all"),
        "exclude": request.args.get("exclude", "").strip(),
        "prioritize_parts_list": request.args.get("parts_list") == "1",
        "hide_old": request.args.get("hide_old") == "1",
        "file_wide": request.args.get("file_wide") == "1",
    }

    results = []
    search_time = 0
    if query and selected_dbs:
        start_t = time.perf_counter()
        target_paths = [DEFAULT_DB_FOLDER / db_file for db_file in selected_dbs]
        
        raw_results = []
        with ThreadPoolExecutor(max_workers=min(10, len(target_paths))) as executor:
            futures = [executor.submit(search_one_database, db, query, filters) for db in target_paths]
            for future in as_completed(futures):
                raw_results.extend(future.result())

        for r in raw_results:
            results.append({
                "area": r[0],
                "file_name": r[1],
                "sheet_name": r[2],
                "row_num": r[3],
                "content": highlight_matches(r[4], query),
                "is_bom": r[5],
                "is_old": r[6],
                "path": r[7],
                "ext": r[8]
            })
        search_time = round(time.perf_counter() - start_t, 2)

    page = request.args.get("page", 1, type=int)
    total_results = len(results)
    total_pages = max(1, (total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
    page = max(1, min(page, total_pages))
    
    start_idx = (page - 1) * RESULTS_PER_PAGE
    paginated_results = results[start_idx : start_idx + RESULTS_PER_PAGE]

    query_args = request.args.copy()
    query_args.pop("page", None)
    query_params = query_args.to_dict(flat=False)
    
    flat_params = []
    for k, v_list in query_params.items():
        for v in v_list:
            flat_params.append(f"{k}={v}")
    clean_query_params = "&".join(flat_params)
    raw_query_string = request.query_string.decode('utf-8')

    return render_template_string(
        HTML_TEMPLATE,
        query=query,
        results=paginated_results,
        total_results=total_results,
        current_page=page,
        total_pages=total_pages,
        query_params=clean_query_params,
        raw_query_string=raw_query_string,
        search_time=search_time,
        all_dbs=available_dbs,
        selected_dbs=selected_dbs,
        doc_cat=filters["doc_cat"],
        ext=filters["ext"],
        date_range=filters["date_range"],
        exclude=filters["exclude"],
        prioritize_parts_list=filters["prioritize_parts_list"],
        hide_old=filters["hide_old"],
        file_wide=filters["file_wide"]
    )

@app.route("/api/context", methods=["POST"])
def get_context():
    data = request.get_json(silent=True) or {}
    file_path = data.get("path")
    sheet_name = data.get("sheet")
    row_num = data.get("row")

    if not file_path or not sheet_name or row_num is None:
        return jsonify({"status": "error", "message": "Missing arguments"}), 400

    target_paths = [DEFAULT_DB_FOLDER / db_file for db_file in get_available_dbs().keys()]
    context_rows = []

    for db_path in target_paths:
        try:
            con = open_readonly(db_path)
            f_row = con.execute("SELECT id FROM files WHERE full_path = ?", (file_path,)).fetchone()
            if f_row:
                file_id = f_row[0]
                rows = con.execute("""
                    SELECT row_number, row_text 
                    FROM rows_data 
                    WHERE file_id = ? AND sheet_name = ? AND row_number BETWEEN ? AND ?
                    ORDER BY row_number ASC
                """, (file_id, sheet_name, max(1, row_num - 4), row_num + 4)).fetchall()
                con.close()

                context_rows = [{"row_num": r[0], "text": r[1], "is_target": r[0] == row_num} for r in rows]
                break
            con.close()
        except Exception:
            continue

    return jsonify({"status": "ok", "rows": context_rows})


@app.route("/export")
def export_csv():
    query = request.args.get("q", "").strip()
    available_dbs = get_available_dbs()
    selected_dbs = request.args.getlist("dbs") or list(available_dbs.keys())
    selected_dbs = [db for db in selected_dbs if db in available_dbs]

    filters = {
        "doc_cat": request.args.get("doc_cat", "all"),
        "ext": request.args.get("ext", "all"),
        "date_range": request.args.get("date_range", "all"),
        "exclude": request.args.get("exclude", "").strip(),
        "prioritize_parts_list": request.args.get("parts_list") == "1",
        "hide_old": request.args.get("hide_old") == "1",
        "file_wide": request.args.get("file_wide") == "1", 
    }
    
    results = []
    if query and selected_dbs:
        target_paths = [DEFAULT_DB_FOLDER / db_file for db_file in selected_dbs]
        with ThreadPoolExecutor(max_workers=min(4, len(target_paths))) as executor:
            futures = [executor.submit(search_one_database, db, query, filters) for db in target_paths]
            for future in as_completed(futures):
                results.extend(future.result())

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Area", "File Name", "Sheet Name", "Row Number", "Row Content", "Is Parts List", "Is Old", "Full Path", "Extension"])
    for r in results:
        cw.writerow(r)

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=search_results_{query}.csv"}
    )

import socket

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    from waitress import serve
    lan_ip = get_local_ip()
    print("\n" + "=" * 50)
    print("🚀 Engineering Search Portal is LIVE!")
    print(f" ├─ Local Access:   http://localhost:5000")
    print(f" └─ Network Access: http://{lan_ip}:5000")
    print("=" * 50 + "\n")
    
    serve(app, host=os.getenv("ENGINEERING_SEARCH_HOST", "127.0.0.1"), port=int(os.getenv("ENGINEERING_SEARCH_PORT", r"")), threads=16)
