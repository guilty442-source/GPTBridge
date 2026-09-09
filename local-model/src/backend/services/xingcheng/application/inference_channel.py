from __future__ import annotations

from .infer_envelopes import InferEnvelopesMixin
from .infer_finalize import InferFinalizeMixin
from .infer_handle import InferHandleMixin
from .infer_planning import InferPlanningMixin
from .infer_specialists import InferSpecialistsMixin
from .infer_transformer import InferTransformerMixin


class InferenceChannelMixin(
    InferHandleMixin,
    InferSpecialistsMixin,
    InferTransformerMixin,
    InferEnvelopesMixin,
    InferFinalizeMixin,
    InferPlanningMixin,
):
    pass
