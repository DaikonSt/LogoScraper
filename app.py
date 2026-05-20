import csv
import os
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO, StringIO
from flask import Flask, jsonify, render_template, send_from_directory, send_file, Response, request
from scraper import scrape_logo, LOGOS_DIR

BASE_DIR   = os.path.dirname(__file__)
CSV_PATH   = os.path.join(BASE_DIR, "companies.csv")
STATE_PATH = os.path.join(BASE_DIR, "results.json")

app = Flask(__name__)

# ── company list ──────────────────────────────────────────────────────────────

def load_companies():
    if not os.path.exists(CSV_PATH):
        return []
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Accept column names case-insensitively
            rl = {k.lower().strip(): v.strip() for k, v in row.items() if k}
            company = rl.get("company") or rl.get("name") or ""
            if not company:
                continue
            # "Location" maps to state (used to disambiguate search queries)
            # "Industry" maps to category (also used in search queries)
            state    = rl.get("location") or rl.get("state") or ""
            category = rl.get("industry") or rl.get("category (optional)") or rl.get("category") or ""
            rows.append({"company": company, "state": state, "category": category})
    return rows


# ── results persistence ───────────────────────────────────────────────────────

def load_results():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_results(results):
    with open(STATE_PATH, "w") as f:
        json.dump(results, f, indent=2)


_lock     = threading.Lock()
_progress = {"running": False, "done": 0, "total": 0, "current": ""}

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload_csv", methods=["POST"])
def upload_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be a .csv"}), 400
    f.save(CSV_PATH)
    companies = load_companies()
    if not companies:
        os.remove(CSV_PATH)
        return jsonify({"error": "CSV has no valid Company rows. "
                        "Ensure the file has a 'Company' column header."}), 400
    return jsonify({"count": len(companies), "filename": f.filename, "companies": companies})


@app.route("/api/sample_csv")
def sample_csv():
    sample = (
        "Company,Location,Industry\n"
        "Apple,California,Technology\n"
        "Walmart,Arkansas,Retail\n"
        "ExxonMobil,Texas,Energy\n"
        "Bering Straits Native,,\n"
    )
    return Response(sample, mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=companies_sample.csv"})


@app.route("/api/results")
def api_results():
    return jsonify(load_results())


@app.route("/api/progress")
def api_progress():
    with _lock:
        return jsonify(dict(_progress))


@app.route("/api/scrape/all", methods=["POST"])
def scrape_all():
    with _lock:
        if _progress["running"]:
            return jsonify({"error": "already running"}), 409
        _progress["running"] = True
        _progress["done"]    = 0
        _progress["current"] = ""

    companies = load_companies()
    _progress["total"] = len(companies)

    def worker():
        results = load_results()

        def _scrape_one(item):
            company  = item["company"]
            category = item.get("category", "")
            state    = item.get("state", "")
            with _lock:
                _progress["current"] = company
            result = scrape_logo(company, category=category, state=state)
            return company, result

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_scrape_one, item): item for item in companies}
            for future in as_completed(futures):
                try:
                    company, result = future.result()
                except Exception as exc:
                    item    = futures[future]
                    company = item["company"]
                    result  = {"company": company, "status": "error", "error": str(exc)}
                with _lock:
                    results[company] = result
                    _progress["done"] += 1
                save_results(results)

        with _lock:
            _progress["running"] = False
            _progress["current"] = ""

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/scrape/<path:company>", methods=["POST"])
def scrape_one(company):
    category, state = "", ""
    for item in load_companies():
        if item["company"] == company:
            category = item.get("category", "")
            state    = item.get("state", "")
            break
    result  = scrape_logo(company, category=category, state=state)
    results = load_results()
    results[company] = result
    save_results(results)
    return jsonify(result)


@app.route("/api/scrape/<path:company>/next", methods=["POST"])
def scrape_next(company):
    """Retry with the next-best candidate, skipping all previously shown URLs."""
    results = load_results()
    current = results.get(company, {})

    tried = list(current.get("_tried_urls", []))
    current_url = current.get("url")
    if current_url and current_url not in tried:
        tried.append(current_url)

    category, state = "", ""
    for item in load_companies():
        if item["company"] == company:
            category = item.get("category", "")
            state    = item.get("state", "")
            break

    result = scrape_logo(company, category=category, state=state, exclude_urls=tried)
    result["_tried_urls"] = tried
    results[company] = result
    save_results(results)
    return jsonify(result)


@app.route("/api/clear", methods=["POST"])
def clear_results():
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
    for f in os.listdir(LOGOS_DIR):
        if any(f.endswith(ext) for ext in (".svg", ".png", ".jpg", ".jpeg", ".webp")):
            os.remove(os.path.join(LOGOS_DIR, f))
    return jsonify({"cleared": True})


@app.route("/logos/<path:filename>")
def serve_logo(filename):
    return send_from_directory(LOGOS_DIR, filename)


@app.route("/api/download_zip")
def download_zip():
    buf     = BytesIO()
    results = load_results()
    found   = [r for r in results.values() if r.get("status") == "found" and r.get("file")]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in found:
            path = os.path.join(LOGOS_DIR, r["file"])
            if os.path.exists(path):
                zf.write(path, r["file"])
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name="logos.zip")


if __name__ == "__main__":
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", 5050)) == 0:
            print("ERROR: Port 5050 already in use. Kill existing Python processes first.")
            raise SystemExit(1)
    app.run(debug=False, port=5050, use_reloader=False, threaded=True)
