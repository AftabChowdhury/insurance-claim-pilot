"""The policy-chat LangGraph.

START → guard_input ─(blocked)──────────────────────────────────────────────────────────────┐
           └→ rewrite_query → [hybrid_search ∥ confidential_probe] → semantic_rank → build_context │
              → access_check ─(restricted | not_found)──────────────────────────────────────────┤
                    └→ answer_agent ─(partial access + public clauses can't answer)─────────────┤
                         → evaluator_agent ─(fail, retries left)→ prepare_retry                  │
                                            │            prepare_retry ─(retrieval)→ rewrite_query
                                            │            prepare_retry ─(generation)→ answer_agent
                                            ├─(fail, no retries left)───────────────────────────┤
                                            └→ guard_output ─(violation, once)→ answer_agent     │
                                                     └──────────────────────────→ respond ←──────┘ → END
"""
from langgraph.graph import END, START, StateGraph

from .agents import (access_check, answer_agent, build_context, confidential_probe, evaluator_agent, hybrid_search,
                     prepare_retry, respond, rewrite_query, route_after_access, route_after_answer, route_after_eval,
                     route_after_retry, semantic_rank)
from .guardrails import guard_input, guard_output, route_after_input, route_after_output
from .state import ChatState


def build_chat_graph(checkpointer=None):
    g = StateGraph(ChatState)
    for name, fn in [("guard_input", guard_input), ("rewrite_query", rewrite_query),
                     ("hybrid_search", hybrid_search), ("confidential_probe", confidential_probe),
                     ("semantic_rank", semantic_rank), ("build_context", build_context),
                     ("access_check", access_check), ("answer_agent", answer_agent),
                     ("evaluator_agent", evaluator_agent), ("prepare_retry", prepare_retry),
                     ("guard_output", guard_output), ("respond", respond)]:
        g.add_node(name, fn)

    g.add_edge(START, "guard_input")
    g.add_conditional_edges("guard_input", route_after_input, ["rewrite_query", "respond"])

    # public hybrid search and the confidential probe run in parallel, joined before ranking
    g.add_edge("rewrite_query", "hybrid_search")
    g.add_edge("rewrite_query", "confidential_probe")
    g.add_edge(["hybrid_search", "confidential_probe"], "semantic_rank")
    g.add_edge("semantic_rank", "build_context")
    g.add_edge("build_context", "access_check")
    g.add_conditional_edges("access_check", route_after_access, ["answer_agent", "respond"])

    # generate → evaluate → retry (re-retrieve or regenerate) / fallback
    g.add_conditional_edges("answer_agent", route_after_answer, ["evaluator_agent", "respond"])
    g.add_conditional_edges("evaluator_agent", route_after_eval, ["guard_output", "prepare_retry", "respond"])
    g.add_conditional_edges("prepare_retry", route_after_retry, ["rewrite_query", "answer_agent"])

    g.add_conditional_edges("guard_output", route_after_output, ["answer_agent", "respond"])
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)
