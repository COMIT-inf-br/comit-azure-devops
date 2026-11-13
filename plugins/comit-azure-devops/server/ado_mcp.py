"""Servidor MCP stdio do plugin. Stub do S1: só a ferramenta `hello`."""
import json
import os
import shutil
import sys

INSTRUCTIONS = (
    "comit-azure-devops: operações de work items do Azure DevOps.\n"
    "Antes de qualquer operação, chame ado_status."
)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def hello():
    # Nunca devolva os.environ: o ambiente pode ter credenciais.
    info = {
        "executable": sys.executable,
        "python": sys.version.split()[0],
        "script": os.path.abspath(__file__),
        "uv_on_path": shutil.which("uv"),
    }
    return {"content": [{"type": "text", "text": json.dumps(info)}]}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        params = msg.get("params") or {}
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "comit-azure-devops", "version": "0.0.1"},
            "instructions": INSTRUCTIONS,
        }})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
            "name": "hello",
            "description": "Diagnóstico do runtime do servidor (stub do S1).",
            "inputSchema": {"type": "object", "properties": {}},
        }]}})
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        if name == "hello":
            send({"jsonrpc": "2.0", "id": mid, "result": hello()})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32602, "message": "ferramenta desconhecida"}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32601, "message": "método não suportado"}})
    # notificações (sem id), como notifications/initialized: sem resposta


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            log(f"erro: {type(exc).__name__}")


if __name__ == "__main__":
    main()
