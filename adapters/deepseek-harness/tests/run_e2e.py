from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
ALTM_ROOT = Path(
    os.environ.get("ALTM_ROOT", str(Path(__file__).resolve().parents[3]))
).resolve()
DSH_REPO = Path(
    os.environ.get("DSH_REPO", str(ALTM_ROOT.parent / "deepseek-harness"))
).resolve()
CONFIG_PATH = ADAPTER_ROOT / "tests" / "fixtures" / "cordis.yml"
DRIVER_PATH = ADAPTER_ROOT / "tests" / "driver.mjs"
PROFILE = "altm-e2e"


def main() -> int:
    node_bin = _node_bin()
    environment = dict(os.environ)
    environment["PATH"] = "%s%s%s" % (
        node_bin,
        os.pathsep,
        environment.get("PATH", ""),
    )
    environment["DSH_REPO"] = str(DSH_REPO)
    environment["TSX_TSCONFIG_PATH"] = str(DSH_REPO / "tsconfig.json")
    _require_checkout(environment)

    with tempfile.TemporaryDirectory(prefix="altm-dsh-e2e-") as tmp:
        root = Path(tmp)
        corepack_bin = root / "corepack-bin"
        corepack_bin.mkdir()
        isolated_environment = {
            **environment,
            "PNPM_HOME": str(corepack_bin),
            "XDG_STATE_HOME": str(root / "xdg-state"),
        }
        _run(
            ["corepack", "enable", "--install-directory", str(corepack_bin)],
            cwd=DSH_REPO,
            env=isolated_environment,
            timeout=30,
        )
        dsh_home = root / "dsh-home"
        profile_manifest = dsh_home / "profiles" / PROFILE / "package.json"
        package_path = _pack_adapter(root, isolated_environment)
        profile_environment = {
            **isolated_environment,
            "DSH_HOME": str(dsh_home),
            "PATH": "%s%s%s" % (
                corepack_bin,
                os.pathsep,
                isolated_environment["PATH"],
            ),
        }
        _run(
            [
                "corepack",
                "pnpm",
                "dsh",
                "plugin",
                "--profile",
                PROFILE,
                "add",
                str(package_path),
            ],
            cwd=DSH_REPO,
            env=profile_environment,
            timeout=240,
        )
        dump = _run(
            [
                "corepack",
                "pnpm",
                "dsh",
                "--profile",
                PROFILE,
                "--dump-config",
            ],
            cwd=DSH_REPO,
            env=profile_environment,
            timeout=120,
        ).stdout
        _require("@altm/deepseek-harness" in dump, "bundle is absent from profile dump")
        _require("id: altm-memory" in dump, "ALTM row is absent from profile dump")

        port = _free_port()
        endpoint = "http://127.0.0.1:%s/mcp" % port
        api_key = secrets.token_urlsafe(32)
        db_path = root / "altm.sqlite3"
        server_log_path = root / "altm-server.log"
        server_environment = {
            **environment,
            "ALTM_MCP_API_KEY_SHA256": hashlib.sha256(
                api_key.encode("utf-8")
            ).hexdigest(),
            "ALTM_MCP_RESOURCE_SERVER_URL": endpoint,
        }
        with server_log_path.open("w+", encoding="utf-8") as server_log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "altm.cli",
                    "mcp-server",
                    "--db",
                    str(db_path),
                    "--transport",
                    "streamable-http",
                    "--profile",
                    "runtime",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ALTM_ROOT,
                env=server_environment,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                _wait_for_server(server, port, server_log)
                _assert_invalid_auth(endpoint)
                common = {
                    **profile_environment,
                    "ALTM_MCP_ENDPOINT": endpoint,
                    "ALTM_MCP_API_KEY": api_key,
                }
                remembered = _run_session(
                    root,
                    profile_manifest,
                    common,
                    session_id="session-memory",
                    user_id="user-a",
                    tasks=[
                        '{"release_codename":"cobalt"}',
                        "What is release_codename?",
                    ],
                )
                isolated = _run_session(
                    root,
                    profile_manifest,
                    common,
                    session_id="session-isolated",
                    user_id="user-b",
                    tasks=["What is release_codename?"],
                )
                missing_key_environment = dict(common)
                missing_key_environment.pop("ALTM_MCP_API_KEY", None)
                fail_open = _run_session(
                    root,
                    profile_manifest,
                    missing_key_environment,
                    session_id="session-no-key",
                    user_id="user-a",
                    tasks=['{"independent":"turn"}'],
                )
            finally:
                _stop_server(server)

        database = _inspect_database(db_path)
        logs = _inspect_session_logs(root / ".sessions")
        stored, recalled = remembered["outputs"]
        _require(stored == "Stored the release codename.", "store turn failed")
        _require(
            "release codename is cobalt" in recalled,
            "recall did not affect reply: remembered=%r database=%r logs=%r"
            % (remembered, database, logs),
        )
        _require("memory://" in recalled, "recall reply did not cite a marker")
        _require(
            isolated["outputs"] == ["Memory not found for this scope."],
            "cross-user memory leaked into an isolated scope",
        )
        _require(
            fail_open["outputs"] == ["Stored the release codename."],
            "missing ALTM credential blocked the Harness turn",
        )
        _require(database["cycle_count"] == 3, "unexpected ALTM runtime cycle count")
        _require(database["committed_count"] == 3, "not every prepared cycle committed")
        _require(database["recall_citation_count"] >= 1, "citation feedback was not stored")
        _require(database["isolated_citation_count"] == 0, "isolated turn cited foreign memory")
        _require(database["l0_count"] >= 6, "user and assistant L0 records are incomplete")
        _require(database["cited_signal_count"] >= 1, "cited_by_agent signal is absent")
        _require(logs["session_count"] == 3, "Harness session logs are incomplete")
        _require(logs["recall_context_count"] >= 1, "recalled context is absent from Harness log")
        _require(logs["recall_context_has_cobalt"], "durable recalled context lost its value")
        _require(
            not logs["isolated_context_has_cobalt"],
            "isolated Harness log contains another user's memory",
        )

        print(
            json.dumps(
                {
                    "status": "passed",
                    "bundle_install": True,
                    "invalid_auth_rejected": True,
                    "same_session_recall": True,
                    "citation_feedback": True,
                    "scope_isolation": True,
                    "missing_credential_fail_open": True,
                    "database": database,
                    "harness_logs": logs,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return 0


def _node_bin() -> Path:
    configured = os.environ.get("DSH_NODE_BIN")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / ".local" / "share" / "node-current" / "bin",
        Path("/opt/homebrew/bin"),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        node = candidate / "node"
        corepack = candidate / "corepack"
        if not node.is_file() or not corepack.is_file():
            continue
        version = subprocess.run(
            [str(node), "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        major, minor, *_ = [int(part) for part in version.lstrip("v").split(".")]
        if major >= 24 or (major == 22 and minor >= 19):
            return candidate
    raise RuntimeError(
        "DeepSeek Harness requires Node ^22.19 or >=24; set DSH_NODE_BIN "
        "to a compatible bin directory"
    )


def _require_checkout(environment: dict[str, str]) -> None:
    _require((ALTM_ROOT / "src" / "altm").is_dir(), "ALTM_ROOT is not an ALTM checkout")
    _require((DSH_REPO / "AGENTS.md").is_file(), "DSH_REPO is not a Harness checkout")
    _require(
        (DSH_REPO / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs").is_file(),
        "DeepSeek Harness dependencies are not installed",
    )
    version = _run(
        ["corepack", "pnpm", "--version"],
        cwd=DSH_REPO,
        env=environment,
        timeout=30,
    ).stdout.strip()
    _require(bool(version), "pnpm is unavailable")


def _pack_adapter(destination: Path, environment: dict[str, str]) -> Path:
    result = _run(
        ["npm", "pack", "--pack-destination", str(destination)],
        cwd=ADAPTER_ROOT,
        env=environment,
        timeout=180,
    )
    filename = result.stdout.strip().splitlines()[-1]
    package_path = destination / filename
    _require(package_path.is_file(), "npm pack did not create the adapter tarball")
    return package_path


def _run_session(
    cwd: Path,
    profile_manifest: Path,
    environment: dict[str, str],
    *,
    session_id: str,
    user_id: str,
    tasks: list[str],
) -> dict[str, object]:
    result = _run(
        [
            "node",
            "--import",
            str(DSH_REPO / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"),
            str(DRIVER_PATH),
            str(CONFIG_PATH),
            str(profile_manifest),
            json.dumps(tasks),
        ],
        cwd=cwd,
        env={
            **environment,
            "DSH_ALTM_SESSION_ID": session_id,
            "DSH_ALTM_USER_ID": user_id,
        },
        timeout=90,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    _require(bool(lines), "Harness driver produced no result")
    payload = json.loads(lines[-1])
    return {
        "outputs": [str(result["output"]) for result in payload["results"]],
        "stderr": result.stderr,
        "context": _session_recall_context(cwd / ".sessions", session_id),
    }


def _session_recall_context(root: Path, session_id: str) -> str:
    for path in root.rglob("*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        header = json.loads(lines[0])
        if header.get("id") != session_id:
            continue
        contexts = []
        for line in lines[1:]:
            event = json.loads(line)
            if event.get("type") != "user/message":
                continue
            data = event.get("data", {})
            source = data.get("source", {})
            if source.get("plugin") != "altm-memory":
                continue
            contexts.extend(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
        return "\n".join(contexts)
    return ""


def _inspect_database(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        cycles = connection.execute(
            """
            SELECT session_id, turn_id, status, cited_memory_ids_json
            FROM runtime_cycles
            ORDER BY created_at, session_id, turn_id
            """
        ).fetchall()
        l0_count = connection.execute(
            "SELECT COUNT(*) FROM memory_units WHERE layer = 'L0'"
        ).fetchone()[0]
        cited_signal_count = connection.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE signal = 'cited_by_agent'"
        ).fetchone()[0]
    by_turn = {
        (row["session_id"], row["turn_id"]): row
        for row in cycles
    }
    recall_row = by_turn.get(("session-memory", "2"))
    isolated_row = by_turn.get(("session-isolated", "1"))
    recall_citations = (
        json.loads(recall_row["cited_memory_ids_json"])
        if recall_row is not None
        else []
    )
    isolated_citations = (
        json.loads(isolated_row["cited_memory_ids_json"])
        if isolated_row is not None
        else []
    )
    return {
        "cycle_keys": [
            "%s/%s" % (row["session_id"], row["turn_id"])
            for row in cycles
        ],
        "cycle_count": len(cycles),
        "committed_count": sum(row["status"] == "committed" for row in cycles),
        "recall_citation_count": len(recall_citations),
        "isolated_citation_count": len(isolated_citations),
        "l0_count": int(l0_count),
        "cited_signal_count": int(cited_signal_count),
    }


def _inspect_session_logs(root: Path) -> dict[str, object]:
    files = sorted(root.rglob("*.jsonl"))
    contexts: dict[str, list[str]] = {}
    step_positions: dict[str, list[str]] = {}
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        session_id = str(json.loads(lines[0]).get("id", ""))
        for line in lines[1:]:
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") == "step/start":
                data = event.get("data", {})
                step_positions.setdefault(session_id, []).append(
                    "%s/%s" % (data.get("turn"), data.get("step"))
                )
            if event.get("type") != "user/message":
                continue
            data = event.get("data", {})
            source = data.get("source", {})
            if source.get("plugin") != "altm-memory":
                continue
            text = "\n".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            contexts.setdefault(session_id, []).append(text)
    recall_values = contexts.get("session-memory", [])
    isolated_values = contexts.get("session-isolated", [])
    recall = "\n".join(recall_values)
    isolated = "\n".join(isolated_values)
    return {
        "session_count": len(files),
        "step_positions": step_positions,
        "recall_context_count": len(recall_values),
        "recall_context_has_cobalt": "cobalt" in recall,
        "isolated_context_has_cobalt": "cobalt" in isolated,
    }


def _assert_invalid_auth(endpoint: str) -> None:
    request = urllib.request.Request(
        endpoint,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": "Bearer invalid",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        _require(error.code == 401, "invalid MCP key did not return HTTP 401")
        return
    raise AssertionError("invalid MCP key was accepted")


def _wait_for_server(
    process: subprocess.Popen[str],
    port: int,
    log,
) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.seek(0)
            raise RuntimeError("ALTM MCP server exited:\n%s" % log.read())
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("ALTM MCP server did not start within 20 seconds")


def _stop_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "command failed (%s): %s\nstdout:\n%s\nstderr:\n%s"
            % (
                result.returncode,
                " ".join(command),
                result.stdout,
                result.stderr,
            )
        )
    return result


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
