"""
claude_agent.py  –  PantryAI Claude Code Agent
------------------------------------------------
How to use:
  1. Write your request in prompt.txt (in this same folder)
  2. Run: python claude_agent.py
  3. Claude reads your prompt + any files you mention, and makes changes

Example prompts in prompt.txt:
  "Read reorder.py and fix the Blinkit search selector"
  "Read detector.py and add count-based quantity tracking"
  "Read all files and explain what each one does"
  "Fix the email authentication issue in reorder.py"

Set your API key:
  Add to your .env file:  ANTHROPIC_API_KEY=sk-ant-...
"""

import os
import sys
import json
import re
from pathlib import Path
from dotenv import load_dotenv

try:
    import anthropic
except ImportError:
    print("Installing anthropic SDK...")
    os.system(f"{sys.executable} -m pip install anthropic")
    import anthropic

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_DIR  = Path(__file__).parent
PROMPT_FILE  = PROJECT_DIR / 'prompt.txt'
API_KEY      = os.getenv('ANTHROPIC_API_KEY')

# Files the agent is allowed to read and modify
ALLOWED_FILES = [
    'detector.py',
    'inventory.py',
    'reorder.py',
    'app.py',
    'prompt.txt',
    'test_blinkit.py',
    'test_blinkit2.py',
    'test_blinkit3.py',
]

PROJECT_CONTEXT = """
You are an AI assistant helping develop PantryAI — a smart pantry monitoring system.

Project overview:
- detector.py: YOLOv8 camera detection, tracks items on shelf
- inventory.py: Tracks item presence/absence, triggers reorders
- reorder.py: Opens Blinkit via Selenium, sends Gmail confirmation
- app.py: Flask web server, serves dashboard at localhost:5000
- templates/index.html: Dashboard UI

Tech stack: Python 3.13, YOLOv8n, Flask, Selenium, SQLite, Gmail SMTP
Hardware: ASUS TUF Gaming A15, RTX 3050, webcam

Current known issues:
- Blinkit automation: location popup blocks search
- Gmail App Password auth failing (535 error)
- Reorder triggers while item still present (should only trigger on absence)

When modifying files, output the COMPLETE new file content wrapped like:
===FILE: filename.py===
<complete file content here>
===END FILE===

You can output multiple files if needed.
"""

# ── File tools ────────────────────────────────────────────────────────────────

def read_file(filename: str) -> str:
    path = PROJECT_DIR / filename
    if not path.exists():
        return f"[File not found: {filename}]"
    if filename not in ALLOWED_FILES and not filename.endswith('.py'):
        return f"[Access denied: {filename}]"
    return path.read_text(encoding='utf-8')


def write_file(filename: str, content: str) -> str:
    path = PROJECT_DIR / filename
    # Backup original
    if path.exists():
        backup = PROJECT_DIR / f"{filename}.bak"
        backup.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
        print(f"  Backed up original to {filename}.bak")
    path.write_text(content, encoding='utf-8')
    return f"Written: {filename} ({len(content)} chars)"


def extract_and_write_files(response_text: str) -> list:
    """Parse ===FILE: name=== blocks from Claude's response and write them."""
    pattern = r'===FILE:\s*(.+?)===\n(.*?)===END FILE==='
    matches = re.findall(pattern, response_text, re.DOTALL)
    written = []
    for filename, content in matches:
        filename = filename.strip()
        content  = content.strip()
        result   = write_file(filename, content)
        written.append(filename)
        print(f"  ✓ {result}")
    return written


# ── Auto-detect which files to include ───────────────────────────────────────

def detect_relevant_files(prompt: str) -> list:
    """Include files mentioned in the prompt, plus always include reorder + detector."""
    mentioned = []
    for f in ALLOWED_FILES:
        name = f.replace('.py', '')
        if name.lower() in prompt.lower() or f.lower() in prompt.lower():
            mentioned.append(f)

    # Always include these for context
    defaults = ['detector.py', 'inventory.py', 'reorder.py']
    all_files = list(dict.fromkeys(defaults + mentioned))  # deduplicated

    # If prompt says "all files" include everything
    if 'all' in prompt.lower() or 'every' in prompt.lower():
        all_files = [f for f in ALLOWED_FILES if (PROJECT_DIR / f).exists()]

    return [f for f in all_files if (PROJECT_DIR / f).exists()]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not found in .env file")
        print("Add this line to your .env file:")
        print("  ANTHROPIC_API_KEY=sk-ant-your-key-here")
        sys.exit(1)

    if not PROMPT_FILE.exists():
        PROMPT_FILE.write_text("Explain what each file in my project does.\n")
        print(f"Created prompt.txt — edit it and run again")
        sys.exit(0)

    prompt = PROMPT_FILE.read_text(encoding='utf-8').strip()
    if not prompt:
        print("prompt.txt is empty — write your request and run again")
        sys.exit(0)

    print(f"\n{'='*60}")
    print(f"PROMPT: {prompt[:100]}{'...' if len(prompt)>100 else ''}")
    print(f"{'='*60}\n")

    # Build file context
    relevant_files = detect_relevant_files(prompt)
    file_context   = ""
    for fname in relevant_files:
        content = read_file(fname)
        file_context += f"\n\n--- {fname} ---\n{content}"
        print(f"  Including: {fname}")

    # Build message to Claude
    full_prompt = f"{prompt}\n\nProject files:{file_context}"

    print(f"\nSending to Claude claude-sonnet-4-20250514...\n")

    client   = anthropic.Anthropic(api_key=API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8096,
        system=PROJECT_CONTEXT,
        messages=[{"role": "user", "content": full_prompt}]
    )

    reply = response.content[0].text

    print("="*60)
    print("CLAUDE'S RESPONSE:")
    print("="*60)
    print(reply)
    print("="*60)

    # Auto-write any file changes Claude provided
    written = extract_and_write_files(reply)
    if written:
        print(f"\n✓ Files updated: {', '.join(written)}")
        print("  Originals backed up as .bak files")
    else:
        print("\n(No file changes — response was informational)")

    # Save response to a log
    log_file = PROJECT_DIR / 'agent_response.txt'
    log_file.write_text(reply, encoding='utf-8')
    print(f"\nFull response saved to: agent_response.txt")

    # Clear prompt.txt after successful run
    PROMPT_FILE.write_text("")
    print("prompt.txt cleared — ready for next prompt\n")


if __name__ == '__main__':
    main()