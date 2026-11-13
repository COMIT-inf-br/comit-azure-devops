"""Caminhos locais do plugin por sistema operacional.

A config fica fora do repositório e fora de ${CLAUDE_PLUGIN_DATA}: assim o
Claude Code e o Cowork local leem a mesma config. Só variáveis de caminho são
lidas do ambiente (APPDATA, XDG_CONFIG_HOME, LOCALAPPDATA, ProgramFiles).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Optional

APP_NAME = "comit-azure-devops"

# server/adoplug/paths.py -> plugin root = parents[2]
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def system(platform: Optional[str] = None) -> str:
    """Devolve 'mac', 'windows' ou 'linux'."""
    p = platform or sys.platform
    if p == "darwin":
        return "mac"
    if p.startswith("win"):
        return "windows"
    return "linux"


def home_dir(home: Optional[os.PathLike] = None) -> Path:
    return Path(home) if home is not None else Path.home()


def config_dir(home: Optional[os.PathLike] = None, env: Optional[Mapping[str, str]] = None,
               platform: Optional[str] = None) -> Path:
    """Diretório da config local (seção 5.3 do plano)."""
    env = os.environ if env is None else env
    h = home_dir(home)
    s = system(platform)
    if s == "mac":
        return h / "Library" / "Application Support" / APP_NAME
    if s == "windows":
        base = env.get("APPDATA")
        return (Path(base) if base else h / "AppData" / "Roaming") / APP_NAME
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else h / ".config"
    return base / APP_NAME


def config_file(cdir: Optional[Path] = None, **kw) -> Path:
    return (cdir or config_dir(**kw)) / "config.json"


def learned_profiles_dir(cdir: Optional[Path] = None, **kw) -> Path:
    return (cdir or config_dir(**kw)) / "profiles"


def previews_dir(cdir: Optional[Path] = None, **kw) -> Path:
    return (cdir or config_dir(**kw)) / "previews"


def embedded_profiles_dir(plugin_root: Optional[Path] = None) -> Path:
    return (plugin_root or PLUGIN_ROOT) / "profiles"


def ensure_private_dir(path: Path) -> Path:
    """Cria a pasta com permissão 0700 (macOS/Linux) e devolve o caminho."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def write_private(path: Path, data: bytes) -> None:
    """Grava de forma atômica, com o arquivo criado em 0600 antes do conteúdo."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name != "nt":
        os.chmod(path, 0o600)
