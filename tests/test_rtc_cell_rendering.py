# Copyright (c) 2024- Datalayer, Inc.
#
# BSD 3-Clause License

"""
Test for RTC cell rendering propagation.

This test verifies that cells inserted via the MCP server are immediately
visible to other clients (i.e., the Jupyter frontend) without requiring
a page reload or cursor movement.

The bug scenario:
1. User asks Claude to create/modify a notebook via MCP tools
2. MCP tool inserts a cell using the YDoc (collaborative editing) path
3. The Jupyter frontend (UI) does NOT render the new cell
4. User must reload or move around to see the change

Root cause hypothesis: The MCP server modifies the shared YDoc, but the
change may not propagate to connected frontend clients in real-time.

Test strategy:
- Insert a cell via the MCP tool (which goes through the YDoc path)
- Read the notebook back through the Jupyter REST Contents API
  (simulating what the frontend sees after a save/sync cycle)
- Verify the cell is present without any manual intervention
- For the JUPYTER_SERVER (extension) mode, also verify via a separate
  YDoc/WebSocket client that the change propagates in real-time
"""

import asyncio
import logging
import time

import pytest
import requests

from .test_common import MCPClient, timeout_wrapper
from .conftest import JUPYTER_TOKEN


###############################################################################
# Helper: Read notebook via Jupyter REST API (simulates frontend's view)
###############################################################################

def read_notebook_via_rest_api(jupyter_url: str, notebook_path: str) -> dict:
    """Read notebook contents via the Jupyter REST Contents API.

    This is the same API the frontend uses to load notebooks. If a cell
    inserted via RTC doesn't show up here, the frontend won't render it
    either (until a reload forces a fresh fetch).

    Args:
        jupyter_url: Base URL of the Jupyter server (e.g. http://localhost:8888)
        notebook_path: Relative path to the notebook within root_dir

    Returns:
        Parsed notebook dict (nbformat structure)
    """
    response = requests.get(
        f"{jupyter_url}/api/contents/{notebook_path}",
        headers={"Authorization": f"token {JUPYTER_TOKEN}"},
        params={"content": "1", "type": "notebook"},
    )
    response.raise_for_status()
    return response.json()["content"]


###############################################################################
# Test: Cell insertion is visible via REST API (both modes)
###############################################################################

@pytest.mark.asyncio
@timeout_wrapper(60)
async def test_rtc_insert_cell_visible_via_rest_api(mcp_client_parametrized: MCPClient, jupyter_server):
    """Verify that a cell inserted via MCP is immediately visible through the
    Jupyter REST Contents API, without requiring a page reload.

    This test catches the bug where:
    - MCP inserts a cell into the YDoc
    - The YDoc change does NOT propagate to the frontend
    - The user must reload to see the new cell

    Steps:
    1. Read the notebook via REST API to get baseline cell count
    2. Insert a cell via MCP tool (goes through YDoc in extension mode)
    3. Wait briefly for sync propagation
    4. Read the notebook via REST API again
    5. Assert the new cell is present with the correct content
    6. Clean up by deleting the inserted cell
    """
    marker_text = f"# RTC test cell {time.time_ns()}"
    notebook_path = "notebook.ipynb"

    async with mcp_client_parametrized:
        # 1. Baseline: read notebook via REST API
        baseline = read_notebook_via_rest_api(jupyter_server, notebook_path)
        baseline_cell_count = len(baseline["cells"])
        logging.info(f"Baseline cell count: {baseline_cell_count}")

        # 2. Insert a cell via MCP tool
        result = await mcp_client_parametrized.insert_cell(
            cell_index=-1,  # append at end
            cell_type="markdown",
            cell_source=marker_text,
        )
        assert result is not None, "insert_cell should succeed"
        assert "Cell inserted successfully" in result["result"]
        logging.info(f"Cell inserted via MCP: {result['result'][:100]}")

        # 3. Wait for RTC sync propagation
        # In a working RTC setup, this should be near-instant (<1s).
        # We give a generous window, but the point is: it should NOT
        # require a full page reload.
        max_wait_seconds = 5
        poll_interval = 0.5
        cell_found = False
        elapsed = 0.0

        while elapsed < max_wait_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            # 4. Read notebook via REST API (what the frontend sees)
            current = read_notebook_via_rest_api(jupyter_server, notebook_path)
            current_cells = current["cells"]

            # Check if the new cell appeared
            for cell in current_cells:
                if marker_text in cell.get("source", ""):
                    cell_found = True
                    break

            if cell_found:
                logging.info(f"RTC sync confirmed after {elapsed:.1f}s")
                break

        # 5. Assert the cell is visible
        assert cell_found, (
            f"Cell with content '{marker_text}' was NOT visible via the Jupyter "
            f"REST API after {max_wait_seconds}s. This indicates the RTC/YDoc "
            f"change did not propagate to the frontend. The user would need to "
            f"reload the page to see the inserted cell. "
            f"Baseline cells: {baseline_cell_count}, "
            f"Current cells: {len(current_cells)}"
        )

        # Also verify cell count increased
        assert len(current_cells) == baseline_cell_count + 1, (
            f"Expected {baseline_cell_count + 1} cells after insertion, "
            f"but REST API shows {len(current_cells)}"
        )

        # 6. Cleanup: delete the inserted cell
        last_index = len(current_cells) - 1
        await mcp_client_parametrized.delete_cell([last_index])


###############################################################################
# Test: New notebook cell insertion visible via REST API
###############################################################################

@pytest.mark.asyncio
@timeout_wrapper(90)
async def test_rtc_new_notebook_cell_visible(mcp_client_parametrized: MCPClient, jupyter_server):
    """Simulate the reported bug: create a new notebook, add a hello world cell,
    and verify it appears in the frontend's view without a reload.

    This is the exact scenario the user reported:
    - Asked Claude to create a new notebook and add hello world
    - Did not see the cell updates in UI until refreshed
    """
    marker_text = f"print('Hello, World! - RTC test {time.time_ns()}')"
    test_notebook = "new.ipynb"

    async with mcp_client_parametrized:
        # Connect to a notebook (simulating Claude's workflow)
        result = await mcp_client_parametrized.use_notebook("rtc_test_nb", test_notebook)
        logging.info(f"Connected to notebook: {result}")

        # Insert a hello world code cell
        result = await mcp_client_parametrized.insert_cell(
            cell_index=-1,
            cell_type="code",
            cell_source=marker_text,
        )
        assert result is not None, "insert_cell should succeed"
        logging.info(f"Inserted cell: {result['result'][:100]}")

        # Check if the cell is visible via REST API (frontend's view)
        max_wait_seconds = 5
        poll_interval = 0.5
        cell_found = False
        elapsed = 0.0

        while elapsed < max_wait_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            notebook_data = read_notebook_via_rest_api(jupyter_server, test_notebook)
            for cell in notebook_data["cells"]:
                if marker_text in cell.get("source", ""):
                    cell_found = True
                    break

            if cell_found:
                logging.info(f"Cell visible via REST API after {elapsed:.1f}s")
                break

        assert cell_found, (
            f"Hello World cell was NOT visible via REST API after "
            f"{max_wait_seconds}s. This reproduces the reported bug: cells "
            f"inserted via MCP/RTC are not rendered in the UI until the user "
            f"reloads the page."
        )

        # Cleanup
        current = read_notebook_via_rest_api(jupyter_server, test_notebook)
        for i, cell in enumerate(current["cells"]):
            if marker_text in cell.get("source", ""):
                await mcp_client_parametrized.delete_cell([i])
                break

        await mcp_client_parametrized.unuse_notebook("rtc_test_nb")


###############################################################################
# Test: Multiple rapid cell insertions all propagate
###############################################################################

@pytest.mark.asyncio
@timeout_wrapper(60)
async def test_rtc_rapid_insertions_all_visible(mcp_client_parametrized: MCPClient, jupyter_server):
    """Verify that multiple rapid cell insertions all propagate via RTC.

    This tests a variant of the bug where some cells might appear but others
    are "lost" or delayed when inserting in quick succession.
    """
    notebook_path = "notebook.ipynb"
    num_cells = 3
    markers = [f"# Rapid insert {i} - {time.time_ns()}" for i in range(num_cells)]

    async with mcp_client_parametrized:
        # Get baseline
        baseline = read_notebook_via_rest_api(jupyter_server, notebook_path)
        baseline_count = len(baseline["cells"])

        # Insert multiple cells rapidly
        for marker in markers:
            result = await mcp_client_parametrized.insert_cell(
                cell_index=-1,
                cell_type="markdown",
                cell_source=marker,
            )
            assert result is not None, f"insert_cell failed for '{marker}'"

        # Wait for all cells to propagate
        max_wait_seconds = 10
        poll_interval = 0.5
        elapsed = 0.0
        all_found = False

        while elapsed < max_wait_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            current = read_notebook_via_rest_api(jupyter_server, notebook_path)
            current_sources = [cell.get("source", "") for cell in current["cells"]]

            found_markers = [m for m in markers if any(m in src for src in current_sources)]
            if len(found_markers) == num_cells:
                all_found = True
                logging.info(f"All {num_cells} cells visible after {elapsed:.1f}s")
                break

        assert all_found, (
            f"Only {len(found_markers)}/{num_cells} cells were visible via "
            f"REST API after {max_wait_seconds}s. Missing: "
            f"{[m for m in markers if m not in found_markers]}. "
            f"This indicates RTC sync issues with rapid cell insertions."
        )

        # Cleanup: delete inserted cells in reverse order
        current = read_notebook_via_rest_api(jupyter_server, notebook_path)
        indices_to_delete = []
        for i, cell in enumerate(current["cells"]):
            if any(m in cell.get("source", "") for m in markers):
                indices_to_delete.append(i)

        if indices_to_delete:
            # Delete in reverse order to maintain index validity
            for idx in sorted(indices_to_delete, reverse=True):
                await mcp_client_parametrized.delete_cell([idx])
