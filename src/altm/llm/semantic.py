"""Parallel semantic evaluator chain for L3/L4 governance gates."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from altm.contracts import SemanticEvaluation, SemanticGateResult
from altm.llm.openai_compatible import OpenAICompatibleClient, llm_config_from_env


@dataclass(frozen=True)
class EvaluatorSpec:
    stage: str
    task: str
    labels: tuple[str, ...]
    rubric: str


SCENE_EVALUATORS = (
    EvaluatorSpec(
        stage="reranker",
        task="scene_relevance",
        labels=("match", "mismatch"),
        rubric=(
            "Determine whether all candidate memories describe the same reusable "
            "project, task, topic, relationship, or workflow scene."
        ),
    ),
    EvaluatorSpec(
        stage="nli",
        task="scene_nli",
        labels=("entails", "contradicts", "neutral"),
        rubric=(
            "Detect semantic contradiction between candidate memories. Differences "
            "in negation, requirement strength, project, time, or scope matter."
        ),
    ),
    EvaluatorSpec(
        stage="scope",
        task="scene_boundary",
        labels=("same_boundary", "boundary_risk"),
        rubric=(
            "Check project, workspace, time, actor, and task boundaries. Similar "
            "wording across incompatible boundaries is boundary_risk."
        ),
    ),
    EvaluatorSpec(
        stage="l3",
        task="scene_judge",
        labels=("same_scene", "weak_relation", "reject"),
        rubric=(
            "Judge whether the evidence forms one coherent L3 SceneBlock rather "
            "than a text cluster. Require a shared context, explanation, and navigation."
        ),
    ),
)


PERSONA_EVALUATORS = (
    EvaluatorSpec(
        stage="reranker",
        task="persona_relevance",
        labels=("match", "mismatch"),
        rubric=(
            "Determine whether evidence supports one consistent user persona facet."
        ),
    ),
    EvaluatorSpec(
        stage="nli",
        task="persona_nli",
        labels=("entails", "contradicts", "neutral"),
        rubric=(
            "Detect counter-evidence, preference reversal, scope mismatch, or "
            "conflicting constraints."
        ),
    ),
    EvaluatorSpec(
        stage="scope",
        task="persona_scope",
        labels=("user_workspace", "project", "temporary"),
        rubric=(
            "Classify the evidence as a stable user-workspace trait, a project-only "
            "rule, or a temporary one-turn instruction."
        ),
    ),
    EvaluatorSpec(
        stage="l4",
        task="persona_judge",
        labels=("stable", "observe", "reject"),
        rubric=(
            "Judge whether repeated evidence supports a stable L4 persona facet. "
            "A single session, a temporary request, or unresolved conflict cannot be stable."
        ),
    ),
)


class OpenAICompatibleSemanticEvaluator:
    def __init__(self, spec: EvaluatorSpec) -> None:
        self.spec = spec
        self.client = OpenAICompatibleClient(llm_config_from_env(spec.stage))

    def evaluate(
        self,
        payload: Mapping[str, object],
        evidence_memory_ids: Sequence[str],
    ) -> SemanticEvaluation:
        response = self.client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You are an ALTM semantic evaluator. Return only JSON with "
                        "label, score (0-1), and reason. Allowed labels: %s. %s"
                        % (", ".join(self.spec.labels), self.spec.rubric)
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ]
        )
        label = str(response.get("label", "")).strip().lower()
        score_value = response.get("score")
        reason = str(response.get("reason", "")).strip()
        if label not in self.spec.labels:
            raise ValueError(
                "%s evaluator returned unsupported label: %s"
                % (self.spec.stage, label)
            )
        if not isinstance(score_value, (int, float)) or not 0.0 <= float(score_value) <= 1.0:
            raise ValueError("%s evaluator requires score between 0 and 1" % self.spec.stage)
        if not reason:
            raise ValueError("%s evaluator requires a non-empty reason" % self.spec.stage)
        return SemanticEvaluation(
            evaluator=self.spec.stage,
            task=self.spec.task,
            label=label,
            score=float(score_value),
            reason=reason,
            model=self.client.config.model,
            evidence_memory_ids=list(evidence_memory_ids),
            output=response,
        )


class SemanticModelChain:
    def __init__(
        self,
        specs: Sequence[EvaluatorSpec],
        min_score: float | None = None,
    ) -> None:
        self.specs = tuple(specs)
        configured_score = (
            float(os.environ["ALTM_SEMANTIC_MIN_SCORE"])
            if "ALTM_SEMANTIC_MIN_SCORE" in os.environ
            else 0.80
        )
        self.min_score = configured_score if min_score is None else min_score
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError("ALTM semantic minimum score must be between 0 and 1")

    @classmethod
    def for_scene(cls, min_score: float | None = None) -> SemanticModelChain:
        return cls(SCENE_EVALUATORS, min_score=min_score)

    @classmethod
    def for_persona(cls, min_score: float | None = None) -> SemanticModelChain:
        return cls(PERSONA_EVALUATORS, min_score=min_score)

    def evaluate_scene(
        self,
        payload: Mapping[str, object],
        evidence_memory_ids: Sequence[str],
    ) -> SemanticGateResult:
        evaluations, errors = self._evaluate(payload, evidence_memory_ids)
        if errors:
            return _deferred("required_evaluator_unavailable", evaluations, errors)
        by_stage = {evaluation.evaluator: evaluation for evaluation in evaluations}
        if by_stage["nli"].label == "contradicts":
            return _gate("keep_both", evaluations, "scene evidence contradicts")
        if by_stage["scope"].label == "boundary_risk":
            return _gate("weak_edge", evaluations, "scene boundary risk is unresolved")
        if by_stage["reranker"].label != "match":
            return _gate("defer", evaluations, "scene candidates do not match")
        if by_stage["l3"].label == "weak_relation":
            return _gate("weak_edge", evaluations, "only a weak relation is supported")
        if by_stage["l3"].label != "same_scene":
            return _gate("defer", evaluations, "L3 judge rejected materialization")
        if min(evaluation.score for evaluation in evaluations) < self.min_score:
            return _gate("defer", evaluations, "semantic evaluator confidence is too low")
        return _gate("write_observing", evaluations, "all scene evaluators passed")

    def evaluate_persona(
        self,
        payload: Mapping[str, object],
        evidence_memory_ids: Sequence[str],
    ) -> SemanticGateResult:
        evaluations, errors = self._evaluate(payload, evidence_memory_ids)
        if errors:
            return _deferred("required_evaluator_unavailable", evaluations, errors)
        by_stage = {evaluation.evaluator: evaluation for evaluation in evaluations}
        if by_stage["nli"].label == "contradicts":
            return _gate("observe_conflict", evaluations, "persona counter-evidence exists")
        if by_stage["scope"].label != "user_workspace":
            return _gate(
                "keep_lower_layer",
                evaluations,
                "evidence is project-scoped or temporary",
            )
        if by_stage["reranker"].label != "match":
            return _gate("defer", evaluations, "persona evidence is not coherent")
        if by_stage["l4"].label != "stable":
            return _gate("observe", evaluations, "persona stability is not established")
        if min(evaluation.score for evaluation in evaluations) < self.min_score:
            return _gate("observe", evaluations, "persona evaluator confidence is too low")
        return _gate("write_observing", evaluations, "all persona evaluators passed")

    def _evaluate(
        self,
        payload: Mapping[str, object],
        evidence_memory_ids: Sequence[str],
    ) -> tuple[list[SemanticEvaluation], dict[str, str]]:
        evaluations: list[SemanticEvaluation] = []
        errors: dict[str, str] = {}
        evaluators: dict[str, OpenAICompatibleSemanticEvaluator] = {}
        for spec in self.specs:
            try:
                evaluators[spec.stage] = OpenAICompatibleSemanticEvaluator(spec)
            except Exception as exc:
                errors[spec.stage] = str(exc)
        if errors:
            return evaluations, errors

        with ThreadPoolExecutor(max_workers=len(evaluators)) as executor:
            futures = {
                executor.submit(
                    evaluator.evaluate,
                    payload,
                    evidence_memory_ids,
                ): stage
                for stage, evaluator in evaluators.items()
            }
            for future in as_completed(futures):
                stage = futures[future]
                try:
                    evaluations.append(future.result())
                except Exception as exc:
                    errors[stage] = str(exc)
        evaluations.sort(key=lambda item: item.evaluator)
        return evaluations, errors


def _gate(
    decision: str,
    evaluations: Sequence[SemanticEvaluation],
    reason: str,
) -> SemanticGateResult:
    confidence = min((evaluation.score for evaluation in evaluations), default=0.0)
    return SemanticGateResult(
        decision=decision,
        confidence=confidence,
        reason=reason,
        evaluations=list(evaluations),
    )


def _deferred(
    reason: str,
    evaluations: Sequence[SemanticEvaluation],
    errors: Mapping[str, str],
) -> SemanticGateResult:
    return SemanticGateResult(
        decision="defer",
        confidence=0.0,
        reason=reason,
        evaluations=list(evaluations),
        metadata={"errors": dict(errors)},
    )
