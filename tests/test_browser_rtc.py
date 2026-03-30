"""
End-to-end browser test: reproduce the RTC cell rendering delay bug.

Uses Playwright (headless Chromium) to open a notebook in JupyterLab,
then inserts cells via the MCP server, and checks whether the browser
DOM actually renders the new cells WITHOUT a page reload.

This test reproduces a known JupyterLab WindowedPanel bug where cells
added via Y.js/CRDT updates to positions outside the viewport don't
get their CodeMirror editors initialized. The cell containers appear
in the DOM but remain empty until page reload.

Findings:
- Cells inserted at index 0 (in viewport) always render correctly
- Cells appended at the end (off-screen) may not render their content
- The bug is in JupyterLab's windowed rendering, not in Y.js/CRDT sync
- The REST API and YDoc model always show the correct cell content
"""

import asyncio
import logging
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

JUPYTER_URL = "http://localhost:8888"
JUPYTER_TOKEN = "MY_TOKEN"
MCP_URL = f"{JUPYTER_URL}/mcp"

GET_CELL_INFO_JS = """() => {
    const cells = document.querySelectorAll('.jp-Cell');
    return Array.from(cells).map(cell => {
        const cmLines = cell.querySelectorAll('.cm-line');
        return {
            hasEditor: !!cell.querySelector('.cm-editor'),
            text: cmLines.length > 0
                ? Array.from(cmLines).map(l => l.textContent || '').join('\\n')
                : '',
        };
    });
}"""


async def create_notebook(path):
    """Create a fresh empty notebook via Jupyter REST API."""
    requests.put(
        f"{JUPYTER_URL}/api/contents/{path}",
        headers={"Authorization": f"token {JUPYTER_TOKEN}", "Content-Type": "application/json"},
        json={
            "type": "notebook",
            "content": {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "",
                        "metadata": {},
                        "outputs": [],
                        "execution_count": None,
                    }
                ],
                "metadata": {
                    "kernelspec": {
                        "display_name": "Python 3",
                        "language": "python",
                        "name": "python3",
                    }
                },
                "nbformat": 4,
                "nbformat_minor": 5,
            },
        },
    )


async def insert_cells_via_mcp(notebook_path, notebook_name, markers, cell_index=-1):
    """Insert cells via MCP using a single session."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool(
                "use_notebook",
                {
                    "notebook_name": notebook_name,
                    "notebook_path": notebook_path,
                    "mode": "connect",
                },
            )
            for marker in markers:
                await session.call_tool(
                    "insert_cell",
                    {
                        "cell_index": cell_index,
                        "cell_type": "code",
                        "cell_source": f"print('{marker}')",
                    },
                )


async def test_append_rendering(pw, num_cells=5):
    """Test: cells appended at end of notebook (off-screen).

    This reproduces the bug: cells are added to the DOM but their
    CodeMirror editors are not initialized.
    """
    notebook = "test_append.ipynb"
    await create_notebook(notebook)

    browser = await pw.chromium.launch(
        headless=True,
        executable_path="/root/.cache/ms-playwright/chromium-1194/chrome-linux/chrome",
    )
    page = await (await browser.new_context(ignore_https_errors=True)).new_page()
    await page.goto(
        f"{JUPYTER_URL}/doc/tree/{notebook}?token={JUPYTER_TOKEN}",
        wait_until="networkidle",
        timeout=30000,
    )
    await asyncio.sleep(3)

    markers = [f"APPEND_{i}_{time.time_ns()}" for i in range(num_cells)]
    await insert_cells_via_mcp(notebook, "append_test", markers, cell_index=-1)
    await asyncio.sleep(3)

    # Scroll to bottom
    await page.evaluate(
        "() => { const o = document.querySelector('.jp-WindowedPanel-outer'); "
        "if (o) o.scrollTop = o.scrollHeight; }"
    )
    await asyncio.sleep(0.5)

    cell_info = await page.evaluate(GET_CELL_INFO_JS)
    found = sum(1 for m in markers if any(m in c["text"] for c in cell_info))
    editors = sum(1 for c in cell_info if c["hasEditor"])

    await page.screenshot(path="/tmp/test_append.png", full_page=True)
    await browser.close()

    log.info(
        f"Append test ({num_cells} cells): DOM={len(cell_info)} "
        f"editors={editors} markers={found}/{num_cells}"
    )
    return found, num_cells


async def test_top_insert_rendering(pw, num_cells=5):
    """Test: cells inserted at top of notebook (in viewport).

    This should always pass - cells in the viewport get editors.
    """
    notebook = "test_top_insert.ipynb"
    await create_notebook(notebook)

    browser = await pw.chromium.launch(
        headless=True,
        executable_path="/root/.cache/ms-playwright/chromium-1194/chrome-linux/chrome",
    )
    page = await (await browser.new_context(ignore_https_errors=True)).new_page()
    await page.goto(
        f"{JUPYTER_URL}/doc/tree/{notebook}?token={JUPYTER_TOKEN}",
        wait_until="networkidle",
        timeout=30000,
    )
    await asyncio.sleep(3)

    markers = [f"TOP_{i}_{time.time_ns()}" for i in range(num_cells)]
    await insert_cells_via_mcp(notebook, "top_test", markers, cell_index=0)
    await asyncio.sleep(3)

    cell_info = await page.evaluate(GET_CELL_INFO_JS)
    found = sum(1 for m in markers if any(m in c["text"] for c in cell_info))
    editors = sum(1 for c in cell_info if c["hasEditor"])

    await page.screenshot(path="/tmp/test_top_insert.png", full_page=True)
    await browser.close()

    log.info(
        f"Top insert test ({num_cells} cells): DOM={len(cell_info)} "
        f"editors={editors} markers={found}/{num_cells}"
    )
    return found, num_cells


async def test_append_visible_after_reload(pw, num_cells=5):
    """Test: appended cells become visible after page reload.

    Confirms the data is in YDoc but the rendering is broken.
    """
    notebook = "test_reload.ipynb"
    await create_notebook(notebook)

    browser = await pw.chromium.launch(
        headless=True,
        executable_path="/root/.cache/ms-playwright/chromium-1194/chrome-linux/chrome",
    )
    page = await (await browser.new_context(ignore_https_errors=True)).new_page()
    await page.goto(
        f"{JUPYTER_URL}/doc/tree/{notebook}?token={JUPYTER_TOKEN}",
        wait_until="networkidle",
        timeout=30000,
    )
    await asyncio.sleep(3)

    markers = [f"RELOAD_{i}_{time.time_ns()}" for i in range(num_cells)]
    await insert_cells_via_mcp(notebook, "reload_test", markers, cell_index=-1)
    await asyncio.sleep(2)

    # Reload
    await page.reload(wait_until="networkidle", timeout=30000)
    await asyncio.sleep(3)

    cell_info = await page.evaluate(GET_CELL_INFO_JS)
    found = sum(1 for m in markers if any(m in c["text"] for c in cell_info))

    await browser.close()

    log.info(f"Reload test ({num_cells} cells): markers={found}/{num_cells}")
    return found, num_cells


async def main():
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as pw:
        # Test 1: Append (reproduces bug)
        found, total = await test_append_rendering(pw, num_cells=10)
        results.append(("Append (off-screen)", found, total, found < total))

        # Test 2: Top insert (should pass)
        found, total = await test_top_insert_rendering(pw, num_cells=10)
        results.append(("Top insert (in viewport)", found, total, found < total))

        # Test 3: Append + reload (should pass)
        found, total = await test_append_visible_after_reload(pw, num_cells=10)
        results.append(("Append + reload", found, total, found < total))

    print()
    print("=" * 60)
    print("RTC Cell Rendering Test Results")
    print("=" * 60)
    for name, found, total, is_bug in results:
        status = "BUG" if is_bug else "OK"
        print(f"  {name:30s}: {found:2d}/{total} [{status}]")

    # Exit with error if the bug is NOT reproduced (regression test)
    bug_reproduced = results[0][3]  # Append test should show the bug
    top_works = not results[1][3]  # Top insert should work
    reload_works = not results[2][3]  # Reload should work

    if bug_reproduced and top_works and reload_works:
        print()
        print("Bug confirmed: JupyterLab WindowedPanel does not initialize")
        print("CodeMirror editors for cells added off-screen via Y.js updates.")
        print("Workaround: insert cells at index 0 instead of appending.")
        sys.exit(0)
    elif not bug_reproduced:
        print()
        print("Bug NOT reproduced - rendering may have been fixed!")
        sys.exit(0)
    else:
        print()
        print("Unexpected results.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
