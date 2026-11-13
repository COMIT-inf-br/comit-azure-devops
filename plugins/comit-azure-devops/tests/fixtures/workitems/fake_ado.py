"""Azure DevOps falso em 127.0.0.1, com estado: work items, comentários, anexos e WIQL simples.

Imita o que o S4 depende do ADO real:
- item com `url` canônica `/<org>/<projeto>/_apis/wit/workItems/<id>`;
- `validateOnly=true` que confere obrigatórios e não grava;
- `test /rev` que recusa revisão velha;
- comentário só na rota com projeto, com `target="_blank"` devolvido sem aspas;
- anexo só na rota com GUID do projeto; a relação volta na forma do projeto, sem `?fileName`;
- falhas injetadas: conexão fechada antes ou depois do efeito, e 5xx depois do efeito.

GUIDs e e-mails são sintéticos. Nenhum pedido sai da máquina.
"""
import copy
import datetime as dt
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PROC = "0a0a0a0a-0000-4000-8000-00000000000a"
PROJ = "0c0c0c0c-0000-4000-8000-00000000000c"
PROJ2 = "0d0d0d0d-0000-4000-8000-00000000000d"
ME = "1a1a1a1a-0000-4000-8000-00000000001a"
ANA = "2b2b2b2b-0000-4000-8000-00000000002b"
ANA2 = "3c3c3c3c-0000-4000-8000-00000000003c"
TEAM = "0b0b0b0b-0000-4000-8000-00000000000b"
ME_UNIQUE = "eu@example.com"

STATES = {
    "Task": [("Aberto", "Proposed"), ("Em Andamento", "InProgress"), ("Concluído", "Completed"), ("Removido", "Removed")],
    "User Story": [("Aberto", "Proposed"), ("Em Andamento", "InProgress"), ("Concluído", "Completed"), ("Removido", "Removed")],
    "Epic": [("Aberto", "Proposed"), ("Em Andamento", "InProgress"), ("Suspenso", "InProgress"), ("Concluído", "Completed")],
    "Bug": [("Aberto", "Proposed"), ("Em Andamento", "InProgress"), ("Resolvido", "Resolved"), ("Concluído", "Completed")],
}
REQUIRED = {
    "Task": ["System.Title", "System.AreaPath", "System.IterationPath", "Custom.Equipe",
             "Microsoft.VSTS.Scheduling.OriginalEstimate"],
    "User Story": ["System.Title"],
    "Epic": ["System.Title"],
    "Bug": ["System.Title"],
}


def profile():
    """Perfil sintético da org de teste, no formato do S3."""
    def t(name, req, extra=None):
        d = {"reference_name": "Proc." + name.replace(" ", ""), "states": [{"name": n, "category": c} for n, c in STATES[name]],
             "required_on_create": req, "required_by_rule": [], "defaults": {}, "picklists": {}, "fields": {}}
        d.update(extra or {})
        return d
    return {
        "schema_version": 1, "org": "OrgA", "projects": [
            {"id": PROJ, "name": "Projeto Ágil", "process": PROC},
            {"id": PROJ2, "name": "Projeto Beta", "process": PROC}],
        "processes": {PROC: {"name": "Proc", "parent": "Agile", "work_item_types": {
            "Task": t("Task", ["Custom.Equipe", "System.AreaPath", "System.IterationPath", "System.Title"], {
                "required_by_rule": [{"fields": ["Microsoft.VSTS.Scheduling.OriginalEstimate"],
                                      "conditions": [{"type": "create"}], "when": "create", "source": "rule:x"}],
                "defaults": {"Microsoft.VSTS.Common.Priority": 2},
                "picklists": {"Custom.Equipe": ["DADOS", "QA"], "Microsoft.VSTS.Common.Priority": [1, 2, 3, 4]},
                "fields": {"Microsoft.VSTS.Common.Priority": {"name": "Priority", "type": "integer"},
                           "Microsoft.VSTS.Scheduling.OriginalEstimate": {"name": "Original Estimate", "type": "double"},
                           "Microsoft.VSTS.Scheduling.RemainingWork": {"name": "Remaining Work", "type": "double"},
                           "Custom.Equipe": {"name": "Equipe", "type": "string"}}}),
            "User Story": t("User Story", ["System.AreaPath", "System.IterationPath", "System.Title"]),
            "Epic": t("Epic", ["System.AreaPath", "System.IterationPath", "System.Title"]),
            "Bug": t("Bug", ["System.AreaPath", "System.IterationPath", "System.Title"]),
        }}},
    }


class FakeAuth:
    """Credencial falsa com um sentinela, para varrer vazamento."""

    def __init__(self, token):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))
        from adoplug.auth import Credential
        from adoplug.vault import Secret
        self.secret = Secret(token)
        self._cred = Credential("az", "az", self.secret)

    def get_credential(self, org):
        return self._cred

    def invalidate(self, org):
        pass

    def known_secrets(self):
        return (self.secret,)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def ado_html(text):
    """Normalizações do ADO ao gravar comentário."""
    return text.replace('target="_blank"', "target=_blank")


class FakeAdo:
    def __init__(self, org="OrgA"):
        self.org = org
        self.lock = threading.Lock()
        self.requests = []
        self.faults = []  # [{"method", "path" (regex), "action"}]; consumidas na ordem
        self.projects = {PROJ: "Projeto Ágil", PROJ2: "Projeto Beta"}
        self.items = {}
        self.comments = {}
        self.attachments = {}
        self.next_id = 1000
        self.next_comment = 500
        self.comment_page = 2
        self.members = [
            {"identity": {"displayName": "Ana Souza", "uniqueName": "ana@example.com", "id": ANA}},
            {"identity": {"displayName": "Ana Lima", "uniqueName": "ana.lima@example.com", "id": ANA2}},
            {"identity": {"displayName": "Eu Mesmo", "uniqueName": ME_UNIQUE, "id": ME}},
        ]
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                u = urlsplit(self.path)
                query = dict(parse_qsl(u.query, keep_blank_values=True))
                req = {"method": self.command, "path": u.path, "raw_path": self.path, "query": query,
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                outer.requests.append(req)
                fault = outer._take_fault(self.command, u.path)
                if fault == "drop_before":
                    self.close_connection = True
                    return
                if fault == "400_before":
                    status, data = 400, {"message": "Recusado pelo teste.", "typeKey": "TestException"}
                else:
                    status, data = outer.dispatch(self.command, u.path, query, body, req)
                if fault == "drop_after":
                    self.close_connection = True
                    return
                if fault == "500_after":
                    status, data = 503, {"message": "Serviço indisponível."}
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # --- apoio dos testes ---------------------------------------------------------------

    def fault(self, method, path_regex, action):
        self.faults.append({"method": method, "path": path_regex, "action": action})

    def _take_fault(self, method, path):
        with self.lock:
            for f in self.faults:
                if f["method"] == method and re.search(f["path"], path):
                    self.faults.remove(f)
                    return f["action"]
        return None

    def writes(self):
        """Pedidos que alteram estado (fora WIQL e validateOnly)."""
        return [r for r in self.requests if r["method"] in ("POST", "PATCH") and not r["path"].endswith("/wiql")
                and r["query"].get("validateOnly") != "true"]

    def item_url(self, pid, i):
        return f"https://dev.azure.com/{self.org}/{pid}/_apis/wit/workItems/{i}"

    def add_item(self, wtype="Task", title="Item", pid=PROJ, state=None, **fields):
        with self.lock:
            self.next_id += 1
            i = self.next_id
        f = {"System.Id": i, "System.WorkItemType": wtype, "System.Title": title, "System.TeamProject": self.projects[pid],
             "System.State": state or STATES[wtype][0][0], "System.AreaPath": self.projects[pid],
             "System.IterationPath": self.projects[pid], "System.CreatedDate": now_iso(),
             "System.CreatedBy": {"displayName": "Eu Mesmo", "uniqueName": ME_UNIQUE, "id": ME}}
        f.update(fields)
        self.items[i] = {"id": i, "rev": 1, "pid": pid, "fields": f, "relations": []}
        return i

    def link_parent(self, child, parent):
        c, p = self.items[child], self.items[parent]
        c["relations"].append({"rel": "System.LinkTypes.Hierarchy-Reverse", "url": self.item_url(p["pid"], parent), "attributes": {}})
        c["fields"]["System.Parent"] = parent
        p["relations"].append({"rel": "System.LinkTypes.Hierarchy-Forward", "url": self.item_url(c["pid"], child), "attributes": {}})

    # --- roteamento -----------------------------------------------------------------------

    def dispatch(self, method, path, q, body, req):
        prefix = "/" + self.org
        if not path.lower().startswith(prefix.lower() + "/"):
            return 404, {"message": "org"}
        p = unquote(path[len(prefix):])
        j = json.loads(body.decode("utf-8")) if body and "json" in req["headers"].get("content-type", "") else None
        m = re.fullmatch(r"/_apis/projects/([^/]+)", p)
        if m and method == "GET":
            for pid, name in self.projects.items():
                if m.group(1).casefold() in (pid, name.casefold()):
                    return 200, {"id": pid, "name": name}
            return 404, {"message": "projeto"}
        if p == "/_apis/connectionData":
            return 200, {"authenticatedUser": {"id": ME, "providerDisplayName": "Eu Mesmo",
                                               "properties": {"Account": {"$value": ME_UNIQUE}}}}
        if p == "/_apis/work/processes":
            return 200, {"value": [{"typeId": PROC, "name": "Proc", "projects": [
                {"id": k, "name": v} for k, v in self.projects.items()]}]}
        m = re.fullmatch(r"/_apis/projects/(" + GUID + r")/teams", p)
        if m:
            return 200, {"value": [{"id": TEAM, "name": "Time Ágil"}]}
        m = re.fullmatch(r"/_apis/projects/(" + GUID + r")/teams/(" + GUID + r")/members", p)
        if m:
            return 200, {"value": self.members}
        m = re.fullmatch(r"/(" + GUID + r")/(" + GUID + r")/_apis/work/teamsettings/iterations", p)
        if m:
            its = [{"id": "s1", "name": "Sprint 1", "path": "P\\Sprint 1", "attributes": {"timeFrame": "past"}},
                   {"id": "s2", "name": "Sprint 2", "path": "P\\Sprint 2", "attributes": {"timeFrame": "current"}},
                   {"id": "s3", "name": "Sprint 3", "path": "P\\Sprint 3", "attributes": {"timeFrame": "future"}}]
            if q.get("$timeframe") == "current":
                its = [i for i in its if i["attributes"]["timeFrame"] == "current"]
            return 200, {"value": its}
        if p == "/_apis/identities":
            v = q.get("filterValue", "").casefold()
            return 200, {"value": [{"id": m["identity"]["id"], "providerDisplayName": m["identity"]["displayName"],
                                    "properties": {"Account": {"$value": m["identity"]["uniqueName"]}}}
                                   for m in self.members if v in m["identity"]["displayName"].casefold()]}
        m = re.fullmatch(r"/(" + GUID + r")/_apis/wit/workitemtypes/([^/]+)/states", p)
        if m:
            return 200, {"value": [{"name": n, "category": c} for n, c in STATES.get(m.group(2), [])]}
        m = re.fullmatch(r"/(" + GUID + r")/_apis/wit/workitemtypes", p)
        if m:
            return 200, {"value": [{"name": t, "states": [{"name": n, "category": c} for n, c in s]} for t, s in STATES.items()]}
        if p == "/_apis/wit/workitems" and method == "GET":
            ids = [int(x) for x in q.get("ids", "").split(",") if x]
            fl = q.get("fields", "").split(",")
            return 200, {"value": [self._view(i, fields=fl) for i in ids if i in self.items]}
        m = re.fullmatch(r"/_apis/wit/workitems/(\d+)", p)
        if m and method == "GET":
            i = int(m.group(1))
            if i not in self.items:
                return 404, {"message": f"TF401232: Work item {i} does not exist."}
            fl = q.get("fields", "").split(",") if q.get("fields") else None
            return 200, self._view(i, fields=fl, relations=q.get("$expand") in ("relations", "all"))
        m = re.fullmatch(r"(?:/(" + GUID + r"))?/_apis/wit/wiql", p)
        if m and method == "POST":
            return self._wiql(m.group(1), j["query"], int(q.get("$top", 50)))
        m = re.fullmatch(r"/([^/]+)/_apis/wit/workitems/\$(.+)", p)
        if m and method == "POST":
            if not re.fullmatch(GUID, m.group(1)):
                return 404, {"message": "projeto pelo nome"}
            return self._create(m.group(1), m.group(2), j, q.get("validateOnly") == "true")
        m = re.fullmatch(r"/(" + GUID + r")/_apis/wit/workitems/(\d+)", p)
        if m and method == "PATCH":
            return self._patch(int(m.group(2)), j, q.get("validateOnly") == "true")
        m = re.fullmatch(r"/(" + GUID + r")/_apis/wit/workItems/(\d+)/comments(?:/(\d+))?", p)
        if m:
            if q.get("api-version") != "7.1-preview.4":
                return 400, {"message": "api-version"}
            return self._comments(method, int(m.group(2)), m.group(3), j, q)
        if re.fullmatch(r"/_apis/wit/workItems/(\d+)/comments", p):
            return 404, {"message": "comentário sem projeto"}
        m = re.fullmatch(r"/([^/]+)/_apis/wit/attachments", p)
        if m and method == "POST":
            if not re.fullmatch(GUID, m.group(1)):
                return 404, {"message": "projeto pelo nome"}
            g = str(uuid.UUID(int=0x5000 + len(self.attachments) + 1))
            self.attachments[g] = {"name": q.get("fileName"), "data": body, "pid": m.group(1)}
            return 201, {"id": g, "url": f"https://dev.azure.com/{self.org}/_apis/wit/attachments/{g}?fileName={q.get('fileName')}"}
        return 404, {"message": f"rota desconhecida: {method} {p}"}

    def _view(self, i, fields=None, relations=False):
        it = self.items[i]
        f = it["fields"] if not fields else {k: v for k, v in it["fields"].items() if k in fields}
        out = {"id": i, "rev": it["rev"], "fields": copy.deepcopy(f), "url": self.item_url(it["pid"], i)}
        if relations:
            out["relations"] = copy.deepcopy(it["relations"])
        return out

    def _apply_ops(self, it, ops):
        rels_url = []
        for o in ops:
            path = o["path"]
            if o["op"] == "test" and path == "/rev":
                if o["value"] != it["rev"]:
                    return 400, {"message": "TF26071: This work item has been changed by someone else since you opened it.",
                                 "typeKey": "WorkItemRevisionMismatchException"}
            elif o["op"] == "add" and path.startswith("/fields/"):
                it["fields"][path[8:]] = o["value"]
            elif o["op"] == "remove" and path.startswith("/fields/"):
                it["fields"].pop(path[8:], None)
            elif o["op"] == "add" and path == "/relations/-":
                v = copy.deepcopy(o["value"])
                if v["rel"] == "AttachedFile":
                    g = re.search(r"/attachments/(" + GUID + ")", v["url"]).group(1)
                    a = self.attachments.get(g)
                    if not a:
                        return 400, {"message": "anexo inexistente"}
                    v["url"] = f"https://dev.azure.com/{self.org}/{a['pid']}/_apis/wit/attachments/{g}"
                    v.setdefault("attributes", {}).update({"name": a["name"], "resourceSize": len(a["data"]),
                                                           "authorizedDate": now_iso()})
                if v["rel"] == "System.LinkTypes.Hierarchy-Reverse":
                    pid_ = int(v["url"].rsplit("/", 1)[1])
                    if pid_ not in self.items:
                        return 400, {"message": "parent inexistente"}
                    if any(r["rel"] == v["rel"] for r in it["relations"]):
                        return 400, {"message": "TF201036: já tem parent"}
                    it["fields"]["System.Parent"] = pid_
                    v["url"] = self.item_url(self.items[pid_]["pid"], pid_)
                    rels_url.append(pid_)
                v.setdefault("attributes", {})
                it["relations"].append(v)
            elif o["op"] == "remove" and path.startswith("/relations/"):
                r = it["relations"].pop(int(path.split("/")[2]))
                if r["rel"] == "System.LinkTypes.Hierarchy-Reverse":
                    it["fields"].pop("System.Parent", None)
            else:
                return 400, {"message": f"op inválida {o}"}
        return None, rels_url

    def _create(self, pid, wtype, ops, validate_only):
        if not isinstance(ops, list):
            return 400, {"message": "corpo"}
        it = {"id": 0, "rev": 0, "pid": pid, "fields": {}, "relations": []}
        err, parents = self._apply_ops(it, ops)
        if err:
            return err, parents
        for ref in REQUIRED.get(wtype, []):
            if it["fields"].get(ref) in (None, ""):
                return 400, {"message": f"TF401320: Rule Error for field {ref}. Error code: Required, InvalidEmpty.",
                             "typeKey": "RuleValidationException", "errorCode": 0}
        if validate_only:
            return 200, {"id": 0, "rev": 0, "fields": it["fields"]}
        with self.lock:
            self.next_id += 1
            i = self.next_id
        it["fields"].update({"System.Id": i, "System.WorkItemType": wtype, "System.TeamProject": self.projects[pid],
                             "System.State": it["fields"].get("System.State") or STATES[wtype][0][0],
                             "System.CreatedDate": now_iso(),
                             "System.CreatedBy": {"displayName": "Eu Mesmo", "uniqueName": ME_UNIQUE, "id": ME}})
        it.update({"id": i, "rev": 1})
        self.items[i] = it
        for p in parents:
            self.items[p]["relations"].append({"rel": "System.LinkTypes.Hierarchy-Forward",
                                               "url": self.item_url(pid, i), "attributes": {}})
        return 200, self._view(i, relations=True)

    def _patch(self, i, ops, validate_only):
        if i not in self.items:
            return 404, {"message": "item"}
        it = copy.deepcopy(self.items[i])
        err, _ = self._apply_ops(it, ops)
        if err:
            return err, _
        if validate_only:
            return 200, {"id": i, "rev": it["rev"]}
        it["rev"] += 1
        self.items[i] = it
        return 200, self._view(i, relations=True)

    def _comments(self, method, i, cid, j, q):
        lst = self.comments.setdefault(i, [])
        if method == "POST":
            self.next_comment += 1
            c = {"id": self.next_comment, "text": ado_html(j["text"]), "createdDate": now_iso(), "version": 1,
                 "createdBy": {"displayName": "Eu Mesmo", "uniqueName": ME_UNIQUE, "id": ME}}
            lst.append(c)
            return 200, c
        if cid:
            for c in lst:
                if c["id"] == int(cid):
                    return 200, c
            return 404, {"message": "comentário"}
        start = int(q.get("continuationToken") or 0)
        page = lst[start:start + self.comment_page]
        out = {"comments": page, "count": len(page)}
        if start + self.comment_page < len(lst):
            out["continuationToken"] = str(start + self.comment_page)
        return 200, out

    def _wiql(self, pid, query, top):
        m = re.search(r"WHERE (.*?)(?: ORDER BY .*)?$", query)
        conds = [c.strip() for c in m.group(1).split(" AND ")] if m else []
        out = []
        for i, it in self.items.items():
            if all(self._cond(c, it, pid) for c in conds):
                out.append({"id": i})
        return 200, {"workItems": out[:top]}

    def _cond(self, c, it, pid):
        f = it["fields"]
        m = re.fullmatch(r"\[([^\]]+)\] (=|>=|>|IN) (.+)", c)
        if not m:
            raise ValueError("WIQL não suportada no fake: " + c)
        ref, op, rhs = m.groups()
        if rhs == "@project":
            return it["pid"] == pid
        if rhs == "@Me":
            v = f.get(ref)
            return isinstance(v, dict) and v.get("id") == ME
        if rhs.startswith("@Today"):
            return True
        if op == "IN":
            vals = [x.replace("''", "'") for x in re.findall(r"'((?:[^']|'')*)'", rhs)]
            return f.get(ref) in vals
        if rhs.startswith("'"):
            return str(f.get(ref)) == rhs[1:-1].replace("''", "'")
        if op == ">":
            return int(f.get(ref) or 0) > int(rhs)
        return f.get(ref) == int(rhs)
