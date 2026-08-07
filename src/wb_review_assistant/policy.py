from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CONTACT = re.compile(
    r"(?:https?://|www\.|@[\w.-]+|[\w.+-]+@[\w.-]+|(?:\+7|8)[\s()\-\d]{9,})", re.IGNORECASE
)
ABUSE = re.compile(r"\b(?:идиот|мраз|сука|бляд|дебил|твар)\w*\b", re.IGNORECASE)
DELIVERY_ONLY = re.compile(
    r"\b(?:курьер|доставк|пвз|пункт выдачи|склад|упаковк)\w*\b", re.IGNORECASE
)
PRODUCT = re.compile(
    r"\b(?:товар|размер|цвет|качест|материал|комплект|работает|сломал|брак|оригинал)\w*\b",
    re.IGNORECASE,
)


def choose_goal(kind: str, rating: int | None, text: str) -> tuple[str, list[str]]:
    low = text.lower()
    risks: list[str] = []
    if CONTACT.search(text):
        risks.append("personal_or_contact_data")
    if any(word in low for word in ("верните деньги", "суд", "ущерб", "пожар", "травм")):
        risks.append("legal_or_safety_escalation")
    if kind == "question":
        return ("support_conversion" if not risks else "reduce_uncertainty"), risks
    if rating and rating >= 4:
        return "thank_and_reinforce", risks
    return "deescalate", risks


@dataclass
class ComplaintAssessment:
    eligible: bool
    category: str
    confidence: float
    reasons: list[str]
    evidence: list[str]
    questions: list[str]
    draft_text: str

    def dict(self) -> dict:
        return asdict(self)


def assess_complaint(review_id: str, nm_id: int | None, text: str) -> ComplaintAssessment:
    reasons: list[str] = []
    evidence: list[str] = [f"ID отзыва: {review_id}", f"Артикул WB: {nm_id or 'не определён'}"]
    category = "no_violation"
    eligible = False
    confidence = 0.75
    if ABUSE.search(text):
        eligible, category, confidence = True, "abuse", 0.95
        reasons.append("Обнаружены оскорбления или вероятная ненормативная лексика")
    elif CONTACT.search(text):
        eligible, category, confidence = True, "advertising_or_contacts", 0.9
        reasons.append("Обнаружены ссылка, реклама или контактные данные")
    elif DELIVERY_ONLY.search(text) and not PRODUCT.search(text):
        category, confidence = "delivery_only_not_rating", 0.9
        reasons.append(
            "Текст относится только к доставке/складу/ПВЗ; по справке WB такой отзыв не удаляют, но не учитывают в рейтинге"
        )
    else:
        reasons.append("Явных оснований для удаления по опубликованным критериям WB не найдено")
    questions = [
        "Какая точная фраза нарушает правило?",
        "Есть ли скриншот отзыва и карточки товара?",
        "Подавалась ли уже единственная доступная жалоба?",
        "Если заявлена конкуренция — какие проверяемые признаки накрутки есть?",
    ]
    draft = (
        f"Прошу проверить отзыв {review_id} к товару {nm_id or '[артикул]'} по основанию «{category}». "
        f"Фрагмент и факты нарушения: [укажите дословный фрагмент без домыслов]. "
        "Прошу сопоставить содержание отзыва с правилами публикации и сообщить результат модерации."
    )
    return ComplaintAssessment(
        eligible, category, confidence, reasons, evidence, questions, draft[:1000]
    )


def validate_answer(text: str) -> list[str]:
    errors: list[str] = []
    if not 2 <= len(text) <= 5000:
        errors.append("invalid_length")
    if CONTACT.search(text):
        errors.append("contact_data_forbidden")
    if text.isupper() and len(text) > 10:
        errors.append("uppercase_forbidden")
    if re.search(r"[!?]{3,}", text):
        errors.append("excessive_punctuation")
    return errors
