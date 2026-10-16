from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

import exercises.exercise_3_escalation as exercise_3
import exercises.exercise_4_audit as exercise_4
from common.db import db_conn, replay_events
from common.schemas import AuditEntry, PRAnalysis, ReviewComment


PR_URL = "https://github.com/example/repo/pull/2"
THREAD_ID = "audit-escalation-test"


def analysis(confidence: float, questions: list[str] | None = None) -> PRAnalysis:
    return PRAnalysis(
        summary="Adds password login and cloud sync.",
        risk_factors=["MD5 password hashing", "SQL input is interpolated"],
        comments=[ReviewComment(
            file="auth.py", line=22, severity="blocker", body="Use a password hash."
        )],
        confidence=confidence,
        confidence_reasoning="Security behavior needs system context.",
        escalation_questions=questions or [],
    )


def graph_input(pr_analysis: PRAnalysis | None = None) -> dict:
    return {
        "pr_url": PR_URL,
        "thread_id": THREAD_ID,
        "pr_title": "Add login and cloud sync",
        "pr_diff": "diff content mentioning auth.py and storage.py",
        "analysis": pr_analysis or analysis(0.4, ["Why MD5 in auth.py?"]),
    }


class Exercise3Tests(TestCase):
    def test_analyzer_requests_specific_questions_below_shared_threshold(self):
        result_analysis = analysis(0.4, ["Why MD5 in auth.py?"])
        structured_llm = Mock()
        structured_llm.invoke.return_value = result_analysis
        llm = Mock()
        llm.with_structured_output.return_value = structured_llm

        with patch.object(exercise_3, "get_llm", return_value=llm):
            result = exercise_3.node_analyze({
                "pr_title": "Add login", "pr_diff": "auth.py uses MD5"
            })

        self.assertIs(result["analysis"], result_analysis)
        system_prompt = structured_llm.invoke.call_args.args[0][0]["content"]
        self.assertIn("0.58", system_prompt)
        self.assertIn("2–4", system_prompt)
        self.assertIn("file", system_prompt.lower())
        self.assertIn("auth.py uses MD5", structured_llm.invoke.call_args.args[0][1]["content"])

    def test_escalation_payload_uses_diff_questions_and_returns_answers(self):
        state = graph_input(analysis(0.4))
        expected_answers = {"What is the intent of this PR?": "legacy compatibility"}

        with patch.object(exercise_3, "interrupt", return_value=expected_answers) as interrupt:
            result = exercise_3.node_escalate(state)

        payload = interrupt.call_args.args[0]
        self.assertEqual(payload["kind"], "escalation")
        self.assertEqual(payload["pr_url"], PR_URL)
        self.assertEqual(payload["confidence"], 0.4)
        self.assertEqual(payload["confidence_reasoning"], "Security behavior needs system context.")
        self.assertEqual(payload["summary"], "Adds password login and cloud sync.")
        self.assertEqual(payload["risk_factors"], ["MD5 password hashing", "SQL input is interpolated"])
        self.assertEqual(payload["questions"], ["What is the intent of this PR?", "Any migration concerns?"])
        self.assertEqual(result, {"escalation_answers": expected_answers})

    def test_synthesis_prompt_includes_diff_initial_review_and_question_answers(self):
        initial = analysis(0.4, ["Why MD5 in auth.py?"])
        refined = analysis(0.9)
        structured_llm = Mock()
        structured_llm.invoke.return_value = refined
        llm = Mock()
        llm.with_structured_output.return_value = structured_llm
        state = graph_input(initial)
        state["escalation_answers"] = {"Why MD5 in auth.py?": "Only test data uses it."}

        with patch.object(exercise_3, "get_llm", return_value=llm):
            result = exercise_3.node_synthesize(state)

        self.assertIs(result["analysis"], refined)
        llm.with_structured_output.assert_called_once_with(PRAnalysis)
        prompt = structured_llm.invoke.call_args.args[0][1]["content"]
        self.assertIn("diff content mentioning auth.py", prompt)
        self.assertIn(initial.summary, prompt)
        self.assertIn("Why MD5 in auth.py?", prompt)
        self.assertIn("Only test data uses it.", prompt)
        self.assertIn('"confidence": 0.4', prompt)

    def test_escalation_graph_resumes_into_synthesis_and_commit(self):
        initial = analysis(0.4, ["Why MD5 in auth.py?"])
        refined = analysis(0.9)
        pr = SimpleNamespace(
            title="Add login", diff="auth.py uses MD5", files_changed=["auth.py"],
            head_sha="abcdef0",
        )
        structured_llm = Mock()
        structured_llm.invoke.side_effect = [initial, refined]
        llm = Mock()
        llm.with_structured_output.return_value = structured_llm
        config = {"configurable": {"thread_id": "exercise-3-resume"}}

        with (
            patch.object(exercise_3, "fetch_pr", return_value=pr),
            patch.object(exercise_3, "get_llm", return_value=llm),
            patch.object(exercise_3, "post_review_comment") as post_comment,
        ):
            app = exercise_3.build_graph()
            paused = app.invoke({"pr_url": PR_URL, "thread_id": "exercise-3-resume"}, config)
            final = app.invoke(Command(resume={"Why MD5 in auth.py?": "Demo-only credentials."}), config)

        self.assertEqual(paused["__interrupt__"][0].value["kind"], "escalation")
        self.assertEqual(final["final_action"], "committed_after_escalation")
        self.assertEqual(final["analysis"].confidence, 0.9)
        post_comment.assert_called_once()

    def test_empty_escalation_answer_mapping_still_commits_refined_review(self):
        state = graph_input(analysis(0.9))
        state["escalation_answers"] = {}
        with patch.object(exercise_3, "post_review_comment") as post_comment:
            result = exercise_3.node_commit(state)

        self.assertEqual(result, {"final_action": "committed_after_escalation"})
        post_comment.assert_called_once()


class Exercise4Tests(IsolatedAsyncioTestCase):
    async def test_analyzer_requests_context_questions_below_shared_threshold(self):
        result_analysis = analysis(0.4, ["Why MD5 in auth.py?"])
        llm = Mock()
        structured_llm = Mock()
        structured_llm.ainvoke = AsyncMock(return_value=result_analysis)
        llm.with_structured_output.return_value = structured_llm

        with (
            patch.object(exercise_4, "get_llm", return_value=llm),
            patch.object(exercise_4, "audit", AsyncMock()),
        ):
            await exercise_4.node_analyze(graph_input())

        system_prompt = structured_llm.ainvoke.call_args.args[0][0]["content"]
        self.assertIn("0.58", system_prompt)
        self.assertIn("2–4", system_prompt)
        self.assertIn("file", system_prompt.lower())

    async def test_thread_helper_resumes_saved_interrupt(self):
        payload = {"kind": "approval_request", "confidence": 0.65}
        snapshot = SimpleNamespace(
            values={"pr_url": PR_URL},
            tasks=(SimpleNamespace(interrupts=(SimpleNamespace(value=payload),)),),
            next=("human_approval",),
        )
        graph = Mock()
        graph.aget_state = AsyncMock(return_value=snapshot)
        graph.ainvoke = AsyncMock(return_value={"final_action": "committed"})
        cfg = {"configurable": {"thread_id": THREAD_ID}}
        answer = {"choice": "approve", "feedback": "looks good"}

        with patch.object(exercise_4, "handle_interrupt", return_value=answer):
            result = await exercise_4.invoke_or_resume(graph, PR_URL, THREAD_ID, cfg)

        graph.ainvoke.assert_awaited_once_with(Command(resume=answer), cfg)
        self.assertEqual(result, {"final_action": "committed"})

    async def test_thread_helper_does_not_restart_completed_session(self):
        final_state = {"pr_url": PR_URL, "final_action": "rejected"}
        snapshot = SimpleNamespace(values=final_state, tasks=(), next=())
        graph = Mock()
        graph.aget_state = AsyncMock(return_value=snapshot)
        graph.ainvoke = AsyncMock()
        cfg = {"configurable": {"thread_id": THREAD_ID}}

        result = await exercise_4.invoke_or_resume(graph, PR_URL, THREAD_ID, cfg)

        graph.ainvoke.assert_not_awaited()
        self.assertEqual(result, final_state)

    async def test_thread_helper_starts_when_checkpoint_is_empty(self):
        snapshot = SimpleNamespace(values={}, tasks=(), next=())
        graph = Mock()
        graph.aget_state = AsyncMock(return_value=snapshot)
        graph.ainvoke = AsyncMock(return_value={"final_action": "auto_committed"})
        cfg = {"configurable": {"thread_id": THREAD_ID}}

        result = await exercise_4.invoke_or_resume(graph, PR_URL, THREAD_ID, cfg)

        graph.ainvoke.assert_awaited_once_with(
            {"pr_url": PR_URL, "thread_id": THREAD_ID}, cfg
        )
        self.assertEqual(result, {"final_action": "auto_committed"})

    async def test_audit_persists_structured_event_with_session_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "audit.db")
            entry = AuditEntry(
                agent_id="test-agent", action="route", confidence=0.4,
                risk_level="high", decision="escalate", execution_time_ms=8,
            )
            with patch.dict(os.environ, {"HITL_DB_PATH": database}):
                await exercise_4.audit(graph_input(), entry)
                async with db_conn() as conn:
                    async with conn.execute(
                        "SELECT thread_id, pr_url, action, decision FROM audit_events WHERE thread_id = ?",
                        (THREAD_ID,),
                    ) as cursor:
                        row = await cursor.fetchone()

        self.assertEqual(row["thread_id"], THREAD_ID)
        self.assertEqual(row["pr_url"], PR_URL)
        self.assertEqual(row["action"], "route")
        self.assertEqual(row["decision"], "escalate")

    async def test_route_audit_uses_allowed_decisions_and_shared_risk(self):
        cases = ((0.73, "auto_approve", "auto", "low"),
                 (0.58, "human_approval", "pending", "med"),
                 (0.579, "escalate", "escalate", "high"))
        for value, branch, audit_decision, risk in cases:
            with self.subTest(confidence=value):
                writer = AsyncMock()
                with patch.object(exercise_4, "audit", writer):
                    result = await exercise_4.node_route({"analysis": analysis(value)})
                self.assertEqual(result, {"decision": branch})
                entry = writer.await_args.args[1]
                self.assertEqual(entry.action, "route")
                self.assertEqual(entry.decision, audit_decision)
                self.assertEqual(entry.risk_level, risk)
                self.assertIsNone(entry.reviewer_id)
                self.assertGreaterEqual(entry.execution_time_ms, 0)

    async def test_human_approval_audits_pending_then_reviewer_response(self):
        state = graph_input(analysis(0.65))
        writer = AsyncMock()
        response = {"choice": "approve", "feedback": "migration is covered"}
        with (
            patch.object(exercise_4, "audit", writer),
            patch.object(exercise_4, "interrupt", return_value=response),
            patch.dict(os.environ, {"GITHUB_USER": "minmin"}),
        ):
            await exercise_4.node_human_approval_pending(state)
            result = await exercise_4.node_human_approval(state)

        self.assertEqual(result, {"human_choice": "approve", "human_feedback": "migration is covered"})
        before, after = [call.args[1] for call in writer.await_args_list]
        self.assertEqual((before.action, before.decision, before.risk_level), ("human_approval", "pending", "med"))
        self.assertIsNone(before.reviewer_id)
        self.assertEqual((after.action, after.decision, after.reviewer_id), ("human_approval", "approve", "minmin"))
        self.assertEqual(after.reason, "migration is covered")

    async def test_escalation_audits_pending_then_answers(self):
        state = graph_input()
        writer = AsyncMock()
        answers = {"Why MD5 in auth.py?": "Only a local demo."}
        with (
            patch.object(exercise_4, "audit", writer),
            patch.object(exercise_4, "interrupt", return_value=answers) as interrupt,
            patch.dict(os.environ, {"GITHUB_USER": "minmin"}),
        ):
            await exercise_4.node_escalation_pending(state)
            result = await exercise_4.node_escalate(state)

        payload = interrupt.call_args.args[0]
        self.assertEqual(payload["kind"], "escalation")
        self.assertEqual(payload["questions"], ["Why MD5 in auth.py?"])
        self.assertEqual(result, {"escalation_answers": answers})
        before, after = [call.args[1] for call in writer.await_args_list]
        self.assertEqual((before.action, before.decision, before.reviewer_id), ("escalate", "pending", None))
        self.assertEqual((after.action, after.decision, after.reviewer_id), ("escalate", "escalate", "minmin"))
        self.assertIn("Only a local demo.", after.reason)

    async def test_async_nodes_audit_analysis_and_refined_confidence(self):
        initial = analysis(0.4)
        refined = analysis(0.9)
        writer = AsyncMock()
        llm = Mock()
        structured_llm = Mock()
        structured_llm.ainvoke = AsyncMock(return_value=initial)
        llm.with_structured_output.return_value = structured_llm
        with (
            patch.object(exercise_4, "get_llm", return_value=llm),
            patch.object(exercise_4, "audit", writer),
        ):
            await exercise_4.node_analyze(graph_input())
            analyze_entry = writer.await_args_list[-1].args[1]
            self.assertEqual((analyze_entry.action, analyze_entry.confidence, analyze_entry.risk_level),
                             ("analyze", 0.4, "high"))
            self.assertEqual(analyze_entry.decision, "pending")
            self.assertEqual(analyze_entry.reason, initial.confidence_reasoning)

            structured_llm.ainvoke = AsyncMock(return_value=refined)
            state = graph_input(initial)
            state["escalation_answers"] = {"Why MD5?": "Test-only account."}
            result = await exercise_4.node_synthesize(state)
            synthesize_entry = writer.await_args_list[-1].args[1]

        self.assertIs(result["analysis"], refined)
        self.assertEqual((synthesize_entry.action, synthesize_entry.confidence), ("synthesize", 0.9))
        self.assertEqual(synthesize_entry.risk_level, "low")
        self.assertEqual(synthesize_entry.decision, "pending")

    async def test_auto_approval_audits_automated_decision_without_reviewer(self):
        writer = AsyncMock()
        with (
            patch.object(exercise_4, "audit", writer),
            patch.object(exercise_4, "_post", return_value="committed"),
        ):
            result = await exercise_4.node_auto_approve(graph_input(analysis(0.9)))

        self.assertEqual(result, {"final_action": "auto_committed"})
        entry = writer.await_args.args[1]
        self.assertEqual((entry.action, entry.decision, entry.risk_level), ("auto_approve", "auto", "low"))
        self.assertIsNone(entry.reviewer_id)
        self.assertGreaterEqual(entry.execution_time_ms, 0)

    async def test_empty_escalation_answer_mapping_is_still_a_commit(self):
        state = graph_input(analysis(0.9))
        state["escalation_answers"] = {}
        writer = AsyncMock()
        with (
            patch.object(exercise_4, "audit", writer),
            patch.object(exercise_4, "_post", return_value="committed"),
        ):
            result = await exercise_4.node_commit(state)

        self.assertEqual(result, {"final_action": "committed"})
        self.assertEqual(writer.await_args.args[1].decision, "escalate")

    async def test_durable_escalation_resume_writes_replayable_audit_sequence(self):
        initial = analysis(0.4, ["Why MD5 in auth.py?"])
        refined = analysis(0.9)
        fake_pr = {
            "pr_title": "Add login", "pr_diff": "auth.py uses MD5",
            "pr_files": ["auth.py"], "pr_head_sha": "abcdef0",
        }
        llm = Mock()
        structured_llm = Mock()
        structured_llm.ainvoke = AsyncMock(side_effect=[initial, refined])
        llm.with_structured_output.return_value = structured_llm

        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "audit.db")
            with (
                patch.dict(os.environ, {"HITL_DB_PATH": database, "GITHUB_USER": "minmin"}),
                patch.object(exercise_4, "fetch_pr", return_value=SimpleNamespace(
                    title=fake_pr["pr_title"], diff=fake_pr["pr_diff"],
                    files_changed=fake_pr["pr_files"], head_sha=fake_pr["pr_head_sha"],
                )),
                patch.object(exercise_4, "get_llm", return_value=llm),
                patch.object(exercise_4, "post_review_comment") as post_comment,
            ):
                async with AsyncSqliteSaver.from_conn_string(database) as checkpointer:
                    await checkpointer.setup()
                    app = exercise_4.build_graph(checkpointer)
                    config = {"configurable": {"thread_id": THREAD_ID}}
                    with self.assertNoLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
                        paused = await app.ainvoke({"pr_url": PR_URL, "thread_id": THREAD_ID}, config)
                        self.assertEqual(paused["__interrupt__"][0].value["kind"], "escalation")
                        final = await app.ainvoke(
                            Command(resume={"Why MD5 in auth.py?": "Demo-only credentials."}), config
                        )

                events = await replay_events(THREAD_ID)

        self.assertEqual(final["final_action"], "committed")
        post_comment.assert_called_once()
        actions = [event["action"] for event in events]
        self.assertEqual(actions, ["fetch_pr", "analyze", "route", "escalate", "escalate", "synthesize", "commit"])
        self.assertEqual([event["decision"] for event in events if event["action"] == "escalate"],
                         ["pending", "escalate"])
        self.assertEqual(events[-2]["confidence"], 0.9)
        self.assertEqual(events[-1]["decision"], "escalate")
        self.assertEqual(events[4]["reviewer_id"], "minmin")
        self.assertIsNone(events[-1]["reviewer_id"])
