-- Retrieval-readiness links across independently imported canonical datasets.
-- Adds no items or facts. Every endpoint must already exist.

INSERT OR IGNORE INTO knowledge_item_relations (from_item_id, relation_type, to_item_id)
SELECT a.id, v.relation_type, b.id
FROM (
    SELECT 'ta.platform' AS from_key, 'has_feature' AS relation_type, 'ta.guest_pass' AS to_key
    UNION ALL SELECT 'ta.platform', 'has_policy', 'ta.best_price_guarantee'
    UNION ALL SELECT 'ta.platform', 'has_membership_feature', 'ta.elite_turbo_features'
    UNION ALL SELECT 'ta.platform', 'has_booking_guidance', 'ta.booking_status_inventory'
    UNION ALL SELECT 'ta.platform', 'has_service_catalog', 'ta.platform_services_delta'
    UNION ALL SELECT 'ta.platform', 'has_support_guidance', 'ta.payments_and_support_routing'
    UNION ALL SELECT 'mwr.life', 'has_role', 'mwr.lifestyle_ambassador'
    UNION ALL SELECT 'mwr.life', 'has_structure', 'mwr.team_structures'
    UNION ALL SELECT 'mwr.life', 'has_training', 'mwr.getting_started'
    UNION ALL SELECT 'mwr.life', 'has_qualification_concept', 'mwr.qualification_status'
    UNION ALL SELECT 'mwr.getting_started', 'includes', 'mwr.partner_product_knowledge'
    UNION ALL SELECT 'mwr.lifestyle_ambassador', 'has_training_responsibility', 'mwr.sponsor_training_responsibility'
    UNION ALL SELECT 'mwr.lifestyle_ambassador', 'governed_by', 'mwr.claims_and_staleness_compliance'
    UNION ALL SELECT 'ta.membership', 'includes_feature', 'ta.elite_turbo_features'
    UNION ALL SELECT 'ta.loyalty_points', 'has_rule', 'ta.points_transfer_and_use_delta'
    UNION ALL SELECT 'ta.travel_credits', 'has_rule', 'ta.points_transfer_and_use_delta'
    UNION ALL SELECT 'ta.best_price_guarantee', 'governed_by', 'mwr.claims_and_staleness_compliance'
    UNION ALL SELECT 'ta.booking_status_inventory', 'routes_to', 'ta.support'
    UNION ALL SELECT 'ta.payments_and_support_routing', 'routes_to', 'ta.support'
    UNION ALL SELECT 'ta.payments_and_support_routing', 'routes_to', 'mwr.support'
    UNION ALL SELECT 'ta.rank_qualification', 'explained_by', 'mwr.team_structures'
) AS v
JOIN knowledge_items AS a ON a.stable_key = v.from_key
JOIN knowledge_items AS b ON b.stable_key = v.to_key;

INSERT OR IGNORE INTO knowledge_item_tags (item_id, tag)
SELECT i.id, v.tag
FROM (
    SELECT 'mwr.getting_started' AS item_key, 'новый партнёр' AS tag
    UNION ALL SELECT 'mwr.getting_started', 'с чего начать'
    UNION ALL SELECT 'ta.booking_status_inventory', 'зависло бронирование'
    UNION ALL SELECT 'ta.support', 'проблема с бронью'
    UNION ALL SELECT 'ta.payments_and_support_routing', 'комиссия'
    UNION ALL SELECT 'ta.payments_and_support_routing', 'криптовалюта'
    UNION ALL SELECT 'ta.best_price_guarantee', 'всегда дешевле'
    UNION ALL SELECT 'mwr.claims_and_staleness_compliance', 'актуальный life experience'
    UNION ALL SELECT 'mwr.claims_and_staleness_compliance', 'гарантированно заработаю'
) AS v
JOIN knowledge_items AS i ON i.stable_key = v.item_key;
