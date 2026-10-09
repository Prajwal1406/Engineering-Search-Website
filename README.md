# Engineering Search System

A portfolio-safe document indexing and search project using Python, SQLite FTS5, Flask, Tkinter, and OpenPyXL.

## Privacy note

This public version contains no databases, documents, credentials, private server names, internal folder paths, customer names, or company-specific mappings. Configure document and database locations locally with environment variables or command-line arguments.

## Install

```powershell
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
```

## Index sample documents

```powershell
py engineering_search_v2.py --scan --root ".\sample_documents" --db ".\databases\engineering_search.db"
```

## Desktop search

```powershell
py engineering_search_v2.py --gui --db ".\databases\engineering_search.db"
```

## Web search

```powershell
set ENGINEERING_SEARCH_DB_FOLDER=%CD%\databases
py web_search.py
```

The web server binds to `127.0.0.1` by default. Set `ENGINEERING_SEARCH_HOST=0.0.0.0` only when you intentionally want LAN access and have reviewed your firewall and authorization requirements.

## Weekend scan example

`weekend_scan_example.bat` uses only generic sample paths. Set `SEARCH_ROOT_AREA1` through `SEARCH_ROOT_AREA4` locally when needed. Never commit private UNC paths.

## Before publishing

Run a repository-wide search for private server names, usernames, UNC paths, IP addresses, credentials, customer names, and internal project names. Databases and sample-document contents are excluded by `.gitignore`.
