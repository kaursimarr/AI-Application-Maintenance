from flask import Flask, render_template, request, jsonify
from ai.ai_engine2 import process_issue
import json
import uuid
from datetime import datetime
from pathlib import Path
from flask import send_from_directory
import os
import shutil

app = Flask(__name__)

ISSUES_FILE    = "data/issues.json"
RESOLVED_FILE  = "data/resolved_issues.json"
INPUT_DIR      = "input"
ORIGINAL_FILE  = "emi_calculator_ORIGINAL.html"   # Bug 1 fix: permanent master copy


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def update_issue_status(issue_id, new_status):
    """Helper — updates one issue's status in issues.json."""
    issues = load_json(ISSUES_FILE)
    for i in issues:
        if str(i["id"]) == str(issue_id):
            i["status"] = new_status
    save_json(ISSUES_FILE, issues)


@app.route("/")
def home():
    return render_template("emi_calculator.html")


@app.route("/dashboard")
def dashboard():
    return render_template("Maintainance_Dashboard.html")


@app.route("/download/<issue_id>")
def download(issue_id):
    """Returns the fixed file for a given issue."""
    output_folder = "output"
    for file in os.listdir(output_folder):
        # Bug fix: match exact prefix to avoid substring collisions
        if file.startswith(f"fixed_{issue_id}_"):
            return send_from_directory(output_folder, file, as_attachment=True)
    return jsonify({"error": "Fixed file not found for this issue."}), 404


@app.route("/submit_issue", methods=["POST"])
def submit_issue():
    data = request.get_json()

    # Validate input
    if not data.get("title") or not data.get("description"):
        return jsonify({"error": "Title and description are required."}), 400

    issue_id = str(uuid.uuid4())[:8]

    issue = {
        "id":          issue_id,
        "title":       data["title"],
        "description": data["description"],
        "status":      "Open",
        "createdOn":   datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        "ai_remark":   None,   # will be filled after AI runs
    }

    # ── Step 1: Save as Open ──────────────────────────────────────────────────
    issues = load_json(ISSUES_FILE)
    issues.append(issue)
    save_json(ISSUES_FILE, issues)

    # ── Step 2: Mark In Progress ──────────────────────────────────────────────
    update_issue_status(issue_id, "In Progress")

    # ── Bug 1 Fix: always restore the ORIGINAL file into input/ before AI runs ─
    # ORIGINAL file is the true source of truth — never the previous AI output.
    # This prevents each ticket from fixing the previous ticket's AI output.
    original_path = os.path.join("templates", ORIGINAL_FILE)
    input_path    = os.path.join(INPUT_DIR, "emi_calculator.html")

    if not os.path.exists(original_path):
        # First time setup: create the permanent master copy from current file
        shutil.copy(
            os.path.join("templates", "emi_calculator.html"),
            original_path
        )

    # Always give AI the original clean file, not whatever previous AI produced
    os.makedirs(INPUT_DIR, exist_ok=True)
    shutil.copy(original_path, input_path)

    # ── Step 3: Run AI engine ─────────────────────────────────────────────────
    try:
        result = process_issue(
            issue_id=issue_id,
            filename="emi_calculator.html",
            title=issue["title"],
            description=issue["description"]
        )
    except Exception as e:
        # AI failed — mark issue as failed so dashboard shows correct status
        update_issue_status(issue_id, "AI Failed")
        return jsonify({
            "error": f"AI engine error: {str(e)}",
            "issue_id": issue_id
        }), 500

    # ── Step 4: Extract AI remark for user ───────────────────────────────────
    # Pull the human-readable explanation out of the log to show in dashboard
    ai_remark = "AI processed this issue."
    if result.get("status") == "done":
        log_text = result.get("log", "")
        # Extract MESSAGE line from structured log
        for line in log_text.split("\n"):
            if line.startswith("MESSAGE"):
                # Everything after "MESSAGE     : "
                ai_remark = line.split(":", 1)[-1].strip()
                break

    # ── Step 5: Mark Resolved + save AI remark ───────────────────────────────
    final_status = "Resolved" if result.get("status") == "done" else "AI Failed"

    issues = load_json(ISSUES_FILE)
    for i in issues:
        if str(i["id"]) == issue_id:
            i["status"]    = final_status
            i["ai_remark"] = ai_remark   # stored so dashboard can show it
    save_json(ISSUES_FILE, issues)

    # ── Step 6: Save to resolved_issues.json ─────────────────────────────────
    resolved = load_json(RESOLVED_FILE)
    resolved.append({
        "issue_id":  issue_id,
        "ai_result": result,
        "ai_remark": ai_remark
    })
    save_json(RESOLVED_FILE, resolved)

    return jsonify({
        "message":   "Issue processed successfully.",
        "issue_id":  issue_id,
        "status":    final_status,
        "ai_remark": ai_remark,
        "result":    result
    })


@app.route("/get_issues")
def get_issues():
    """Returns all issues — dashboard polls this to stay live."""
    issues = load_json(ISSUES_FILE)
    return jsonify(issues)


@app.route("/delete_issue/<issue_id>", methods=["DELETE"])
def delete_issue(issue_id):
    issues = load_json(ISSUES_FILE)
    updated = [i for i in issues if str(i["id"]) != str(issue_id)]
    save_json(ISSUES_FILE, updated)
    return jsonify({"message": "Issue deleted successfully."}), 200


if __name__ == "__main__":
    app.run(debug=True)