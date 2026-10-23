"""Loader path regressions: no Abaqus imports or real files."""
import ast
import contextlib
import io
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

class LoaderPathTests(unittest.TestCase):
    def check_loader(self, filename, function_name):
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        home = str(Path("D:/offline-bridge-fixture").resolve())
        main = types.ModuleType("__main__")
        main.expected_root = home
        main.sys = sys
        main.loaded = False
        namespace = {"os": os, "sys": sys, "traceback": __import__("traceback")}
        source = "loaded = expected_root in sys.path"
        namespace["open"] = lambda *args, **kwargs: io.StringIO(source)
        exec(compile(ast.Module(body=functions, type_ignores=[]), filename, "exec"), namespace)
        with mock.patch.dict(os.environ, {"ABAQUS_MCP_HOME": home}), \
             mock.patch.dict(sys.modules, {"__main__": main}), \
             mock.patch.object(sys, "path", ["EXISTING_PATH"]), \
             mock.patch.object(os.path, "exists", return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            namespace[function_name]()
            self.assertTrue(main.loaded, "Resolved bridge home must be importable for bridge_runtime")
            self.assertIn("EXISTING_PATH", sys.path)

    def test_menu_loader_adds_resolved_bridge_home(self):
        self.check_loader("abaqus_plugins/mcp_control/mcp_control_plugin.py", "_load_main_plugin")

    def test_startup_hook_adds_resolved_bridge_home(self):
        self.check_loader("abaqus_v6.env.example", "onCaeStartup")

if __name__ == "__main__":
    unittest.main()
