from __future__ import annotations

import re
from typing import Any, Mapping


class StarNativePlanMixin:
    """Semantic plan construction that orchestrates all understanding layers."""

    @classmethod
    def semantic_plan(
        cls,
        prompt: str,
        *,
        context: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        raw_input = str(prompt or "")
        normalization = cls._normalize_chinese_semantics(raw_input)
        normalized = str(normalization["text"])
        intents = cls.classify_intents(normalized)
        _, prohibited_intents = cls._matched_intents(normalized.casefold())
        tokenized = cls._tokenize(normalized)
        matched_terms = {
            intent: [token for token in tokens if token.casefold() in normalized.casefold()]
            for intent, tokens in cls._INTENTS
            if intent in intents
        }
        symbols = list(
            dict.fromkeys(
                match.upper()
                for match in re.findall(
                    r"(?<![A-Za-z0-9])(?:[A-Z]{1,6}|\d{4,6})(?:\.(?:TW|TWO|HK|TO|L))?(?![A-Za-z0-9])",
                    normalized,
                    flags=re.IGNORECASE,
                )
            )
        )[:20]
        isins = list(
            dict.fromkeys(
                match.replace(" ", "").replace("-", "").upper()
                for match in re.findall(
                    r"\b[A-Z]{2}[A-Z0-9 -]{9,14}\d\b",
                    normalized,
                    flags=re.IGNORECASE,
                )
            )
        )[:10]
        markets = [
            market
            for label, market in cls._MARKET_ALIASES.items()
            if label in normalized
        ]
        time_horizon = next(
            (
                horizon
                for token, horizon in (
                    ("短期", "short"),
                    ("中期", "medium"),
                    ("長期", "long"),
                    ("今年", "year-to-date"),
                )
                if token in normalized
            ),
            "unspecified",
        )
        comprehension = cls._comprehension_features(normalized)
        actions = cls._extract_command_actions(normalized)
        if actions:
            comprehension["command"]["recognized"] = True
            comprehension["command"]["requested_actions"] = list(
                dict.fromkeys(
                    [
                        *comprehension["command"]["requested_actions"],
                        *[str(item["action"]) for item in actions],
                    ]
                )
            )
            comprehension["command"]["execution_requested"] = bool(
                comprehension["command"]["execution_requested"]
                or any(
                    str(item["action"])
                    in {
                        "create",
                        "generate",
                        "modify",
                        "delete",
                        "move",
                        "copy",
                        "rename",
                        "execute",
                        "stop",
                    }
                    for item in actions
                )
            )
        operation_objects = cls._extract_operation_objects(normalized)
        parameters = cls._extract_command_parameters(raw_input)
        specific_constraints = cls._specific_constraint_flags(normalized)
        context_completion = cls._context_completion(
            command=raw_input.strip(),
            context=context,
            actions=actions,
            objects=operation_objects,
            parameters=parameters,
        )
        intent_explicit = bool(matched_terms)
        effective_actions = list(actions)
        effective_objects = list(operation_objects)
        if (
            context_completion["needed"] is True
            and context_completion["resolved"] is True
        ):
            for item in context_completion["context_actions"]:
                if item.get("action") not in {
                    action.get("action") for action in effective_actions
                }:
                    effective_actions.append(dict(item))
            for item in context_completion["context_objects"]:
                if item.get("object_type") not in {
                    target.get("object_type") for target in effective_objects
                }:
                    effective_objects.append(dict(item))
            if not intent_explicit and str(context or "").strip():
                context_intents = cls.classify_intents(str(context))
                intents = list(
                    dict.fromkeys(
                        [
                            *context_intents,
                            *[intent for intent in intents if intent != "conversation"],
                        ]
                    )
                ) or intents
                context_lower = str(context).casefold()
                matched_terms = {
                    intent: [
                        token
                        for candidate_intent, tokens in cls._INTENTS
                        if candidate_intent == intent
                        for token in tokens
                        if token.casefold() in context_lower
                    ]
                    for intent in intents
                }
        safety = cls._command_safety(
            command=raw_input,
            actions=actions,
            objects=operation_objects,
            parameters=parameters,
            context_completion=context_completion,
            intent_explicit=intent_explicit,
            confirmed=bool(confirmed),
        )
        task_classification = cls._task_classification(
            intents=intents,
            actions=effective_actions,
            objects=effective_objects,
            text=normalized,
        )
        effective_action_names = {
            str(item.get("action") or "") for item in effective_actions
        }
        effective_object_names = {
            str(item.get("object_type") or "") for item in effective_objects
        }
        if (
            effective_object_names & {"image", "video"}
            and effective_action_names & {"query", "classify", "analyze", "compare"}
            and "visual" not in intents
        ):
            intents = ["visual", *[intent for intent in intents if intent != "conversation"]]
            task_classification = cls._task_classification(
                intents=intents,
                actions=effective_actions,
                objects=effective_objects,
                text=normalized,
            )
        ambiguities = list(safety["remaining_ambiguities"])
        if not matched_terms and not actions:
            ambiguities.append("intent-not-explicit")
        task_intensity = cls._task_intensity(
            actions=effective_actions,
            objects=effective_objects,
            constraints=[*comprehension["constraints"], *specific_constraints],
            task_categories=task_classification["categories"],
            ambiguity_count=len(ambiguities),
            destructive_operation=safety["destructive_operation"] is True,
            text=normalized,
        )
        tasks = []
        for sequence, intent in enumerate(intents, start=1):
            required_inputs = {
                "search": ["holdings-or-symbols"],
                "distribution": ["holdings-or-symbols"],
                "quote": ["holdings-or-symbols"],
                "risk": ["holdings"],
                "analysis": ["holdings"],
                "calculation": ["expression-or-cash-flows"],
                "statistics": ["numbers-or-return-series"],
                "data_organization": ["records"],
                "reasoning": ["premises"],
                "coding": ["requirements-or-code-spec"],
                "visual": ["images-or-video-frames-or-document-images"],
                "file_management": ["files-and-visual-content-when-applicable"],
                "self_upgrade": ["upgrade-requirements-or-code-spec"],
                "reading": ["document-text-or-documents"],
            }.get(intent, [])
            tasks.append(
                {
                    "sequence": sequence,
                    "intent": intent,
                    "depends_on": [sequence - 1] if sequence > 1 else [],
                    "required_inputs": required_inputs,
                    "matched_terms": matched_terms.get(intent, []),
                    "confidence": round(
                        min(0.99, 0.62 + 0.08 * len(matched_terms.get(intent, []))),
                        2,
                    ),
                }
            )
        return {
            "schema": "star-semantic-plan/v1",
            "understanding_layer": "traditional-chinese-taiwan-first",
            "raw_input": raw_input,
            "normalized_input": normalized,
            "normalization": normalization,
            "language": cls._language(normalized),
            "language_priority": ["zh-TW", "mixed-zh-latin", "en"],
            "english_intermediate_representation_used": False,
            "token_count": len(tokenized),
            "primary_intent": intents[0],
            "intents": intents,
            "prohibited_intents": sorted(prohibited_intents),
            "entities": {
                "symbols": symbols,
                "isins": isins,
                "markets": list(dict.fromkeys(markets)),
                "numbers": [float(value) for value in re.findall(r"\d+(?:\.\d+)?", normalized)[:30]],
                "dates": re.findall(
                    r"(?<!\d)(?:19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])(?:[-/.月](?:0?[1-9]|[12]\d|3[01])日?)?(?!\d)",
                    normalized,
                )[:20],
                "percentages": re.findall(r"(?<!\w)[+-]?\d+(?:\.\d+)?%", normalized)[:20],
                "money": re.findall(
                    r"(?:NT\$|US\$|\$|新台幣|美元)\s?\d[\d,.]*",
                    normalized,
                    flags=re.IGNORECASE,
                )[:20],
                "urls": re.findall(
                    r"https?://[^\s)\]>，。！？；]+", normalized
                )[:20],
                "emails": re.findall(
                    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])",
                    normalized,
                )[:20],
                "time_horizon": time_horizon,
            },
            "comprehension": comprehension,
            "actions": actions,
            "operation_objects": operation_objects,
            "parameters": parameters,
            "specific_constraints": specific_constraints,
            "context_completion": context_completion,
            "resolved_command": context_completion["resolved_command"],
            "intent_determination": {
                "primary_intent": intents[0],
                "actions": [item["action"] for item in effective_actions],
                "operation_objects": [
                    item["object_type"] for item in effective_objects
                ],
                "actual_operation_separately_gated": True,
            },
            "task_classification": task_classification,
            "task_intensity": task_intensity,
            "safety": safety,
            "tasks": tasks,
            "ambiguities": list(dict.fromkeys(ambiguities)),
        }
