"""Structured outputs of the policy-chat agents."""
from typing import Literal

from pydantic import BaseModel, Field


class ChatQuery(BaseModel):
    standalone_question: str = Field(description="The user's latest message rewritten as a self-contained question")
    queries: list[str] = Field(description="2-4 short search queries against the insurance policy clauses")
    policy_type: Literal["home", "auto", "none"] = Field(
        description="'home' or 'auto' if the question is clearly about that policy, else 'none'")


class PolicyAnswer(BaseModel):
    answer: str = Field(description="Plain-language answer. Cite every policy statement inline like [Clause 4.2].")
    citations: list[str] = Field(description="All clause IDs cited in the answer, e.g. ['4.2', '5.2']")
    confidence: float = Field(ge=0, le=1, description="How well the context answers the question")
    answer_coverage: Literal["full", "partial", "none"] = Field(
        description="How much of the question the policy context answers. 'full' only if the context states the "
                    "specific facts asked for (amounts, percentages, thresholds, conditions); 'partial' if only "
                    "related general rules are there or the answer has to say a detail is not specified; 'none' "
                    "if the context does not contain the requested information")
    follow_up_suggestions: list[str] = Field(description="Up to 3 short follow-up questions the user might ask")


class ChatJudgement(BaseModel):
    faithfulness: float = Field(ge=0, le=1, description="Share of answer statements supported by the context")
    answer_relevance: float = Field(ge=0, le=1, description="Does the answer address the user's question?")
    citation_coverage: float = Field(ge=0, le=1,
                                     description="Share of policy statements that carry a correct [Clause] citation")
    unsupported_statements: list[str]
    missing_information: list[str] = Field(description="What the answer should have covered but did not")
    failure_cause: Literal["none", "retrieval", "generation"] = Field(
        description="'retrieval' if the context lacks what is needed to answer; 'generation' if the context has "
                    "it but the answer is wrong, unsupported or uncited; 'none' if the answer is good")
    feedback: str = Field(description="Concrete advice for the next attempt")
