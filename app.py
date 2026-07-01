from flask import Flask, render_template, request, jsonify
from ai.ai_engine2 import process_issue
from ai.bug_detector import detect_bugs
import json
import uuid
from datetime import datetime
from pathlib import Path
from flask import send_from_directory
import os
import shutil

app = Flask(__name__)

ISSUES_FILE     = "data/issues.json"
RESOLVED_FILE   = "data/resolved_issues.json"
TOKEN_LOG_FILE  = "data/token_usage.json"
INPUT_DIR       = "input"
TEMPLATES_DIR   = "templates"
WORKING_FILE    = "emi_calculator.html"
ORIGINAL_FILE   = "emi_calculator_ORIGINAL.html"

# Groq free tier: 500,000 tokens/day for llama-3.3-70b-versatile
# (adjust this if you're on a paid plan or different model limit)
GROQ_DAILY_LIMIT = 500_000


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def update_issue_status(issue_id, new_status):
    issues = load_json(ISSUES_FILE)
    for i in issues:
        if str(i["id"]) == str(issue_id):
            i["status"] = new_status
    save_json(ISSUES_FILE, issues)

def ensure_original_saved():
    original_path = os.path.join(TEMPLATES_DIR, ORIGINAL_FILE)
    working_path  = os.path.join(TEMPLATES_DIR, WORKING_FILE)
    if not os.path.exists(original_path):
        shutil.copy(working_path, original_path)


@app.route("/")
def home():
    return render_template("emi_calculator.html")


@app.route("/dashboard")
def dashboard():
    return render_template("Maintainance_Dashboard.html")


@app.route("/download/<issue_id>")
def download(issue_id):
    output_folder = "output"
    for file in os.listdir(output_folder):
        if file.startswith(f"fixed_{issue_id}_"):
            return send_from_directory(output_folder, file, as_attachment=True)
    return jsonify({"error": "Fixed file not found for this issue."}), 404


@app.route("/preview/<issue_id>")
def preview(issue_id):
    output_folder = "output"
    for file in os.listdir(output_folder):
        if file.startswith(f"fixed_{issue_id}_"):
            return send_from_directory(output_folder, file, as_attachment=False)
    return jsonify({"error": "Fixed file not found for this issue."}), 404


@app.route("/diff/<issue_id>")
def diff(issue_id):
    original_path = os.path.join(TEMPLATES_DIR, ORIGINAL_FILE)
    if not os.path.exists(original_path):
        original_path = os.path.join(TEMPLATES_DIR, WORKING_FILE)

    output_folder = "output"
    fixed_path = None
    for file in os.listdir(output_folder):
        if file.startswith(f"fixed_{issue_id}_"):
            fixed_path = os.path.join(output_folder, file)
            break

    if not fixed_path:
        return jsonify({"error": "Fixed file not found for this issue."}), 404

    try:
        with open(original_path, "r", encoding="utf-8") as f:
            original_html = f.read()
        with open(fixed_path, "r", encoding="utf-8") as f:
            fixed_html = f.read()
    except Exception as e:
        return jsonify({"error": f"Could not read files: {str(e)}"}), 500

    return jsonify({"original": original_html, "fixed": fixed_html, "issue_id": issue_id})


@app.route("/token_stats")
def token_stats():
    """
    Returns token usage stats for the dashboard:
    - total_used:       sum of all tokens ever used (all time)
    - used_today:       tokens used since midnight today
    - daily_limit:      the Groq daily quota
    - remaining_today:  daily_limit - used_today
    - per_issue_avg:    average tokens per completed issue
    - estimated_next:   rough estimate for a new issue (based on current file size)
    - history:          last 20 individual call records (for the table)
    - breakdown:        prompt vs completion totals
    """
    if not os.path.exists(TOKEN_LOG_FILE):
        token_log = []
    else:
        with open(TOKEN_LOG_FILE, "r") as f:
            token_log = json.load(f)

    today_str = datetime.now().strftime("%d-%m-%Y")

    total_used        = sum(e.get("total_tokens", 0) for e in token_log)
    total_prompt      = sum(e.get("prompt_tokens", 0) for e in token_log)
    total_completion  = sum(e.get("completion_tokens", 0) for e in token_log)
    used_today        = sum(
        e.get("total_tokens", 0) for e in token_log
        if e.get("timestamp", "").replace(" UTC", "").startswith(today_str)
    )

    remaining_today   = max(0, GROQ_DAILY_LIMIT - used_today)
    per_issue_avg     = round(total_used / len(token_log)) if token_log else 0

    # Estimate for next issue: read current HTML file size → tokens ≈ chars / 4
    # System prompt + user prompt overhead ≈ 500 tokens
    # Completion (full HTML back) ≈ same size as input
    working_path = os.path.join(TEMPLATES_DIR, WORKING_FILE)
    try:
        file_chars = len(open(working_path, "r", encoding="utf-8").read())
    except Exception:
        file_chars = 0

    file_tokens      = file_chars // 4
    system_overhead  = 500   # system prompt + instruction text
    estimated_input  = file_tokens + system_overhead
    estimated_output = file_tokens  # AI returns full HTML back
    estimated_next   = estimated_input + estimated_output

    return jsonify({
        "total_used":       total_used,
        "total_prompt":     total_prompt,
        "total_completion": total_completion,
        "used_today":       used_today,
        "daily_limit":      GROQ_DAILY_LIMIT,
        "remaining_today":  remaining_today,
        "per_issue_avg":    per_issue_avg,
        "estimated_next":   estimated_next,
        "estimated_input":  estimated_input,
        "estimated_output": estimated_output,
        "file_tokens":      file_tokens,
        "history":          list(reversed(token_log))[:20]   # newest first, max 20
    })


@app.route("/estimate_tokens", methods=["POST"])
def estimate_tokens():
    """
    Given a description string from the form, returns a refined estimate
    of how many tokens the next AI call will use, factoring in:
    - current HTML file size (input)
    - the description the user typed (adds to prompt)
    - expected output size (roughly same as input HTML)
    """
    data        = request.get_json()
    description = data.get("description", "")
    title       = data.get("title", "")

    # Prompt content: system prompt + title + description + HTML source
    working_path = os.path.join(TEMPLATES_DIR, WORKING_FILE)
    try:
        file_chars = len(open(working_path, "r", encoding="utf-8").read())
    except Exception:
        file_chars = 0

    desc_chars    = len(description) + len(title)
    system_chars  = 500 * 4   # approx chars in system prompt
    prompt_tokens = (file_chars + desc_chars + system_chars) // 4

    # Output: AI returns the full HTML + JSON wrapper overhead
    output_tokens = (file_chars // 4) + 200

    return jsonify({
        "prompt_tokens":  prompt_tokens,
        "output_tokens":  output_tokens,
        "total_estimate": prompt_tokens + output_tokens
    })


@app.route("/submit_issue", methods=["POST"])
def submit_issue():
    data = request.get_json()

    if not data.get("title") or not data.get("description"):
        return jsonify({"error": "Title and description are required."}), 400

    ensure_original_saved()

    issue_id = str(uuid.uuid4())[:8]

    issue = {
        "id":               issue_id,
        "title":            data["title"],
        "description":      data["description"],
        "status":           "Open",
        "source":           "Manual",
        "createdOn":        datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        "ai_remark":        None,
        "rejection_reason": None,
    }

    issues = load_json(ISSUES_FILE)
    issues.append(issue)
    save_json(ISSUES_FILE, issues)

    update_issue_status(issue_id, "In Progress")

    working_path = os.path.join(TEMPLATES_DIR, WORKING_FILE)
    input_path   = os.path.join(INPUT_DIR, WORKING_FILE)
    os.makedirs(INPUT_DIR, exist_ok=True)
    shutil.copy(working_path, input_path)

    try:
        result = process_issue(
            issue_id=issue_id,
            filename=WORKING_FILE,
            title=issue["title"],
            description=issue["description"]
        )
    except Exception as e:
        update_issue_status(issue_id, "AI Failed")
        return jsonify({"error": f"AI engine error: {str(e)}", "issue_id": issue_id}), 500

    ai_remark = "AI processed this issue."
    if result.get("status") == "done":
        log_text = result.get("log", "")
        for line in log_text.split("\n"):
            if line.startswith("MESSAGE"):
                ai_remark = line.split(":", 1)[-1].strip()
                break

    final_status = "In Review" if result.get("status") == "done" else "AI Failed"

    issues = load_json(ISSUES_FILE)
    for i in issues:
        if str(i["id"]) == issue_id:
            i["status"]    = final_status
            i["ai_remark"] = ai_remark
    save_json(ISSUES_FILE, issues)

    resolved = load_json(RESOLVED_FILE)
    resolved.append({"issue_id": issue_id, "ai_result": result, "ai_remark": ai_remark})
    save_json(RESOLVED_FILE, resolved)

    return jsonify({
        "message":   "Issue processed. Awaiting your review.",
        "issue_id":  issue_id,
        "status":    final_status,
        "ai_remark": ai_remark,
        "result":    result
    })


@app.route("/approve_issue/<issue_id>", methods=["POST"])
def approve_issue(issue_id):
    output_folder = "output"
    fixed_file = None
    for file in os.listdir(output_folder):
        if file.startswith(f"fixed_{issue_id}_"):
            fixed_file = file
            break

    if not fixed_file:
        return jsonify({"error": "No fix found for this issue."}), 404

    fixed_path   = os.path.join(output_folder, fixed_file)
    working_path = os.path.join(TEMPLATES_DIR, WORKING_FILE)
    shutil.copy(fixed_path, working_path)
    update_issue_status(issue_id, "Resolved")
    return jsonify({"message": "Fix approved and applied. This is now the live version."})


@app.route("/reject_issue/<issue_id>", methods=["POST"])
def reject_issue(issue_id):
    body   = request.get_json(silent=True) or {}
    reason = body.get("reason", "").strip() or None

    issues = load_json(ISSUES_FILE)
    for i in issues:
        if str(i["id"]) == str(issue_id):
            i["status"]           = "Rejected"
            i["rejection_reason"] = reason
    save_json(ISSUES_FILE, issues)
    return jsonify({"message": "Fix rejected. Working file was not changed.", "rejection_reason": reason})


@app.route("/restore_original", methods=["POST"])
def restore_original():
    ensure_original_saved()
    original_path = os.path.join(TEMPLATES_DIR, ORIGINAL_FILE)
    working_path  = os.path.join(TEMPLATES_DIR, WORKING_FILE)
    shutil.copy(original_path, working_path)
    return jsonify({"message": "Restored to the original version. All approved changes have been undone."})


@app.route("/scan_bugs")
def scan_bugs():
    """
    Runs the AI bug detector on the live HTML file.
    Detected bugs are added to issues.json (duplicates skipped by title).
    """
    issues = load_json(ISSUES_FILE)
    bugs   = detect_bugs()

    existing_titles = {issue["title"] for issue in issues}
    added = 0

    for bug in bugs:
        if bug["title"] not in existing_titles:
            issues.append(bug)
            added += 1

    save_json(ISSUES_FILE, issues)
    return jsonify({"message": "Scan completed", "bugs_found": added})


@app.route("/resolve_issue/<issue_id>", methods=["POST"])
def resolve_issue(issue_id):
    """Manually marks an issue as Resolved."""
    issues = load_json(ISSUES_FILE)
    for issue in issues:
        if str(issue["id"]) == str(issue_id):
            issue["status"] = "Resolved"
    save_json(ISSUES_FILE, issues)
    return jsonify({"message": "Issue marked as Resolved"})


@app.route("/accept_issue/<issue_id>", methods=["POST"])
def accept_issue(issue_id):
    """Resets a rejected/detected issue back to Open so it can be resubmitted."""
    update_issue_status(issue_id, "Open")
    return jsonify({"message": "Issue reset to Open"})


@app.route("/get_issues")
def get_issues():
    issues = load_json(ISSUES_FILE)
    return jsonify(issues)


@app.route("/delete_issue/<issue_id>", methods=["DELETE"])
def delete_issue(issue_id):
    issues = load_json(ISSUES_FILE)
    updated = [i for i in issues if str(i["id"]) != str(issue_id)]
    save_json(ISSUES_FILE, updated)
    return jsonify({"message": "Issue deleted successfully."}), 200


if __name__ == "__main__":
    ensure_original_saved()
    app.run(debug=True)