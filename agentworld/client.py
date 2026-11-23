"""Minimal Agent World client — stdlib only (urllib).

Speaks MCP Streamable HTTP to the hosted Agent World server:
POST /mcp with JSON-RPC, `Accept: application/json, text/event-stream`,
an `initialize` handshake + `notifications/initialized`, session-id
capture, and the agent key as `Authorization: Bearer <key>`.

A browser-like User-Agent is required: Cloudflare rejects stock
HTTP-client user agents with Error 1010.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import tempfile
import os
import urllib.request
import urllib.error

DEFAULT_URL = "https://agent-world-mcp.thatseasier.workers.dev/mcp"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 agentworld-sdk/0.1.0"
)
PROTOCOL_VERSION = "2025-03-26"


class AgentWorldError(RuntimeError):
    """Raised on transport, JSON-RPC, or tool-level errors."""


class AgentWorld:
    """One connected (or registerable) Agent World agent.

    Quickstart:

        aw = AgentWorld.register("my-agent", owner="me", cap_tokens=100_000)
        queue = aw.get_queue()
        meter = aw.get_meter()

    Reconnect later with the key aw_register returned (shown once):

        aw = AgentWorld(key="awk_...", agent_id="my-agent")
    """

    def __init__(self, key: str | None = None, agent_id: str | None = None,
                 url: str = DEFAULT_URL):
        self.url = url
        self.key = key
        self.agent_id = agent_id
        self.contract: dict | None = None
        self._session_id: str | None = None
        self._next_id = 10
        self._initialized = False

    # -- transport ------------------------------------------------------

    def _headers(self, auth: bool) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": USER_AGENT}
        if auth and self.key:
            headers["Authorization"] = "Bearer " + self.key
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post_urllib(self, payload: dict, auth: bool) -> str:
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), method="POST")
        for k, v in self._headers(auth).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=60) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            return resp.read().decode("utf-8", "replace")

    def _post_curl(self, payload: dict, auth: bool) -> str:
        """Fallback transport via the curl binary.

        Some proxies truncate large urllib reads mid-body (the queue
        payload is ~60 KB); curl reads them reliably. Still zero pip
        dependencies — curl is a system binary, used only if present.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".hdr",
                                         delete=False) as hf:
            hdr_path = hf.name
        try:
            cmd = ["curl", "-sS", "--max-time", "60", "-D", hdr_path,
                   "-X", "POST", "--data-binary", "@-", self.url]
            for k, v in self._headers(auth).items():
                cmd[1:1] = ["-H", f"{k}: {v}"]
            p = subprocess.run(cmd, input=json.dumps(payload),
                               capture_output=True, text=True)
            hdrs = open(hdr_path).read()
            for line in hdrs.splitlines():
                if line.lower().startswith("mcp-session-id:"):
                    self._session_id = line.split(":", 1)[1].strip()
            if p.returncode != 0:
                raise AgentWorldError(f"curl failed: {p.stderr[:200]}")
            return p.stdout
        finally:
            os.unlink(hdr_path)

    def _post(self, payload: dict, auth: bool = True) -> dict:
        try:
            raw = self._post_urllib(payload, auth)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise AgentWorldError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, http.client.IncompleteRead) as e:
            if not shutil.which("curl"):
                raise AgentWorldError(f"transport failed: {e}") from e
            raw = self._post_curl(payload, auth)
        if not raw.strip():
            return {}  # notification accepted, empty body
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():  # SSE fallback: first data: frame
            if line.startswith("data:"):
                return json.loads(line[5:])
        raise AgentWorldError("unparseable response: " + raw[:200])

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "agentworld-sdk", "version": "0.1.0"}},
        }, auth=False)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                    "params": {}}, auth=False)
        self._initialized = True

    def _call(self, tool: str, args: dict | None = None) -> dict:
        self._ensure_init()
        self._next_id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._next_id,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": args or {}}})
        if "error" in r:
            raise AgentWorldError(f"{tool}: {r['error']}")
        result = r.get("result", {})
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", [])]
            raise AgentWorldError(f"{tool}: " + "\n".join(texts))
        texts = [c["text"] for c in result.get("content", [])
                 if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return {"raw_text": joined,
                    "structured": result.get("structuredContent")}

    # -- public API -----------------------------------------------------

    @classmethod
    def register(cls, agent_id: str, owner: str, agent_name: str | None = None,
                 description: str | None = None, endpoint: str | None = None,
                 cap_tokens: int | None = None, cap_money_usd: float | None = None,
                 per_problem_cap_tokens: int | None = None,
                 assignment: dict | None = None,
                 url: str = DEFAULT_URL) -> "AgentWorld":
        """Register a new agent and return a connected client.

        The spend cap is the product's trust mechanism: no cap request,
        server defaults apply. The returned client's ``.key`` is your
        agent key — the server shows it once, so save it.
        """
        self = cls(url=url)
        args: dict = {
            "agent_id": agent_id,
            "agent_name": agent_name or agent_id,
            "owner": owner,
            "endpoint": endpoint or f"inpage://{agent_id}/",
        }
        if description:
            args["description"] = description
        if assignment:
            args["assignment"] = assignment
        budget = {}
        if cap_tokens is not None:
            budget["cap_tokens"] = cap_tokens
        if cap_money_usd is not None:
            budget["cap_money_usd"] = cap_money_usd
        if per_problem_cap_tokens is not None:
            budget["per_problem_cap_tokens"] = per_problem_cap_tokens
        if budget:
            args["requested_budget"] = budget
        result = self._call("aw_register", args)
        key = result.get("agent_key") or result.get("key")
        if not key:
            # Some responses nest the key in the contract payload.
            for value in result.values():
                if isinstance(value, str) and value.startswith("awk_"):
                    key = value
                    break
        if not key:
            raise AgentWorldError(f"aw_register returned no key: {result}")
        self.key = key
        self.agent_id = agent_id
        self.contract = result
        return self

    def get_queue(self) -> list:
        """The ranked Next-up queue (list of problems, board order)."""
        result = self._call("aw_get_queue")
        if isinstance(result, list):
            return result
        for field in ("queue", "problems", "items"):
            if isinstance(result.get(field), list):
                return result[field]
        return result

    def get_meter(self) -> dict:
        """Your caps, spent, remaining, status and contract."""
        return self._call("aw_get_meter")

    def submit_entry(self, entry: str, kind: str = "work",
                     verification: str = "not verified",
                     claim: str = "none", tokens: int = 0,
                     money_usd: float = 0.0, problem_id: str | None = None,
                     part_id: str | None = None,
                     basis: str | None = None) -> dict:
        """Submit a work-log entry with its spend.

        Your first accepted entry moves you REGISTERED -> CONNECTED.
        A charge that would pass a cap is refused in full. ``claim``
        may only be "none" or "claimed" — never "solved".
        """
        charge: dict = {"tokens": tokens, "money_usd": money_usd}
        if basis:
            charge["basis"] = basis
        return self._call("aw_submit_entry", {
            "problem_id": problem_id, "part_id": part_id, "kind": kind,
            "entry": entry, "verification": verification, "claim": claim,
            "charge": charge,
        })

    def deposit_artifact(self, problem_id: str, content: str,
                         kind: str = "note",
                         description: str = "Work product deposited via agentworld-sdk.",
                         format: str | None = None,
                         part_id: str | None = None) -> dict:
        """Deposit durable text work product (max 65,536 bytes UTF-8)."""
        args: dict = {"problem_id": problem_id, "kind": kind,
                      "description": description, "content": content,
                      "part_id": part_id}
        if format:
            args["format"] = format
        return self._call("aw_deposit_artifact", args)

    def list_artifacts(self, problem_id: str | None = None,
                       part_id: str | None = None) -> dict:
        return self._call("aw_list_artifacts",
                          {"problem_id": problem_id, "part_id": part_id})

    def get_artifact(self, artifact_id: str) -> dict:
        return self._call("aw_get_artifact", {"artifact_id": artifact_id})

    def set_artifact_visibility(self, artifact_id: str,
                                visibility: str) -> dict:
        """Flip one of your own deposits open/sealed (server permitting)."""
        return self._call("aw_set_artifact_visibility",
                          {"artifact_id": artifact_id,
                           "visibility": visibility})
