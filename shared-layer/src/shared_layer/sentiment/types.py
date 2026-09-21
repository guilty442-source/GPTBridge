from enum import Enum
from dataclasses import dataclass
from typing import Optional


class SentimentPolarity(Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class EmotionCategory(Enum):
    JOY = "joy"
    ANGER = "anger"
    SORROW = "sorrow"
    HAPPINESS = "happiness"
    FEAR = "fear"
    SURPRISE = "surprise"

    @property
    def chinese(self) -> str:
        mapping = {
            EmotionCategory.JOY: "喜",
            EmotionCategory.ANGER: "怒",
            EmotionCategory.SORROW: "哀",
            EmotionCategory.HAPPINESS: "樂",
            EmotionCategory.FEAR: "懼",
            EmotionCategory.SURPRISE: "驚",
        }
        return mapping[self]


class IntensityLevel(Enum):
    VERY_LOW = 1
    LOW = 2
    MODERATE = 3
    HIGH = 4
    VERY_HIGH = 5

    @property
    def label(self) -> str:
        mapping = {
            IntensityLevel.VERY_LOW: "極低",
            IntensityLevel.LOW: "低",
            IntensityLevel.MODERATE: "中等",
            IntensityLevel.HIGH: "高",
            IntensityLevel.VERY_HIGH: "極高",
        }
        return mapping[self]


@dataclass(frozen=True)
class EmotionScore:
    category: EmotionCategory
    score: float
    intensity: IntensityLevel

    def __post_init__(self):
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")


@dataclass(frozen=True)
class SentimentResult:
    text: str
    polarity: SentimentPolarity
    polarity_score: float
    primary_emotion: Optional[EmotionScore] = None
    emotion_scores: tuple[EmotionScore, ...] = ()
    overall_intensity: IntensityLevel = IntensityLevel.MODERATE

    def __post_init__(self):
        if not 0.0 <= self.polarity_score <= 1.0:
            raise ValueError("polarity_score must be between 0.0 and 1.0")
        if self.primary_emotion is not None and self.primary_emotion not in self.emotion_scores:
            object.__setattr__(self, "emotion_scores", (*self.emotion_scores, self.primary_emotion))

    @property
    def is_positive(self) -> bool:
        return self.polarity == SentimentPolarity.POSITIVE

    @property
    def is_negative(self) -> bool:
        return self.polarity == SentimentPolarity.NEGATIVE

    @property
    def is_neutral(self) -> bool:
        return self.polarity == SentimentPolarity.NEUTRAL