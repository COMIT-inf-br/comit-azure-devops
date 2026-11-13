import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
import fakelib  # noqa: E402  (põe server/ no sys.path)
from adoplug import paths  # noqa: E402


class ConfigDirTest(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)

    def test_mac(self):
        d = paths.config_dir(home=self.h.home, env={}, platform="darwin")
        self.assertEqual(d, self.h.home / "Library" / "Application Support" / "comit-azure-devops")

    def test_windows_appdata(self):
        d = paths.config_dir(home=self.h.home, env={"APPDATA": str(self.h.root / "Roam")}, platform="win32")
        self.assertEqual(d, self.h.root / "Roam" / "comit-azure-devops")

    def test_windows_sem_appdata(self):
        d = paths.config_dir(home=self.h.home, env={}, platform="win32")
        self.assertEqual(d, self.h.home / "AppData" / "Roaming" / "comit-azure-devops")

    def test_linux_xdg(self):
        xdg = str(self.h.root / "xdg")
        d = paths.config_dir(home=self.h.home, env={"XDG_CONFIG_HOME": xdg}, platform="linux")
        self.assertEqual(d, Path(xdg) / "comit-azure-devops")

    def test_linux_xdg_relativo_ignorado(self):
        d = paths.config_dir(home=self.h.home, env={"XDG_CONFIG_HOME": "rel"}, platform="linux")
        self.assertEqual(d, self.h.home / ".config" / "comit-azure-devops")

    def test_subpastas(self):
        c = paths.config_dir(home=self.h.home, env={}, platform="linux")
        self.assertEqual(paths.config_file(c), c / "config.json")
        self.assertEqual(paths.learned_profiles_dir(c), c / "profiles")
        self.assertEqual(paths.previews_dir(c), c / "previews")

    def test_perfis_embutidos_no_plugin(self):
        self.assertEqual(paths.embedded_profiles_dir().name, "profiles")
        self.assertTrue((paths.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").is_file())

    def test_system(self):
        self.assertEqual(paths.system("darwin"), "mac")
        self.assertEqual(paths.system("win32"), "windows")
        self.assertEqual(paths.system("linux"), "linux")


class WritePrivateTest(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)

    def test_grava_atomico_sem_temporario(self):
        p = self.h.home / "a" / "b" / "config.json"
        paths.write_private(p, b"{}")
        paths.write_private(p, b'{"x": 1}')
        self.assertEqual(p.read_bytes(), b'{"x": 1}')
        self.assertEqual([f.name for f in p.parent.iterdir()], ["config.json"])

    @unittest.skipIf(os.name == "nt", "permissão POSIX")
    def test_permissoes(self):
        p = self.h.home / "cfg" / "config.json"
        paths.write_private(p, b"{}")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
