"""Optional Chromium smoke test. Requires playwright and its Chromium installation.

CI: python scripts/check_eye_browser.py
Local system Chromium: python scripts/check_eye_browser.py --chromium /usr/bin/chromium
Uses set_content; there is no network server or external resource to load.
"""
import argparse
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
from qudgym.eye.cli import demo_records
from qudgym.eye.replay import render_html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chromium', help='Optional executable path for a system Chromium')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        html = Path(temp) / 'replay.html'
        render_html(demo_records('listener'), html)
        with sync_playwright() as p:
            options = {'headless': True}
            if args.chromium:
                options['executable_path'] = args.chromium
            browser = p.chromium.launch(**options)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.set_content(html.read_text(encoding='utf-8'))
                assert 'SYNTHETIC FIXTURE' in page.locator('#origin').inner_text(), errors
                page.locator('#next').click()
                assert 'prompt' in page.locator('#cursor').inner_text()
                page.locator('#seek').evaluate('(e)=>{e.value=4;e.dispatchEvent(new Event("input"));}')
                assert 'INFERENCE, NOT FACT' in page.locator('#beliefs').inner_text()
                assert 'hearing' in page.locator('#entities').inner_text()
                page.locator('#memory').uncheck()
                page.locator('#memory').check()
                page.set_viewport_size({'width': 700, 'height': 1000})
                assert page.locator('#next').is_visible()
                assert not errors, errors
                print('Agent-eye Chromium smoke passed: seek, prompts, hearing, memory, hypotheses.')
            finally:
                browser.close()


if __name__ == '__main__':
    main()
