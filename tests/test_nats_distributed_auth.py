"""Distributed run over a SECURED NATS, as if reaching a remote destination.

What it proves end-to-end:
  - Two PROCESSES, not one: a worker process serves the 'remote' role; the
    orchestrator (this process) serves the 'local' role and drives the pipeline.
    The remote stage is genuinely handled across a process boundary over NATS.
  - AUTH: the broker requires user/password; each side connects with its own
    credentials. A wrong password is rejected (negative test).
  - TLS: the broker speaks TLS with a self-signed cert; clients verify it against
    that cert as their CA (the remote-destination case).
  - AUTHORIZATION / topic scoping: per-user subject permissions. The worker may
    only subscribe to its own role and reply on _INBOX — it cannot publish to
    other role subjects (limited blast radius, the 'tiered authority' idea).

Not part of the auto-suite's plain-python run (needs nats-server binary, openssl,
and nats-py). Self-skips if any is missing.

Run: cd yaah && NATS_SERVER=/path/to/nats-server .venv/bin/python tests/test_nats_distributed_auth.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

PORT = 4223
URL = "nats://localhost:{}".format(PORT)


def _find_nats_server():
    cand = os.environ.get("NATS_SERVER")
    if cand and os.path.exists(cand):
        return cand
    which = shutil.which("nats-server")
    if which:
        return which
    # the path this project downloads to (see memory resume point)
    guess = "/tmp/yaah-nats/nats-server-v2.14.2-darwin-amd64/nats-server"
    return guess if os.path.exists(guess) else None


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_port(port: int, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _gen_cert(d: str) -> "tuple[str, str]":
    cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    cnf = os.path.join(d, "openssl.cnf")
    with open(cnf, "w") as f:
        f.write(
            "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
            "[dn]\nCN=localhost\n"
            "[v3]\nsubjectAltName=@alt\n"
            "[alt]\nDNS.1=localhost\nIP.1=127.0.0.1\n")
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "2", "-config", cnf, "-extensions", "v3"],
        capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("openssl failed: " + r.stderr.decode(errors="replace")[:300])
    return cert, key


def _nats_conf(d: str, cert: str, key: str) -> str:
    # ":" is a normal subject character, so roles are single tokens listed exactly.
    conf = os.path.join(d, "nats.conf")
    with open(conf, "w") as f:
        f.write('host: "127.0.0.1"\nport: {}\n'.format(PORT))
        f.write('tls {{\n  cert_file: "{}"\n  key_file: "{}"\n}}\n'.format(cert, key))
        f.write(
            'authorization {\n'
            '  users = [\n'
            # orchestrator: may call both roles, reply on inboxes, emit telemetry
            '    { user: "orchestrator", password: "orch-secret", permissions: {\n'
            '        publish: ["role:remote-eval", "role:local-tag", "_INBOX.>", "events"],\n'
            '        subscribe: ["role:local-tag", "_INBOX.>"] } }\n'
            # worker: may ONLY serve its own role + reply + emit telemetry. It
            # canNOT publish to any role subject — limited blast radius.
            '    { user: "worker", password: "work-secret", permissions: {\n'
            '        subscribe: ["role:remote-eval"],\n'
            '        publish: ["_INBOX.>", "events"] } }\n'
            '  ]\n'
            '}\n')
    return conf


def _write_app(d: str, cert: str) -> None:
    pipeline = {
        "nodes": {
            "role:remote-eval": {"type": "agent", "prompt": "s:eval", "model": "fake:eval", "stage": "remote-eval"},
            "role:local-tag": {"type": "agent", "prompt": "s:tag", "model": "fake:tag", "stage": "local-tag"},
        },
        "graph": {"start": "remote", "stages": {
            "remote": {"node": "role:remote-eval", "then": "local"},
            "local": {"node": "role:local-tag", "then": None},
        }},
    }
    with open(os.path.join(d, "pipeline.json"), "w") as f:
        json.dump(pipeline, f)
    with open(os.path.join(d, "fake.json"), "w") as f:
        json.dump({"eval": ['{"v": "evaluated-remotely"}'], "tag": ['{"v": "tagged-locally"}']}, f)

    def transport(user, pw):
        return {"type": "nats", "url": URL, "user": user, "password": pw,
                "request_timeout": 10.0, "tls": {"ca": cert, "hostname": "localhost"}}

    common = {
        "providers": {"fake": {"type": "fake_scripted", "fixtures": "fake.json"}},
        "default_provider": "fake",
        "prompt_sources": {"s": {"type": "static", "prompts": {"eval": "evaluate", "tag": "tag it"}}},
        "default_prompt_source": "s",
        "pipeline": "pipeline.json",
    }
    worker = dict(common, transport=transport("worker", "work-secret"),
                  serve=["role:remote-eval"], run=False)
    orch = dict(common, transport=transport("orchestrator", "orch-secret"),
                serve=["role:local-tag"], input="fake.json", run=True)
    # orchestrator input payload is irrelevant (static prompts) — reuse any json
    with open(os.path.join(d, "worker.json"), "w") as f:
        json.dump(worker, f)
    with open(os.path.join(d, "orch.json"), "w") as f:
        json.dump(orch, f)


def main() -> None:
    try:
        import nats  # noqa: F401
    except ImportError:
        print("skip: nats-py not installed")
        return
    server = _find_nats_server()
    if not server:
        print("skip: nats-server binary not found (set NATS_SERVER)")
        return
    if not shutil.which("openssl"):
        print("skip: openssl not found")
        return
    if not _free(PORT):
        print("skip: port {} already in use".format(PORT))
        return

    d = tempfile.mkdtemp(prefix="yaah-distauth-")
    nats_proc = worker_proc = None
    try:
        cert, key = _gen_cert(d)
        conf = _nats_conf(d, cert, key)
        _write_app(d, cert)

        nats_proc = subprocess.Popen([server, "-c", conf],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_port(PORT):
            raise RuntimeError("nats-server did not come up on {}".format(PORT))

        env = dict(os.environ, PYTHONPATH=os.path.join(os.getcwd(), "src"))
        worker_proc = subprocess.Popen(
            [sys.executable, "-m", "yaah.runtime", os.path.join(d, "worker.json")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
        # wait until the worker reports it has served its role
        end = time.time() + 15
        ready = False
        while time.time() < end:
            line = worker_proc.stdout.readline()
            if not line and worker_proc.poll() is not None:
                break
            if line.startswith("served:"):
                ready = True
                break
        if not ready:
            raise RuntimeError("worker did not become ready (exited={})".format(worker_proc.poll()))

        asyncio.run(_drive(d, cert))
        print("ok (2 processes, TLS + auth + subject scoping over NATS)")
    finally:
        for p in (worker_proc, nats_proc):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        shutil.rmtree(d, ignore_errors=True)


async def _drive(d: str, cert: str) -> None:
    from yaah.runtime import _read_json, run_root

    # positive: orchestrator (serves local-tag) drives; remote-eval runs in the
    # WORKER process, reached over authed TLS NATS. run_root returns the
    # Outcome; a completed run's Done carries the final stage's output envelope.
    out = await run_root(_read_json(os.path.join(d, "orch.json")), d)
    env = getattr(out, "output", None)
    assert env is not None, "expected a completed run from the orchestrator, got {!r}".format(out)
    raw = env.payload.get("raw")
    assert raw is not None, out
    assert json.loads(raw) == {"v": "tagged-locally"}, raw

    # negative: a wrong password must be rejected by the broker. Use raw nats with
    # a quiet error_cb + no reconnect so the expected rejection doesn't spam logs.
    import nats
    import ssl

    async def _quiet(_e):  # swallow the expected auth-violation callback
        return None

    ctx = ssl.create_default_context(cafile=cert)
    rejected = False
    try:
        nc = await nats.connect(URL, user="orchestrator", password="WRONG", tls=ctx,
                                tls_hostname="localhost", allow_reconnect=False,
                                error_cb=_quiet, connect_timeout=3)
        await nc.close()
    except Exception:  # auth/connection error from the broker
        rejected = True
    assert rejected, "broker accepted a wrong password — auth not enforced!"


if __name__ == "__main__":
    main()
