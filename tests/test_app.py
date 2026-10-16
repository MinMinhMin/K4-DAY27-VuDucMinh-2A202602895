from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import unittest
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langgraph.types import Command

from common.db import write_audit_event
from common.schemas import AuditEntry, PRAnalysis, ReviewComment
import exercises.exercise_4_audit as exercise_4

logging.disable(logging.CRITICAL)
import app
logging.disable(logging.NOTSET)


PR_URL = "https://github.com/example/repo/pull/2"
THREAD_ID = "streamlit-session-1"


class FakeStreamlit:
    def __init__(self):
        self.session_state = {}
        self.sidebar = nullcontext()
        self.selected = None
        self.button_values = {}
        self.form_values = {}
        self.text_values = {}
        self.messages = []
        self.rerun = Mock()

    def subheader(self, value):
        self.messages.append(("subheader", value))

    def header(self, value):
        self.messages.append(("header", value))

    def caption(self, value):
        self.messages.append(("caption", value))

    def markdown(self, value):
        self.messages.append(("markdown", value))

    def error(self, value):
        self.messages.append(("error", value))

    def success(self, value):
        self.messages.append(("success", value))

    def warning(self, value):
        self.messages.append(("warning", value))

    def info(self, value):
        self.messages.append(("info", value))

    def code(self, value, language=None):
        self.messages.append(("code", value))

    def text_input(self, label, value="", key=None, **kwargs):
        if label in self.text_values:
            return self.text_values[label]
        return value

    def columns(self, count):
        return [FakeColumn(self) for _ in range(count)]

    def button(self, label, **kwargs):
        return self.button_values.get(label, False)

    def form(self, key):
        return nullcontext()

    def form_submit_button(self, label):
        return self.form_values.get(label, False)

    def expander(self, label):
        return nullcontext()

    def selectbox(self, label, options, format_func=None, key=None):
        self.options = options
        return self.selected if self.selected is not None else (options[0] if options else None)


class FakeColumn:
    def __init__(self, st):
        self.st = st

    def button(self, label, **kwargs):
        return self.st.button(label, **kwargs)


class AppRunGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_graph_starts_on_thread_and_resumes_with_command(self):
        checkpointer = Mock()
        checkpointer.setup = AsyncMock()
        graph = Mock()
        graph.ainvoke = AsyncMock(side_effect=[{"__interrupt__": []}, {"final_action": "committed"}])

        @asynccontextmanager
        async def saver_context(path):
            self.assertEqual(path, "test.db")
            yield checkpointer

        with (
            patch.object(app.AsyncSqliteSaver, "from_conn_string", side_effect=saver_context),
            patch.object(app, "db_path", return_value="test.db"),
            patch.object(app, "build_graph", return_value=graph) as build_graph,
        ):
            first = await app.run_graph(PR_URL, THREAD_ID)
            answer = {"choice": "approve", "feedback": "looks good"}
            second = await app.run_graph(PR_URL, THREAD_ID, resume_value=answer)

        cfg = {"configurable": {"thread_id": THREAD_ID}}
        build_graph.assert_has_calls([unittest.mock.call(checkpointer), unittest.mock.call(checkpointer)])
        graph.ainvoke.assert_has_awaits([
            unittest.mock.call({"pr_url": PR_URL, "thread_id": THREAD_ID}, cfg),
            unittest.mock.call(Command(resume=answer), cfg),
        ])
        self.assertEqual(first, {"__interrupt__": []})
        self.assertEqual(second, {"final_action": "committed"})


class AppRendererTests(unittest.TestCase):
    def setUp(self):
        self.fake_st = FakeStreamlit()
        self.original_st = app.st
        app.st = self.fake_st

    def tearDown(self):
        app.st = self.original_st

    def test_approval_buttons_return_exact_resume_payloads(self):
        payload = {
            "confidence": 0.65, "confidence_reasoning": "Needs a human check.",
            "summary": "Updates login flow.", "comments": [], "diff_preview": "diff",
        }
        self.fake_st.text_values["Feedback (optional)"] = "migration is covered"
        for choice in ("Approve", "Reject", "Edit"):
            with self.subTest(choice=choice):
                self.fake_st.button_values = {choice: True}
                result = app.render_approval_card(payload)
                self.assertEqual(result, {
                    "choice": choice.lower(), "feedback": "migration is covered",
                })

    def test_escalation_form_returns_question_to_answer_mapping(self):
        payload = {
            "confidence": 0.4, "confidence_reasoning": "Need context.",
            "summary": "Changes password storage.", "risk_factors": ["MD5"],
            "questions": ["Why is MD5 used?", "Does this affect existing users?"],
        }
        self.fake_st.text_values.update({
            "Why is MD5 used?": "Legacy fixture only.",
            "Does this affect existing users?": "No, migration is included.",
        })
        self.fake_st.form_values["Submit answers"] = True

        result = app.render_escalation_card(payload)

        self.assertEqual(result, {
            "Why is MD5 used?": "Legacy fixture only.",
            "Does this affect existing users?": "No, migration is included.",
        })

    def test_recent_session_selection_restores_interrupt_or_final_state(self):
        session = {"thread_id": THREAD_ID, "pr_url": PR_URL, "worst_risk": "high", "last_event": "now"}
        payload = {"kind": "escalation", "questions": ["Why?"], "confidence": 0.4}
        self.fake_st.selected = session
        self.fake_st.button_values["Open selected session"] = True
        restored = {"values": {"analysis": "saved"}, "interrupt_payload": payload, "final": None}

        with (
            patch.object(app, "list_recent_sessions", new=AsyncMock(return_value=[session])),
            patch.object(app, "load_session", new=AsyncMock(return_value=restored)) as load_session,
        ):
            app.render_recent_sessions()

        load_session.assert_awaited_once_with(THREAD_ID)
        self.assertEqual(self.fake_st.session_state["thread_id"], THREAD_ID)
        self.assertEqual(self.fake_st.session_state["pr_url"], PR_URL)
        self.assertEqual(self.fake_st.session_state["interrupt_payload"], payload)
        self.assertIsNone(self.fake_st.session_state["final"])
        self.fake_st.rerun.assert_called_once()

    def test_auto_approve_result_shows_confidence_summary_and_pr_link(self):
        final = {
            "final_action": "auto_committed",
            "analysis": SimpleNamespace(confidence=0.91, summary="Adds passwordless login."),
        }

        app.render_final_result(final, PR_URL, THREAD_ID)

        rendered = "\n".join(value for _, value in self.fake_st.messages)
        self.assertIn("91%", rendered)
        self.assertIn("Adds passwordless login.", rendered)
        self.assertIn(f"]({PR_URL})", rendered)


class AppPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_sessions_query_returns_grouped_audit_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "sessions.db")
            with patch.dict(os.environ, {"HITL_DB_PATH": database}):
                for action, risk in (("analyze", "low"), ("route", "high")):
                    await write_audit_event(
                        thread_id=THREAD_ID,
                        pr_url=PR_URL,
                        entry=AuditEntry(
                            agent_id="test-agent", action=action, confidence=0.4,
                            risk_level=risk, decision="pending", execution_time_ms=2,
                        ),
                    )
                sessions = await app.list_recent_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["thread_id"], THREAD_ID)
        self.assertEqual(sessions[0]["pr_url"], PR_URL)
        self.assertEqual(sessions[0]["worst_risk"], "high")
        self.assertIn("last_event", sessions[0])
        self.assertEqual(sessions[0]["event_count"], 2)

    async def test_load_session_rehydrates_saved_interrupt_from_checkpointer(self):
        payload = {"kind": "approval_request", "confidence": 0.6}
        snapshot = SimpleNamespace(
            values={"pr_url": PR_URL, "analysis": "saved"},
            tasks=(SimpleNamespace(interrupts=(SimpleNamespace(value=payload),)),),
        )
        checkpointer = Mock()
        checkpointer.setup = AsyncMock()
        graph = Mock()
        graph.aget_state = AsyncMock(return_value=snapshot)

        @asynccontextmanager
        async def saver_context(path):
            yield checkpointer

        with (
            patch.object(app.AsyncSqliteSaver, "from_conn_string", side_effect=saver_context),
            patch.object(app, "build_graph", return_value=graph),
        ):
            result = await app.load_session(THREAD_ID)

        graph.aget_state.assert_awaited_once_with({"configurable": {"thread_id": THREAD_ID}})
        self.assertEqual(result["values"], snapshot.values)
        self.assertEqual(result["interrupt_payload"], payload)
        self.assertIsNone(result["final"])

    async def test_real_checkpointer_restores_paused_session(self):
        fake_pr = SimpleNamespace(
            title="Add login", diff="auth.py uses MD5", files_changed=["auth.py"],
            head_sha="abcdef0",
        )
        initial = PRAnalysis(
            summary="Adds password login.",
            risk_factors=["MD5 hashing"],
            comments=[ReviewComment(
                file="auth.py", line=22, severity="blocker", body="Use a password hash."
            )],
            confidence=0.4,
            confidence_reasoning="Need context for the migration.",
            escalation_questions=["Why is MD5 used in auth.py?"],
        )
        structured_llm = Mock()
        structured_llm.ainvoke = AsyncMock(return_value=initial)
        llm = Mock()
        llm.with_structured_output.return_value = structured_llm

        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "checkpoints.db")
            with (
                patch.dict(os.environ, {"HITL_DB_PATH": database}),
                patch.object(exercise_4, "fetch_pr", return_value=fake_pr),
                patch.object(exercise_4, "get_llm", return_value=llm),
            ):
                paused = await app.run_graph(PR_URL, THREAD_ID)
                restored = await app.load_session(THREAD_ID)

        self.assertEqual(paused["__interrupt__"][0].value["kind"], "escalation")
        self.assertEqual(restored["interrupt_payload"]["kind"], "escalation")
        self.assertEqual(restored["interrupt_payload"]["questions"], initial.escalation_questions)
        self.assertIsNone(restored["final"])

