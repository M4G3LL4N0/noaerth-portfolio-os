import tempfile
import unittest
from pathlib import Path

from portfolio_os.db import connect
from portfolio_os.httpapi import handle
from portfolio_os.localauth import consume_bootstrap, issue_bootstrap, resolve_token


class UiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(Path(tempfile.mkdtemp()) / "portfolio.db")
        self.root = Path(tempfile.mkdtemp())
        self.token = "local-test-token-ok"
        self.conn.execute(
            "INSERT INTO startups (slug, name, owner_private, is_public, health) VALUES ('acme', 'Acme', 0, 1, 'REVIEW_REQUIRED')"
        )
        self.conn.execute(
            "INSERT INTO startups (slug, name, owner_private, is_public, health) VALUES ('hidden-matter', 'Hidden', 1, 0, 'REVIEW_REQUIRED')"
        )
        self.conn.execute(
            """
            INSERT INTO work_items (startup_id, type, title, priority, status, assigned_role, created_at)
            VALUES (1, 'team_task', 'keep running', 90, 'running', 'ENGINEER', '2026-09-28T00:00:00Z')
            """
        )
        self.conn.execute(
            """
            INSERT INTO work_items (startup_id, type, title, priority, status, assigned_role, created_at)
            VALUES (2, 'team_task', 'do not show', 90, 'running', 'ENGINEER', '2026-09-28T00:00:00Z')
            """
        )

    def test_ui_requires_login_and_hides_private_rows(self) -> None:
        status, _, _, extra = handle(self.conn, "GET", "/", {}, b"", self.token, self.root)
        self.assertEqual(status, 302)
        self.assertEqual(extra["Location"], "/login")
        status, content, body, _ = handle(
            self.conn,
            "GET",
            "/",
            {"cookie": f"portfolio_os_session={self.token}"},
            b"",
            self.token,
            self.root,
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", content)
        text = body.decode("utf-8")
        self.assertIn("Engine", text)
        self.assertNotIn("hidden-matter", text)
        self.assertNotIn("do not show", text)

    def test_cancel_refuses_a_running_task(self) -> None:
        status, _, body, _ = handle(
            self.conn,
            "POST",
            "/ui/action",
            {"cookie": f"portfolio_os_session={self.token}"},
            b"action=cancel_task&slug=acme&work_item=1",
            self.token,
            self.root,
        )
        self.assertEqual(status, 400)
        self.assertIn(b"unsafe_cancel", body)
        status, _, body, _ = handle(
            self.conn,
            "POST",
            "/ui/action",
            {"cookie": f"portfolio_os_session={self.token}"},
            b"action=cancel_task&slug=hidden-matter&work_item=1",
            self.token,
            self.root,
        )
        self.assertEqual(status, 404)
        self.assertNotIn(b"hidden-matter", body)

    def test_bootstrap_is_single_use_and_loopback_only(self) -> None:
        os_root = Path(tempfile.mkdtemp())
        (os_root / "data").mkdir()
        nonce = issue_bootstrap(os_root)
        status, _, _, extra = handle(
            self.conn, "GET", f"/bootstrap/{nonce}", {}, b"", self.token, os_root, "10.0.0.8"
        )
        self.assertEqual(status, 404)
        status, _, _, extra = handle(
            self.conn, "GET", f"/bootstrap/{nonce}", {}, b"", self.token, os_root, "127.0.0.1"
        )
        self.assertEqual(status, 302)
        self.assertIn("portfolio_os_session=", extra["Set-Cookie"])
        self.assertNotIn(nonce, extra["Set-Cookie"])
        again, _, _, _ = handle(
            self.conn, "GET", f"/bootstrap/{nonce}", {}, b"", self.token, os_root, "127.0.0.1"
        )
        self.assertEqual(again, 404)
        self.assertIsNotNone(resolve_token(os_root, "127.0.0.1"))
        self.assertIsNone(resolve_token(os_root, "10.0.0.8"))
