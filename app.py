"""Streamlit approval UI for the HITL PR review agent.

Run with::

    uv run streamlit run app.py
"""

from __future__ import annotations

import asyncio
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from streamlit.runtime.scriptrunner_utils.script_run_context import get_script_run_ctx

from common.db import db_conn, db_path
from exercises.exercise_4_audit import build_graph


def initialize_session_state() -> None:
    defaults = {
        "thread_id": None,
        "pr_url": "",
        "interrupt_payload": None,
        "final": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


async def run_graph(pr_url: str, thread_id: str, resume_value=None):
    """Invoke or resume Exercise 4 on the same durable thread."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as checkpointer:
        await checkpointer.setup()
        graph = build_graph(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        if resume_value is None:
            return await graph.ainvoke(
                {"pr_url": pr_url, "thread_id": thread_id},
                config,
            )
        return await graph.ainvoke(Command(resume=resume_value), config)


async def list_recent_sessions(limit: int = 25) -> list[dict]:
    """Return recent sessions with a risk rank and last audit event."""
    async with db_conn() as conn:
        async with conn.execute(
            """
            SELECT thread_id,
                   pr_url,
                   MIN(timestamp) AS started,
                   MAX(timestamp) AS last_event,
                   CASE MAX(CASE risk_level
                            WHEN 'high' THEN 3
                            WHEN 'med' THEN 2
                            ELSE 1 END)
                       WHEN 3 THEN 'high'
                       WHEN 2 THEN 'med'
                       ELSE 'low' END AS worst_risk,
                   COUNT(*) AS event_count
              FROM audit_events
             GROUP BY thread_id, pr_url
             ORDER BY last_event DESC
             LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def load_session(thread_id: str) -> dict:
    """Read the latest checkpoint and expose any saved interrupt payload."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as checkpointer:
        await checkpointer.setup()
        graph = build_graph(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await graph.aget_state(config)

    interrupt_payload = None
    for task in getattr(snapshot, "tasks", ()):
        for interrupted in getattr(task, "interrupts", ()):
            interrupt_payload = interrupted.value
            break
        if interrupt_payload is not None:
            break

    values = dict(snapshot.values)
    return {
        "values": values,
        "interrupt_payload": interrupt_payload,
        "final": None if interrupt_payload is not None else values,
    }


def render_recent_sessions() -> None:
    """List sessions and restore the selected session from its checkpoint."""
    with st.sidebar:
        st.header("Recent sessions")
        sessions = asyncio.run(list_recent_sessions())
        if not sessions:
            st.caption("No review sessions yet.")
            return

        selected = st.selectbox(
            "Open a saved session",
            options=sessions,
            format_func=lambda session: (
                f"{session['pr_url']} · {session['worst_risk']} risk · "
                f"{session['last_event']}"
            ),
            key="recent_session",
        )
        if selected is not None and st.button("Open selected session"):
            restored = asyncio.run(load_session(selected["thread_id"]))
            st.session_state["thread_id"] = selected["thread_id"]
            st.session_state["pr_url"] = selected["pr_url"]
            st.session_state["interrupt_payload"] = restored["interrupt_payload"]
            st.session_state["final"] = restored["final"]
            st.rerun()


def render_approval_card(payload: dict) -> dict | None:
    """Render an approval interrupt and return its exact resume value."""
    confidence = payload["confidence"]
    st.subheader(f"Approval requested — confidence {confidence:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])

    for comment in payload.get("comments", []):
        st.markdown(
            f"- **[{comment['severity']}]** "
            f"`{comment['file']}:{comment.get('line') or '?'}` — {comment['body']}"
        )

    with st.expander("Diff"):
        st.code(payload.get("diff_preview", ""), language="diff")

    feedback = st.text_input("Feedback (optional)", key="approval_feedback")
    approve, reject, edit = st.columns(3)
    if approve.button("Approve", type="primary"):
        return {"choice": "approve", "feedback": feedback}
    if reject.button("Reject"):
        return {"choice": "reject", "feedback": feedback}
    if edit.button("Edit"):
        return {"choice": "edit", "feedback": feedback}
    return None


def render_escalation_card(payload: dict) -> dict[str, str] | None:
    """Render context questions and return a question-to-answer mapping."""
    confidence = payload["confidence"]
    st.subheader(f"Strong escalation — confidence {confidence:.0%}")
    st.caption(payload["confidence_reasoning"])
    if payload.get("risk_factors"):
        st.error("Risks: " + ", ".join(payload["risk_factors"]))
    st.markdown(payload["summary"])

    with st.form("escalation"):
        answers = {
            question: st.text_input(question, key=f"escalation_answer_{index}")
            for index, question in enumerate(payload["questions"])
        }
        submitted = st.form_submit_button("Submit answers")
    return answers if submitted else None


def render_final_result(final: dict, pr_url: str, thread_id: str) -> None:
    """Show the final review summary and a link to its pull request."""
    action = final.get("final_action", "?")
    if action == "auto_committed":
        st.success("Automatically approved — review comment posted.")
    elif action in {"committed", "committed_after_escalation"}:
        st.success("Review completed — comment posted to GitHub.")
    elif action in {"auto_commit_failed", "commit_failed"}:
        st.error("The review comment could not be posted.")
    elif action == "rejected":
        st.warning("Rejected — no comment posted.")
    else:
        st.info(f"final_action = {action}")

    analysis = final.get("analysis")
    if analysis is not None:
        st.subheader(f"Review confidence: {analysis.confidence:.0%}")
        st.markdown(analysis.summary)
        if action == "auto_committed":
            st.markdown(f"[View comment on GitHub]({pr_url})")
        else:
            st.markdown(f"[View pull request on GitHub]({pr_url})")

    st.caption(
        f"thread_id = {thread_id} · replay: "
        f"`uv run python -m audit.replay --thread {thread_id}`"
    )


def _store_graph_result(result: dict) -> None:
    if "__interrupt__" in result:
        st.session_state["interrupt_payload"] = result["__interrupt__"][0].value
        st.session_state["final"] = None
    else:
        st.session_state["interrupt_payload"] = None
        st.session_state["final"] = result


def main() -> None:
    load_dotenv()
    st.set_page_config(page_title="HITL PR Review", layout="wide")
    initialize_session_state()
    st.title("HITL PR Review Agent")

    render_recent_sessions()

    with st.form("start"):
        pr_url = st.text_input(
            "PR URL",
            value=st.session_state["pr_url"],
            placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
        )
        submitted = st.form_submit_button("Run review")

    if submitted and pr_url.strip():
        thread_id = str(uuid.uuid4())
        st.session_state["pr_url"] = pr_url.strip()
        st.session_state["thread_id"] = thread_id
        st.session_state["interrupt_payload"] = None
        st.session_state["final"] = None
        with st.spinner("Fetching PR + asking the LLM..."):
            result = asyncio.run(run_graph(st.session_state["pr_url"], thread_id))
        _store_graph_result(result)

    payload = st.session_state["interrupt_payload"]
    if payload is not None:
        if payload["kind"] == "approval_request":
            answer = render_approval_card(payload)
        elif payload["kind"] == "escalation":
            answer = render_escalation_card(payload)
        else:
            st.error(f"Unknown review interrupt: {payload['kind']}")
            answer = None

        if answer is not None:
            with st.spinner("Resuming review..."):
                result = asyncio.run(run_graph(
                    st.session_state["pr_url"],
                    st.session_state["thread_id"],
                    resume_value=answer,
                ))
            _store_graph_result(result)
            st.rerun()

    if st.session_state["final"] is not None:
        render_final_result(
            st.session_state["final"],
            st.session_state["pr_url"],
            st.session_state["thread_id"],
        )


if get_script_run_ctx() is not None:
    main()
