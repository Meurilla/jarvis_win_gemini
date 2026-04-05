"""
Test the LLM intent classifier with 20 sample voice command phrases.

Run: python tests/test_classifier.py
Requires: GEMINI_API_KEY in .env or environment
"""

import asyncio
import os
import sys
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Import the classifier and speech corrections
from server import classify_intent, apply_speech_corrections

# Test cases: (input_text, expected_action)
TEST_CASES = [
    # open_terminal (Gemini CLI)
    ("open the terminal", "open_terminal"),
    ("open gemini code", "open_terminal"),
    ("launch Gemini", "open_terminal"),
    ("open up the terminal for me", "open_terminal"),
    ("start jimmy nigh", "open_terminal"),          # misheard "Gemini"

    # browse
    ("search for Python tutorials", "browse"),
    ("go to github.com", "browse"),
    ("pull up React documentation", "browse"),
    ("look up restaurants near me", "browse"),
    ("Google the weather in New York", "browse"),

    # build
    ("build me a landing page", "build"),
    ("create a snake game", "build"),
    ("make a todo app with React", "build"),
    ("build a REST API for my project", "build"),
    ("create a dashboard for analytics", "build"),

    # chat
    ("how are you doing today", "chat"),
    ("what time is it", "chat"),
    ("tell me a joke", "chat"),
    ("good morning JARVIS", "chat"),
    ("what's the weather like", "chat"),
]

async def run_tests():
    # Check Gemini API key (classify_intent uses GEMINI_API_KEY internally)
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set in .env or environment")
        sys.exit(1)

    passed = 0
    failed = 0

    print(f"\nRunning {len(TEST_CASES)} classification tests with Gemini...\n")
    print(f"{'Input':<45} {'Expected':<15} {'Got':<15} {'Status'}")
    print("-" * 85)

    for text, expected in TEST_CASES:
        corrected = apply_speech_corrections(text)
        result = await classify_intent(corrected)
        actual = result["action"]

        if actual == expected:
            status = "PASS"
            passed += 1
        else:
            status = "FAIL"
            failed += 1

        print(f"{text:<45} {expected:<15} {actual:<15} {status}")

    print(f"\n{'='*85}")
    print(f"Results: {passed}/{len(TEST_CASES)} passed, {failed} failed")

    if failed == 0:
        print("All tests passed!")
    else:
        print(f"WARNING: {failed} tests failed")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_tests())