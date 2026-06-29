import os
import json
import shutil
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from ai.groq_client import get_client

load_dotenv()

MAX_RETRIES = 3

SYSTEM_PROMPT_PYTHON = (
    "You are an automated code-repair agent. Fix Python code based on pytest error logs.\n"
    "You MUST respond ONLY with a valid JSON object. No markdown, no extra text.\n"
    "Exact structure:\n"
    "{\n"
    "  \"explanation\": \"What was wrong and how you fixed it.\",\n"
    "  \"fixed_code\": \"Complete updated file as a single string.\"\n"
    "}"
)

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

# ── Token tracking file ────────────────────────────────────────────────────────
TOKEN_LOG_FILE = "data/token_usage.json"

def load_token_log():
    if not os.path.exists(TOKEN_LOG_FILE):
        return []
    with open(TOKEN_LOG_FILE, "r") as f:
        return json.load(f)

def save_token_entry(issue_id: str, prompt_tokens: int,
                     completion_tokens: int, total_tokens: int, model: str):
    """Appends one token usage record to data/token_usage.json."""
    os.makedirs("data", exist_ok=True)
    log = load_token_log()
    log.append({
        "issue_id":          issue_id,
        "model":             model,
        "prompt_tokens":     prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens":      total_tokens,
        "timestamp":         datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M:%S UTC")
    })
    with open(TOKEN_LOG_FILE, "w") as f:
        json.dump(log, f, indent=4)


# ── Block A: Utilities ─────────────────────────────────────────────────────────

def build_prompt_python(title: str, description: str, source_code: str) -> str:
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
    os.makedirs(logs_dir, exist_ok=True)
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
    output_files = list(Path(output_dir).glob(f'fixed_{issue_id}_*'))
    return 'done' if output_files else 'processing'


# ── Block B: Testing Safety Net (Python only) ──────────────────────────────────

def run_tests(test_file="test_calculator.py"):
    print("🔍 Running pytest...")
    result = subprocess.run(
        ["pytest", test_file, "--tb=short", "-q"],
        capture_output=True, text=True
    )
    return result.returncode == 0, result.stdout + result.stderr


def run_tests_on_fixed_code(fixed_code):
    source_file = "calculator.py"
    backup_path = f"{Path(source_file).stem}_backup.py"
    shutil.copy(source_file, backup_path)
    try:
        with open(source_file, "w") as f:
            f.write(fixed_code)
        passed, log_output = run_tests()
        return passed, log_output
    finally:
        shutil.copy(backup_path, source_file)
        os.remove(backup_path)


def save_version(code, attempt_number, output_dir, filename):
    os.makedirs(output_dir, exist_ok=True)
    base = Path(filename).stem
    ext  = Path(filename).suffix
    path = str(Path(output_dir) / f"{base}_v{attempt_number}{ext}")
    with open(path, "w") as f:
        f.write(code)
    print(f"💾 Saved version to {path}")
    return path


# ── Block C1: Python Stateful AI Retry Loop ────────────────────────────────────

def generate_and_test_fix(title, description, source_code,
                           output_dir, filename, issue_id, max_retries=MAX_RETRIES):
    try:
        client = get_client()
    except ValueError as e:
        print(e)
        return False, source_code, str(e)

    MODEL = "llama-3.3-70b-versatile"
    conversation_history = [{"role": "system", "content": SYSTEM_PROMPT_PYTHON}]

    passed, log_output = run_tests()
    if passed:
        print("✅ Code already passes all tests. Nothing to fix.")
        return True, source_code, "Code already passing tests."

    fixed_code = source_code

    for attempt in range(1, max_retries + 1):
        print(f"\n--- Attempt {attempt} of {max_retries} ---")

        if attempt == 1:
            user_content = build_prompt_python(title, description, source_code)
            user_content += f"\n\nPytest Error Log:\n{log_output}"
        else:
            user_content = (
                f"Your previous fix still failed the tests.\n\n"
                f"The code you produced was:\n{fixed_code}\n\n"
                f"New pytest error log:\n{log_output}\n\n"
                f"Analyze your previous mistake, try a completely different "
                f"approach, and return the full fixed file in JSON format."
            )

        conversation_history.append({"role": "user", "content": user_content})

        print(f"Consulting AI ({MODEL})...")
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=conversation_history,
                response_format={"type": "json_object"}
            )

            # ── Capture and save token usage ──────────────────────────────
            usage = response.usage
            if usage:
                save_token_entry(
                    issue_id=issue_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    model=MODEL
                )
                print(f"📊 Tokens — prompt: {usage.prompt_tokens}, "
                      f"completion: {usage.completion_tokens}, "
                      f"total: {usage.total_tokens}")

            raw_content = response.choices[0].message.content
            conversation_history.append({"role": "assistant", "content": raw_content})

            parsed      = json.loads(raw_content)
            explanation = parsed.get("explanation", "No explanation provided.")
            fixed_code  = parsed.get("fixed_code", "")

            print(f"✅ AI Explanation: {explanation}")

            if not fixed_code:
                print("⚠️ AI returned empty code. Retrying...")
                continue

        except json.JSONDecodeError:
            print("⚠️ Failed to parse AI response as JSON. Retrying...")
            continue
        except Exception as e:
            print(f"❌ API error: {e}")
            return False, source_code, str(e)

        save_version(fixed_code, attempt, output_dir, filename)
        passed, log_output = run_tests_on_fixed_code(fixed_code)

        if passed:
            print(f"✅ Fix verified on attempt {attempt}!")
            return True, fixed_code, explanation
        else:
            print("⚠️ Tests failed. Feeding error back to AI...")

        time.sleep(1)

    print(f"\n❌ Could not fix after {max_retries} attempts.")
    return False, source_code, "Failed after max retries."


# ── Block C2: Web Analysis Engine ──────────────────────────────────────────────

def analyze_web_file(title, description, source_code, output_dir, filename, issue_id):
    try:
        client = get_client()
    except ValueError as e:
        print(e)
        return False, source_code, str(e)

    MODEL    = "llama-3.3-70b-versatile"
    file_ext = Path(filename).suffix.lower()

    print("🌐 Web file detected — running Web Analysis Engine...")

    prompt = build_prompt_web(title, description, source_code, file_ext)

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_WEB},
                {"role": "user",   "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        # ── Capture and save token usage ──────────────────────────────────
        usage = response.usage
        if usage:
            save_token_entry(
                issue_id=issue_id,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                model=MODEL
            )
            print(f"📊 Tokens — prompt: {usage.prompt_tokens}, "
                  f"completion: {usage.completion_tokens}, "
                  f"total: {usage.total_tokens}")

        raw_content = response.choices[0].message.content
        parsed = json.loads(raw_content)

        bug_explanation   = parsed.get("bug_explanation",   "No bug explanation provided.")
        ui_ux_suggestions = parsed.get("ui_ux_suggestions", "No UI/UX suggestions provided.")
        fixed_code        = parsed.get("fixed_code",        "")

        print(f"✅ Bug Analysis: {bug_explanation}")
        print(f"✅ UI/UX Suggestions: {ui_ux_suggestions}")

        if not fixed_code:
            print("⚠️ AI returned empty code.")
            return False, source_code, "AI returned empty fixed_code."

        save_version(fixed_code, 1, output_dir, filename)
        summary = f"BUGS: {bug_explanation} | UI/UX: {ui_ux_suggestions}"
        return True, fixed_code, summary

    except json.JSONDecodeError:
        print("⚠️ Failed to parse AI response as JSON.")
        return False, source_code, "JSON parse error."
    except Exception as e:
        print(f"❌ API error: {e}")
        return False, source_code, str(e)


# ── Block D: Grand Pipeline ────────────────────────────────────────────────────

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
    for d in [input_dir, output_dir, processed_dir, logs_dir]:
        os.makedirs(d, exist_ok=True)

    input_path = Path(input_dir) / filename
    file_ext   = Path(filename).suffix.lower()

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

    print(f"\n📋 Processing '{filename}' for issue: {title}")

    if file_ext == '.py':
        print("🐍 Python file detected — running V1 Pytest Loop...")
        shutil.copy(str(input_path), filename)

        success, final_code, summary = generate_and_test_fix(
            title=title,
            description=description,
            source_code=source_code,
            output_dir=output_dir,
            filename=filename,
            issue_id=issue_id,
        )

        if os.path.exists(filename):
            os.remove(filename)

        file_type = 'python'
        verified  = success

    elif file_ext in WEB_EXTENSIONS:
        success, final_code, summary = analyze_web_file(
            title=title,
            description=description,
            source_code=source_code,
            output_dir=output_dir,
            filename=filename,
            issue_id=issue_id,
        )
        file_type = 'web'
        verified  = False

    else:
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE',
                        f'Unsupported file type: {file_ext}.')
        return {'status': 'error', 'log': log}

    if not success:
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE', summary)
        return {'status': 'error', 'log': log}

    output_filename = f'fixed_{issue_id}_{filename}'
    output_path = Path(output_dir) / output_filename
    try:
        output_path.write_text(final_code, encoding='utf-8')
        status_label = "✅ Verified" if verified else "⚠️ Unverified (human review needed)"
        print(f"{status_label} fix saved to {output_path}")
    except Exception as e:
        log = write_log(logs_dir, issue_id, filename, title, 'FAILURE',
                        f'Failed to write output file: {e}')
        return {'status': 'error', 'log': log}

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        processed_filename = f"{timestamp}_{filename}"
        shutil.copy(str(input_path), str(Path(processed_dir) / processed_filename))
        input_path.write_text(final_code, encoding='utf-8')
    except Exception as e:
        logging.getLogger('merged_agent').warning(f'Could not update files: {e}')

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


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 5:
        print("Usage: python ai-engine.py <issue_id> <filename> <title> <description>")
        sys.exit(1)
    issue_id    = sys.argv[1]
    filename    = sys.argv[2]
    title       = sys.argv[3]
    description = sys.argv[4]
    if not os.path.exists(f"input/{filename}"):
        print(f"❌ 'input/{filename}' not found.")
        sys.exit(1)
    result = process_issue(issue_id=issue_id, filename=filename,
                           title=title, description=description)
    print(f"\n✅ Result: {result['status']}")
    print(result['log'])