"""
End-to-end browser test: reproduce the RTC cell rendering delay bug.

Uses Playwright (headless Chromium) to open a notebook in JupyterLab,
then inserts a cell via the MCP server, and checks whether the browser
DOM actually renders the new cell WITHOUT a page reload.

This is the definitive test — it checks what the user actually sees.
"""

import asyncio
import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

JUPYTER_URL = "http://localhost:8888"
JUPYTER_TOKEN = "MY_TOKEN"
MCP_URL = f"{JUPYTER_URL}/mcp"


async def insert_cell_via_mcp(cell_source: str, cell_type: str = "code", cell_index: int = -1):
    """Insert a cell via the MCP server's Streamable HTTP endpoint."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "insert_cell",
                {"cell_index": cell_index, "cell_type": cell_type, "cell_source": cell_source},
            )
            text = result.content[0].text if result.content else ""
            log.info(f"MCP insert_cell result: {text[:120]}")
            return text


async def get_all_cell_texts(page):
    """Extract cell content from the browser DOM using multiple selector strategies.

    JupyterLab uses CodeMirror 6 which renders text in .cm-line elements.
    We also check .jp-Editor textContent and the notebook model as fallbacks.
    """
    # Strategy 1: CodeMirror 6 line content (.cm-line elements within cells)
    cm_texts = await page.evaluate("""
        () => {
            const cells = document.querySelectorAll('.jp-Cell');
            return Array.from(cells).map(cell => {
                const lines = cell.querySelectorAll('.cm-line');
                if (lines.length > 0) {
                    return Array.from(lines).map(l => l.textContent || '').join('\\n');
                }
                // Fallback: try .cm-content
                const cmContent = cell.querySelector('.cm-content');
                if (cmContent) return cmContent.textContent || '';
                // Fallback: try jp-Editor
                const editor = cell.querySelector('.jp-Editor');
                if (editor) return editor.textContent || '';
                // Fallback: try rendered markdown
                const rendered = cell.querySelector('.jp-MarkdownOutput, .jp-RenderedMarkdown');
                if (rendered) return rendered.textContent || '';
                return '';
            });
        }
    """)

    # Strategy 2: Get the full inner text of the notebook panel (catches everything)
    notebook_text = await page.evaluate("""
        () => {
            const panel = document.querySelector('.jp-NotebookPanel');
            return panel ? panel.innerText : '';
        }
    """)

    return cm_texts, notebook_text


async def scroll_to_bottom(page):
    """Scroll the notebook to the bottom to force windowed cells to render."""
    await page.evaluate("""
        () => {
            const outer = document.querySelector('.jp-WindowedPanel-outer');
            if (outer) outer.scrollTop = outer.scrollHeight;
        }
    """)
    await asyncio.sleep(0.2)


async def main():
    from playwright.async_api import async_playwright

    marker = f"print('Hello World from MCP - {time.time_ns()}')"
    notebook_path = "notebook.ipynb"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path="/root/.cache/ms-playwright/chromium-1194/chrome-linux/chrome",
        )
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        # ── 1. Open notebook in JupyterLab ──────────────────────────
        url = f"{JUPYTER_URL}/lab/tree/{notebook_path}?token={JUPYTER_TOKEN}"
        log.info(f"Opening {url}")
        await page.goto(url, wait_until="networkidle", timeout=30000)

        # Wait for JupyterLab to fully render the notebook
        log.info("Waiting for notebook cells to render...")
        await page.wait_for_selector(".jp-Cell", timeout=30000)
        await asyncio.sleep(2)  # Let RTC fully connect

        # ── 2. Count baseline cells in the DOM ──────────────────────
        baseline_cells = await page.locator(".jp-Cell").count()
        log.info(f"Baseline: {baseline_cells} cells visible in browser DOM")

        await scroll_to_bottom(page)
        baseline_cm_texts, baseline_nb_text = await get_all_cell_texts(page)
        log.info(f"Baseline cell texts (last 3): {[t[:60] for t in baseline_cm_texts[-3:]]}")

        # ── 3. Take a screenshot before insertion ────────────────────
        await page.screenshot(path="/tmp/before_insert.png", full_page=True)
        log.info("Screenshot saved: /tmp/before_insert.png")

        # ── 4. Insert cell via MCP ──────────────────────────────────
        log.info(f"Inserting cell via MCP: {marker}")
        await insert_cell_via_mcp(marker)

        # ── 5. Wait and check if the cell appears in the DOM ────────
        max_wait = 10
        poll_interval = 0.5
        elapsed = 0.0
        cell_found = False

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            # Scroll to bottom to ensure new cell is in viewport
            await scroll_to_bottom(page)

            current_cells = await page.locator(".jp-Cell").count()
            cm_texts, nb_text = await get_all_cell_texts(page)

            # Check in CodeMirror cell texts
            for src in cm_texts:
                if marker in src:
                    cell_found = True
                    break

            # Also check in the full notebook panel text
            if not cell_found and marker in nb_text:
                cell_found = True
                log.info("  (found via notebook panel innerText)")

            if cell_found:
                log.info(f"Cell FOUND in browser DOM after {elapsed:.1f}s")
                log.info(f"  Cells: {baseline_cells} -> {current_cells}")
                break
            else:
                last_text = repr(cm_texts[-1][:80]) if cm_texts else 'none'
                log.info(
                    f"  [{elapsed:.1f}s] Cell NOT yet visible. "
                    f"DOM cells: {current_cells} (was {baseline_cells}). "
                    f"Last cell text: {last_text}"
                )

        # ── 6. Take screenshot after waiting ─────────────────────────
        await scroll_to_bottom(page)
        await page.screenshot(path="/tmp/after_insert.png", full_page=True)
        log.info("Screenshot saved: /tmp/after_insert.png")

        # ── 6b. Dump all cell texts for debugging ────────────────────
        cm_texts, _ = await get_all_cell_texts(page)
        log.info(f"All cell texts after insert ({len(cm_texts)} cells):")
        for i, t in enumerate(cm_texts):
            log.info(f"  Cell {i}: {repr(t[:100])}")

        # ── 7. If not found, try after a forced reload ───────────────
        found_after_reload = False
        if not cell_found:
            log.warning("Cell NOT visible in DOM. Trying page reload...")
            await page.reload(wait_until="networkidle", timeout=30000)
            await page.wait_for_selector(".jp-Cell", timeout=15000)
            await asyncio.sleep(2)

            await scroll_to_bottom(page)

            reload_cm_texts, reload_nb_text = await get_all_cell_texts(page)
            for src in reload_cm_texts:
                if marker in src:
                    found_after_reload = True
                    break
            if not found_after_reload and marker in reload_nb_text:
                found_after_reload = True

            reload_cells = await page.locator(".jp-Cell").count()
            await page.screenshot(path="/tmp/after_reload.png", full_page=True)
            log.info(f"After reload: {reload_cells} cells, marker found: {found_after_reload}")
            log.info("Screenshot saved: /tmp/after_reload.png")

            log.info(f"All cell texts after reload ({len(reload_cm_texts)} cells):")
            for i, t in enumerate(reload_cm_texts):
                log.info(f"  Cell {i}: {repr(t[:100])}")

        await browser.close()

    # ── 8. Report results ────────────────────────────────────────
    print("\n" + "=" * 60)
    if cell_found:
        print("RESULT: PASS - Cell appeared in browser DOM without reload")
        print(f"  Latency: {elapsed:.1f}s")
    elif found_after_reload:
        print("RESULT: BUG REPRODUCED!")
        print("  Cell was NOT visible in the browser until page reload.")
        print("  This confirms the RTC rendering delay bug.")
        sys.exit(1)
    else:
        print("RESULT: FAIL - Cell not found even after reload")
        print("  The MCP insertion may have failed entirely.")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
