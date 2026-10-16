from __future__ import annotations

from unittest import TestCase
from unittest.mock import Mock, patch

from langgraph.types import Command

import exercises.exercise_1_confidence as exercise_1
import exercises.exercise_2_hitl as exercise_2
from common.schemas import PRAnalysis, ReviewComment


PR_URL = "https://github.com/example/repo/pull/1"


def analysis(confidence: float) -> PRAnalysis:
    return PRAnalysis(
        summary="Adds task priority.",
        risk_factors=["Migration compatibility needs review."],
        comments=[ReviewComment(
            file="src/tasks.py", line=12, severity="issue", body="Check migration."
        )],
        confidence=confidence,
        confidence_reasoning="One schema question remains.",
        escalation_questions=[],
    )


class Exercise1Tests(TestCase):
    def test_route_confidence_boundaries(self):
        for value, expected in (
            (0.579, "escalate"),
            (0.58, "human_approval"),
            (0.729, "human_approval"),
            (0.73, "auto_approve"),
        ):
            with self.subTest(confidence=value):
                self.assertEqual(
                    exercise_1.node_route({"analysis": analysis(value)}),
                    {"decision": expected},
                )

    def test_node_analyze_uses_structured_output_and_pr_context(self):
        expected = analysis(0.65)
        structured_llm = Mock()
        structured_llm.invoke.return_value = expected
        llm = Mock()
        llm.with_structured_output.return_value = structured_llm

        with patch.object(exercise_1, "get_llm", return_value=llm):
            result = exercise_1.node_analyze({
                "pr_title": "Add task priority", "pr_diff": "@@ -1 +1 @@\n+priority"
            })

        self.assertIs(result["analysis"], expected)
        llm.with_structured_output.assert_called_once_with(PRAnalysis)
        messages = structured_llm.invoke.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Add task priority", messages[1]["content"])
        self.assertIn("priority", messages[1]["content"])

    def test_graph_routes_each_confidence_bucket_to_its_terminal(self):
        cases = (
            (0.80, "auto_approved"),
            (0.65, "pending_human_approval"),
            (0.40, "pending_escalation"),
        )
        for value, expected_action in cases:
            with self.subTest(confidence=value):
                with (
                    patch.object(exercise_1, "node_fetch_pr", return_value={}),
                    patch.object(
                        exercise_1, "node_analyze", return_value={"analysis": analysis(value)}
                    ),
                ):
                    result = exercise_1.build_graph().invoke({"pr_url": PR_URL})
                self.assertEqual(result["final_action"], expected_action)


class Exercise2Tests(TestCase):
    def invoke_until_interrupt(self, app, cfg):
        return app.invoke({"pr_url": PR_URL, "thread_id": cfg["configurable"]["thread_id"]}, cfg)

    def test_approval_payload_and_resume_posts_only_on_approve(self):
        fake_pr = {
            "pr_title": "Add task priority",
            "pr_diff": "x" * 2500,
            "pr_files": ["src/tasks.py"],
            "pr_head_sha": "abcdef0",
        }
        with (
            patch.object(exercise_2, "node_fetch_pr", return_value=fake_pr),
            patch.object(
                exercise_2, "node_analyze", return_value={"analysis": analysis(0.65)}
            ),
            patch.object(exercise_2, "post_review_comment") as post_comment,
        ):
            app = exercise_2.build_graph()
            cfg = {"configurable": {"thread_id": "approval-test"}}
            with self.assertNoLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
                paused = self.invoke_until_interrupt(app, cfg)

                self.assertIn("__interrupt__", paused)
                payload = paused["__interrupt__"][0].value
                self.assertEqual(payload["kind"], "approval_request")
                self.assertEqual(payload["confidence"], 0.65)
                self.assertEqual(payload["confidence_reasoning"], "One schema question remains.")
                self.assertEqual(payload["summary"], "Adds task priority.")
                self.assertEqual(payload["comments"], [analysis(0.65).comments[0].model_dump()])
                self.assertEqual(payload["diff_preview"], "x" * 2000)
                post_comment.assert_not_called()

                final = app.invoke(
                    Command(resume={"choice": "approve", "feedback": "looks good"}), cfg
                )
                self.assertEqual(final["final_action"], "committed")
                post_comment.assert_called_once_with(PR_URL, unittest_mock_comment_body())

    def test_reject_and_edit_do_not_post_comments(self):
        fake_pr = {
            "pr_title": "Add task priority", "pr_diff": "diff",
            "pr_files": ["src/tasks.py"], "pr_head_sha": "abcdef0",
        }
        for choice in ("reject", "edit"):
            with self.subTest(choice=choice):
                with (
                    patch.object(exercise_2, "node_fetch_pr", return_value=fake_pr),
                    patch.object(
                        exercise_2, "node_analyze", return_value={"analysis": analysis(0.65)}
                    ),
                    patch.object(exercise_2, "post_review_comment") as post_comment,
                ):
                    app = exercise_2.build_graph()
                    cfg = {"configurable": {"thread_id": f"{choice}-test"}}
                    self.invoke_until_interrupt(app, cfg)
                    final = app.invoke(
                        Command(resume={"choice": choice, "feedback": "please adjust"}), cfg
                    )
                    self.assertEqual(final["final_action"], "rejected")
                    post_comment.assert_not_called()


def unittest_mock_comment_body() -> str:
    return (
        "### Automated review (confidence 65%)\n\n"
        "Adds task priority.\n\n"
        "- **[issue]** `src/tasks.py:12` — Check migration.\n\n"
        "_Reviewer note: looks good_"
    )
