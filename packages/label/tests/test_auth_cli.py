"""Team mode token auth, token never logged, and the `caliper label` CLI dispatch (dev 3)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from caliper_eval.cli import main
from caliper_label import auth

sys.path.insert(0, str(Path(__file__).parent))  # --import-mode=importlib: find loop_helpers
from loop_helpers import client_for, make_ctx

TOKEN = "fictional-token-Zq81x"


def test_auth_required_wrong_and_right(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path), auth_token=TOKEN)
    st = c.get("/api/status").json()
    assert st == {"auth": "token", "authed": False, "mode": None, "project": None}
    r = c.get("/api/items")
    assert r.status_code == 401 and r.json()["detail"] == "access token required"
    assert "?token=" in r.json()["hint"] and r.headers["www-authenticate"] == "Bearer"
    r = c.get("/api/items", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["detail"] == "wrong access token"
    assert c.get("/api/items?token=nope").status_code == 401
    assert (
        c.post(
            "/api/labels", json={"item_id": "t0", "labeller": "a", "decision": "correct"}
        ).status_code
        == 401
    )
    ok = c.get("/api/items", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    # the static UI and the status probe are public (no data in them)
    assert c.get("/").status_code == 200 and c.get("/static/js/boot.js").status_code == 200


def test_query_token_sets_cookie_once(tmp_path: Path) -> None:
    c = client_for(make_ctx(tmp_path), auth_token=TOKEN)
    r = c.get(f"/api/status?token={TOKEN}")
    assert r.json()["authed"] is True
    cookie = r.headers.get("set-cookie", "")
    assert auth.COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    # the client now carries the cookie: images and downloads work without a header
    assert c.get("/api/items").status_code == 200
    assert "set-cookie" not in c.get("/api/items").headers  # not re-set every time
    c.cookies.clear()
    assert c.get("/api/items").status_code == 401


def test_constant_time_compare(monkeypatch, tmp_path: Path) -> None:
    seen = []
    real = auth.secrets.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.secrets, "compare_digest", spy)
    c = client_for(make_ctx(tmp_path), auth_token=TOKEN)
    c.get("/api/items", headers={"Authorization": "Bearer x"})
    assert seen and all(isinstance(a, bytes) for a, _ in seen)


def test_token_env_var(monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV, "from-env")
    assert auth.make_token() == "from-env"
    monkeypatch.delenv(auth.ENV)
    assert len(auth.make_token()) >= 24


def test_redaction_filter() -> None:
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/1.1" %d',
        ("127.0.0.1", "GET", f"/api/items?token={TOKEN}&x=1", 200),
        None,
    )
    auth.RedactTokenFilter(TOKEN).filter(rec)
    assert TOKEN not in rec.getMessage() and "token=[redacted]" in rec.getMessage()
    assert TOKEN not in auth.redact(f"Authorization: Bearer {TOKEN}")


def test_token_not_logged_after_startup(monkeypatch, tmp_path: Path, capsys, caplog) -> None:
    """Start the CLI in team mode, then make authenticated requests: the token is printed once
    (the startup link) and never appears in any log line or output after that."""
    import uvicorn
    from fastapi.testclient import TestClient

    monkeypatch.setenv(auth.ENV, TOKEN)
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    data = tmp_path / "rows.jsonl"
    data.write_text('{"id": "a", "text": "fictional"}\n{"id": "b", "text": "fictional"}\n')
    cfg = tmp_path / "label.yaml"
    cfg.write_text("fields: [{key: text}]\ndecisions: [{key: '1', value: ok}]\n")
    rc = main(
        [
            "label",
            "serve",
            str(data),
            "--config",
            str(cfg),
            "--store",
            str(tmp_path / "s.jsonl"),
            "--no-browser",
            "--auth",
            "token",
            "--port",
            "8786",
        ]
    )
    assert rc == 0
    assert started["access_log"] is False and started["log_level"] == "warning"
    startup = capsys.readouterr()
    assert startup.out.count(TOKEN) == 1 and f"?token={TOKEN}" in startup.out

    caplog.set_level(logging.DEBUG)
    access = logging.getLogger("uvicorn.access")
    c = TestClient(started["app"])
    for url in (
        f"/api/status?token={TOKEN}",
        "/api/next?labeller=ana",
        f"/api/items?token={TOKEN}",
    ):
        r = c.get(url, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        access.info('%s - "%s %s HTTP/1.1" %d', "127.0.0.1", "GET", url, r.status_code)
    c.get("/api/items", headers={"Authorization": "Bearer wrong"})
    after = capsys.readouterr()
    assert TOKEN not in after.out + after.err
    # every server-side log line (the test's own HTTP client, httpx, is the "browser")
    server = "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    )
    assert TOKEN not in server and "[redacted]" in server


def test_cli_open_dispatch(monkeypatch, tmp_path: Path, capsys) -> None:
    import uvicorn

    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    # no folder: the start screen, no project open
    assert main(["label", "--no-browser", "--port", "8787"]) == 0
    assert started["port"] == 8787 and started["app"].state.ctx is None
    assert "start screen" in capsys.readouterr().out
    # a folder that is not one
    assert main(["label", str(tmp_path / "nope"), "--no-browser"]) == 2
    assert "is not a folder" in capsys.readouterr().err
    # help lists the subcommands
    assert main(["label", "--help"]) == 0
    out = capsys.readouterr().out
    assert "serve" in out and "export" in out and "PROJECT_DIR" in out
    with pytest.raises(SystemExit):
        main(["label", "--auth", "maybe"])


def test_cli_export_needs_destination(tmp_path: Path, capsys) -> None:
    assert main(["label", "export", str(tmp_path), "--format", "csv"]) == 2
    assert "--out" in capsys.readouterr().err
    assert (
        main(
            [
                "label",
                "export",
                "--data",
                "x.jsonl",
                "--config",
                "y.yaml",
                "--format",
                "csv",
                "--out",
                "o",
            ]
        )
        == 2
    )
    assert "needs a project folder" in capsys.readouterr().err


def test_team_mode_keeps_setup_on_the_server_machine(tmp_path: Path) -> None:
    from caliper_label.app import create_app
    from fastapi.testclient import TestClient

    app = create_app(ctx=make_ctx(tmp_path), auth_token=TOKEN)
    h = {"Authorization": f"Bearer {TOKEN}"}
    remote = TestClient(app, client=("10.0.0.7", 50000))
    src = {"source": {"kind": "http", "path": "https://example.invalid/x.csv"}}
    for method, path, body in [
        ("post", "/api/source/test", src),
        ("post", "/api/source/preview", src),
        ("get", "/api/source/preview", None),
        ("get", "/api/setup/browse", None),
        ("get", "/api/projects", None),
        ("put", "/api/settings", {}),
        ("post", "/api/export", {"format": "csv"}),
    ]:
        r = getattr(remote, method)(path, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code)
        assert r.json()["detail"] == "only available on the server machine" and r.json()["hint"]
    # labelling, team views and downloads stay open to labellers
    assert remote.get("/api/next?labeller=ana", headers=h).status_code == 200
    assert remote.get("/api/dashboard", headers=h).status_code == 200
    assert remote.get("/api/export/download?format=csv", headers=h).status_code == 200
    local = TestClient(app, client=("127.0.0.1", 50001))
    r = local.post("/api/export", headers=h, json={"format": "csv"})
    assert r.status_code == 200
    # without team mode nothing changes (local-first, single user)
    open_app = TestClient(create_app(ctx=make_ctx(tmp_path)), client=("10.0.0.7", 1))
    assert open_app.post("/api/export", json={"format": "csv"}).status_code == 200
