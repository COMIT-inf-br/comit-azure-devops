"""Os mocks de evals/ vêm do servidor real no modo --mock. Se o servidor ou o fake mudar, regenere."""
import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "server"))


class EvalMocksTest(unittest.TestCase):
    def test_mocks_em_dia(self):
        spec = importlib.util.spec_from_file_location("gen_mocks", PLUGIN / "evals" / "gen_mocks.py")
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        wanted = gen.build()
        stale = [rel for rel, text in wanted.items()
                 if not (PLUGIN / "evals" / rel).is_file()
                 or (PLUGIN / "evals" / rel).read_text(encoding="utf-8") != text + "\n"]
        self.assertEqual(stale, [], "rode: uv run --python 3.12 --no-project evals/gen_mocks.py")

    def test_toda_ferramenta_tem_mock(self):
        from adoplug import tools
        have = {p.stem for p in (PLUGIN / "evals" / "mocks" / "ado").glob("ado_*.md")}
        self.assertEqual({t["name"] for t in tools.tool_list()} - have, set())


if __name__ == "__main__":
    unittest.main()
