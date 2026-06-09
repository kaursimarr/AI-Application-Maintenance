import os
import json
import shutil
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from groq_client import get_client

load_dotenv()

MAX_RETRIES = 3

# Used for .py files — focuses on bug fixing + pytest verification
SYSTEM_PROMPT_PYTHON = (
    "You are an automated code-repair agent. Fix Python code based on pytest error logs.\n"
    "You MUST respond ONLY with a valid JSON object. No markdown, no extra text.\n"
    "Exact structure:\n"
    "{\n"
    "  \"explanation\": \"What was wrong and how you fixed it.\",\n"
    "  \"fixed_code\": \"Complete updated file as a single string.\"\n"
    "}"
)

# Used for .html/.css/.js files — focuses on bugs + UI/UX suggestions
SYSTEM_PROMPT_WEB = (
    "You are a senior web developer and UI/UX expert with 15+ years of experience.\n"
    "You will be given a web file (HTML/CSS/JS) and an issue description.\n"
    "Analyze the code for bugs, UI problems, and improvement opportunities.\n"
    "You MUST respond ONLY with a valid JSON object. No markdown, no extra text.\n"
    "Exact structure:\n"
    "{\n"
    "  \"bug_explanation\": \"What bugs or errors exist in the code and why.\",\n"
    "  \"ui_ux_suggestions\": \"Specific UI/UX improvements recommended.\",\n"
    "  \"fixed_code\": \"Complete updated file as a single string with all fixes applied.\"\n"
    "}"
)

WEB_EXTENSIONS = {'.html', '.css', '.js'}


# ── Block A: Utilities

def build_prompt_python(title: str, description: str, source_code: str) -> str:
    """Builds prompt for Python files — includes issue context and source code."""
    return (
        f"ISSUE TITLE: {title}\n\n"
        f"ISSUE DESCRIPTION:\n{description}\n\n"
        f"FILE TYPE: Python\n\n"
        f"SOURCE CODE:\n{source_code}\n\n"
        f"---\n"
        f"Fix the bug in the above Python code.\n"
        f"Return ONLY a valid JSON object with 'explanation' and 'fixed_code' keys."
    )


def build_prompt_web(title: str, description: str, source_code: str,
                     file_ext: str) -> str:
    """
    Builds prompt for web files — includes issue + UI/UX analysis request.
    Phase 2 addition: separate prompt for HTML/CSS/JS files.
    """
    lang_map = {'.html': 'HTML', '.css': 'CSS', '.js': 'JavaScript'}
    language = lang_map.get(file_ext, 'Web')

    return (
        f"ISSUE TITLE: {title}\n\n"
        f"ISSUE DESCRIPTION:\n{description}\n\n"
        f"FILE TYPE: {language}\n\n"
        f"SOURCE CODE:\n{source_code}\n\n"
        f"---\n"
        f"Analyze the above {language} code. Do the following:\n"
        f"1. Identify and fix the reported bug.\n"
        f"2. Identify any other bugs you find.\n"
        f"3. Suggest and apply UI/UX improvements.\n"
        f"Return ONLY a valid JSON object with "
        f"'bug_explanation', 'ui_ux_suggestions', and 'fixed_code' keys."
    )


def write_log(logs_dir: str, issue_id: str, filename: str, title: str,
              status: str, message: str = '') -> str:
    """
    Writes a structured log entry to logs/ and returns the log text.
    Each log file is timestamped + issue_id so they never overwrite each other.
    Fixed deprecation warning: using timezone-aware datetime.now(timezone.utc).
    """
    os.makedirs(logs_dir, exist_ok=True)
    # Fixed: replaced deprecated utcnow() with timezone-aware now()
    now = datetime.now(timezone.utc)
    timestamp = now.strftime('%Y-%m-%d %H:%M:%S UTC')
    log_lines = [
        "=" * 60,
        f"TIMESTAMP   : {timestamp}",
        f"ISSUE ID    : {issue_id}",
        f"FILE NAME   : {filename}",
        f"ISSUE TITLE : {title}",
        f"STATUS      : {status}",
    ]
    if message:
        log_lines.append(f"MESSAGE     : {message}")
    log_lines.append("=" * 60)
    log_text = "\n".join(log_lines)

    log_filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{issue_id[:8]}.log"
    log_path = Path(logs_dir) / log_filename
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write(log_text + "\n")

    logging.getLogger('merged_agent').info(log_text)
    return log_text


def get_issue_status(issue_id: str, output_dir: str) -> str:
    """Checks if a fix already exists for this issue in output/."""
    output_files = list(Path(output_dir).glob(f'fixed_{issue_id}_*'))
    return 'done' if output_files else 'processing'


# ── Block B: Testing Safety Net (Python only)

def run_tests(test_file="test_calculator.py"):
    """
    Runs pytest on the test file and returns (passed_bool, output_logs).
    Only used for .py files — web files have no automated test runner.
    """
    print("🔍 Running pytest...")
    result = subprocess.run(
        ["pytest", test_file, "--tb=short", "-q"],
        capture_output=True,
        text=True
    )
    return result.returncode == 0, result.stdout + result.stderr


def run_tests_on_fixed_code(fixed_code):
    """
    Safely tests fixed Python code without permanently modifying calculator.py.
    Backup original → swap in fix → run tests → always restore original.
    Only called for .py files.
    """
    source_file = "calculator.py"
    backup_path = f"{Path(source_file).stem}_backup.py"  # calculator_backup.py

    shutil.copy(source_file, backup_path)
    try:
        with open(source_file, "w") as f:
            f.write(fixed_code)
        passed, log_output = run_tests()
        return passed, log_output
    finally:
        # Always restores original — even if pytest crashes or exception occurs
        shutil.copy(backup_path, source_file)
        os.remove(backup_path)


def save_version(code, attempt_number, output_dir, filename):
    """
    Saves each AI attempt as a versioned file: calculator_v1.py, v2.py etc.
    Saved before testing so even failed attempts are preserved for review.
    """
    os.makedirs(output_dir, exist_ok=True)
    base = Path(filename).stem
    ext  = Path(filename).suffix
    path = str(Path(output_dir) / f"{base}_v{attempt_number}{ext}")
    with open(path, "w") as f:
        f.write(code)
    print(f"💾 Saved version to {path}")
    return path


# ── Block C1: Python Stateful AI Retry Loop (V1 — unchanged)

def generate_and_test_fix(title, description, source_code,
                           output_dir, filename, max_retries=MAX_RETRIES):
    """
    V1 Core: Stateful Groq retry loop for Python files.
    - Maintains full conversation history so AI knows what it tried before
    - Each failed attempt feeds error + previous bad code back to AI
    - Verifies fix by running pytest
    - Returns (success_bool, final_code, explanation)
    """
    try:
        client = get_client()
    except ValueError as e:
        print(e)
        return False, source_code, str(e)

    conversation_history = [
        {"role": "system", "content": SYSTEM_PROMPT_PYTHON}
    ]

    # Check if code already passes — no point calling AI if tests are green
    passed, log_output = run_tests()
    if passed:
        print(" Code already passes all tests. Nothing to fix.")
        return True, source_code, "Code already passing tests."

    # Initialize outside loop so attempt 2+ can reference previous failed code
    fixed_code = source_code

    for attempt in range(1, max_retries + 1):
        print(f"\n---  Attempt {attempt} of {max_retries} ---")

        if attempt == 1:
            # First attempt: send full code + error log
            user_content = build_prompt_python(title, description, source_code)
            user_content += f"\n\nPytest Error Log:\n{log_output}"
        else:
            # Retry: send previous bad code + new error explicitly
            # AI shouldn't have to guess what it wrote from history alone
            user_content = (
                f"Your previous fix still failed the tests.\n\n"
                f"The code you produced was:\n{fixed_code}\n\n"
                f"New pytest error log:\n{log_output}\n\n"
                f"Analyze your previous mistake, try a completely different "
                f"approach, and return the full fixed file in JSON format."
            )

        # Full conversation history sent every call — AI remembers all attempts
        conversation_history.append({"role": "user", "content": user_content})

        print("Consulting AI (llama-3.3-70b-versatile)...")
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=conversation_history,
                response_format={"type": "json_object"}
            )

            raw_content = response.choices[0].message.content
            conversation_history.append({"role": "assistant", "content": raw_content})

            parsed      = json.loads(raw_content)
            explanation = parsed.get("explanation", "No explanation provided.")
            fixed_code  = parsed.get("fixed_code", "")  # updates for next retry

            print(f" AI Explanation: {explanation}")

            if not fixed_code:
                print(" AI returned empty code. Retrying...")
                continue

        except json.JSONDecodeError:
            print(" Failed to parse AI response as JSON. Retrying...")
            continue
        except Exception as e:
            print(f" API error: {e}")
            return False, source_code, str(e)

        # Save versioned copy BEFORE testing — preserves all attempts for review
        save_version(fixed_code, attempt, output_dir, filename)

        # Safely test: backup → swap → run pytest → restore original
        passed, log_output = run_tests_on_fixed_code(fixed_code)

        if passed:
            print(f" Fix verified on attempt {attempt}!")
            return True, fixed_code, explanation
        else:
            print(" Tests failed. Feeding error back to AI...")

        time.sleep(1)

    print(f"\n Could not fix after {max_retries} attempts. Check output/ for last attempt.")
    return False, source_code, "Failed after max retries."


# ── Block C2: Web Analysis Engine (V2 — new)

def analyze_web_file(title, description, source_code, output_dir, filename):
    """
    V2 Core: Single-shot AI analysis for HTML/CSS/JS files.
    No pytest loop — web files have no automated test runner.
    AI returns bug fixes + UI/UX suggestions in one call.
    Output is saved but marked as 'unverified' — human review needed.
    Returns (success_bool, final_code, summary)
    """
    try:
        client = get_client()
    except ValueError as e:
        print(e)
        return False, source_code, str(e)

    file_ext = Path(filename).suffix.lower()

    print("Web file detected — running Web Analysis Engine...")
    print(" Note: No automated tests available for web files. Output requires human review.")

    prompt = build_prompt_web(title, description, source_code, file_ext)

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_WEB},
                {"role": "user",   "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        raw_content = response.choices[0].message.content
        parsed = json.loads(raw_content)

        bug_explanation   = parsed.get("bug_explanation",   "No bug explanation provided.")
        ui_ux_suggestions = parsed.get("ui_ux_suggestions", "No UI/UX suggestions provided.")
        fixed_code        = parsed.get("fixed_code",        "")

        print(f" Bug Analysis: {bug_explanation}")
        print(f" UI/UX Suggestions: {ui_ux_suggestions}")

        if not fixed_code:
            print(" AI returned empty code.")
            return False, source_code, "AI returned empty fixed_code."

        # Save versioned copy to output/
        save_version(fixed_code, 1, output_dir, filename)

        # Summary combines both findings for the log
        summary = f"BUGS: {bug_explanation} | UI/UX: {ui_ux_suggestions}"
        return True, fixed_code, summary

    except json.JSONDecodeError:
        print(" Failed to parse AI response as JSON.")
        return False, source_code, "JSON parse error."
    except Exception as e:
        print(f"API error: {e}")
        return False, source_code, str(e)


# ── Block D: Grand Pipeline with Smart Router 

def process_issue(
    issue_id: str,
    filename: str,
    title: str,
    description: str,
    input_dir: str     = "input",
    output_dir: str    = "output",
    processed_dir: str = "processed",
    logs_dir: str      = "logs",
) -> dict:
    """
    Full pipeline — entry point for Sameer's dashboard to call.
    Smart Router inside decides which engine to use based on file extension:
      .py           → generate_and_test_fix() — V1 pytest loop
      .html/.css/.js → analyze_web_file()     — V2 web analysis

    Returns dict with 'status', 'output_file', 'file_type', and 'log'.
    """
    for d in [input_dir, output_dir, processed_dir, logs_dir]:
        os.makedirs(d, exist_ok=True)

    input_path = Path(input_dir) / filename
    file_ext   = Path(filename).suffix.lower()

    # ── STEP 1: Read source file 
    if not input_path.exists():
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE',
                        'Source file not found in input directory.')
        return {'status': 'error', 'log': log}

    try:
        source_code = input_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE',
                        f'Failed to read source file: {e}')
        return {'status': 'error', 'log': log}

    # ── STEP 2: Smart Router — decide which engine to use
    print(f"\n Processing '{filename}' for issue: {title}")

    if file_ext == '.py':
        # ── V1: Python path — copy to CWD so pytest import works 
        print(" Python file detected — running V1 Pytest Loop...")
        shutil.copy(str(input_path), filename)

        success, final_code, summary = generate_and_test_fix(
            title=title,
            description=description,
            source_code=source_code,
            output_dir=output_dir,
            filename=filename,
        )

        # Clean up CWD copy regardless of outcome
        if os.path.exists(filename):
            os.remove(filename)

        file_type = 'python'
        verified  = success  # pytest confirmed the fix

    elif file_ext in WEB_EXTENSIONS:
        # ── V2: Web path — no pytest, AI analysis only 
        success, final_code, summary = analyze_web_file(
            title=title,
            description=description,
            source_code=source_code,
            output_dir=output_dir,
            filename=filename,
        )

        file_type = 'web'
        verified  = False  # web fixes are never auto-verified

    else:
        # ── Unsupported file type
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE',
                        f'Unsupported file type: {file_ext}. '
                        f'Supported: .py, .html, .css, .js')
        return {'status': 'error', 'log': log}

    if not success:
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE', summary)
        return {'status': 'error', 'log': log}

    # ── STEP 3: Save verified/suggested fix to output
    output_filename = f'fixed_{issue_id}_{filename}'
    output_path = Path(output_dir) / output_filename
    try:
        output_path.write_text(final_code, encoding='utf-8')
        status_label = " Verified" if verified else " Unverified (human review needed)"
        print(f"{status_label} fix saved to {output_path}")
    except Exception as e:
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE',
                        f'Failed to write output file: {e}')
        return {'status': 'error', 'log': log}

    # ── STEP 4: Move original to processed
    try:
        shutil.move(str(input_path), str(Path(processed_dir) / filename))
        print(f" Original archived to processed/{filename}")
    except Exception as e:
        logging.getLogger('merged_agent').warning(f'Could not archive original: {e}')

    # ── STEP 5: Write success log 
    verified_str = "AUTO-VERIFIED by pytest" if verified else "UNVERIFIED — human review required"
    log = write_log(
        logs_dir, issue_id, filename, title, 'SUCCESS',
        f'[{verified_str}] Fix saved to output/{output_filename}. {summary}'
    )

    return {
        'status':      'done',
        'file_type':   file_type,
        'verified':    verified,
        'output_file': output_filename,
        'log':         log,
    }


# ── Entry Point 

if __name__ == "__main__":
    import sys

    # Usage: python ai-engine.py <issue_id> <filename> <title> <description>
    # Example: python ai-engine.py ISS-001 calculator.py "Zero division bug" "Crashes when rate is 0"
    
    if len(sys.argv) < 5:
        print("Usage: python ai-engine.py <issue_id> <filename> <title> <description>")
        print("Example: python ai-engine.py ISS-001 calculator.py \"Zero division bug\" \"Crashes when rate is 0\"")
        sys.exit(1)

    issue_id    = sys.argv[1]
    filename    = sys.argv[2]
    title       = sys.argv[3]
    description = sys.argv[4]

    # File must already be in input/ folder before running
    if not os.path.exists(f"input/{filename}"):
        print(f" 'input/{filename}' not found.")
        print(f"   Place the file in the input/ folder first.")
        sys.exit(1)

    result = process_issue(
        issue_id=issue_id,
        filename=filename,
        title=title,
        description=description,
    )

    print(f"\n Result: {result['status']}")
    if result.get('verified') is not None:
        print(f" Verified: {result['verified']}")
    print(result['log'])