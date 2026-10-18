"""Seed sample data for the cultural_safety_rules table — at least 3 JAPAC
markets (Vietnam, Indonesia, Philippines) so SafetyAgent has real rules to
check against from the start (see AGENTS.md principle #6: rules live in the DB,
not hardcoded).

Idempotent — running it multiple times does not create duplicate rows (dedup
by (target_market, rule_description)).

Run: python -m scripts.seed_cultural_safety_rules
"""

import asyncio

from sqlalchemy import select

from api.db.models import CulturalSafetyRule
from api.db.session import AsyncSessionLocal

SEED_RULES: list[dict[str, str]] = [
    {
        "target_market": "vi",
        "rule_description": "Tránh đề cập trực tiếp đến các vấn đề chính trị nhạy cảm.",
        "severity": "high",
    },
    {
        "target_market": "vi",
        "rule_description": (
            "Cẩn trọng với hình ảnh/câu đùa liên quan đến các ngày lễ, tín ngưỡng dân gian."
        ),
        "severity": "medium",
    },
    {
        "target_market": "id",
        "rule_description": "Tránh hình ảnh liên quan đến rượu bia, thức ăn không phù hợp Halal.",
        "severity": "high",
    },
    {
        "target_market": "id",
        "rule_description": (
            "Cẩn trọng khi thể hiện trang phục hở hoặc tiếp xúc thân mật giữa các nhân vật."
        ),
        "severity": "medium",
    },
    {
        "target_market": "ph",
        "rule_description": "Cân nhắc yếu tố tôn giáo Công giáo trong các câu đùa liên quan.",
        "severity": "medium",
    },
    {
        "target_market": "ph",
        "rule_description": (
            "Tránh nội dung chế giễu gia đình/người lớn tuổi — văn hóa Philippines "
            "đề cao kính trọng gia đình."
        ),
        "severity": "medium",
    },
    {
        "target_market": "tl",
        "rule_description": (
            "Tránh nội dung chế giễu gia đình/người lớn tuổi và cẩn trọng với các yếu tố tôn giáo Công giáo."
        ),
        "severity": "medium",
    },
    {
        "target_market": "hi",
        "rule_description": (
            "Tránh đề cập đến thịt bò hoặc các hình ảnh động chạm đến sự linh thiêng của loài bò trong văn hóa Hindu."
        ),
        "severity": "high",
    },
    {
        "target_market": "hi",
        "rule_description": (
            "Cẩn trọng với các yếu tố phân biệt đẳng cấp xã hội hoặc tôn giáo giữa các nhân vật thiếu nhi."
        ),
        "severity": "high",
    },
    {
        "target_market": "th",
        "rule_description": (
            "Tuyệt đối tránh mọi bình luận, câu đùa hoặc hàm ý liên quan đến Hoàng gia Thái Lan."
        ),
        "severity": "high",
    },
    {
        "target_market": "th",
        "rule_description": (
            "Tránh chạm vào đầu nhân vật hoặc chỉ chân vào người khác/tượng Phật (kiêng kỵ văn hóa Thái)."
        ),
        "severity": "medium",
    },
    {
        "target_market": "ja",
        "rule_description": (
            "Đảm bảo kính ngữ và cách xưng hô phù hợp thứ bậc xã hội và chuẩn mực ứng xử trẻ em Nhật Bản."
        ),
        "severity": "medium",
    },
    {
        "target_market": "ko",
        "rule_description": (
            "Tôn trọng tôn ti trật tự tuổi tác trong giao tiếp và xưng hô giữa các nhân vật thiếu nhi."
        ),
        "severity": "medium",
    },
    {
        "target_market": "zh",
        "rule_description": (
            "Tránh các biểu tượng mê tín dị đoan thái quá và đảm bảo ngôn từ trong sáng, tích cực theo chuẩn mực giáo dục."
        ),
        "severity": "medium",
    },
]


async def seed() -> int:
    inserted = 0
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(CulturalSafetyRule))
        existing = {(row.target_market, row.rule_description) for row in result.scalars().all()}
        for rule in SEED_RULES:
            key = (rule["target_market"], rule["rule_description"])
            if key in existing:
                continue
            session.add(CulturalSafetyRule(**rule))
            inserted += 1
        await session.commit()
    return inserted


if __name__ == "__main__":
    count = asyncio.run(seed())
    print(f"Inserted {count} new cultural_safety_rules row(s) (skipped duplicates).")
