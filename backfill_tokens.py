"""
Run this ONCE from your project root to backfill token estimates
for issues that were processed before token tracking was added.

Usage:  python backfill_tokens.py
"""
import json, os
from datetime import datetime, timezone

ISSUES_FILE    = "data/issues.json"
TOKEN_LOG_FILE = "data/token_usage.json"
TEMPLATES_DIR  = "templates"
WORKING_FILE   = "emi_calculator.html"

# Rough token estimate per issue:
# - HTML file size → tokens
# - system prompt overhead
# - AI response (full HTML back)
def estimate_tokens_for_issue(html_chars):
    input_tokens  = (html_chars // 4) + 500   # file + system prompt overhead
    output_tokens = (html_chars // 4) + 200   # AI returns full HTML + JSON wrapper
    return input_tokens, output_tokens, input_tokens + output_tokens

def main():
    # Load existing token log (don't overwrite real records)
    if os.path.exists(TOKEN_LOG_FILE):
        with open(TOKEN_LOG_FILE) as f:
            token_log = json.load(f)
    else:
        token_log = []

    existing_ids = {e["issue_id"] for e in token_log}

    # Load issues
    with open(ISSUES_FILE) as f:
        issues = json.load(f)

    # Get current HTML file size for estimation
    html_path = os.path.join(TEMPLATES_DIR, WORKING_FILE)
    try:
        html_chars = len(open(html_path, encoding="utf-8").read())
    except:
        html_chars = 3000  # fallback

    added = 0
    for issue in issues:
        iid = issue["id"]
        # Only backfill issues that went through AI (not Open/In Progress)
        if issue["status"] in ("Open", "In Progress", "AI Failed"):
            continue
        if iid in existing_ids:
            continue  # already tracked, skip

        inp, out, tot = estimate_tokens_for_issue(html_chars)

        token_log.append({
            "issue_id":          iid,
            "model":             "llama-3.3-70b-versatile",
            "prompt_tokens":     inp,
            "completion_tokens": out,
            "total_tokens":      tot,
            "timestamp":         issue.get("createdOn", "—") + " (backfilled estimate)",
            "backfilled":        True
        })
        added += 1
        print(f"  Backfilled {iid} ({issue['title'][:30]}) — ~{tot} tokens")

    os.makedirs("data", exist_ok=True)
    with open(TOKEN_LOG_FILE, "w") as f:
        json.dump(token_log, f, indent=4)

    print(f"\n✅ Done — added {added} backfilled records to {TOKEN_LOG_FILE}")
    print("   Refresh the dashboard token drawer to see updated stats.")

if __name__ == "__main__":
    main()
