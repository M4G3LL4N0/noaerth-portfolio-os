import tempfile
import unittest
from pathlib import Path

from portfolio_os.db import connect
from portfolio_os.httpapi import handle


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
