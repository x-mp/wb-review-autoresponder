from wb_review_assistant.policy import assess_complaint, validate_answer


def test_delivery_only_is_not_deletion_ground():
    result = assess_complaint("r1", 123, "Курьер опоздал, ужасный ПВЗ")
    assert result.eligible is False
    assert result.category == "delivery_only_not_rating"


def test_abuse_is_candidate_not_guarantee():
    result = assess_complaint("r2", 123, "Продавец идиот")
    assert result.eligible is True
    assert result.category == "abuse"


def test_answer_validation_blocks_contacts():
    assert "contact_data_forbidden" in validate_answer("Позвоните +7 999 123-45-67")
