"""The ClaimPilot LangGraph: supervisor-style workflow with parallel agents, a retry loop and a human interrupt.

START → verify_evidence → guard_input ─(attack)→ human_review (security: reject | release ↺ intake_agent)
        └→ intake_agent → rewrite_query → hybrid_search → semantic_rank → build_context
           → [coverage_agent ∥ fraud_agent] → writer_agent
           → evaluator_agent ─(fail, retries left)→ prepare_retry ↺ rewrite_query
           → guard_output ─(needs review)→ human_review (admin accept / reject) ─┐
                          └(automatic decision)──────────────→ close_claim ←────┘ → END
"""
from langgraph.graph import END, START, StateGraph

from .agents import (coverage_agent, evaluator_agent, fraud_agent, intake_agent, prepare_retry, route_after_eval,
                     writer_agent)
from .guardrails import guard_input, guard_output, route_after_input, route_after_output
from .evidence import verify_evidence
from .human import close_claim, human_review, route_after_human
from .retrieval import build_context, hybrid_search, rewrite_query, semantic_rank
from .state import ClaimState


def build_graph(checkpointer=None):
    g = StateGraph(ClaimState)

    g.add_node("verify_evidence", verify_evidence)
    g.add_node("guard_input", guard_input)
    g.add_node("intake_agent", intake_agent)
    g.add_node("rewrite_query", rewrite_query)
    g.add_node("hybrid_search", hybrid_search)
    g.add_node("semantic_rank", semantic_rank)
    g.add_node("build_context", build_context)
    g.add_node("coverage_agent", coverage_agent)
    g.add_node("fraud_agent", fraud_agent)
    g.add_node("writer_agent", writer_agent)
    g.add_node("evaluator_agent", evaluator_agent)
    g.add_node("prepare_retry", prepare_retry)
    g.add_node("guard_output", guard_output)
    g.add_node("human_review", human_review)
    g.add_node("close_claim", close_claim)

    g.add_edge(START, "verify_evidence")
    g.add_edge("verify_evidence", "guard_input")
    g.add_conditional_edges("guard_input", route_after_input, ["intake_agent", "human_review"])

    # Retrieve → Rank → Context
    g.add_edge("intake_agent", "rewrite_query")
    g.add_edge("rewrite_query", "hybrid_search")
    g.add_edge("hybrid_search", "semantic_rank")
    g.add_edge("semantic_rank", "build_context")

    # Parallel specialist agents, joined before generation
    g.add_edge("build_context", "coverage_agent")
    g.add_edge("build_context", "fraud_agent")
    g.add_edge(["coverage_agent", "fraud_agent"], "writer_agent")

    # Generate → Evaluate → retry
    g.add_edge("writer_agent", "evaluator_agent")
    g.add_conditional_edges("evaluator_agent", route_after_eval, ["prepare_retry", "guard_output"])
    g.add_edge("prepare_retry", "rewrite_query")

    # Guardrails → human in the loop → close
    g.add_conditional_edges("guard_output", route_after_output, ["human_review", "close_claim"])
    g.add_conditional_edges("human_review", route_after_human, ["intake_agent", "close_claim"])
    g.add_edge("close_claim", END)

    return g.compile(checkpointer=checkpointer)
