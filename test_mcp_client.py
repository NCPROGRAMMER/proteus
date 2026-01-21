#!/usr/bin/env python3
"""
Unit tests for MCP client functionality.
Tests the client logic without requiring external network access.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from mcp_client import (
    MCPClientManager,
    SSEServerParameters,
    MCPTool,
    ToolCallParser,
    execute_tool_calls,
)


class TestSSEServerParameters(unittest.TestCase):
    """Test SSEServerParameters dataclass."""

    def test_basic_creation(self):
        params = SSEServerParameters(url="https://gitmcp.io/owner/repo")
        self.assertEqual(params.url, "https://gitmcp.io/owner/repo")
        self.assertEqual(params.headers, {})

    def test_with_headers(self):
        params = SSEServerParameters(
            url="https://gitmcp.io/owner/repo",
            headers={"Authorization": "Bearer token"}
        )
        self.assertEqual(params.headers["Authorization"], "Bearer token")


class TestMCPTool(unittest.TestCase):
    """Test MCPTool dataclass."""

    def test_creation(self):
        tool = MCPTool(
            server_name="test-server",
            name="fetch_docs",
            description="Fetches documentation",
            input_schema={"type": "object", "properties": {}}
        )
        self.assertEqual(tool.server_name, "test-server")
        self.assertEqual(tool.name, "fetch_docs")
        self.assertEqual(tool.description, "Fetches documentation")


class TestToolCallParser(unittest.TestCase):
    """Test ToolCallParser functionality."""

    def test_extract_single_tool_call(self):
        text = '''Some preamble text

### TOOL_CALL: fetch_docs
```json
{"repo": "anthropics/sdk"}
```

More text after'''

        calls = ToolCallParser.extract_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "fetch_docs")
        self.assertEqual(calls[0][1], {"repo": "anthropics/sdk"})

    def test_extract_multiple_tool_calls(self):
        text = '''
### TOOL_CALL: fetch_docs
```json
{"repo": "repo1"}
```

### TOOL_CALL: search_code
```json
{"query": "function"}
```
'''

        calls = ToolCallParser.extract_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "fetch_docs")
        self.assertEqual(calls[1][0], "search_code")

    def test_extract_no_tool_calls(self):
        text = "Just regular text without any tool calls"
        calls = ToolCallParser.extract_tool_calls(text)
        self.assertEqual(len(calls), 0)

    def test_extract_malformed_json(self):
        text = '''
### TOOL_CALL: bad_tool
```json
{not valid json}
```
'''
        calls = ToolCallParser.extract_tool_calls(text)
        self.assertEqual(len(calls), 0)  # Should skip malformed JSON

    def test_replace_tool_call_with_result(self):
        text = '''
### TOOL_CALL: fetch_docs
```json
{"repo": "test"}
```
'''
        result = ToolCallParser.replace_tool_call_with_result(
            text, "fetch_docs", {"repo": "test"}, "Documentation content here"
        )
        self.assertIn("### TOOL_RESULT: fetch_docs", result)
        self.assertIn("Documentation content here", result)
        self.assertNotIn("TOOL_CALL", result)


class TestMCPClientManager(unittest.TestCase):
    """Test MCPClientManager functionality."""

    def test_initialization(self):
        manager = MCPClientManager()
        self.assertEqual(manager.connections, {})
        self.assertEqual(manager._connection_tasks, {})

    def test_is_connected_false_by_default(self):
        manager = MCPClientManager()
        self.assertFalse(manager.is_connected("any-server"))

    def test_get_connected_servers_empty(self):
        manager = MCPClientManager()
        self.assertEqual(manager.get_connected_servers(), [])

    def test_get_all_tools_empty(self):
        manager = MCPClientManager()
        self.assertEqual(manager.get_all_tools(), [])

    def test_format_tools_for_prompt_empty(self):
        manager = MCPClientManager()
        result = manager.format_tools_for_prompt()
        self.assertEqual(result, "No tools available.")

    def test_format_tool_call_instructions(self):
        manager = MCPClientManager()
        instructions = manager.format_tool_call_instructions()
        self.assertIn("TOOL_CALL", instructions)
        self.assertIn("json", instructions)


class TestMCPClientManagerWithMockedConnection(unittest.TestCase):
    """Test MCPClientManager with mocked connections."""

    def setUp(self):
        self.manager = MCPClientManager()
        # Simulate a connected server with tools
        mock_connection = MagicMock()
        mock_connection.tools = [
            MCPTool(
                server_name="test-server",
                name="fetch_documentation",
                description="Fetches primary documentation from a repo",
                input_schema={"type": "object", "properties": {"repo": {"type": "string"}}}
            ),
            MCPTool(
                server_name="test-server",
                name="search_code",
                description="Searches code in the repository",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}}
            )
        ]
        mock_connection.session = AsyncMock()
        self.manager.connections["test-server"] = mock_connection

    def test_is_connected_true(self):
        self.assertTrue(self.manager.is_connected("test-server"))

    def test_get_connected_servers(self):
        servers = self.manager.get_connected_servers()
        self.assertEqual(servers, ["test-server"])

    def test_get_all_tools(self):
        tools = self.manager.get_all_tools()
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0].name, "fetch_documentation")
        self.assertEqual(tools[1].name, "search_code")

    def test_get_tools_for_server(self):
        tools = self.manager.get_tools_for_server("test-server")
        self.assertEqual(len(tools), 2)

    def test_get_tools_for_nonexistent_server(self):
        tools = self.manager.get_tools_for_server("nonexistent")
        self.assertEqual(tools, [])

    def test_format_tools_for_prompt_with_tools(self):
        result = self.manager.format_tools_for_prompt()
        self.assertIn("fetch_documentation", result)
        self.assertIn("search_code", result)
        self.assertIn("test-server", result)

    def test_call_tool(self):
        async def run_test():
            # Setup mock response
            mock_result = MagicMock()
            mock_result.content = [MagicMock(text="Mock documentation content")]
            self.manager.connections["test-server"].session.call_tool = AsyncMock(return_value=mock_result)

            result = await self.manager.call_tool("test-server", "fetch_documentation", {"repo": "test"})
            self.assertEqual(result.content[0].text, "Mock documentation content")

        asyncio.run(run_test())

    def test_call_tool_nonexistent_server(self):
        async def run_test():
            with self.assertRaises(ValueError) as context:
                await self.manager.call_tool("nonexistent", "some_tool", {})
            self.assertIn("Not connected", str(context.exception))

        asyncio.run(run_test())

    def test_call_tool_by_name(self):
        async def run_test():
            mock_result = MagicMock()
            mock_result.content = [MagicMock(text="Search results")]
            self.manager.connections["test-server"].session.call_tool = AsyncMock(return_value=mock_result)

            result = await self.manager.call_tool_by_name("search_code", {"query": "test"})
            self.assertEqual(result.content[0].text, "Search results")

        asyncio.run(run_test())

    def test_call_tool_by_name_not_found(self):
        async def run_test():
            with self.assertRaises(ValueError) as context:
                await self.manager.call_tool_by_name("nonexistent_tool", {})
            self.assertIn("not found", str(context.exception))

        asyncio.run(run_test())


class TestExecuteToolCalls(unittest.TestCase):
    """Test execute_tool_calls function."""

    def test_execute_tool_calls_no_calls(self):
        async def run_test():
            manager = MCPClientManager()
            text = "Just regular text"
            result = await execute_tool_calls(manager, text)
            self.assertEqual(result, text)

        asyncio.run(run_test())

    def test_execute_tool_calls_with_mock(self):
        async def run_test():
            manager = MCPClientManager()

            # Setup mock connection
            mock_connection = MagicMock()
            mock_connection.tools = [
                MCPTool("test", "fetch_docs", "Fetch docs", {})
            ]
            mock_result = MagicMock()
            mock_result.content = [MagicMock(text="Fetched documentation")]
            mock_connection.session = AsyncMock()
            mock_connection.session.call_tool = AsyncMock(return_value=mock_result)
            manager.connections["test"] = mock_connection

            text = '''
### TOOL_CALL: fetch_docs
```json
{}
```
'''
            result = await execute_tool_calls(manager, text)
            self.assertIn("TOOL_RESULT", result)
            self.assertIn("Fetched documentation", result)

        asyncio.run(run_test())


def run_tests():
    """Run all tests and return results."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Add all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestSSEServerParameters))
    suite.addTests(loader.loadTestsFromTestCase(TestMCPTool))
    suite.addTests(loader.loadTestsFromTestCase(TestToolCallParser))
    suite.addTests(loader.loadTestsFromTestCase(TestMCPClientManager))
    suite.addTests(loader.loadTestsFromTestCase(TestMCPClientManagerWithMockedConnection))
    suite.addTests(loader.loadTestsFromTestCase(TestExecuteToolCalls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result.wasSuccessful()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("MCP Client Unit Tests")
    print("=" * 60 + "\n")

    success = run_tests()

    print("\n" + "=" * 60)
    print(f"Overall result: {'ALL TESTS PASSED ✅' if success else 'SOME TESTS FAILED ❌'}")
    print("=" * 60)

    exit(0 if success else 1)
